// "Отправить запрос" screen.
//
// Page modules own the network and the address bar; components get plain data.
// The full "Прогон" screen from §10 of the concept lands in wave 1 — this is the
// existing test console moved out of the template and onto the Run envelope.

import { api, ApiError, query } from '../core/api.js';
import { el, clear, toast } from '../core/dom.js';
import { costCell, duration, tokens } from '../core/format.js';
import { bind } from '../core/keys.js';

const $ = (id) => document.getElementById(id);

// --- model picker -------------------------------------------------------
// `allModels` is the full list for the current modality+tier; the four selects
// narrow it down. Each model_id looks like "<vendor>/<name>[:variant]" — we slice
// it into vendor / base name / variant so the dropdowns stay short. Wave 1 moves
// this split to the server, which already stores the vendor in its own column.
let allModels = [];

const vendorOf = (m) =>
  m.model_provider || (m.model_id.includes('/') ? m.model_id.split('/')[0] : 'other');

function baseOf(m) {
  let rest = m.model_id;
  const slash = rest.indexOf('/');
  if (slash >= 0) rest = rest.slice(slash + 1);
  const colon = rest.indexOf(':');
  if (colon >= 0) rest = rest.slice(0, colon);
  return rest;
}

const variantOf = (m) => {
  const colon = m.model_id.indexOf(':');
  return colon >= 0 ? m.model_id.slice(colon + 1) : '';
};

function fillSelect(sel, options, anyLabel) {
  const prev = sel.value;
  clear(sel);
  sel.appendChild(el('option', anyLabel));
  for (const o of options) {
    const opt = el('option', o.label);
    opt.value = o.value;
    sel.appendChild(opt);
  }
  sel.value = options.some((o) => o.value === prev) ? prev : '';
}

const byProvider = () => {
  const p = $('sel-provider').value;
  return p ? allModels.filter((m) => m.service_provider === p) : allModels;
};
const byVendor = () => {
  const v = $('sel-vendor').value;
  return v ? byProvider().filter((m) => vendorOf(m) === v) : byProvider();
};
const byBase = () => {
  const b = $('sel-model').value;
  return b ? byVendor().filter((m) => baseOf(m) === b) : byVendor();
};

const uniqSorted = (xs) => [...new Set(xs)].sort();

function rebuildProviders() {
  fillSelect($('sel-provider'),
    uniqSorted(allModels.map((m) => m.service_provider)).map((v) => ({ value: v, label: v })),
    '— любой —');
  rebuildVendors();
}
function rebuildVendors() {
  fillSelect($('sel-vendor'),
    uniqSorted(byProvider().map(vendorOf)).map((v) => ({ value: v, label: v })),
    '— любой —');
  rebuildBases();
}
function rebuildBases() {
  fillSelect($('sel-model'),
    uniqSorted(byVendor().map(baseOf)).map((v) => ({ value: v, label: v })),
    '— любая —');
  rebuildVersions();
}
function rebuildVersions() {
  const seen = new Set();
  const opts = [];
  for (const m of byBase()) {
    if (seen.has(m.model_id)) continue;
    seen.add(m.model_id);
    opts.push({ value: m.model_id, label: `${variantOf(m) || 'базовая'} · ${m.tier}` });
  }
  opts.sort((a, b) => a.label.localeCompare(b.label));
  fillSelect($('sel-version'), opts, '— любая —');
  syncModelId();
}
function syncModelId() {
  const version = $('sel-version').value;
  if (version) { $('model').value = version; return; }
  // No explicit version chosen, but if the narrowed set is a single model, use it.
  const ms = byBase();
  if ($('sel-model').value && ms.length === 1) $('model').value = ms[0].model_id;
}

async function loadModels() {
  const modality = $('modality').value;
  const tier = $('tier').value;
  const answer = await api(`/models${query({ modality, tier })}`);
  allModels = answer.items;
  const list = $('models-list');
  clear(list);
  for (const x of allModels.slice(0, 1000)) {
    const opt = el('option');
    opt.value = x.model_id;
    opt.label = `${x.model_id} (${x.tier})`;
    list.appendChild(opt);
  }
  rebuildProviders();
  toast(`Загружено моделей: ${allModels.length}`);
}

// --- request handling ---------------------------------------------------
function onModalityChange() {
  const m = $('modality').value;
  $('file-field').hidden = (m !== 'stt');
  $('text-field').hidden = (m === 'stt');
  $('engine-field').hidden = (m !== 'cli');
  $('picker').hidden = (m === 'cli');  // CLI is driven by the engine selector, not the registry
  loadModels();
}

