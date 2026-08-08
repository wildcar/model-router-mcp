// «Прогон»: the screen where money gets spent, so it explains itself first.
//
// Three rules from §5 and §10 shape everything here. The form is built from
// /api/v1/capabilities, so a field is on screen exactly when the adapter behind
// the chosen (task, provider) pair reads it, and the input controls follow the
// task's own signature — which is what makes text and a picture usable together.
// The plan is free and recomputed as the discrete fields change, so "куда уйдёт
// запрос" is answered before the send button. And the send button carries the estimate, because a price after
// the fact is a receipt, not a decision.

import { api, ApiError, query } from '../core/api.js';
import { el, clear, fill, toast } from '../core/dom.js';
import { costCell, duration, money, tokens } from '../core/format.js';
import { bind } from '../core/keys.js';
import { cancel, idempotencyKey, isDone, start, upload, watch } from '../core/runs.js';
import { loadLocal, readParams, saveLocal, writeParams } from '../core/state.js';
import { inspector } from '../components/inspector.js';
import { timeline } from '../components/timeline.js';

const $ = (id) => document.getElementById(id);
const PLAN_DEBOUNCE_MS = 300;

let caps = null;
let tree = null;
let mode = 'auto';          // 'auto' | 'pick'
let lastPlan = null;
const feed = [];            // last runs of this session

// --- capabilities-driven form ------------------------------------------
function currentTask() {
  return $('task').value;
}

// The signature of the chosen task: which media it needs, which it may take,
// what it produces. The form reads this instead of comparing the task to a
// literal, so a new task needs no branch here.
function signature() {
  return caps.tasks?.[currentTask()] || { requires: [], optional: [], produces: [] };
}

function providersFor(task) {
  const byTask = caps.params_by_task[task] || {};
  return caps.providers.filter((p) => p.id in byTask);
}

// With a provider pinned the form is exact. Without one the router will choose,
// and the candidates no longer agree on their knobs: codex-oauth answers text
// with `instructions` and `reasoning_effort` where OpenRouter and DeepSeek take
// `temperature` and `top_p`. The intersection of those is empty, so showing only
// what every candidate accepts now means showing nothing at all.
//
// So the union, with each field naming the providers that read it. Sending a
// field the chosen adapter ignores costs nothing and is reported afterwards as
// «проигнорировано» — whereas a form with no fields hides half the API.
function paramSpecs() {
  const provider = currentProvider();
  const byProvider = caps.params_by_task[currentTask()] || {};
  if (provider) return byProvider[provider]?.params || [];

  const readers = new Map();   // param name -> [provider ids]
  const specs = new Map();     // param name -> the spec, first one wins
  const configured = Object.entries(byProvider)
    .filter(([id]) => caps.providers.find((p) => p.id === id)?.configured);
  // Nothing configured yet is a first-run installation: describe what the
  // request would take anyway. The plan says separately that nothing can run.
  const entries = configured.length ? configured : Object.entries(byProvider);
  for (const [id, spec] of entries) {
    for (const param of spec.params) {
      if (!specs.has(param.name)) specs.set(param.name, param);
      readers.set(param.name, [...(readers.get(param.name) || []), id]);
    }
  }
  return [...specs.values()].map((param) => {
    const who = readers.get(param.name) || [];
    if (who.length === entries.length) return param;
    return { ...param, help: `${param.help} · читают: ${who.join(', ')}` };
  });
}

// What the pinned model itself accepts for a parameter, when its catalog entry
// says so. The pair-level list is a union across the provider's models, and a
// model answers 400 for a value it does not know — so a chosen model narrows the
// select to its own values. Nothing pinned: null, and the union stands.
function modelOptions(name) {
  const id = currentModelId();
  if (!id || !tree) return null;
  for (const provider of tree.children) {
    for (const vendor of provider.children) {
      for (const family of vendor.children) {
        for (const variant of family.children) {
          if (variant.value !== id) continue;
          const options = variant.param_options?.[name];
          return options && options.length ? options : null;
        }
      }
    }
  }
  return null;
}

