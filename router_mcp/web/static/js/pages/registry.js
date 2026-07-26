// "Реестр" screen: providers, the catalog table, manual model entry.
//
// Moved out of the template unchanged in behaviour; the three-panel screen with
// facets and in-place price editing (§11) is wave 2. What did change: lists
// arrive in the `{items, ...}` envelope and the provider row shows the name of
// the environment variable that configures it — never its value.

import { api, query } from '../core/api.js';
import { el, row, clear, toast } from '../core/dom.js';
import { pricePerMillion } from '../core/format.js';

const $ = (id) => document.getElementById(id);

async function loadProviders() {
  const answer = await api('/registry/providers');
  const body = document.querySelector('#providers tbody');
  clear(body);
  for (const p of answer.items) {
    const tr = row([p.id, p.display_name, p.adapter]);

    const toggleCell = el('td');
    const toggle = el('input');
    toggle.type = 'checkbox';
    toggle.checked = p.enabled;
    toggle.addEventListener('change', () =>
      api(`/registry/providers/${p.id}`, { method: 'PATCH', body: { enabled: toggle.checked } })
        .then(() => toast('Сохранено'))
        .catch((e) => toast(e.message, true)));
    toggleCell.appendChild(toggle);
    tr.appendChild(toggleCell);

    tr.appendChild(el('td', p.configured ? '✓' : '✗', {
      className: p.configured ? 'ok' : 'err',
      title: p.configured
        ? 'Адаптер настроен, запросы пройдут'
        : `Нет ключа или бинарника: диспетчер откажет, даже если поставщик включён.${
            p.env_var ? ` Переменная ${p.env_var} не задана.` : ''}`,
    }));

    const priceCell = el('td');
    if (p.pricing_url) {
      const link = el('a', 'прайс', { href: p.pricing_url });
      link.target = '_blank';
      link.rel = 'noreferrer';
      priceCell.appendChild(link);
    } else {
      priceCell.textContent = '—';
    }
    tr.appendChild(priceCell);
    body.appendChild(tr);
  }
}

async function syncCatalogs() {
  const res = await api('/registry/refresh', { method: 'POST', body: {} });
  const done = res.providers.filter((r) => !r.skipped);
  toast(done.length
    ? done.map((r) => `${r.provider_id}: +${r.added} ~${r.updated} -${r.deprecated}`).join('; ')
    : 'Ни один поставщик не отдал каталог');
  loadModels();
}

const PRICE_KIND_LABEL = {
  api: 'из каталога',
  page: 'с прайс-страницы',
  manual: 'введена вручную',
  estimate: 'прикидка',
  unknown: 'цены нет',
};

function priceOriginCell(m) {
  const label = PRICE_KIND_LABEL[m.price_source_kind] || m.price_source_kind;
  const cell = el('td');
  if (m.price_source && /^https?:/.test(m.price_source)) {
    const link = el('a', label, { href: m.price_source });
    link.target = '_blank';
    link.rel = 'noreferrer';
    cell.appendChild(link);
  } else {
    cell.textContent = label;
    if (m.price_source) cell.title = m.price_source;
  }
  const notes = [];
  if (!m.price_is_verified) {
    cell.className = 'muted';
    notes.push('Цена не подтверждена: стоимость в журнале помечается как estimated.');
  }
  if (m.price_source && !/^https?:/.test(m.price_source)) notes.push(m.price_source);
  if (m.price_checked_at) notes.push(`Проверено: ${m.price_checked_at}`);
  if (notes.length) cell.title = notes.join('\n');
  return cell;
}

function statusCell(m) {
  if (m.is_active) {
    return el('td', 'активна', {
      title: m.last_seen_at ? `Последняя сверка с каталогом: ${m.last_seen_at}` : '',
    });
  }
  return el('td', 'снята', {
    className: 'warn',
    title: m.deprecated_at
      ? `Пропала из каталога поставщика ${m.deprecated_at}`
      : 'Отключена',
  });
}

async function loadModels() {
  const answer = await api(`/registry/models${query({ source: $('f-source').value })}`);
  const body = document.querySelector('#models tbody');
  clear(body);
  for (const m of answer.items.slice(0, 1000)) {
    const tr = row([
      m.service_provider,
      { text: m.model_id, className: 'mono' },
      m.modalities.join(','),
      m.tier,
      { text: pricePerMillion(m.prompt_price), className: 'num' },
      { text: pricePerMillion(m.completion_price), className: 'num' },
      m.source,
    ]);
    tr.appendChild(priceOriginCell(m));
    tr.appendChild(statusCell(m));

    const actions = el('td');
    if (m.source === 'manual') {
      actions.appendChild(el('button', '✕', {
        title: 'Удалить строку, добавленную вручную',
        onClick: async () => {
          await api(`/registry/models${query({
            service_provider_id: m.service_provider, model_id: m.model_id })}`,
            { method: 'DELETE' });
          loadModels();
        },
      }));
    }
    tr.appendChild(actions);
    body.appendChild(tr);
  }
  toast(`Моделей: ${answer.total}`);
}

async function addModel() {
  const body = {
    service_provider_id: $('m-sp').value.trim(),
    model_id: $('m-id').value.trim(),
    model_provider: $('m-mp').value.trim() || null,
    display_name: $('m-name').value.trim(),
    modalities: $('m-mods').value.split(',').map((s) => s.trim()).filter(Boolean),
    // The form takes $/1M tokens; the registry stores USD per single token.
    prompt_price: (parseFloat($('m-pp').value) || 0) / 1e6,
    completion_price: (parseFloat($('m-cp').value) || 0) / 1e6,
    image_price: parseFloat($('m-ip').value) || 0,
    audio_price_per_second: parseFloat($('m-ap').value) || 0,
    pricing_mode: $('m-mode').value,
    price_source: $('m-psrc').value.trim() || null,
  };
  if (!body.service_provider_id || !body.model_id) {
    toast('Укажите поставщика и model_id', true);
    return;
  }
  await api('/registry/models', { method: 'POST', body });
  toast('Модель добавлена');
  loadModels();
}

$('reload').addEventListener('click', loadModels);
$('sync').addEventListener('click', syncCatalogs);
$('m-add').addEventListener('click', () => addModel().catch((e) => toast(e.message, true)));
loadProviders();
loadModels();
