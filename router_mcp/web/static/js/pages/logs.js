// "Журнал" screen: filters, cursor paging, the error opened in place, the
// entry in a window over the table.
//
// Two things the old page could not do, both of which made it useless exactly
// when it mattered: the error text was in the database and never on screen, and
// times were shown in UTC for an operator sitting in UTC+5 (§13). «Расход» used
// to be a tab here; it is a screen of its own now (/spend), and this one opens
// straight on the rows.

import { api, ApiError, query } from '../core/api.js';
import { el, row, clear, toast } from '../core/dom.js';
import { costCell, count, localTime, money, timeZoneLabel, tokens, duration } from '../core/format.js';
import { SIDE_LABEL, fullUrl, runImages, thumbUrl } from '../core/media.js';
import { idempotencyKey, start, watch } from '../core/runs.js';
import { readParams, writeParams } from '../core/state.js';
import { inspector } from '../components/inspector.js';
import { mountLogFilters, toApiParams } from '../components/log-filters.js';

const $ = (id) => document.getElementById(id);
const PAGE_SIZE = 50;

let cursor = null;
let shown = 0;
let labels = null;

// The filter row is the shared component («Картинки» mounts the same boxes);
// this screen adds the money checkboxes and the day pager the markup carries.
const filterRow = mountLogFilters({
  onChange: () => loadLogs().catch(() => {}),
  onReload: () => reload(),
});
const filters = () => filterRow.values();
const apiParams = (f) => toApiParams(f);

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
  cell.appendChild(el('a', `подробности #${r.id}`, {
    href: `/logs?log=${r.id}`,
    // The link stays a link (copyable, openable in a new tab); a plain click
    // opens the window here instead of reloading the page under it.
    onClick: (event) => {
      event.preventDefault?.();
      writeParams({ ...readParams(), log: r.id });
      openEntry(r.id);
    },
  }));
  const tr = el('tr');
  tr.appendChild(cell);
  return tr;
}

// Four counters, four columns — the combined cell hid which kind of token the
// money went to. A row billed by seconds, characters or images has no token to
// show, so its measure takes the first cell instead of four dashes lying zero.
function tokenCells(r) {
  const num = (v) => ({
    text: v === null || v === undefined ? '—' : count(v),
    className: v ? 'num' : 'num muted',
  });
  const counted = [r.prompt_tokens, r.completion_tokens, r.cache_read_tokens, r.reasoning_tokens]
    .some((v) => v !== null && v !== undefined);
  if (!counted) {
    const measure = tokens({
      audio_seconds: r.audio_seconds, images: r.images, characters: r.characters,
    });
    return [{ text: measure, className: 'num muted' }, num(null), num(null), num(null)];
  }
  return [num(r.prompt_tokens), num(r.cache_read_tokens),
          num(r.completion_tokens), num(r.reasoning_tokens)];
}

// The cell shows the words that were asked; the JSON stays in the tooltip.
// `request_text` holds the raw message list when the caller sent history, and
// a column of JSON punctuation answers «что спросили» for nobody.
function requestCell(r) {
  const raw = (r.request_text || '').trim();
  let text = raw;
  let title = raw;
  if (raw.startsWith('[') || raw.startsWith('{')) {
    try {
      const parsed = JSON.parse(raw);
      title = JSON.stringify(parsed, null, 2);
      text = messagesText(parsed) || raw;
    } catch (_) {
      // The column is cut at 4000 characters, so a long history arrives as
      // JSON with its tail missing. The words are still in there — pull them
      // out by shape instead of showing the punctuation.
      text = salvagedText(raw) || raw;
    }
  }
  return { text: text.slice(0, 60), className: 'trunc', title };
}

// `"role": "…", "content": "…"` pairs scanned straight off a JSON string that
// does not parse. The last user message wins; failing that, the last message
// with any words at all (a cut can leave only the system prompt).
function salvagedText(raw) {
  const re = /"role"\s*:\s*"(\w+)"\s*,\s*"content"\s*:\s*"((?:[^"\\]|\\.)*)/g;
  let lastUser = '';
  let lastAny = '';
  for (let m = re.exec(raw); m !== null; m = re.exec(raw)) {
    const words = unescapeJson(m[2]);
    if (!words) continue;
    lastAny = words;
    if (m[1] === 'user') lastUser = words;
  }
  return lastUser || lastAny;
}