function renderParams() {
  const specs = paramSpecs();
  const host = $('params');
  clear(host);
  $('params-empty').hidden = specs.length > 0;
  const overrides =
    caps.params_by_task[currentTask()]?.[currentProvider()]?.model_overrides || {};
  for (const spec of specs) {
    const override = overrides[spec.name] || {};
    const label = el('label', undefined, { className: spec.type === 'select' ? 'grow' : '' });
    label.appendChild(document.createTextNode(
      override.required ? `${spec.label} *` : spec.label));
    let field;
    if (spec.type === 'select') {
      field = el('select');
      // The empty value is the point of the placeholder: an <option> without one
      // takes its label as its value, and «— по умолчанию —» then travelled to
      // the provider as the parameter itself (the Codex backend answered
      // «Invalid value: '— по умолчанию —'» on reasoning.effort). `collectParams`
      // drops empty strings, which is how "не задавать" reaches the adapter.
      const placeholder = el('option', '— по умолчанию —');
      placeholder.value = '';
      field.appendChild(placeholder);
      for (const option of modelOptions(spec.name) || spec.options || []) {
        const node = el('option', option);
        node.value = option;
        field.appendChild(node);
      }
    } else if (spec.type === 'boolean') {
      // A checkbox, not a true/false select: unchecked means «не задавать»,
      // same as an empty field, so the parameter simply stays out of the call.
      field = el('input');
      field.type = 'checkbox';
    } else {
      field = el('input');
      field.type = spec.type === 'text' ? 'text' : 'number';
      if (spec.min !== undefined) field.min = spec.min;
      if (spec.max !== undefined) field.max = spec.max;
      if (spec.step !== undefined) field.step = spec.step;
      const preset = override.default ?? spec.default;
      if (preset !== null && preset !== undefined) field.placeholder = String(preset);
    }
    field.id = `param-${spec.name}`;
    field.dataset.param = spec.name;
    field.dataset.kind = spec.type;
    label.appendChild(field);
    // The explanation is text under the control, not a tooltip: a tooltip is
    // for refinement, never for the first explanation (§3).
    label.appendChild(el('span', override.help || spec.help, { className: 'under' }));
    host.appendChild(label);
  }
}

function collectParams() {
  const out = {};
  for (const field of $('params').querySelectorAll('[data-param]')) {
    if (field.dataset.kind === 'boolean') {
      // `field.value` of a checkbox is the constant "on"; the state lives in
      // `checked`. Unchecked stays out of the call entirely.
      if (field.checked) out[field.dataset.param] = true;
      continue;
    }
    const raw = field.value;
    if (raw === '' || raw === null) continue;
    out[field.dataset.param] =
      field.dataset.kind === 'text' || field.dataset.kind === 'select'
        ? raw
        : Number(raw);
  }
  return out;
}

// --- model selection ----------------------------------------------------
function currentProvider() {
  return mode === 'auto' ? $('auto-provider').value : $('sel-provider').value;
}

function currentModelId() {
  return mode === 'auto' ? '' : $('model-id').value.trim();
}

function fillSelect(select, options, anyLabel) {
  const previous = select.value;
  clear(select);
  const any = el('option', anyLabel);
  any.value = '';           // same reason as in renderParams: no value, no meaning
  select.appendChild(any);
  for (const option of options) {
    const node = el('option', option.label ?? option.value);
    node.value = option.value;
    select.appendChild(node);
  }
  select.value = options.some((o) => o.value === previous) ? previous : '';
}

// The cascade comes from the server: splitting model_id on "/" and ":" in the
// browser guesses at a structure the database already stores in its own column.
function treeLevel(path) {
  let level = tree.children;
  for (const step of path) {
    const node = level.find((n) => n.value === step);
    if (!node) return [];
    level = node.children;
  }
  return level;
}

