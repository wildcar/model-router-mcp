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
      entry.modality,
      { text: entry.error_message || '—', className: 'trunc', title: entry.error_message || '' },
      el('a', `#${entry.id}`, { href: `/logs?log=${entry.id}` }),
    ]));
  }

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

boot().catch((error) => toast(`Не удалось собрать обзор: ${error.message}`, true));
