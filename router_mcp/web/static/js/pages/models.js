// «Модели»: the registry, and the one screen where a number gets changed by hand.
//
// Editing a price is the dangerous action here, so the card carries the three
// guards from §11: the unit lives inside the field, the previous value stays
// visible underneath, and an order-of-magnitude change asks before saving. The
// edit itself is a PATCH of the named columns — the whole-row write it replaced
// wiped context length and catalog dates and revived retired models.

import { api, ApiError, query } from '../core/api.js';
import { el, row, clear, fill, toast } from '../core/dom.js';
import { pricePerMillion } from '../core/format.js';
import { readParams, writeParams } from '../core/state.js';

const $ = (id) => document.getElementById(id);
const ORDER_OF_MAGNITUDE = 10;

let caps = null;
let rows = [];
let selected = null;

function filters() {
  return {
    source: $('f-source').value,
    modality: $('f-modality').value,
    search: $('search').value.trim().toLowerCase(),
  };
}

function priceOriginCell(model) {
  const kind = caps.labels.price_source_kinds[model.price_source_kind];
  const cell = el('td', kind?.label || model.price_source_kind, {
    className: model.price_is_verified ? '' : 'muted',
    title: [kind?.help, model.price_source,
            model.price_checked_at && `Проверено: ${model.price_checked_at}`]
      .filter(Boolean).join('\n'),
  });
  return cell;
}

function stateCell(model) {
  if (model.is_active) {
    return el('td', 'активна', {
      title: model.last_seen_at ? `Последняя сверка: ${model.last_seen_at}` : '',
    });
  }
  return el('td', 'снята', {
    className: 'warn',
    title: model.deprecated_at
      ? `Пропала из каталога поставщика ${model.deprecated_at}`
      : 'Отключена',
  });
}

function render() {
  const { search } = filters();
  const visible = rows.filter((m) => !search
    || m.model_id.toLowerCase().includes(search)
    || (m.display_name || '').toLowerCase().includes(search));
  const body = document.querySelector('#models tbody');
  clear(body);
  for (const model of visible.slice(0, 500)) {
    const tr = row([
      model.service_provider,
      { text: model.model_id, className: 'mono' },
      model.modalities.join(','),
      model.tier,
      { text: pricePerMillion(model.prompt_price), className: 'num' },
      { text: pricePerMillion(model.completion_price), className: 'num' },
    ]);
    tr.appendChild(priceOriginCell(model));
    tr.appendChild(stateCell(model));
    tr.addEventListener('click', () => select(model));
    if (selected
        && selected.service_provider === model.service_provider
        && selected.model_id === model.model_id) {
      tr.className = 'selected';
    }
    body.appendChild(tr);
  }
  $('count').textContent = visible.length > 500
    ? `показано 500 из ${visible.length}`
    : `${visible.length} из ${rows.length}`;
}

function priceField(id, label, perMillion) {
  const wrap = el('label', undefined, { className: 'price-field' });
  wrap.appendChild(document.createTextNode(label));
  const group = el('span', undefined, { className: 'input-unit' });
  const input = el('input', undefined, { id });
  input.type = 'number';
  input.step = 'any';
  input.value = pricePerMillion(perMillion);
  group.appendChild(input);
  // The unit sits inside the field: "0.27" with the unit in the column header
  // three rows up is how a price ends up off by a factor of a million.
  group.appendChild(el('span', '$/1M', { className: 'unit' }));
  wrap.appendChild(group);
  wrap.appendChild(el('span', `было ${pricePerMillion(perMillion)}`, { className: 'under' }));
  return wrap;
}

function select(model) {
  selected = model;
  writeParams({ ...readParams(), model: model.model_id, provider: model.service_provider });
  const card = $('card');
  card.hidden = false;
  clear(card);

  card.appendChild(el('h2', `${model.service_provider} · ${model.model_id}`,
    { className: 'mono' }));
  const facts = el('table', undefined, { className: 'kv' });
  const body = el('tbody');
  for (const [key, value] of [
    ['Вендор', model.model_provider],
    ['Модальности', model.modalities.join(', ')],
    ['Контекст', model.context_length || '—'],
    ['Ценовой уровень', model.tier],
    ['Строка', caps.labels.sources[model.source]?.label || model.source],
    ['Ревизия', model.revision],
  ]) {
    const tr = el('tr');
    tr.appendChild(el('th', key));
    tr.appendChild(el('td', value, { className: 'mono' }));
    body.appendChild(tr);
  }
  facts.appendChild(body);
  card.appendChild(facts);

  card.appendChild(el('h3', 'Цены'));
  card.appendChild(priceField('edit-prompt', 'Промпт', model.prompt_price));
  card.appendChild(priceField('edit-completion', 'Ответ', model.completion_price));

  const origin = caps.labels.price_source_kinds[model.price_source_kind];
  card.appendChild(el('p', `Откуда: ${origin?.label || model.price_source_kind}`,
    { className: 'hint', title: origin?.help || '' }));
  card.appendChild(el('p',
    'Введённая вручную цена переживёт синхронизацию: защита завязана на происхождение цены, '
    + 'а строка продолжит получать обновления имени, контекста и модальностей.',
    { className: 'hint' }));

  const actions = el('div', undefined, { className: 'row' });
  actions.appendChild(el('button', 'Сохранить', {
    className: 'primary',
    onClick: () => save(model).catch((e) => toast(e.message, true)),
  }));
  if (model.price_source_kind === 'manual' && model.source === 'catalog') {
    actions.appendChild(el('button', 'Вернуть цену каталога', {
      onClick: () => revert(model).catch((e) => toast(e.message, true)),
    }));
  }
  actions.appendChild(el('a', 'Тест →', {
    href: `/?modality=chat&model_id=${encodeURIComponent(model.model_id)}`,
  }));
  card.appendChild(actions);
  render();
}