function rebuildCascade(from) {
  const provider = $('sel-provider').value;
  const vendor = $('sel-vendor').value;
  const family = $('sel-family').value;
  if (from <= 0) {
    fillSelect($('sel-provider'),
      tree.children.map((n) => ({ value: n.value, label: `${n.value} (${n.count})` })),
      '— любой —');
  }
  if (from <= 1) {
    fillSelect($('sel-vendor'),
      provider ? treeLevel([provider]).map((n) => ({ value: n.value, label: `${n.value} (${n.count})` })) : [],
      '— любой —');
  }
  if (from <= 2) {
    fillSelect($('sel-family'),
      provider && vendor
        ? treeLevel([provider, vendor]).map((n) => ({ value: n.value, label: `${n.value} (${n.count})` }))
        : [],
      '— любая —');
  }
  if (from <= 3) {
    const variants = provider && vendor && family ? treeLevel([provider, vendor, family]) : [];
    fillSelect($('sel-variant'), variants, '— любой —');
    // A family with exactly one variant IS the model. Codex and Anthropic ids
    // have no ":variant" part, so every family there is a single «базовая» —
    // and until it was clicked by hand, model_id stayed empty and the request
    // left with a provider hint alone: «выбрана gpt-5.6-sol, ответила gpt-5.4».
    if (variants.length === 1) $('sel-variant').value = variants[0].value;
  }
  const variant = $('sel-variant').value;
  const field = $('model-id');
  if (variant) {
    field.value = variant;
    field.dataset.fromCascade = '1';
  } else if (field.dataset.fromCascade) {
    // The id came from the previous pick and the cascade no longer says it:
    // keeping it would silently pin the model the operator just walked away
    // from. A hand-typed id (no flag) is not ours to erase.
    field.value = '';
    delete field.dataset.fromCascade;
  }
  checkModelId();
  onFormChange();
}

// The id field reports what it knows about the value as it is typed: found,
// retired on such a date, provider switched off.
async function checkModelId() {
  const value = $('model-id').value.trim();
  const state = $('model-id-state');
  if (!value) {
    state.textContent = 'Пусто — модель выберет сервис';
    state.className = 'under';
    return;
  }
  const found = [];
  for (const provider of tree.children) {
    for (const vendor of provider.children) {
      for (const family of vendor.children) {
        for (const variant of family.children) {
          if (variant.value === value) found.push(provider);
        }
      }
    }
  }
  if (!found.length) {
    state.textContent = 'В реестре активных моделей с таким id нет';
    state.className = 'under warn';
    return;
  }
  const provider = found[0];
  if (!provider.enabled) {
    state.textContent = `Найдена у ${provider.value}, но поставщик выключен`;
    state.className = 'under warn';
  } else if (!provider.configured) {
    state.textContent = `Найдена у ${provider.value}, но поставщик не настроен`;
    state.className = 'under warn';
  } else {
    state.textContent = `Найдена у ${provider.value}`;
    state.className = 'under ok';
  }
}

// --- plan ---------------------------------------------------------------
function fallbackValue() {
  const choice = caps.labels.fallback_choices.find((c) => c.value === $('fallback').value);
  if (!choice || choice.policy === 'off') return null;
  return {
    policy: choice.policy,
    scope: choice.scope,
    max_steps: Number($('fallback-steps').value) || 1,
  };
}

function requestBody() {
  return {
    task: currentTask(),
    external_user_id: $('user').value || 'web-tester',
    text: $('prompt').value,
    model_id: currentModelId() || null,
    tier: mode === 'auto' ? ($('tier').value || null) : null,
    provider: currentProvider() || null,
    params: collectParams(),
    fallback: fallbackValue(),
  };
}

