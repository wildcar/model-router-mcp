// Formatting rules, in one place so a number looks the same on every screen.
//
// Two rules do the heavy lifting (§7 and §8 of docs/UI_CONCEPT_v2.md):
// no dollar figure appears without saying where it came from, and numbers are
// right-aligned monospace with a fixed number of digits.

import { el } from './dom.js';

// Prices are stored per single token; humans compare them per million.
export function pricePerMillion(perToken) {
  return ((Number(perToken) || 0) * 1e6).toFixed(3);
}

export function money(value) {
  if (value === null || value === undefined) return '—';
  return Number(value).toFixed(6);
}

// The marker each price state wears. The wording comes from the server
// glossary (`labels.cost_sources`) so one concept keeps one name across the
// form, the table, the facet and the report; only the symbol lives here,
// because it is presentation.
const COST_MARKS = {
  computed: '',
  provider: '✓',
  estimated: '≈',
  manual: '✎',
  amortized: '∿',
  provider_notional: '( )',
  unpriced: '—',
  cost_unknown: '?',
};

// A figure with no source is the kind of confidently wrong number the console
// exists to prevent, so the marker and the explanation are not optional.
export function costCell(value, source, labels = null) {
  const spec = labels?.cost_sources?.[source];
  const title = spec ? `${spec.label} — ${spec.help}` : `Источник цены: ${source ?? 'неизвестен'}`;
  const known = source !== 'unpriced' && source !== 'cost_unknown';
  const node = el('span', known ? money(value) : '—', { className: 'num', title });
  const mark = COST_MARKS[source] ?? '?';
  if (mark) node.appendChild(el('span', ` ${mark}`, { className: 'muted' }));
  return node;
}

// A count with digit grouping: 12 345, not 12345. Locale-formatted here and
// nowhere else, so every screen groups the same way.
export function count(value) {
  if (value === null || value === undefined) return '—';
  return Number(value).toLocaleString('ru-RU');
}

// Tokens as a breakdown, not one number: with Anthropic reporting cache reads
// outside input_tokens and DeepSeek billing a cache hit an order of magnitude
// cheaper, a single figure misleads by multiples.
export function tokens(usage) {
  if (!usage) return '—';
  const parts = [];
  if (usage.prompt != null) {
    parts.push(usage.cache_read
      ? `${count(usage.prompt)} (из кэша ${count(usage.cache_read)})` : count(usage.prompt));
  }
  if (usage.completion != null) {
    parts.push(usage.reasoning
      ? `${count(usage.completion)} (рассуждение ${count(usage.reasoning)})` : count(usage.completion));
  }
  if (!parts.length && usage.audio_seconds != null) return `${usage.audio_seconds} с`;
  if (!parts.length && usage.characters != null) return `${count(usage.characters)} симв.`;
  if (!parts.length && usage.images != null) return `${usage.images} изобр.`;
  return parts.join(' / ') || '—';
}

export function duration(ms) {
  if (ms === null || ms === undefined) return '—';
  return ms < 1000 ? `${ms} мс` : `${(ms / 1000).toFixed(2)} с`;
}

// Stored timestamps are UTC; the operator should not subtract hours in their
// head, so the console shows local time and says which zone that is.
export function localTime(iso) {
  if (!iso) return '—';
  const stamp = iso.endsWith('Z') || /[+-]\d\d:\d\d$/.test(iso) ? iso : `${iso}Z`;
  const date = new Date(stamp);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString('ru-RU', { hour12: false });
}

export function timeZoneLabel() {
  const offset = -new Date().getTimezoneOffset() / 60;
  const sign = offset >= 0 ? '+' : '−';
  return `время местное (UTC${sign}${Math.abs(offset)})`;
}
