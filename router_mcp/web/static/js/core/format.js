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

// The six states a cost can be in, as a short marker plus a spelled-out title.
// A bare number would let an unconfirmed rate pass for a confirmed one.
const COST_SOURCES = {
  computed: { mark: '', title: 'Ставка подтверждена каталогом' },
  provider: { mark: '✓', title: 'Сумму назвал провайдер' },
  estimated: { mark: '≈', title: 'Ставка не подтверждена' },
  manual: { mark: '✎', title: 'Цену ввёл администратор' },
  amortized: { mark: '∿', title: 'Размазано по подписке' },
  provider_notional: { mark: '( )', title: 'Условная стоимость: работа по подписке, деньги не списывались' },
  unpriced: { mark: '—', title: 'Методики цены нет: потребление есть, ставка нулевая' },
  cost_unknown: { mark: '?', title: 'Ответ не дошёл: запрос, скорее всего, отработан и оплачен' },
};

export function costCell(value, source) {
  const spec = COST_SOURCES[source] || { mark: '?', title: `Источник цены: ${source ?? 'неизвестен'}` };
  const known = source !== 'unpriced' && source !== 'cost_unknown';
  const node = el('span', known ? money(value) : '—', { className: 'num', title: spec.title });
  if (spec.mark) node.appendChild(el('span', ` ${spec.mark}`, { className: 'muted' }));
  return node;
}

// Tokens as a breakdown, not one number: with Anthropic reporting cache reads
// outside input_tokens and DeepSeek billing a cache hit an order of magnitude
// cheaper, a single figure misleads by multiples.
export function tokens(usage) {
  if (!usage) return '—';
  const parts = [];
  if (usage.prompt != null) {
    parts.push(usage.cache_read ? `${usage.prompt} (из кэша ${usage.cache_read})` : String(usage.prompt));
  }
  if (usage.completion != null) {
    parts.push(usage.reasoning ? `${usage.completion} (рассуждение ${usage.reasoning})` : String(usage.completion));
  }
  if (!parts.length && usage.audio_seconds != null) return `${usage.audio_seconds} с`;
  if (!parts.length && usage.characters != null) return `${usage.characters} симв.`;
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