function renderPlan(plan) {
  lastPlan = plan;
  const host = $('plan');
  host.hidden = false;
  clear(host);

  const head = el('div', undefined, { className: 'plan-head' });
  head.appendChild(el('h3', 'План маршрутизации'));
  head.appendChild(el('span', 'ничего не отправлено, деньги не потрачены', { className: 'hint' }));
  host.appendChild(head);

  const facts = [];
  if (plan.requested) {
    facts.push(['Запрошено', `${plan.requested.provider ?? '—'} · ${plan.requested.model_id}`]);
  }
  if (plan.problem) facts.push(['Проблема', plan.problem]);
  const choice = caps.labels.fallback_choices.find(
    (c) => c.policy === plan.policy.policy && c.scope === plan.policy.scope);
  facts.push(['Политика', plan.policy.policy === 'off'
    ? 'показать ошибку'
    : `${choice ? choice.label : plan.policy.policy}, до ${plan.policy.max_steps} замен`]);
  facts.push(['Кандидаты', `отобрано ${plan.eligible} из ${plan.considered}`]);
  const table = el('table', undefined, { className: 'kv' });
  const body = el('tbody');
  for (const [key, value] of facts) {
    const tr = el('tr');
    tr.appendChild(el('th', key));
    tr.appendChild(el('td', value));
    body.appendChild(tr);
  }
  table.appendChild(body);
  host.appendChild(table);

  if (plan.candidates.length) {
    const candidates = el('table', undefined, { className: 'candidates' });
    const thead = el('thead');
    const hrow = el('tr');
    for (const [text, cls] of [['#', 'num'], ['модель', ''], ['цена, $/1M', 'num'],
                               ['цена откуда', ''], ['почему', '']]) {
      const th = el('th', text, { className: cls });
      th.scope = 'col';
      hrow.appendChild(th);
    }
    thead.appendChild(hrow);
    candidates.appendChild(thead);
    const tbody = el('tbody');
    for (const candidate of plan.candidates) {
      const tr = el('tr', undefined, {
        className: candidate.verdict === 'eligible' ? '' : 'muted',
      });
      tr.appendChild(el('td', candidate.rank ?? '—', { className: 'num' }));
      tr.appendChild(el('td', `${candidate.provider} · ${candidate.model_id}`, { className: 'mono' }));
      tr.appendChild(el('td', candidate.price_per_1m.toFixed(3), { className: 'num' }));
      tr.appendChild(el('td',
        caps.labels.price_source_kinds[candidate.price_source_kind]?.label
        ?? candidate.price_source_kind));
      tr.appendChild(el('td', candidate.why));
      tbody.appendChild(tr);
    }
    candidates.appendChild(tbody);
    host.appendChild(candidates);
  }

  const verdict = el('p', undefined, { className: 'plan-verdict' });
  if (plan.will_call) {
    verdict.appendChild(document.createTextNode('Будет вызвана '));
    verdict.appendChild(el('b', `${plan.will_call.provider} · ${plan.will_call.model_id}`));
  } else {
    // "Почему запрос не ушёл" is exactly the case this screen exists for, so it
    // is an answer, not an error.
    verdict.appendChild(el('b', 'Вызывать нечего', { className: 'warn' }));
    if (plan.blocked) verdict.appendChild(el('div', plan.blocked, { className: 'muted' }));
  }
  host.appendChild(verdict);

  if (plan.estimate.assumption) {
    host.appendChild(el('p', `Прикидка: ${plan.estimate.assumption}`, { className: 'hint' }));
  }
  updateSendButton(plan.estimate);
}

function updateSendButton(estimate) {
  // In the multi-model mode the button counts runs and multiplies the estimate,
  // so it owns its own label.
  if (mode === 'multi') { updateMultiButton(); return; }
  const button = $('send');
  clear(button);
  button.appendChild(document.createTextNode('Отправить'));
  if (!estimate) return;
  if (estimate.value === null || estimate.value === undefined) {
    button.appendChild(el('span', ' · стоимость неизвестна', { className: 'est' }));
    return;
  }
  const mark = estimate.source === 'computed' ? '' : '≈';
  button.appendChild(el('span', ` · ${mark}$${money(estimate.value)}`, { className: 'est' }));
}

let planTimer = null;
function schedulePlan() {
  clearTimeout(planTimer);
  planTimer = setTimeout(() => refreshPlan().catch(() => {}), PLAN_DEBOUNCE_MS);
}

async function refreshPlan() {
  const plan = await api('/plan', { method: 'POST', body: requestBody() });
  if (!$('plan').hidden || plan.will_call === null) renderPlan(plan);
  else {
    lastPlan = plan;
    updateSendButton(plan.estimate);
    $('auto-choice').textContent = plan.will_call
      ? `Сервис выберет ${plan.will_call.provider} · ${plan.will_call.model_id}`
      : (plan.blocked || '');
  }
}

// --- sending ------------------------------------------------------------
// Runs go through the registry, not through a request held open: the run gets
// an id, the screen polls it, and closing the tab loses nothing (§10.5).
const watches = new Map();   // run_id -> watcher, so «Прервать» has something to stop
let clockTimer = null;

function runBody(body, { modelId = null, batchId = null } = {}) {
  const out = {
    task: body.task,
    external_user_id: body.external_user_id,
    text: body.text,
    prompt: body.text,
    model_id: modelId ?? body.model_id,
    tier: body.tier,
    provider: body.provider,
    params: body.params,
    fallback: body.fallback,
    client_request_id: idempotencyKey(),
    batch_id: batchId,
    attachments: body.attachments ?? [],
  };
  // `engine` picks between the two local binaries and means nothing anywhere
  // else. It used to be sent with every request of every kind.
  if (body.task === 'cli') {
    out.engine = (body.provider || 'codex-cli').replace('-cli', '');
  }
  return out;
}