function unescapeJson(s) {
  return s
    .replace(/\\u([0-9a-fA-F]{4})/g, (_, hex) => String.fromCharCode(parseInt(hex, 16)))
    .replace(/\\[nt]/g, ' ')     // one-line cell: newlines and tabs read as spaces
    .replace(/\\(.)/g, '$1');
}

// The words the operator typed: the last user message, or failing that the
// last message that carries any text at all.
function messagesText(parsed) {
  const messages = Array.isArray(parsed) ? parsed : [parsed];
  const textOf = (m) => {
    if (!m) return '';
    const content = m.content ?? m.text;
    if (typeof content === 'string') return content;
    if (Array.isArray(content)) {
      return content
        .map((part) => (typeof part === 'string' ? part : (part && part.text) || ''))
        .filter(Boolean).join(' ');
    }
    return '';
  };
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    if (messages[i]?.role === 'user' && textOf(messages[i])) return textOf(messages[i]);
  }
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    if (textOf(messages[i])) return textOf(messages[i]);
  }
  return '';
}

function logRow(r) {
  const tr = row([
    { text: r.id, className: 'num' },
    { text: localTime(r.created_at), title: r.created_at },
    r.external_user_id,
    // Which application asked. Empty means the caller did not say and the
    // request went out under the router's own identity.
    { text: r.app || '—', className: r.app ? 'trunc' : 'muted', title: r.app || '' },
    r.service_provider_id || '—',
    { node: modelCell(r) },
    // The task, because the group is ambiguous between two of them: a row that
    // says «Картинка» does not say whether one was drawn or edited.
    labels?.tasks?.[r.task]?.label || r.task || r.modality,
    r.channel || '—',
    statusCell(r.status),
    { text: duration(r.latency_ms), className: 'num' },
    ...tokenCells(r),
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
    requestCell(r),
  ]);
  tr.addEventListener('click', () => {
    writeParams({ ...readParams(), log: r.id });
    openEntry(r.id);
  });
  return tr;
}

// The `change` listener and the debounced typeahead both call this, often for
// the same filter state; the key check turns the second call into a no-op
// instead of a second identical request.
let lastKey = null;

async function loadLogs({ append = false, force = false } = {}) {
  const params = filters();
  const key = JSON.stringify(params);
  if (!append && !force && key === lastKey) return;
  if (!append) { cursor = null; shown = 0; }
  const answer = await api(`/logs${query({ ...apiParams(params), cursor, limit: PAGE_SIZE })}`);
  lastKey = key;
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
  liveTimer = setTimeout(() => loadLogs({ force: true }).catch(() => {}), LIVE_REFRESH_MS);
}

// Opening a row uses the very same inspector the «Прогон» screen shows after a
// send — same component, same envelope, so the log is not a lesser view of a
// run. `?log=4821` in the address makes it a link a colleague can open.
//
// The entry is a window over the table (a native <dialog>): the row stays where
// the operator found it, Esc closes, and ▲ ▼ next to the number step to the
// neighbouring rows under the same filters — the server names them, so «next»
// with `status=error` set is the next error, not the next row.
let openId = null;

function showDialog(dialog) {
  if (typeof dialog.showModal === 'function') {
    if (!dialog.open) dialog.showModal();
  } else {
    dialog.setAttribute('open', '');   // the node harness has no dialog element
  }
}

function hideDialog(dialog) {
  if (typeof dialog.close === 'function') {
    if (dialog.open) dialog.close();
  } else {
    dialog.removeAttribute?.('open');
  }
}

