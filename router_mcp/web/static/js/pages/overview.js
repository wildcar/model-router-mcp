// «Обзор»: the state of the service in five seconds, and what to go and fix.
//
// Everything comes from one call to /api/v1/health/summary, so the tiles cannot
// disagree with each other the way four separate requests would. The rule that
// shapes the screen (§14): on an empty installation this shows a list of steps,
// not a wall of zeroes — three empty tables teach an operator nothing.

import { api, query } from '../core/api.js';
import { el, row, clear, fill, toast } from '../core/dom.js';
import { localTime, money } from '../core/format.js';

const $ = (id) => document.getElementById(id);

function megabytes(bytes) {
  return `${Math.round((bytes || 0) / (1024 * 1024))} МБ`;
}

function duration(seconds) {
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  return days ? `${days} д ${hours} ч` : `${hours} ч ${minutes} мин`;
}

function firstRunSteps(summary) {
  const missing = summary.providers.filter((p) => !p.configured && p.env_var);
  const steps = [];
  if (missing.length === summary.providers.length) {
    steps.push([
      'Задайте ключ хотя бы одного поставщика в .env и перезапустите сервис',
      `не задано: ${missing.map((p) => p.env_var).join(' · ')}`,
      null,
    ]);
  }
  if (!summary.models_active) {
    steps.push(['Синхронизируйте каталоги', 'модели приедут от поставщиков',
      ['/connections', 'Открыть подключения']]);
  }
  steps.push(['Сделайте первый прогон', 'консоль покажет план, стоимость и шаги',
    ['/', 'Открыть прогон']]);
  return steps;
}

function days(value) {
  return value > 0 ? `${value} дн.` : 'бессрочно';
}

async function renderRetention() {
  const state = await api('/retention');
  $('retention-policy').textContent =
    `Журнал: ${days(state.log_retention_days)} · файлы ответов: `
    + `${days(state.blob_retention_days)}. Меняется в .env `
    + '(LOG_RETENTION_DAYS, BLOB_RETENTION_DAYS).';
  fill($('retention'), [
    ['Записей в журнале', String(state.total_logs)],
    ['Будет удалено записей', String(state.logs_to_delete)],
    ['Будет удалено файлов', String(state.blobs_to_delete)],
    // Files nothing points at: a run that stored a file and then failed leaves
    // one, and until now nothing ever collected them.
    ['Файлов без записи', String(state.orphan_blobs)],
  ].map(([key, value]) => {
    const tr = el('tr');
    tr.appendChild(el('th', key));
    tr.appendChild(el('td', value, { className: 'num' }));
    return tr;
  }));
  $('purge').disabled =
    !(state.logs_to_delete || state.blobs_to_delete || state.orphan_blobs);
}

// Why a failure sent nothing, in the operator's words. The keys are what
// Notifier._note writes into `recent`.
const OUTCOMES = {
  sent: 'отправлено',
  muted_code: 'код в тишине',
  muted_app: 'приложение в тишине',
  muted_user: 'метка в тишине',
  no_channels: 'каналы не настроены',
  throttled: 'повтор, в окне тишины',
  failed: 'канал не ответил',
};

function kv(pairs) {
  return pairs.map(([key, value]) => {
    const tr = el('tr');
    tr.appendChild(el('th', key));
    tr.appendChild(el('td', value));
    return tr;
  });
}

async function renderNotifications() {
  const state = await api('/notifications');
  const channels = state.channels.map((c) => `${c.name} (${c.detail})`).join(', ');
  $('notify-state').textContent = state.enabled
    ? `Включены: ${channels}. Одна и та же ошибка — не чаще раза в `
      + `${Math.round(state.cooldown_seconds / 60)} мин.`
    : 'Выключены: ни один канал не настроен. Ошибки остаются только в журнале.';
  const recent = state.recent.slice(0, 5).map((r) =>
    `#${r.log_id} ${r.code} — ${OUTCOMES[r.outcome] || r.outcome}`
    + (r.detail ? ` (${r.detail})` : ''));
  fill($('notify'), kv([
    ['Не шлются коды', state.muted_codes.join(', ') || 'все коды шлются'],
    ['Не шлются приложения', state.muted_apps.join(', ') || '—'],
    ['Не шлются метки', state.muted_users.join(', ') || '—'],
    ['С запуска', `отправлено ${state.counts.sent} · в тишине ${state.counts.muted}`
      + ` · повторов ${state.counts.throttled} · не доставлено ${state.counts.failed}`],
    ['Последние', recent.join('\n') || 'ошибок с запуска не было'],
  ]));
  $('notify-test').disabled = !state.enabled;
}