// Which `Attachment.kind` a file is, from what the browser says it is. The server
// re-derives the medium from the mime anyway; this is what the API field wants.
function kindOf(file) {
  const mime = file.type || '';
  if (mime.startsWith('image/')) return 'image';
  if (mime.startsWith('audio/')) return 'audio';
  return 'file';
}

async function attachments(body) {
  const sig = signature();
  const needed = sig.requires.filter((m) => m !== 'text');
  const files = Array.from($('attachments').files || []);
  if (!files.length) {
    if (!needed.length) return [];
    toast(`Для этой задачи нужен файл: ${needed.join(', ')}`, true);
    return undefined;
  }
  // Uploaded once, referenced afterwards: the same file is not re-sent for a
  // repeat, and a JSON run body has no way to carry bytes.
  const stored = await Promise.all(files.map((file) => upload(file)));
  return stored.map((item, i) => ({ ref: item.ref, kind: kindOf(files[i]) }));
}

async function send() {
  const body = requestBody();
  const refs = await attachments(body);
  if (refs === undefined) return;     // a file was required and missing
  body.attachments = refs;

  if (mode === 'multi') return sendMany(body);
  return sendOne(body);
}

async function sendOne(body) {
  setBusy(true);
  try {
    const state = await start(runBody(body));
    showLive(state);
    const watcher = watch(state.run_id, showLive);
    watches.set(state.run_id, watcher);
    const done = await watcher.promise;
    finishSingle(done);
  } catch (error) {
    if (error instanceof ApiError) showError(error);
    else toast(`Ошибка: ${error.message}`, true);
  } finally {
    setBusy(false);
  }
}

function finishSingle(state) {
  $('live').hidden = true;
  if (state.status === 'ok') {
    showRun(state.result);
    remember(state.result);
    return;
  }
  const envelope = state.error?.error || { message: `Прогон ${state.status}` };
  showError(new ApiError(envelope, 0));
}

function setBusy(busy) {
  $('send').hidden = busy;
  $('cancel').hidden = !busy;
  if (!busy) {
    clearInterval(clockTimer);
    clockTimer = null;
  }
}

function showLive(state) {
  $('result-empty').hidden = true;
  $('plan').hidden = true;
  $('live').hidden = false;
  const label = caps.labels.statuses?.[state.status];
  $('live-status').textContent = label ? label.label : state.status;
  fill($('live-steps'), [timeline(state.steps, elapsedMs(state))]);
  if (!clockTimer) {
    const started = Date.now();
    clockTimer = setInterval(() => {
      $('live-clock').textContent = `${Math.round((Date.now() - started) / 1000)} с`;
    }, 250);
  }
  // Cancelling addresses this run; a batch is cancelled row by row.
  $('cancel').dataset.runId = state.run_id;
}

function elapsedMs(state) {
  const steps = state.steps || [];
  return steps.length ? steps[steps.length - 1].at_ms : 0;
}

async function cancelRun(runId) {
  if (!runId) return;
  try {
    await cancel(runId);
    toast('Прогон прерван');
  } catch (error) {
    toast(`Не удалось прервать: ${error.message}`, true);
  } finally {
    watches.get(runId)?.stop();
    watches.delete(runId);
  }
}

// --- multi-model mode ---------------------------------------------------
const chips = [];   // model ids to compare

function renderChips() {
  $('multi-empty').hidden = chips.length > 0;
  fill($('chips'), chips.map((id) => {
    const chip = el('span', undefined, { className: 'chip' });
    chip.appendChild(el('span', id, { className: 'mono' }));
    chip.appendChild(el('button', '×', {
      className: 'chip-x',
      title: `Убрать ${id}`,
      onClick: () => { chips.splice(chips.indexOf(id), 1); renderChips(); },
    }));
    return chip;
  }));
  updateMultiButton();
}

function plannedRuns() {
  return chips.length * Math.max(1, Number($('multi-repeats').value) || 1);
}

function updateMultiButton() {
  if (mode !== 'multi') return;
  const total = plannedRuns();
  clear($('send'));
  $('send').appendChild(document.createTextNode(
    total ? `Запустить ${total} ${total === 1 ? 'прогон' : 'прогонов'}` : 'Выберите модели'));
  if (lastPlan?.estimate?.value) {
    $('send').appendChild(el('span', ` · ≈$${money(lastPlan.estimate.value * total)}`,
      { className: 'est' }));
  }
}