// A price off by a factor of ten is invisible in a table and expensive in a
// bill, so the jump is named out loud before it is written.
function confirmJump(name, was, now) {
  if (!was || !now) return true;
  const ratio = now > was ? now / was : was / now;
  if (ratio < ORDER_OF_MAGNITUDE) return true;
  return window.confirm(
    `${name}: ${pricePerMillion(was)} → ${now.toFixed(3)} $/1M, `
    + `отличие в ${ratio.toFixed(0)} раз. Сохранить?`);
}

async function save(model) {
  const prompt = Number($('edit-prompt').value);
  const completion = Number($('edit-completion').value);
  if (!confirmJump('Промпт', model.prompt_price * 1e6, prompt)) return;
  if (!confirmJump('Ответ', model.completion_price * 1e6, completion)) return;
  const updated = await api(
    `/registry/models/${encodeURIComponent(model.service_provider)}/${model.model_id}`,
    { method: 'PATCH', body: {
      prompt_price: prompt, completion_price: completion, revision: model.revision } });
  toast('Цена сохранена');
  replace(updated);
}

async function revert(model) {
  const updated = await api(
    `/registry/models/${encodeURIComponent(model.service_provider)}/${model.model_id}`,
    { method: 'PATCH', body: { revert_price: true, revision: model.revision } });
  toast('Цена вернётся из каталога при ближайшей синхронизации');
  replace(updated);
}

function replace(updated) {
  const index = rows.findIndex(
    (m) => m.service_provider === updated.service_provider && m.model_id === updated.model_id);
  if (index >= 0) rows[index] = updated;
  select(updated);
}

async function load() {
  const { source, modality } = filters();
  const answer = await api(`/registry/models${query({ source, modality })}`);
  rows = answer.items;
  render();
}

function addModelForm() {
  const card = $('card');
  card.hidden = false;
  clear(card);
  card.appendChild(el('h2', 'Добавить модель вручную'));
  card.appendChild(el('p',
    'Ручная строка у поставщика с каталогом будет снята при первой же синхронизации: '
    + 'цикл деприкации не отличает её от исчезнувшей модели.',
    { className: 'hint warn' }));
  const fields = [
    ['add-sp', 'Поставщик', 'openrouter'],
    ['add-id', 'model_id', 'provider/model'],
    ['add-name', 'Название', ''],
    ['add-mods', 'Модальности', 'chat'],
    ['add-prompt', 'Промпт, $/1M', '0'],
    ['add-completion', 'Ответ, $/1M', '0'],
    ['add-source', 'Ссылка на прайс', ''],
  ];
  for (const [id, label, placeholder] of fields) {
    const wrap = el('label', undefined, { className: 'grow' });
    wrap.appendChild(document.createTextNode(label));
    const input = el('input', undefined, { id });
    input.placeholder = placeholder;
    if (id === 'add-mods') input.value = 'chat';
    wrap.appendChild(input);
    card.appendChild(wrap);
  }
  card.appendChild(el('button', 'Добавить', {
    className: 'primary',
    onClick: async () => {
      try {
        await api('/registry/models', { method: 'POST', body: {
          service_provider_id: $('add-sp').value.trim(),
          model_id: $('add-id').value.trim(),
          display_name: $('add-name').value.trim(),
          modalities: $('add-mods').value.split(',').map((s) => s.trim()).filter(Boolean),
          prompt_price: (Number($('add-prompt').value) || 0) / 1e6,
          completion_price: (Number($('add-completion').value) || 0) / 1e6,
          price_source: $('add-source').value.trim() || null,
        } });
        toast('Модель добавлена');
        card.hidden = true;
        load();
      } catch (error) {
        toast(error instanceof ApiError ? error.message : String(error), true);
      }
    },
  }));
}

for (const id of ['f-source', 'f-modality']) {
  $(id).addEventListener('change', () => load().catch((e) => toast(e.message, true)));
}
$('search').addEventListener('input', render);
$('reload').addEventListener('click', () => load().catch((e) => toast(e.message, true)));
$('add-model').addEventListener('click', addModelForm);

(async () => {
  caps = await api('/capabilities');
  const params = readParams();
  if (params.source) $('f-source').value = params.source;
  if (params.modality) $('f-modality').value = params.modality;
  await load();
  if (params.model) {
    const match = rows.find((m) => m.model_id === params.model);
    if (match) select(match);
  }
})().catch((error) => toast(`Не удалось загрузить реестр: ${error.message}`, true));