async function sendTestNotification() {
  const result = await api('/notifications/test', { method: 'POST' });
  const detail = result.results
    .map((r) => `${r.channel}: ${r.status === 'ok' ? 'доставлено' : r.detail}`).join('; ');
  toast(result.ok ? `Пробное ушло — ${detail}` : `Не везде дошло — ${detail}`, !result.ok);
  await renderNotifications();
}

async function purge() {
  const state = await api('/retention');
  const total = state.logs_to_delete + state.blobs_to_delete + state.orphan_blobs;
  if (!confirm(
    `Удалить ${state.logs_to_delete} записей журнала и `
    + `${state.blobs_to_delete + state.orphan_blobs} файлов? Отменить нельзя.`)) return;
  const result = await api('/retention/purge', { method: 'POST' });
  toast(`Удалено: ${result.deleted_logs} записей, ${result.deleted_blobs} файлов`);
  if (!total) return;
  await renderRetention();
}

async function boot() {
  const summary = await api('/health/summary');
  const caps = await api('/capabilities');
  const errors = await api(`/logs${query({ status: 'error', limit: 10 })}`);

  $('service-state').textContent = 'работает';
  $('service-detail').textContent =
    `${summary.version} · ${duration(summary.uptime_seconds)} · база ${megabytes(summary.db_bytes)}`
    + ` · файлы ${megabytes(summary.blob_bytes)}`;

  $('providers-count').textContent =
    `${summary.providers_usable}/${summary.providers_total}`;
  const broken = summary.providers.filter((p) => p.enabled && !p.configured);
  $('providers-detail').textContent = broken.length
    ? `не настроены: ${broken.map((p) => p.id).join(', ')}`
    : 'все включённые поставщики настроены';

  $('models-count').textContent = String(summary.models_active);
  $('models-detail').textContent =
    `${summary.models_unpriced} без цены · ${summary.models_unverified} с неподтверждённой`
    + (summary.catalog_last_seen_at
      ? ` · сверка ${localTime(summary.catalog_last_seen_at)}`
      : '');

  const day = summary.last_24h;
  $('requests-count').textContent = String(day.requests);
  // The headline figure says "не менее" whenever the selection holds requests
  // nobody can price — the same rule as the spend tab.
  $('requests-detail').textContent =
    `${day.is_lower_bound ? 'не менее ' : ''}$${money(day.total_usd)}`
    + (day.errors ? ` · ${day.errors} с ошибкой` : '')
    + (day.unpriced_requests ? ` · ${day.unpriced_requests} без методики цены` : '');

  fill($('work'), summary.work.map((item) => {
    const li = el('li');
    li.appendChild(el('a', item.text, { href: item.href }));
    return li;
  }));
  $('work-empty').hidden = summary.work.length > 0;

  const body = document.querySelector('#errors tbody');
  clear(body);
  $('no-errors').hidden = errors.items.length > 0;
  for (const entry of errors.items) {
    body.appendChild(row([
      localTime(entry.created_at),
      entry.external_user_id,
      caps.labels.channels[entry.channel]?.label || entry.channel,
      entry.task || entry.modality,
      { text: entry.error_message || '—', className: 'trunc', title: entry.error_message || '' },
      el('a', `#${entry.id}`, { href: `/logs?log=${entry.id}` }),
    ]));
  }

  await renderRetention();
  await renderNotifications();

  // Nothing configured and nothing logged means this is a fresh install.
  if (!summary.models_active || (!summary.providers_usable && !day.requests)) {
    $('first-run').hidden = false;
    fill($('steps'), firstRunSteps(summary).map(([title, detail, link]) => {
      const item = el('li');
      item.appendChild(el('b', title));
      item.appendChild(el('div', detail, { className: 'muted mono' }));
      if (link) item.appendChild(el('a', link[1], { href: link[0] }));
      return item;
    }));
  }
}

$('purge').addEventListener('click',
  () => purge().catch((error) => toast(error.message, true)));
$('notify-test').addEventListener('click',
  () => sendTestNotification().catch((error) => toast(error.message, true)));

boot().catch((error) => toast(`Не удалось собрать обзор: ${error.message}`, true));