async function sendMany(body) {
  const total = plannedRuns();
  if (!total) { toast('Добавьте хотя бы одну модель', true); return; }
  // Above three runs the confirmation states the number and the money, because
  // one click here is N charges (§10.6).
  if (total > 3) {
    const estimate = lastPlan?.estimate?.value
      ? ` примерно на $${money(lastPlan.estimate.value * total)}`
      : '';
    if (!confirm(`Запустить ${total} прогонов${estimate}?`)) return;
  }
  const batchId = idempotencyKey();
  const repeats = Math.max(1, Number($('multi-repeats').value) || 1);
  $('multi').hidden = false;
  $('result-empty').hidden = true;
  $('plan').hidden = true;
  clear($('multi').querySelector('tbody'));

  const jobs = [];
  for (const modelId of chips) {
    for (let i = 0; i < repeats; i += 1) {
      jobs.push(startRow(runBody(body, { modelId, batchId }), modelId));
    }
  }
  await Promise.allSettled(jobs);
}

async function startRow(payload, modelId) {
  const tbody = $('multi').querySelector('tbody');
  const tr = el('tr');
  const cells = ['model', 'status', 'time', 'tokens', 'cost', 'text'].map((name) => {
    const td = el('td', name === 'model' ? modelId : '—',
      { className: name === 'model' ? 'mono' : (name === 'time' || name === 'cost' ? 'num' : '') });
    tr.appendChild(td);
    return td;
  });
  tbody.appendChild(tr);

  const update = (state) => {
    const label = caps.labels.statuses?.[state.status];
    clear(cells[1]);
    cells[1].appendChild(el('span', label ? label.label : state.status, {
      className: state.status === 'ok' ? 'ok' : (state.status === 'error' ? 'err' : 'muted'),
    }));
    if (!isDone(state)) {
      cells[1].appendChild(el('button', 'прервать', {
        className: 'linkish',
        onClick: () => cancelRun(state.run_id),
      }));
      return;
    }
    const run = state.result;
    if (!run) {
      cells[5].textContent = state.error?.error?.message || '—';
      cells[5].className = 'err';
      return;
    }
    cells[2].textContent = duration(run.latency_ms);
    cells[3].textContent = tokens(run.usage);
    clear(cells[4]);
    cells[4].appendChild(costCell(run.cost.value, run.cost.source, caps.labels));
    cells[5].textContent = (run.text || '').slice(0, 80);
    tr.addEventListener('click', () => showRun(run));
    remember(run);
  };

  try {
    const state = await start(payload);
    update(state);
    const watcher = watch(state.run_id, update);
    watches.set(state.run_id, watcher);
    await watcher.promise;
  } catch (error) {
    cells[1].textContent = 'ошибка';
    cells[5].textContent = error.message;
  }
}

function showRun(run) {
  $('result-empty').hidden = true;
  $('plan').hidden = true;
  inspector($('inspector'), run, { labels: caps.labels });
  // The address now names this run, so a reload keeps it and the link can be
  // handed to a colleague.
  if (run.log_id) writeParams({ ...readParams(), log: run.log_id });
}

function showError(error) {
  $('result-empty').hidden = true;
  const host = $('inspector');
  clear(host);
  host.className = 'inspector';
  host.appendChild(el('div', error.message, { className: 'err' }));
  if (error.hint?.env_var) {
    host.appendChild(el('div', `Не задана переменная окружения ${error.hint.env_var}`,
      { className: 'muted mono' }));
  }
  if (error.retryAfterS) {
    host.appendChild(el('div', `Провайдер просил подождать ${error.retryAfterS} с`,
      { className: 'muted' }));
  }
  if (error.logId) {
    host.appendChild(el('a', `Открыть в журнале #${error.logId}`,
      { href: `/logs?log=${error.logId}` }));
  }
  toast(error.message, true);
}

function remember(run) {
  feed.unshift(run);
  feed.splice(10);
  const host = $('feed');
  $('feed-card').hidden = false;
  fill(host, feed.map((item) => {
    const chip = el('button', undefined, {
      className: `feed-item ${item.status === 'ok' ? 'ok' : 'err'}`,
      onClick: () => showRun(item),
    });
    chip.appendChild(el('span', `#${item.log_id ?? '—'}`));
    chip.appendChild(el('span', item.model.model_id ?? '—', { className: 'mono' }));
    chip.appendChild(costCell(item.cost.value, item.cost.source, caps.labels));
    return chip;
  }));
}

