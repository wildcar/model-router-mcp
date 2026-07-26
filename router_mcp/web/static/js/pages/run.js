// «Прогон»: the screen where money gets spent, so it explains itself first.
//
// Three rules from §5 and §10 shape everything here. The form is built from
// /api/v1/capabilities, so a field is on screen exactly when the adapter behind
// the chosen (modality, provider) pair reads it. The plan is free and recomputed
// as the discrete fields change, so "куда уйдёт запрос" is answered before the
// send button. And the send button carries the estimate, because a price after
// the fact is a receipt, not a decision.

import { api, ApiError, query } from '../core/api.js';
import { el, clear, fill, toast } from '../core/dom.js';
import { costCell, money } from '../core/format.js';
import { bind } from '../core/keys.js';
import { loadLocal, readParams, saveLocal, writeParams } from '../core/state.js';
import { inspector } from '../components/inspector.js';

const $ = (id) => document.getElementById(id);
const PLAN_DEBOUNCE_MS = 300;

let caps = null;
let tree = null;
let mode = 'auto';          // 'auto' | 'pick'
let lastPlan = null;
const feed = [];            // last runs of this session

// --- capabilities-driven form ------------------------------------------
function providersFor(modality) {
  const byModality = caps.params[modality] || {};
  return caps.providers.filter((p) => p.id in byModality);
}

function paramSpecs() {
  const modality = $('modality').value;
  const provider = currentProvider();
  const byProvider = caps.params[modality] || {};
  if (provider) return byProvider[provider]?.params || [];

  // With no provider pinned the router will choose one, so the form shows the
  // parameters every candidate accepts — a union would offer fields the chosen
  // adapter drops. Configured providers are the candidates; if none is
  // configured we fall back to all of them, because the form still has to
  // describe what the request would take. The plan says separately that nothing
  // can run.
  const configured = Object.entries(byProvider)
    .filter(([id]) => caps.providers.find((p) => p.id === id)?.configured)
    .map(([, spec]) => spec);
  const entries = configured.length ? configured : Object.values(byProvider);
  if (!entries.length) return [];
  const [first, ...rest] = entries;
  return first.params.filter(
    (param) => rest.every((spec) => spec.params.some((p) => p.name === param.name)));
}

