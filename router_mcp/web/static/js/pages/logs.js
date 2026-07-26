// "Журнал" screen: filters, cursor paging, the error opened in place.
//
// Two things the old page could not do, both of which made it useless exactly
// when it mattered: the error text was in the database and never on screen, and
// times were shown in UTC for an operator sitting in UTC+5 (§13).

import { api, ApiError, query } from '../core/api.js';
import { el, row, clear, toast } from '../core/dom.js';
import { costCell, localTime, money, timeZoneLabel, tokens, duration } from '../core/format.js';
import { readParams, writeParams } from '../core/state.js';
import { inspector } from '../components/inspector.js';
import { initSpend, loadSpend } from './spend.js';

const $ = (id) => document.getElementById(id);
const PAGE_SIZE = 50;

let cursor = null;
let shown = 0;
let labels = null;

function filters() {
  return {
    user: $('f-user').value.trim(),
    status: $('f-status').value,
    modality: $('f-modality').value,
    channel: $('f-channel').value,
    with_fallback: $('f-fallback').checked,
    with_retries: $('f-retries').checked,
    unverified_cost: $('f-unverified').checked,
  };
}

// The model cell says which pair actually ran and, when it is not the one that
// was asked for, why. A surprising bill gets explained here or nowhere.
function modelCell(r) {
  const text = r.model_id ?? '—';
  if (!r.requested_model_id) return el('span', text, { className: 'mono' });
  return el('span', `${text} ⇄`, {
    className: 'mono',
    title: `Запрошена ${r.requested_model_id}: ${r.fallback_reason || ''}`,
  });
}

// The error goes under its row, with the failure text and a way out of it.
function errorRow(r, columns) {
  const cell = el('td', undefined, { className: 'log-error' });
  cell.colSpan = columns;
  cell.appendChild(el('span', r.error_message || 'ошибка без описания', { className: 'err' }));
  cell.appendChild(document.createTextNode(' '));
  cell.appendChild(el('a', `подробности #${r.id}`, { href: `/logs?log=${r.id}` }));
  const tr = el('tr');
  tr.appendChild(cell);
  return tr;
}

function logRow(r) {
  const tr = row([
    { text: r.id, className: 'num' },
    { text: localTime(r.created_at), title: r.created_at },
    r.external_user_id,
    { node: modelCell(r) },
    r.modality,
    r.channel || '—',
    r.status === 'ok'
      ? el('span', 'ok', { className: 'ok' })
      : el('span', r.status, { className: 'err' }),
    { text: duration(r.latency_ms), className: 'num' },
    { text: tokens({
        prompt: r.prompt_tokens,
        completion: r.completion_tokens,
        cache_read: r.cache_read_tokens,
        reasoning: r.reasoning_tokens,
        audio_seconds: r.audio_seconds,
        images: r.images,
        characters: r.characters,
      }), className: 'num' },
    { node: costCell(r.cost_usd, r.cost_source, labels) },
    { text: (r.request_text || '').slice(0, 60), className: 'trunc', title: r.request_text || '' },
  ]);
  tr.addEventListener('click', () => {
    writeParams({ ...readParams(), log: r.id });
    openEntry(r.id);
  });
  return tr;
}

async function loadLogs({ append = false } = {}) {
  const params = filters();
  if (!append) { cursor = null; shown = 0; }
  const answer = await api(`/logs${query({ ...params, cursor, limit: PAGE_SIZE })}`);
  const body = document.querySelector('#logs tbody');
  if (!append) clear(body);
  const columns = document.querySelectorAll('#logs thead th').length;
  for (const r of answer.items) {
    body.appendChild(logRow(r));
    if (r.status !== 'ok') body.appendChild(errorRow(r, columns));
  }
  shown += answer.items.length;
  cursor = answer.next_cursor;
  $('more').hidden = cursor === null;
  $('count').textContent = `показано ${shown} из ${answer.total}`;
  writeParams({ ...params, log: readParams().log });
}

// Opening a row uses the very same inspector the «Прогон» screen shows after a
// send — same component, same envelope, so the log is not a lesser view of a
// run. `?log=4821` in the address makes it a link a colleague can open.
async function openEntry(id) {
  try {
    const run = await api(`/logs/${encodeURIComponent(id)}`);
    const card = $('detail');
    card.hidden = false;
    clear(card);
    const head = el('div', undefined, { className: 'row' });
    head.appendChild(el('h2', `Запись #${run.log_id}`));
    head.appendChild(el('span', undefined, { className: 'spacer' }));
    head.appendChild(el('button', 'Повторить в прогоне', {
      // Repeating is a button, never a keystroke: it spends money.
      onClick: () => {
        const to = `/?modality=${run.modality}`
          + (run.model.model_id ? `&model_id=${encodeURIComponent(run.model.model_id)}` : '');
        window.location.href = to;
      },
    }));
    head.appendChild(el('button', 'Закрыть', {
      onClick: () => {
        card.hidden = true;
        writeParams({ ...readParams(), log: undefined });
      },
    }));
    card.appendChild(head);
    card.appendChild(inspector(el('div'), run, { labels }));
    card.scrollIntoView({ block: 'nearest' });
  } catch (e) {
    toast(e instanceof ApiError ? e.message : String(e), true);
  }
}

function openDeepLink() {
  const id = readParams().log;
  if (id) openEntry(id);
}

function reload() {
  loadLogs().catch((e) => toast(`Ошибка: ${e.message}`, true));
}

for (const id of ['f-user', 'f-status', 'f-modality', 'f-channel',
                  'f-fallback', 'f-retries', 'f-unverified']) {
  $(id).addEventListener('change', () => loadLogs());
}
$('reload').addEventListener('click', reload);
$('more').addEventListener('click', () => loadLogs({ append: true }));
$('tz').textContent = timeZoneLabel();

// Restore the filters the URL carries before the first load.
const initial = readParams();
if (initial.user) $('f-user').value = initial.user;
if (initial.status) $('f-status').value = initial.status;
if (initial.modality) $('f-modality').value = initial.modality;
if (initial.channel) $('f-channel').value = initial.channel;
$('f-fallback').checked = initial.with_fallback === 'true';
$('f-retries').checked = initial.with_retries === 'true';
$('f-unverified').checked = initial.unverified_cost === 'true';

// Which tab is open lives in the address, like every other bit of screen state.
function showTab(name) {
  const spend = name === 'spend';
  $('pane-spend').hidden = !spend;
  $('pane-requests').hidden = spend;
  $('tab-spend').classList.toggle('active', spend);
  $('tab-requests').classList.toggle('active', !spend);
  writeParams({ ...readParams(), tab: spend ? 'spend' : undefined });
  if (spend) loadSpend().catch((e) => toast(`Ошибка: ${e.message}`, true));
}

$('tab-requests').addEventListener('click', () => showTab('requests'));
$('tab-spend').addEventListener('click', () => showTab('spend'));
initSpend();

// The glossary comes from the server so one concept keeps one name across the
// table, the inspector and the reports.
api('/capabilities')
  .then((caps) => { labels = caps.labels; })
  .catch(() => {})   // raw codes are ugly, never wrong
  .finally(() => {
    reload();
    openDeepLink();
    if (readParams().tab === 'spend') showTab('spend');
  });