// Tasks grouped by their billing group, because ten flat options read as a list
// of jargon while «Картинка: текст в картинку / картинка в картинку» reads as a
// choice.
//
// Two different unavailabilities, kept apart: no adapter can do it at all
// (disabled), or one can but has no credentials (selectable, labelled). Hiding
// either would leave no way to tell a dead end from a two-minute fix.
function fillTaskSelect() {
  const select = $('task');
  clear(select);
  const groups = new Map();
  for (const [value, row] of Object.entries(caps.tasks || {})) {
    if (!groups.has(row.billing_group)) groups.set(row.billing_group, []);
    groups.get(row.billing_group).push([value, row]);
  }
  for (const [group, rows] of groups) {
    const box = el('optgroup');
    box.label = caps.labels.modalities?.[group]?.label || group;
    for (const [value, row] of rows) {
      let suffix = '';
      if (!row.available) suffix = ' (нет модели)';
      else if (!row.configured) suffix = ' (поставщик не настроен)';
      const option = el('option', `${row.label}${suffix}`);
      option.value = value;
      option.disabled = !row.available;
      box.appendChild(option);
    }
    select.appendChild(box);
  }
}

// For the default selection: prefer a task that would actually run, fall back to
// one that at least exists. A first-run installation has none of the former.
function availableTasks() {
  const rows = Object.entries(caps.tasks || {});
  const configured = rows.filter(([, r]) => r.available && r.configured).map(([v]) => v);
  return configured.length ? configured : rows.filter(([, r]) => r.available).map(([v]) => v);
}

// --- wiring -------------------------------------------------------------
// Media -> the accept filter and the Russian word for it. One table, because
// the alternative is a chain of comparisons against task names.
const MEDIA = {
  image: { accept: 'image/*', one: 'Изображение', many: 'Изображения' },
  audio: { accept: 'audio/*', one: 'Аудиофайл', many: 'Аудиофайлы' },
  video: { accept: 'video/*', one: 'Видео', many: 'Видео' },
  file: { accept: '', one: 'Файл', many: 'Файлы' },
};

function onTaskChange() {
  const task = currentTask();
  const sig = signature();
  const media = [...sig.requires, ...sig.optional].filter((m) => m !== 'text');
  const required = sig.requires.filter((m) => m !== 'text');

  // Text and a file are no longer mutually exclusive: editing a picture needs
  // both, and so does asking a question about one.
  $('text-field').hidden = !sig.requires.includes('text') && !sig.optional.includes('text');
  $('file-field').hidden = media.length === 0;
  const primary = MEDIA[media[0]] || MEDIA.file;
  $('attachments').accept = media.map((m) => (MEDIA[m] || MEDIA.file).accept)
    .filter(Boolean).join(',');
  $('file-label').textContent = primary.many;
  $('file-help').textContent = required.length
    ? 'Без файла запрос не уйдёт'
    : 'Можно не прикреплять';
  $('task-help').textContent = caps.labels.tasks?.[task]?.help || 'Что именно нужно сделать';

  const providers = providersFor(task);
  fillSelect($('auto-provider'),
    providers.map((p) => ({ value: p.id, label: p.configured ? p.id : `${p.id} (не настроен)` })),
    '— любой —');
  renderParams();
  loadTree().then(onFormChange);
  saveLocal('run:task', task);
}

function onFormChange() {
  renderParams();
  saveLocal(`run:draft:${currentTask()}`, $('prompt').value);
  writeParams({
    ...readParams(),
    task: currentTask(),
    model_id: currentModelId() || undefined,
    provider: currentProvider() || undefined,
  });
  schedulePlan();
}

async function loadTree() {
  tree = await api(`/registry/tree${query({ task: currentTask() })}`);
  $('registry-freshness').textContent =
    `${tree.total} ${tree.total === 1 ? 'модель' : 'моделей'} в реестре для этой задачи`;
  rebuildCascade(0);
}