function renderParams() {
  const specs = paramSpecs();
  const host = $('params');
  clear(host);
  $('params-empty').hidden = specs.length > 0;
  const overrides =
    caps.params[$('modality').value]?.[currentProvider()]?.model_overrides || {};
  for (const spec of specs) {
    const override = overrides[spec.name] || {};
    const label = el('label', undefined, { className: spec.type === 'select' ? 'grow' : '' });
    label.appendChild(document.createTextNode(
      override.required ? `${spec.label} *` : spec.label));
    let field;
    if (spec.type === 'select') {
      field = el('select');
      field.appendChild(el('option', '— по умолчанию —'));
      for (const option of spec.options || []) {
        const node = el('option', option);
        node.value = option;
        field.appendChild(node);
      }
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
  select.appendChild(el('option', anyLabel));
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
    fillSelect($('sel-variant'),
      provider && vendor && family ? treeLevel([provider, vendor, family]) : [],
      '— любой —');
  }
  const variant = $('sel-variant').value;
  if (variant) $('model-id').value = variant;
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
    modality: $('modality').value,
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
const ENDPOINTS = {
  chat: (body) => ['/chat', { external_user_id: body.external_user_id, text: body.text,
    model_id: body.model_id, tier: body.tier, provider: body.provider,
    params: body.params, fallback: body.fallback }],
  image: (body) => ['/image', { external_user_id: body.external_user_id, prompt: body.text,
    model_id: body.model_id, provider: body.provider, params: body.params,
    fallback: body.fallback }],
  tts: (body) => ['/tts', { external_user_id: body.external_user_id, text: body.text,
    model_id: body.model_id, provider: body.provider, params: body.params,
    fallback: body.fallback }],
  cli: (body) => ['/cli', { external_user_id: body.external_user_id, prompt: body.text,
    engine: (body.provider || 'codex-cli').replace('-cli', ''), params: body.params }],
};

async function send() {
  const button = $('send');
  button.disabled = true;
  const body = requestBody();
  try {
    let run;
    if (body.modality === 'stt') {
      const file = $('audio').files[0];
      if (!file) { toast('Выберите аудиофайл', true); return; }
      const form = new FormData();
      form.append('file', file);
      form.append('external_user_id', body.external_user_id);
      if (body.model_id) form.append('model_id', body.model_id);
      if (body.provider) form.append('provider', body.provider);
      run = await api('/transcribe', { method: 'POST', formData: form });
    } else {
      const [path, payload] = ENDPOINTS[body.modality](body);
      run = await api(path, { method: 'POST', body: payload });
    }
    showRun(run);
    remember(run);
  } catch (error) {
    if (error instanceof ApiError) showError(error);
    else toast(`Ошибка: ${error.message}`, true);
  } finally {
    button.disabled = false;
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

// --- wiring -------------------------------------------------------------
function onModalityChange() {
  const modality = $('modality').value;
  $('file-field').hidden = modality !== 'stt';
  $('text-field').hidden = modality === 'stt';
  const providers = providersFor(modality);
  fillSelect($('auto-provider'),
    providers.map((p) => ({ value: p.id, label: p.configured ? p.id : `${p.id} (не настроен)` })),
    '— любой —');
  renderParams();
  loadTree().then(onFormChange);
  saveLocal('run:modality', modality);
}

function onFormChange() {
  renderParams();
  saveLocal(`run:draft:${$('modality').value}`, $('prompt').value);
  writeParams({
    ...readParams(),
    modality: $('modality').value,
    model_id: currentModelId() || undefined,
    provider: currentProvider() || undefined,
  });
  schedulePlan();
}

async function loadTree() {
  tree = await api(`/registry/tree${query({ modality: $('modality').value })}`);
  $('registry-freshness').textContent =
    `${tree.total} ${tree.total === 1 ? 'модель' : 'моделей'} в реестре для этой модальности`;
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

  fillSelect($('modality'),
    Object.keys(caps.params).map((value) => ({
      value, label: caps.labels.modalities[value]?.label || value,
    })), '— выберите —');
  $('modality').value = params.modality || loadLocal('run:modality') || 'chat';
  if (!$('modality').value) $('modality').value = Object.keys(caps.params)[0];

  fillSelect($('fallback'),
    caps.labels.fallback_choices.map((c) => ({ value: c.value, label: c.label })), 'показать ошибку');
  $('fallback').value = params.fallback || 'off';

  renderServerSettings();
  $('prompt').value = loadLocal(`run:draft:${$('modality').value}`, '') || '';

  onModalityChange();
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
  $('mode-auto').classList.toggle('active', next === 'auto');
  $('mode-pick').classList.toggle('active', next === 'pick');
  $('auto-block').hidden = next !== 'auto';
  $('pick-block').hidden = next === 'auto';
  onFormChange();
}

$('modality').addEventListener('change', onModalityChange);
$('mode-auto').addEventListener('click', () => setMode('auto'));
$('mode-pick').addEventListener('click', () => setMode('pick'));
$('tier').addEventListener('change', onFormChange);
$('auto-provider').addEventListener('change', onFormChange);
$('sel-provider').addEventListener('change', () => rebuildCascade(1));
$('sel-vendor').addEventListener('change', () => rebuildCascade(2));
$('sel-family').addEventListener('change', () => rebuildCascade(3));
$('sel-variant').addEventListener('change', () => rebuildCascade(4));
$('model-id').addEventListener('input', () => { checkModelId(); onFormChange(); });
$('fallback').addEventListener('change', onFormChange);
$('fallback-steps').addEventListener('change', onFormChange);
$('prompt').addEventListener('input',
  () => saveLocal(`run:draft:${$('modality').value}`, $('prompt').value));
$('show-plan').addEventListener('click',
  () => refreshPlan().then(() => renderPlan(lastPlan)).catch((e) => toast(e.message, true)));
$('send').addEventListener('click', send);
// Ctrl+Enter sends. A bare keystroke never spends money (§8.12).
bind({ key: 'Enter', ctrl: true }, send);

boot().catch((error) => toast(`Не удалось загрузить экран: ${error.message}`, true));