// The pictures a run carried, request side first, as a strip under the head:
// «что рисовали» should not need a tab. Each opens full size in its own tab.
function thumbStrip(run) {
  const images = runImages(run);
  if (!images.length) return null;
  const strip = el('div', undefined, { className: 'thumbs' });
  for (const image of images) {
    const link = el('a', undefined, { href: fullUrl(image), className: 'thumb' });
    link.setAttribute('target', '_blank');
    link.setAttribute('rel', 'noopener');
    const img = el('img');
    img.src = thumbUrl(image);
    img.setAttribute('loading', 'lazy');
    img.setAttribute('alt', SIDE_LABEL[image.side] || image.side);
    link.appendChild(img);
    const side = SIDE_LABEL[image.side] || image.side;
    link.appendChild(el('span', image.role ? `${side} · ${image.role}` : side, {
      className: 'thumb-label',
    }));
    strip.appendChild(link);
  }
  return strip;
}

async function openEntry(id) {
  const dialog = $('detail');
  try {
    const [run, neighbors] = await Promise.all([
      api(`/logs/${encodeURIComponent(id)}`),
      api(`/logs/${encodeURIComponent(id)}/neighbors${query(apiParams(filters()))}`)
        .catch(() => ({ newer_id: null, older_id: null })),
    ]);
    openId = run.log_id;
    clear(dialog);
    const head = el('div', undefined, { className: 'row dialog-head' });
    const step = (target, glyph, title) => {
      const button = el('button', glyph, {
        className: 'nav-btn',
        title: target === null ? 'Дальше записей нет' : title,
        onClick: () => {
          if (target === null) return;
          writeParams({ ...readParams(), log: target });
          openEntry(target);
        },
      });
      button.disabled = target === null;
      return button;
    };
    head.appendChild(step(neighbors.newer_id, '▲', 'Предыдущая запись (выше в таблице, новее)'));
    head.appendChild(el('h2', `Запись #${run.log_id}`));
    head.appendChild(step(neighbors.older_id, '▼', 'Следующая запись (ниже в таблице, старше)'));
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
    head.appendChild(el('button', 'Закрыть', { onClick: () => hideDialog(dialog) }));
    dialog.appendChild(head);
    const strip = thumbStrip(run);
    if (strip) dialog.appendChild(strip);
    dialog.appendChild(inspector(el('div'), run, { labels }));
    showDialog(dialog);
    dialog.scrollTop = 0;
  } catch (e) {
    toast(e instanceof ApiError ? e.message : String(e), true);
  }
}

// Closing by any route — the button, Esc, a click on the backdrop — takes the
// entry out of the address, so a reload comes back to the table.
$('detail').addEventListener('close', () => {
  openId = null;
  writeParams({ ...readParams(), log: undefined });
});
// A click on the backdrop closes too. The dialog's own padding also reports
// the dialog as the target, so the click has to be outside its box.
$('detail').addEventListener('click', (event) => {
  const dialog = $('detail');
  if (event.target !== dialog || typeof dialog.getBoundingClientRect !== 'function') return;
  const box = dialog.getBoundingClientRect();
  const outside = event.clientX < box.left || event.clientX > box.right
    || event.clientY < box.top || event.clientY > box.bottom;
  if (outside) hideDialog(dialog);
});
// ↑ ↓ walk the rows while the window is open; the buttons stay for the mouse.
$('detail').addEventListener('keydown', (event) => {
  if (openId === null) return;
  const tag = (event.target?.tagName || '').toLowerCase();
  if (tag === 'input' || tag === 'textarea' || tag === 'select') return;
  if (event.key !== 'ArrowUp' && event.key !== 'ArrowDown') return;
  const buttons = $('detail').querySelectorAll('.nav-btn');
  const button = event.key === 'ArrowUp' ? buttons[0] : buttons[1];
  if (!button || button.disabled) return;
  event.preventDefault?.();
  button.click?.();
});

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

function reload() {
  loadLogs({ force: true }).catch((e) => toast(`Ошибка: ${e.message}`, true));
}

$('more').addEventListener('click', () => loadLogs({ append: true }));
$('tz').textContent = timeZoneLabel();

// The first load waits for the glossary and the provider list: a deep link
// with `task=` or `provider=` selects an option that has to exist by then.
filterRow.ready.then((glossary) => {
  labels = glossary;
  reload();
  openDeepLink();
});