function renderServerSettings() {
  fill($('server-settings'), caps.server.map((row) => {
    const tr = el('tr');
    tr.appendChild(el('th', row.label));
    const td = el('td', row.value, { className: 'mono' });
    td.appendChild(el('span', ` ${row.env_var}`, { className: 'muted' }));
    tr.appendChild(td);
    return tr;
  }));
}

async function boot() {
  caps = await api('/capabilities');
  const params = readParams();

  fillTaskSelect();
  // A task the address bar or a saved draft names may no longer exist, and a
  // task nobody provides is offered but disabled — so fall back to whatever is
  // actually available rather than to a literal.
  const wanted = params.task || params.modality || loadLocal('run:task') || 'text-to-text';
  $('task').value = availableTasks().includes(wanted) ? wanted : (availableTasks()[0] || '');

  fillSelect($('fallback'),
    caps.labels.fallback_choices.map((c) => ({ value: c.value, label: c.label })), 'показать ошибку');
  $('fallback').value = params.fallback || 'off';

  renderServerSettings();
  // A draft saved under the old key survives one deploy, so nobody loses text
  // they had typed when the page reloaded into the new vocabulary.
  $('prompt').value = loadLocal(`run:draft:${currentTask()}`, '')
    || loadLocal(`run:draft:${caps.tasks?.[currentTask()]?.billing_group}`, '')
    || '';

  onTaskChange();
  if (params.model_id) {
    mode = 'pick';
    setMode('pick');
    $('model-id').value = params.model_id;
  }
  if (params.log) {
    api(`/logs/${encodeURIComponent(params.log)}`)
      .then((run) => showRun(run))
      .catch(() => {});
  }
}

function setMode(next) {
  mode = next;
  for (const name of ['auto', 'pick', 'multi']) {
    $(`mode-${name}`).classList.toggle('active', next === name);
  }
  $('auto-block').hidden = next !== 'auto';
  $('pick-block').hidden = next !== 'pick';
  $('multi-block').hidden = next !== 'multi';
  if (next === 'multi') fillMultiPick();
  onFormChange();
}

// Every active model of the current task, flattened: the cascade is for
// picking one carefully, this is for stacking several up quickly.
function fillMultiPick() {
  const options = [];
  for (const provider of tree?.children || []) {
    for (const vendor of provider.children) {
      for (const family of vendor.children) {
        for (const variant of family.children) {
          options.push({ value: variant.value, label: `${provider.value} · ${variant.value}` });
        }
      }
    }
  }
  fillSelect($('multi-pick'), options, '— выберите —');
}

$('task').addEventListener('change', onTaskChange);
$('mode-auto').addEventListener('click', () => setMode('auto'));
$('mode-pick').addEventListener('click', () => setMode('pick'));
$('mode-multi').addEventListener('click', () => setMode('multi'));
$('multi-add').addEventListener('click', () => {
  const value = $('multi-pick').value;
  if (!value) { toast('Сначала выберите модель', true); return; }
  if (chips.includes(value)) { toast('Эта модель уже в списке'); return; }
  chips.push(value);
  renderChips();
});
$('multi-repeats').addEventListener('change', updateMultiButton);
$('cancel').addEventListener('click', () => cancelRun($('cancel').dataset.runId));
$('tier').addEventListener('change', onFormChange);
$('auto-provider').addEventListener('change', onFormChange);
$('sel-provider').addEventListener('change', () => rebuildCascade(1));
$('sel-vendor').addEventListener('change', () => rebuildCascade(2));
$('sel-family').addEventListener('change', () => rebuildCascade(3));
$('sel-variant').addEventListener('change', () => rebuildCascade(4));
$('model-id').addEventListener('input', () => {
  // Typed by hand: from here on the id is the operator's, not the cascade's.
  delete $('model-id').dataset.fromCascade;
  checkModelId();
  onFormChange();
});
$('fallback').addEventListener('change', onFormChange);
$('fallback-steps').addEventListener('change', onFormChange);
$('prompt').addEventListener('input',
  () => saveLocal(`run:draft:${currentTask()}`, $('prompt').value));
$('show-plan').addEventListener('click',
  () => refreshPlan().then(() => renderPlan(lastPlan)).catch((e) => toast(e.message, true)));
$('send').addEventListener('click', send);
// Ctrl+Enter sends. A bare keystroke never spends money (§8.12).
bind({ key: 'Enter', ctrl: true }, send);

boot().catch((error) => toast(`Не удалось загрузить экран: ${error.message}`, true));
