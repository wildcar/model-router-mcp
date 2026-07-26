// «Расход»: the tab where the console is most tempted to lie.
//
// Every rule here comes from §7. Confirmed rates, unconfirmed rates, notional
// subscription money and requests nobody can price are four different facts, so
// they are four lines rather than one total. When the selection contains
// unpriced or timed-out requests the sum is a floor and says so — "не менее" —
// and each of those groups is a link into the log rows behind it.

import { api, query } from '../core/api.js';
import { el, row, clear, fill } from '../core/dom.js';
import { money } from '../core/format.js';

const $ = (id) => document.getElementById(id);
const BUCKET_HEADS = {
  user: 'Метка пользователя',
  model: 'Модель',
  provider: 'Поставщик',
  channel: 'Канал',
  day: 'День',
};

function since() {
  const days = Number($('s-period').value);
  if (!days) return '';
  return new Date(Date.now() - days * 24 * 3600 * 1000).toISOString();
}

function params() {
  return { group_by: $('s-group').value, since: since() };
}

// Every figure is a line of its own, each with what it means and, where the
// number is a count of unpriced requests, a way to go look at them.
function renderTotals(totals) {
  const host = $('s-totals');
  clear(host);

  const headline = el('div', undefined, { className: 'totals-head' });
  headline.appendChild(el('span', totals.is_lower_bound ? 'всего не менее' : 'всего',
    { className: 'muted' }));
  headline.appendChild(el('b', `$${money(totals.total_usd)}`, { className: 'num' }));
  headline.appendChild(el('span',
    `${totals.requests} запросов · ${totals.total_tokens.toLocaleString('ru-RU')} токенов`,
    { className: 'muted' }));
  host.appendChild(headline);

  const lines = [
    ['подтверждённые ставки', `$${money(totals.confirmed_usd)}`, null, null],
    ['неподтверждённые ставки', `$${money(totals.unconfirmed_usd)} ≈`, null,
      totals.unconfirmed_usd ? '/logs?unverified_cost=true' : null],
    ['условная стоимость подписки', `$${money(totals.notional_usd)}`,
      'не списывалась, в итог не входит', null],
    ['без методики цены', `${totals.unpriced_requests} запросов`, null,
      totals.unpriced_requests ? '/logs?unverified_cost=true' : null],
    ['стоимость неизвестна (таймаут)', `${totals.unknown_requests} запросов`, null,
      totals.unknown_requests ? '/logs?unverified_cost=true' : null],
  ];
  const list = el('ul', undefined, { className: 'totals-list' });
  for (const [label, value, note, href] of lines) {
    const item = el('li');
    item.appendChild(el('span', label, { className: 'muted' }));
    item.appendChild(el('span', value, { className: 'num' }));
    if (note) item.appendChild(el('span', note, { className: 'muted' }));
    if (href) item.appendChild(el('a', 'в журнал →', { href }));
    list.appendChild(item);
  }
  host.appendChild(list);

  if (totals.is_lower_bound) {
    host.appendChild(el(
      'p',
      'Итог — нижняя граница: в выборке есть запросы, стоимость которых сервис назвать не может.',
      { className: 'hint warn' },
    ));
  }
}

// A bar per day, sized against the busiest one. Enough to see a spike; the
// axis it would need to be more than that belongs to a charting library.
function renderSparkline(daily) {
  const host = $('s-spark');
  clear(host);
  if (!daily.length) return;
  const peak = Math.max(...daily.map((d) => d.usd), 0.0000001);
  for (const day of daily) {
    const bar = el('span', undefined, {
      className: 'spark-bar',
      title: `${day.day}: $${money(day.usd)} за ${day.requests} запросов`,
    });
    bar.style.height = `${Math.max(2, Math.round((day.usd / peak) * 40))}px`;
    host.appendChild(bar);
  }
}

function renderRows(items) {
  const body = document.querySelector('#spend tbody');
  fill(body, items.map((item) => {
    const share = item.confirmed_share;
    const shareCell = share === null
      ? el('span', '—', { className: 'muted' })
      : el('span', `${Math.round(share * 100)}%`, {
          className: share < 0.5 ? 'num warn' : 'num',
          title: share < 0.5
            ? 'Больше половины суммы посчитано по неподтверждённым ставкам'
            : '',
        });
    return row([
      { text: item.bucket ?? '—', className: 'mono' },
      { text: item.requests, className: 'num' },
      { text: item.total_tokens.toLocaleString('ru-RU'), className: 'num' },
      { text: `${item.prompt_tokens} / ${item.cache_read_tokens} / ${item.reasoning_tokens}`,
        className: 'num' },
      { text: money(item.total_usd), className: 'num' },
      { node: shareCell },
    ]);
  }));
}

export async function loadSpend() {
  const answer = await api(`/usage${query(params())}`);
  $('s-bucket-head').textContent = BUCKET_HEADS[answer.facets.group_by] || 'Группа';
  renderTotals(answer.facets.totals);
  renderSparkline(answer.facets.daily);
  renderRows(answer.items);
  $('s-export').href = `/api/v1/usage/export${query(params())}`;
}

export function initSpend() {
  $('s-period').addEventListener('change', () => loadSpend());
  $('s-group').addEventListener('change', () => loadSpend());
}
