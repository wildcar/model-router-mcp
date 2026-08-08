// "Журнал" screen: filters, cursor paging, the error opened in place.
//
// Two things the old page could not do, both of which made it useless exactly
// when it mattered: the error text was in the database and never on screen, and
// times were shown in UTC for an operator sitting in UTC+5 (§13).

import { api, ApiError, query } from '../core/api.js';
import { el, row, clear, toast } from '../core/dom.js';
import { costCell, localTime, money, timeZoneLabel, tokens, duration } from '../core/format.js';
import { idempotencyKey, start, watch } from '../core/runs.js';
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
    task: $('f-task').value,
    modality: $('f-modality').value,
    channel: $('f-channel').value,
    with_fallback: $('f-fallback').checked,
    with_retries: $('f-retries').checked,
    unverified_cost: $('f-unverified').checked,
    cost_mismatch: $('f-mismatch').checked,
  };
}

// Flags a row whose provider-reported sum sits away from our arithmetic —
// same thresholds as the server-side filter (2% and a hundredth of a cent),
// so the marks on screen and the filtered list never disagree.
function costMismatch(r) {
  const provider = r.provider_cost_usd;
  if (!provider || provider <= 0) return false;
  if (r.cost_source === 'provider' || r.cost_source === 'provider_notional'
      || r.cost_source === 'amortized') return false;
  return Math.abs((r.cost_usd || 0) - provider) > Math.max(0.0001, 0.02 * provider);
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

// Six states, not two: a run is a row from the moment it starts, so the log
// holds calls in flight, calls the operator stopped and calls a restart cut
// off. Each gets its own word from the server glossary — «ошибка» for a run
// that is still going would be a lie, and «ok» for one that was interrupted a
// more expensive one.
const STATUS_CLASS = {
  ok: 'ok',
  error: 'err',
  running: 'warn',
  queued: 'muted',
  cancelled: 'muted',
  interrupted: 'warn',
};

function statusCell(status) {
  const label = labels?.statuses?.[status];
  return el('span', label ? label.label : status, {
    className: STATUS_CLASS[status] || '',
    title: label ? label.help : '',
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
    // The task, because the group is ambiguous between two of them: a row that
    // says «Картинка» does not say whether one was drawn or edited.
    labels?.tasks?.[r.task]?.label || r.task || r.modality,
    r.channel || '—',
    statusCell(r.status),
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
    { node: (() => {
        const cell = costCell(r.cost_usd, r.cost_source, labels);
        if (costMismatch(r)) {
          cell.appendChild(el('span', ' ≠', {
            className: 'err',
            title: `Поставщик списал ${money(r.provider_cost_usd)} — расходится с расчётом`,
          }));
        }
        return cell;
      })() },
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
    // A run still going has no failure to explain; only a finished one does.
    if (r.status !== 'ok' && r.status !== 'running' && r.status !== 'queued') {
      body.appendChild(errorRow(r, columns));
    }
  }
  shown += answer.items.length;
  cursor = answer.next_cursor;
  $('more').hidden = cursor === null;
  $('count').textContent = `показано ${shown} из ${answer.total}`;
  writeParams({ ...params, log: readParams().log });
  scheduleLiveRefresh(answer.items);
}

// While something is running the first page refreshes itself. Not a general
// auto-refresh: it stops as soon as nothing is in flight, so a log left open on
// a quiet service is not a request every three seconds forever.
const LIVE_REFRESH_MS = 3000;
let liveTimer = null;

function scheduleLiveRefresh(items) {
  clearTimeout(liveTimer);
  const live = items.some((r) => r.status === 'running' || r.status === 'queued');
  if (!live || cursor !== null) return;
  liveTimer = setTimeout(() => loadLogs().catch(() => {}), LIVE_REFRESH_MS);
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
        const to = `/?task=${run.task || run.modality}`
          + (run.model.model_id ? `&model_id=${encodeURIComponent(run.model.model_id)}` : '');
        window.location.href = to;
      },
    }));
    // The audio is still in the blob store, so a transcription can be repeated
    // from here — on another model, without the operator hunting for the file
    // and hoping it is the same one.
    if (run.task === 'speech-to-text' && run.request_ref) {
      head.appendChild(el('button', 'Повторить распознавание', {
        onClick: () => repeatTranscription(run).catch((e) => toast(e.message, true)),
      }));
    }
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

async function repeatTranscription(run) {
  if (!confirm('Отправить тот же файл ещё раз? Это стоит денег.')) return;
  const state = await start({
    task: 'speech-to-text',
    external_user_id: run.client_id || 'web-tester',
    model_id: run.model.model_id,
    attachments: [{ ref: run.request_ref, kind: 'audio' }],
    client_request_id: idempotencyKey(),
  });
  toast('Отправлено, ждём результат…');
  const finished = await watch(state.run_id, () => {}).promise;
  await loadLogs();
  if (finished.log_id) {
    writeParams({ ...readParams(), log: finished.log_id });
    openEntry(finished.log_id);
  }
}

function openDeepLink() {
  const id = readParams().log;
  if (id) openEntry(id);
}

// A filter select, keeping «все» first and whatever the URL had preselected.
function fillFilter(id, rows) {
  const select = $(id);
  if (!select || !rows) return;
  const chosen = select.value;
  for (const [value, row] of Object.entries(rows)) {
    const option = el('option', row.label || value);
    option.value = value;
    select.appendChild(option);
  }
  select.value = chosen;
}

function reload() {
  loadLogs().catch((e) => toast(`Ошибка: ${e.message}`, true));
}

for (const id of ['f-user', 'f-status', 'f-task', 'f-modality', 'f-channel',
                  'f-fallback', 'f-retries', 'f-unverified', 'f-mismatch']) {
  $(id).addEventListener('change', () => loadLogs());
}
$('reload').addEventListener('click', reload);
$('more').addEventListener('click', () => loadLogs({ append: true }));
$('tz').textContent = timeZoneLabel();

// Restore the filters the URL carries before the first load.
const initial = readParams();
if (initial.user) $('f-user').value = initial.user;
if (initial.status) $('f-status').value = initial.status;
if (initial.task) $('f-task').value = initial.task;
if (initial.modality) $('f-modality').value = initial.modality;
if (initial.channel) $('f-channel').value = initial.channel;
$('f-fallback').checked = initial.with_fallback === 'true';
$('f-retries').checked = initial.with_retries === 'true';
$('f-unverified').checked = initial.unverified_cost === 'true';
$('f-mismatch').checked = initial.cost_mismatch === 'true';

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
  .then((caps) => {
    labels = caps.labels;
    // Both filter lists come from the server: spelled out in markup they went
    // stale the moment a task or a group was added.
    fillFilter('f-task', caps.labels.tasks);
    fillFilter('f-modality', caps.labels.modalities);
  })
  .catch(() => {})   // raw codes are ugly, never wrong
  .finally(() => {
    reload();
    openDeepLink();
    if (readParams().tab === 'spend') showTab('spend');
  });
