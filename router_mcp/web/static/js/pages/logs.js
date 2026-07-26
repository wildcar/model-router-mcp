// "Журнал" screen: filters, cursor paging, the error opened in place.
//
// Two things the old page could not do, both of which made it useless exactly
// when it mattered: the error text was in the database and never on screen, and
// times were shown in UTC for an operator sitting in UTC+5 (§13).

import { api, ApiError, query } from '../core/api.js';
import { el, row, clear, toast } from '../core/dom.js';
import { costCell, localTime, money, timeZoneLabel, tokens, duration } from '../core/format.js';
import { readParams, writeParams } from '../core/state.js';

const $ = (id) => document.getElementById(id);
const PAGE_SIZE = 50;

let cursor = null;
let shown = 0;

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

async function loadUsage() {
  const answer = await api('/usage');
  const body = document.querySelector('#usage tbody');
  clear(body);
  for (const r of answer.items) {
    body.appendChild(row([
      r.external_user_id,
      { text: r.requests, className: 'num' },
      { text: r.total_tokens, className: 'num' },
      { text: money(r.total_cost_usd), className: 'num' },
    ]));
  }
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
  return row([
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
    { node: costCell(r.cost_usd, r.cost_source) },
    { text: (r.request_text || '').slice(0, 60), className: 'trunc', title: r.request_text || '' },
  ]);
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

// A deep link (`?log=4821`) opens straight onto the entry it names, so an error
// can be handed to a colleague as a URL.
async function openDeepLink() {
  const id = readParams().log;
  if (!id) return;
  try {
    const run = await api(`/logs/${encodeURIComponent(id)}`);
    const card = $('detail');
    card.hidden = false;
    clear(card);
    card.appendChild(el('h2', `Запись #${run.log_id}`));
    const pairs = [
      ['Статус', run.status],
      ['Канал', run.channel || '—'],
      ['Пара', `${run.model.provider ?? '—'} · ${run.model.model_id ?? '—'}`],
      ['Время ответа', duration(run.latency_ms)],
      ['Токены', tokens(run.usage)],
      ['Стоимость', `${money(run.cost.value)} (${run.cost.source})`],
      ['Обрыв по лимиту', run.finish_reason || 'нет'],
      ['id запроса у провайдера', run.provider_request_id || '—'],
    ];
    const table = el('table');
    const tbody = el('tbody');
    for (const [k, v] of pairs) tbody.appendChild(row([k, { text: v, className: 'mono' }]));
    table.appendChild(tbody);
    card.appendChild(table);
    if (run.error) card.appendChild(el('pre', run.error.message, { className: 'err' }));
    if (run.text) card.appendChild(el('pre', run.text));
  } catch (e) {
    toast(e instanceof ApiError ? e.message : String(e), true);
  }
}

function reload() {
  Promise.all([loadUsage(), loadLogs()]).catch((e) => toast(`Ошибка: ${e.message}`, true));
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

reload();
openDeepLink();