// The summary line of the Run envelope. Every value here came from a provider
// or from caller input, so it is appended as text nodes, never as markup.
function showMeta(run) {
  const meta = $('meta');
  clear(meta);
  const bits = [
    ['Поставщик: ', el('b', run.model.provider)],
    ['Модель: ', el('b', run.model.model_id)],
  ];
  if (run.fallback) {
    const what = run.fallback.kind === 'after_failure' ? 'после отказа' : 'до вызова';
    bits.push([`Подмена (${what}): `,
      el('b', `запрошена ${run.fallback.trigger} — ${run.fallback.reason || ''}`)]);
  }
  bits.push(['Токены: ', el('b', tokens(run.usage))]);
  bits.push(['Время: ', el('b', duration(run.latency_ms))]);
  if (run.attempts > 1) bits.push(['Повторов: ', el('b', run.attempts)]);
  bits.push(['Стоимость: ', costCell(run.cost.value, run.cost.source)]);

  bits.forEach(([label, node], i) => {
    if (i) meta.appendChild(document.createTextNode(' · '));
    meta.appendChild(document.createTextNode(label));
    meta.appendChild(node);
  });
  if (run.log_id) {
    meta.appendChild(document.createTextNode(' · '));
    meta.appendChild(el('a', `в журнале #${run.log_id}`, { href: `/logs?log=${run.log_id}` }));
  }
  // An adapter that silently drops a parameter lets a caller pay for a request
  // that ignored half of what they asked for.
  if (run.ignored_params && run.ignored_params.length) {
    meta.appendChild(el('div', `⚠ адаптер не читает: ${run.ignored_params.join(', ')}`,
      { className: 'warn' }));
  }
}

function resetResult() {
  $('result-card').hidden = false;
  $('answer').hidden = true; $('answer').textContent = '';
  clear($('images'));
  $('audio-out').hidden = true; $('audio-out').removeAttribute('src');
}

function showAnswer(run) {
  resetResult();
  const audio = run.attachments.find((a) => (a.mime || '').startsWith('audio/'));
  const images = run.attachments.filter(
    (a) => a.kind === 'image' || (a.mime || '').startsWith('image/'));
  if (audio) {
    const player = $('audio-out');
    player.src = audio.download_url;
    player.hidden = false;
  }
  for (const image of images) {
    const img = el('img', undefined, { className: 'gen-image' });
    img.src = image.download_url;
    $('images').appendChild(img);
  }
  if (run.text || (!audio && !images.length)) {
    $('answer').hidden = false;
    $('answer').textContent = run.text || '(пусто)';
  }
}

function showError(error) {
  resetResult();
  const meta = $('meta');
  clear(meta);
  meta.appendChild(el('b', `Ошибка: ${error.message}`, { className: 'err' }));
  if (error.hint && error.hint.env_var) {
    meta.appendChild(el('div', `Не задана переменная окружения ${error.hint.env_var}`,
      { className: 'muted mono' }));
  }
  if (error.retryAfterS) {
    meta.appendChild(el('div', `Провайдер просил подождать ${error.retryAfterS} с`,
      { className: 'muted' }));
  }
  if (error.logId) {
    meta.appendChild(el('div', undefined, {
      children: [el('a', `Открыть в журнале #${error.logId}`, { href: `/logs?log=${error.logId}` })],
    }));
  }
  toast(`Ошибка: ${error.message}`, true);
}

async function send() {
  const btn = $('send');
  btn.disabled = true;
  try {
    const m = $('modality').value;
    const user = $('user').value || 'web-tester';
    const model_id = $('model').value || null;
    const tier = $('tier').value || null;
    const provider = $('sel-provider').value || null;
    const fallback = $('fallback').value || null;
    const prompt = $('prompt').value;
    let run;

    if (m === 'chat') {
      run = await api('/chat', { method: 'POST', body: { external_user_id: user, text: prompt, model_id, tier, provider, fallback } });
    } else if (m === 'image') {
      run = await api('/image', { method: 'POST', body: { external_user_id: user, prompt, model_id, provider, fallback } });
    } else if (m === 'cli') {
      run = await api('/cli', { method: 'POST', body: { external_user_id: user, prompt, engine: $('engine').value } });
    } else if (m === 'tts') {
      run = await api('/tts', { method: 'POST', body: { external_user_id: user, text: prompt, model_id, provider, fallback } });
    } else if (m === 'stt') {
      const file = $('audio').files[0];
      if (!file) { toast('Выберите аудиофайл', true); return; }
      const form = new FormData();
      form.append('file', file);
      form.append('external_user_id', user);
      if (model_id) form.append('model_id', model_id);
      if (provider) form.append('provider', provider);
      run = await api('/transcribe', { method: 'POST', formData: form });
    }
    showAnswer(run);
    showMeta(run);
  } catch (e) {
    if (e instanceof ApiError) showError(e);
    else toast(`Ошибка: ${e.message}`, true);
  } finally {
    btn.disabled = false;
  }
}

$('modality').addEventListener('change', onModalityChange);
$('tier').addEventListener('change', loadModels);
$('sel-provider').addEventListener('change', rebuildVendors);
$('sel-vendor').addEventListener('change', rebuildBases);
$('sel-model').addEventListener('change', rebuildVersions);
$('sel-version').addEventListener('change', syncModelId);
$('send').addEventListener('click', send);
$('refresh-models').addEventListener('click', loadModels);
// Ctrl+Enter sends; a bare key never does — sending costs money.
bind({ key: 'Enter', ctrl: true }, send);
onModalityChange();
