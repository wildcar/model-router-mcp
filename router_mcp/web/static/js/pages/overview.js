// «Обзор»: state of the service in five seconds, and a first-run checklist.
//
// The health tile, the freshness of every catalog and the work list arrive in
// wave 2 with /api/v1/health/summary. What this screen already answers from
// capabilities and the log: which providers are usable, whether the registry
// has anything in it, and what went wrong recently.
//
// The rule it does follow today (§14): on an empty installation this shows a
// list of steps, not a wall of zeroes. Three empty tables teach an operator
// nothing about what to do next.

import { api, query } from '../core/api.js';
import { el, row, clear, fill, toast } from '../core/dom.js';
import { localTime } from '../core/format.js';

const $ = (id) => document.getElementById(id);

function since24h() {
  return new Date(Date.now() - 24 * 3600 * 1000).toISOString();
}

function firstRunSteps(caps, modelCount) {
  const missing = caps.providers.filter((p) => !p.configured && p.env_var);
  const steps = [];
  if (missing.length === caps.providers.length) {
    const names = missing.map((p) => p.env_var).join(' · ');
    steps.push([
      'Задайте ключ хотя бы одного поставщика в .env и перезапустите сервис',
      `не задано: ${names}`,
      null,
    ]);
  }
  if (!modelCount) {
    steps.push(['Синхронизируйте каталоги', 'модели приедут от поставщиков',
      ['/connections', 'Открыть подключения']]);
  }
  steps.push(['Сделайте первый прогон', 'консоль покажет план, стоимость и шаги',
    ['/', 'Открыть прогон']]);
  return steps;
}

async function boot() {
  const caps = await api('/capabilities');
  const models = await api('/registry/models');
  const logs = await api(`/logs${query({ since: since24h(), limit: 200 })}`);
  const errors = await api(`/logs${query({ status: 'error', limit: 10 })}`);

  const usable = caps.providers.filter((p) => p.enabled && p.configured);
  $('providers-count').textContent = `${usable.length}/${caps.providers.length}`;
  const broken = caps.providers.filter((p) => p.enabled && !p.configured);
  $('providers-detail').textContent = broken.length
    ? `не настроены: ${broken.map((p) => p.id).join(', ')}`
    : 'все включённые поставщики настроены';

  $('models-count').textContent = String(models.total);
  const unpriced = models.items.filter((m) => m.price_is_unknown).length;
  const unverified = models.items.filter(
    (m) => !m.price_is_verified && !m.price_is_unknown).length;
  $('models-detail').textContent =
    `${unpriced} без цены · ${unverified} с неподтверждённой ценой`;

  $('requests-count').textContent = String(logs.total);
  const failed = logs.items.filter((r) => r.status !== 'ok').length;
  $('requests-detail').textContent = failed
    ? `${failed} с ошибкой за сутки`
    : 'ошибок за сутки нет';

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
  if (!models.total || (!usable.length && !logs.total)) {
    $('first-run').hidden = false;
    fill($('steps'), firstRunSteps(caps, models.total).map(([title, detail, link]) => {
      const item = el('li');
      item.appendChild(el('b', title));
      item.appendChild(el('div', detail, { className: 'muted mono' }));
      if (link) item.appendChild(el('a', link[1], { href: link[0] }));
      return item;
    }));
  }
}

boot().catch((error) => toast(`Не удалось собрать обзор: ${error.message}`, true));
