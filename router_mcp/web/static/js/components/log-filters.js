// The journal's filter row, as one component for every screen that lists log rows.
//
// «Журнал» and «Картинки» ask the same questions of the same table — whose
// rows, which provider, which model, which day — so they share the boxes and
// the wiring: values from the DOM, the day pager, as-you-type подсказки from
// `/logs/suggest`, the task and group lists from `/capabilities`, and the
// restore from the address. A screen mounts only the boxes its markup has;
// the component takes the set from the document, so «Картинки» without the
// money checkboxes is the same code with fewer inputs.

import { api, query } from '../core/api.js';
import { el, clear } from '../core/dom.js';
import { readParams } from '../core/state.js';

const $ = (id) => document.getElementById(id);

// Query parameter → element id. The set a screen actually has is whatever of
// these its template contains.
export const FILTER_IDS = {
  user: 'f-user',
  provider: 'f-provider',
  model: 'f-model',
  app: 'f-app',
  status: 'f-status',
  task: 'f-task',
  modality: 'f-modality',
  channel: 'f-channel',
  date: 'f-date',
  with_fallback: 'f-fallback',
  with_retries: 'f-retries',
  unverified_cost: 'f-unverified',
  cost_mismatch: 'f-mismatch',
};

// Text boxes are search fields with a datalist behind them.
const TEXT_FIELDS = { user: 'f-user-options', model: 'f-model-options', app: 'f-app-options' };
// Lists that come from the server glossary rather than the markup.
const GLOSSARY_FIELDS = { task: 'tasks', modality: 'modalities' };

// The URL keeps the local day; the server compares UTC strings. The bounds are
// rendered in the same `+00:00` shape the rows are stored in, because the
// comparison is lexicographic and a stray `Z` sorts after `+`.
export function dayBounds(date) {
  const [y, m, d] = date.split('-').map(Number);
  const iso = (dt) => dt.toISOString().slice(0, 19) + '+00:00';
  return {
    since: iso(new Date(y, m - 1, d)),
    until: iso(new Date(new Date(y, m - 1, d + 1).getTime() - 1000)),
  };
}

// What actually goes to the API: the day filter travels as since/until.
export function toApiParams(values) {
  const { date, ...rest } = values;
  return date ? { ...rest, ...dayBounds(date) } : rest;
}

function localDate(dt) {
  return `${dt.getFullYear()}-${String(dt.getMonth() + 1).padStart(2, '0')}`
    + `-${String(dt.getDate()).padStart(2, '0')}`;
}

function debounce(fn, ms) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

// Values a filter box can take, from the log itself: the datalist offers the
// labels the journal has actually seen, narrowed as the operator types.
async function suggest(field, q, listId) {
  try {
    const answer = await api(`/logs/suggest${query({ field, q })}`);
    const list = $(listId);
    if (!list) return;
    clear(list);
    for (const value of answer.items) {
      const option = el('option');
      option.value = value;
      list.appendChild(option);
    }
  } catch (_) { /* подсказки — удобство, не повод ронять экран */ }
}

// A filter select, keeping «все» first and whatever the URL had preselected.
function fillSelect(select, rows) {
  if (!select || !rows) return;
  const chosen = select.value;
  for (const [value, row] of Object.entries(rows)) {
    const option = el('option', row.label || value);
    option.value = value;
    select.appendChild(option);
  }
  select.value = chosen;
}

/**
 * Wire the filter boxes the page has.
 *
 * `onChange` fires on any discrete change and, debounced, as the operator
 * types; `onReload` on the «Обновить» button and the day pager. Returns
 * `values()` (the filter set as query parameters), `apiParams()` (the same
 * with the day as since/until), and `ready` — a promise that resolves to the
 * server glossary once the task/group lists and the provider list are in
 * place, which is when a deep link with `task=` can be applied and the first
 * load may go out.
 */
export function mountLogFilters({ onChange, onReload }) {
  const present = Object.entries(FILTER_IDS).filter(([, id]) => $(id) !== null);
  const initial = readParams();

  const values = () => {
    const out = {};
    for (const [key, id] of present) {
      const node = $(id);
      out[key] = node.type === 'checkbox' ? node.checked : String(node.value ?? '').trim();
    }
    return out;
  };

  // Restore what the address carries before the first load. Glossary-backed
  // selects are restored after their options exist — set earlier, a select
  // silently drops the value.
  for (const [key, id] of present) {
    if (key in GLOSSARY_FIELDS || key === 'provider') continue;
    const node = $(id);
    if (node.type === 'checkbox') node.checked = initial[key] === 'true';
    else if (initial[key]) node.value = initial[key];
  }

  for (const [, id] of present) $(id).addEventListener('change', () => onChange());

  // Typing filters as it goes: the list narrows and the подсказки narrow with
  // it. Debounced so a fast typist is one request, not one per keystroke.
  const typeahead = debounce((field, id, listId) => {
    suggest(field, $(id).value.trim(), listId);
    onChange();
  }, 300);
  for (const [field, listId] of Object.entries(TEXT_FIELDS)) {
    const id = FILTER_IDS[field];
    if ($(id)) $(id).addEventListener('input', () => typeahead(field, id, listId));
  }

  // The day pager. From «все дни» either arrow steps into today — the newest
  // day is where the operator was looking anyway.
  const shiftDay = (delta) => {
    const current = $('f-date').value;
    let next = new Date();
    if (current) {
      const [y, m, d] = current.split('-').map(Number);
      next = new Date(y, m - 1, d + delta);
    }
    $('f-date').value = localDate(next);
    onReload();
  };
  $('f-day-prev')?.addEventListener('click', () => shiftDay(-1));
  $('f-day-next')?.addEventListener('click', () => shiftDay(1));
  $('f-day-clear')?.addEventListener('click', () => {
    $('f-date').value = '';
    onReload();
  });
  $('reload')?.addEventListener('click', () => onReload());

  // The glossary comes from the server so one concept keeps one name across
  // the table, the inspector and the reports. The provider list is asked of
  // the log itself, not the registry — a provider disabled yesterday still
  // owns yesterday's rows. Both must land before the first load: a deep link
  // with `provider=` selects an option that has to exist by then.
  let labels = null;
  const ready = Promise.allSettled([
    api('/capabilities').then((caps) => {
      labels = caps.labels;
      for (const [key, glossary] of Object.entries(GLOSSARY_FIELDS)) {
        const select = $(FILTER_IDS[key]);
        if (!select) continue;
        fillSelect(select, caps.labels[glossary]);
        if (initial[key]) select.value = initial[key];
      }
    }),   // raw codes are ugly, never wrong
    $(FILTER_IDS.provider)
      ? api(`/logs/suggest${query({ field: 'provider' })}`).then((answer) => {
          const select = $(FILTER_IDS.provider);
          for (const value of answer.items) {
            const option = el('option', value);
            option.value = value;
            select.appendChild(option);
          }
          if (initial.provider) select.value = initial.provider;
        })
      : Promise.resolve(),
  ]).then(() => {
    // Warm the datalists so the boxes offer recent labels on first focus.
    for (const [field, listId] of Object.entries(TEXT_FIELDS)) {
      const id = FILTER_IDS[field];
      if ($(id)) suggest(field, $(id).value.trim(), listId);
    }
    return labels;
  });

  return { values, apiParams: () => toApiParams(values()), ready, initial };
}
