// «Расход»: the screen where the console is most tempted to lie.
//
// Every rule here comes from §7. Confirmed rates, unconfirmed rates, notional
// subscription money and requests nobody can price are four different facts, so
// they are four lines rather than one total. When the selection contains
// unpriced or timed-out requests the sum is a floor and says so — "не менее" —
// and each of those groups is a link into the log rows behind it.

import { api, query } from '../core/api.js';
import { el, row, clear, fill, toast } from '../core/dom.js';
import { count as groupDigits, money } from '../core/format.js';
import { readParams, writeParams } from '../core/state.js';

const $ = (id) => document.getElementById(id);
const BUCKET_HEADS = {
  user: 'Метка пользователя',
  model: 'Модель',
  provider: 'Поставщик',
  channel: 'Канал',
  app: 'Приложение',
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
    // The total stays: it is the number the bill correlates with. The four
    // kinds each get their own column — a combined cell hid which kind of
    // token the money went to.
    const count = (v) => ({
      text: groupDigits(v || 0),
      className: v ? 'num' : 'num muted',
    });
    return row([
      { text: item.bucket ?? '—', className: 'mono' },
      { text: item.requests, className: 'num' },
      { text: groupDigits(item.total_tokens), className: 'num' },
      count(item.prompt_tokens),
      count(item.cache_read_tokens),
      count(item.completion_tokens),
      count(item.reasoning_tokens),
      { text: money(item.total_usd), className: 'num' },
      { node: shareCell },
    ]);
  }));
}

async function loadSpend() {
  // The period and the grouping are the screen's state, so they live in the
  // address: a link to «расход по моделям за неделю» must open exactly that.
  // «всё время» is the empty option; spelled `all` in the address, because an
  // empty value is dropped from it and would come back as the default.
  writeParams({ period: $('s-period').value || 'all', group_by: $('s-group').value });
  const answer = await api(`/usage${query(params())}`);
  $('s-bucket-head').textContent = BUCKET_HEADS[answer.facets.group_by] || 'Группа';
  renderTotals(answer.facets.totals);
  renderSparkline(answer.facets.daily);
  renderRows(answer.items);
  $('s-export').href = `/api/v1/usage/export${query(params())}`;
}

function reload() {
  loadSpend().catch((e) => toast(`Ошибка: ${e.message}`, true));
}

const initial = readParams();
if (initial.period !== undefined) {
  $('s-period').value = initial.period === 'all' ? '' : initial.period;
}
if (initial.group_by && BUCKET_HEADS[initial.group_by]) $('s-group').value = initial.group_by;
$('s-period').addEventListener('change', reload);
$('s-group').addEventListener('change', reload);
reload();
