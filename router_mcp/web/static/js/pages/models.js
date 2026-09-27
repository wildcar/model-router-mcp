// «Модели»: the registry, and the one screen where a number gets changed by hand.
//
// Editing a price is the dangerous action here, so the card carries the three
// guards from §11: the unit lives inside the field, the previous value stays
// visible underneath, and an order-of-magnitude change asks before saving. The
// edit itself is a PATCH of the named columns — the whole-row write it replaced
// wiped context length and catalog dates and revived retired models.

import { api, ApiError, query } from '../core/api.js';
import { el, clear, fill, toast } from '../core/dom.js';
import { pricePerMillion } from '../core/format.js';
import { readParams, writeParams } from '../core/state.js';

const $ = (id) => document.getElementById(id);
const ORDER_OF_MAGNITUDE = 10;

let caps = null;
let rows = [];
let selected = null;

// Filters live in the address, so a filtered registry is a link.
const FACET_FIELDS = ['task', 'modality', 'provider', 'source', 'price_source_kind', 'tier', 'state'];
const FACET_TITLES = {
  task: 'Задача',
  modality: 'Группа тарификации',
  provider: 'Поставщик',
  source: 'Строка',
  price_source_kind: 'Цена откуда',
  tier: 'Ценовой уровень',
  state: 'Состояние',
};
const STATE_WORDS = { active: 'активные', retired: 'снятые' };

let facets = {};
const selection = new Map();   // "provider\u0000model_id" -> {service_provider_id, model_id}
let lastBulk = null;           // what an undo would put back

function filters() {
  const params = readParams();
  const out = { search: $('search').value.trim() };
  for (const field of FACET_FIELDS) {
    if (params[field]) out[field] = params[field];
  }
  return out;
}

function facetLabel(field, value) {
  if (field === 'state') return STATE_WORDS[value] || value;
  if (field === 'price_source_kind') {
    return caps.labels.price_source_kinds[value]?.label || value;
  }
  if (field === 'source') return caps.labels.sources[value]?.label || value;
  return value;
}

// Counts arrive with the rows, so the sidebar and the table always agree.
function renderFacets() {
  const active = filters();
  fill($('facet-groups'), FACET_FIELDS.filter((f) => facets[f]).map((field) => {
    const group = el('section', undefined, { className: 'facet-group' });
    group.appendChild(el('h4', FACET_TITLES[field]));
    for (const [value, count] of Object.entries(facets[field])) {
      const line = el('label', undefined, { className: 'facet' });
      const box = el('input');
      box.type = 'checkbox';
      box.checked = active[field] === value;
      box.addEventListener('change', () => {
        writeParams({ ...readParams(), [field]: box.checked ? value : undefined });
        load().catch((e) => toast(e.message, true));
      });
      line.appendChild(box);
      line.appendChild(el('span', facetLabel(field, value)));
      line.appendChild(el('span', count, { className: 'num muted' }));
      group.appendChild(line);
    }
    return group;
  }));
}

// A row whose rates depend on the hour shows its base rate in the table, and the
// mark is the only warning that the other rate exists.
function peakMark(model) {
  return model.price_schedule ? ` ×${model.price_schedule.multiplier}` : '';
}

function peakTitle(model) {
  if (!model.price_schedule) return '';
  const { windows, multiplier, label } = model.price_schedule;
  return `Базовая ставка вне пика. ${label} ×${multiplier}: ${windows.join(', ')} UTC.`
    + (model.price_factor_now !== 1 ? '\nСейчас действует пиковая ставка.' : '');
}

// Where the price came from, with everything known about it in the tooltip and
// the cell dimmed when nobody has confirmed the number.
function priceOriginCell(model) {
  const kind = caps.labels.price_source_kinds[model.price_source_kind];
  return el('td', kind?.label || model.price_source_kind, {
    className: model.price_is_verified ? '' : 'muted',
    title: [kind?.help, model.price_source,
            model.price_checked_at && `Проверено: ${model.price_checked_at}`]
      .filter(Boolean).join('\n'),
  });
}

function stateCell(model) {
  if (model.is_active) {
    return el('td', 'активна', {
      title: model.last_seen_at
        ? `Последняя сверка: ${model.last_seen_at}` : '',
    });
  }
  return el('td', 'снята', {
    className: 'warn',
    title: model.deprecated_at
      ? `Пропала из каталога поставщика ${model.deprecated_at}`
      : 'Отключена',
  });
}

function key(model) {
  return `${model.service_provider}\u0000${model.model_id}`;
}

function renderBulkBar() {
  $('bulk-bar').hidden = selection.size === 0;
  $('bulk-count').textContent = `выделено ${selection.size}`;
}

function render() {
  const body = document.querySelector('#models tbody');
  clear(body);
  for (const model of rows.slice(0, 500)) {
    const tr = el('tr');

    const pick = el('td');
    const box = el('input');
    box.type = 'checkbox';
    box.checked = selection.has(key(model));
    box.addEventListener('click', (event) => event.stopPropagation());
    box.addEventListener('change', () => {
      if (box.checked) {
        selection.set(key(model), {
          service_provider_id: model.service_provider, model_id: model.model_id,
        });
      } else {
        selection.delete(key(model));
      }
      renderBulkBar();
    });
    pick.appendChild(box);
    tr.appendChild(pick);

    for (const cell of [
      model.service_provider,
      { text: model.model_id, className: 'mono' },
      (model.tasks || model.modalities).join(','),
      model.tier,
      // Marked where the number in the column is only half the story: a row with
      // peak hours shows its base rate, and an unmarked table would read as the
      // whole price list.
      { text: pricePerMillion(model.prompt_price) + peakMark(model), className: 'num',
        title: peakTitle(model) },
      { text: pricePerMillion(model.completion_price) + peakMark(model), className: 'num',
        title: peakTitle(model) },
    ]) {
      const spec = (cell === null || typeof cell !== 'object') ? { text: cell } : cell;
      tr.appendChild(el('td', spec.text, spec));
    }
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
  $('count').textContent = rows.length > 500
    ? `показано 500 из ${rows.length}`
    : `${rows.length} моделей`;
  renderBulkBar();
}

async function load() {
  const answer = await api(`/registry/models${query(filters())}`);
  rows = answer.items;
  facets = answer.facets || {};
  renderFacets();
  render();
}

// The confirmation lists what it is about to touch, and the action stays
// reversible for a few seconds afterwards.
async function bulkDeprecate() {
  const models = [...selection.values()];
  const names = models.slice(0, 10).map((m) => `${m.service_provider_id} · ${m.model_id}`);
  const more = models.length > names.length ? `\n…и ещё ${models.length - names.length}` : '';
  if (!window.confirm(
    `Снять с учёта ${models.length} моделей?\n\n${names.join('\n')}${more}\n\n`
    + 'Строки не удаляются: журнал и история стоимости на них ссылаются.')) {
    return;
  }
  await api('/registry/models/bulk', {
    method: 'POST', body: { action: 'deprecate', models },
  });
  lastBulk = models;
  selection.clear();
  await load();
  showUndo(`Снято с учёта: ${models.length}`);
}

function showUndo(text) {
  $('undo-text').textContent = text;
  $('undo-bar').hidden = false;
  clearTimeout(showUndo._t);
  showUndo._t = setTimeout(() => { $('undo-bar').hidden = true; }, 8000);
}

// A rate that is already per unit, not per million: $/second of compute or of
// audio. Sharing `priceField` would divide it by a million on the way in.
function plainRateField(id, label, unit, value) {
  const wrap = el('label', undefined, { className: 'price-field' });
  wrap.appendChild(document.createTextNode(label));
  const group = el('span', undefined, { className: 'input-unit' });
  const input = el('input', undefined, { id });
  input.type = 'number';
  input.step = 'any';
  input.value = value ? String(value) : '';
  group.appendChild(input);
  group.appendChild(el('span', unit, { className: 'unit' }));
  wrap.appendChild(group);
  wrap.appendChild(el('span', `было ${value || 0}`, { className: 'under' }));
  return wrap;
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

// Attachment roles, from the glossary: one word per concept, in one file. A
// copy here would drift from «Прогон», which offers the same six.
const roles = () => caps.labels.attachment_roles || [];

// Which of this model's own inputs each role lands on. Only shown where a second
// image is possible: on a chat row it would be six empty fields teaching the
// operator to scroll past the card.
function inputMapFields(model) {
  const box = el('div', undefined, { className: 'input-map' });
  box.appendChild(el('h3', 'Роли вложений'));
  box.appendChild(el('p',
    'Схема модели публикует имена вроде human_img и garm_img и не говорит, где что. '
    + 'Пустое поле — вложение подставляется по порядку, как раньше.',
    { className: 'hint' }));
  for (const { value: role, label, help } of roles()) {
    const wrap = el('label', undefined, { className: 'price-field', title: help });
    wrap.appendChild(document.createTextNode(label));
    const input = el('input', undefined, { id: `edit-role-${role}` });
    input.value = (model.input_map || {})[role] || '';
    input.placeholder = 'имя входа модели';
    wrap.appendChild(input);
    box.appendChild(wrap);
  }
  return box;
}

// This model's own knobs. One text field rather than six: the names come from
// the model's page at the provider and there is no list to offer.
function extraParamsField(model) {
  const box = el('div', undefined, { className: 'input-map' });
  box.appendChild(el('h3', 'Свои параметры модели'));
  box.appendChild(el('p',
    'Имена входов модели через запятую, например scale, category. Разрешает то, '
    + 'что адаптер не объявил: апскейлер добавляется без правки кода. Имя, '
    + 'которого нет в схеме модели, всё равно не уйдёт и попадёт в '
    + '«проигнорировано».',
    { className: 'hint' }));
  const wrap = el('label', undefined, { className: 'price-field' });
  const input = el('input', undefined, { id: 'edit-extra-params' });
  input.value = (model.extra_params || []).join(', ');
  input.placeholder = 'scale, category';
  wrap.appendChild(input);
  box.appendChild(wrap);
  return box;
}

// Which OpenRouter hosts may serve the model. OpenRouter spreads a model across
// every host that runs it, at prices several times apart, while its catalog
// shows only the cheapest; pinning the hosts is what makes the row's price the
// one actually charged. Sent to OpenRouter as its `provider` object, as typed.
function providerRoutingField(model) {
  const box = el('div', undefined, { className: 'input-map' });
  box.appendChild(el('h3', 'Хосты OpenRouter'));
  box.appendChild(el('p',
    'Объект provider из API OpenRouter в JSON, например '
    + '{"only": ["novita/fp8", "phala/fp8"]}. Пустое поле: OpenRouter сам '
    + 'выбирает из всех хостов, по их ценам. Цену строки при этом ставьте '
    + 'вручную по самому дорогому из закреплённых.',
    { className: 'hint' }));
  const wrap = el('label', undefined, { className: 'price-field' });
  const input = el('textarea', undefined, { id: 'edit-provider-routing' });
  const routing = model.provider_routing || {};
  input.value = Object.keys(routing).length ? JSON.stringify(routing, null, 2) : '';
  input.placeholder = '{"only": ["novita/fp8"]}';
  input.rows = 4;
  wrap.appendChild(input);
  box.appendChild(wrap);
  return box;
}

function readProviderRouting() {
  const field = $('edit-provider-routing');
  if (!field) return null;
  const text = field.value.trim();
  if (!text) return {};
  let value;
  try {
    value = JSON.parse(text);
  } catch {
    throw new Error('Хосты OpenRouter: это не JSON');
  }
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error('Хосты OpenRouter: нужен объект {...}');
  }
  return value;
}

// What the model cannot do, in words. A catalog never says «one sheet in frame,
// a two-page spread is unpredictable»; the operator who found out does, here,
// and the applications where a user picks a model show it next to the name.
function descriptionField(model) {
  const box = el('div', undefined, { className: 'input-map' });
  box.appendChild(el('h3', 'Описание'));
  box.appendChild(el('p',
    'Что модель умеет и чего не умеет — словами. Читают операторы здесь и '
    + 'приложения, где пользователь выбирает модель. Пустое поле — без описания.',
    { className: 'hint' }));
  const wrap = el('label', undefined, { className: 'price-field' });
  const input = el('textarea', undefined, { id: 'edit-description' });
  input.value = model.description || '';
  input.rows = 4;
  wrap.appendChild(input);
  box.appendChild(wrap);
  return box;
}

// Peak hours. DeepSeek charges twice its base rate between 01:00 and 04:00 UTC
// and again between 06:00 and 10:00, and the prices above are then the off-peak
// figure — which is not what the row costs right now, and the card has to say so
// rather than let the operator read the cheaper half of a two-rate price list.
function priceScheduleFields(model) {
  const box = el('div', undefined, { className: 'input-map' });
  box.appendChild(el('h3', 'Пиковые часы'));
  box.appendChild(el('p',
    'Окна времени по UTC через запятую и множитель. Цены выше — базовая ставка '
    + 'вне окон; внутри они умножаются. Пустое поле — цена одна круглые сутки.',
    { className: 'hint' }));
  const windows = el('label', undefined, { className: 'price-field' });
  windows.appendChild(document.createTextNode('Окна, UTC'));
  const wInput = el('input', undefined, { id: 'edit-peak-windows' });
  wInput.value = (model.price_schedule?.windows || []).join(', ');
  wInput.placeholder = '01:00-04:00, 06:00-10:00';
  windows.appendChild(wInput);
  box.appendChild(windows);

  const mult = el('label', undefined, { className: 'price-field' });
  mult.appendChild(document.createTextNode('Множитель'));
  const mInput = el('input', undefined, { id: 'edit-peak-multiplier' });
  mInput.type = 'number';
  mInput.step = 'any';
  mInput.value = model.price_schedule?.multiplier || '';
  mInput.placeholder = '2';
  mult.appendChild(mInput);
  box.appendChild(mult);

  if (model.price_factor_now && model.price_factor_now !== 1) {
    box.appendChild(el('p',
      `Сейчас действует ${model.price_schedule.label} ×${model.price_factor_now}: `
      + 'прогон в эту минуту спишется по умноженной ставке.',
      { className: 'hint warn' }));
  }
  return box;
}

function readPriceSchedule(model) {
  const wField = $('edit-peak-windows');
  const mField = $('edit-peak-multiplier');
  if (!wField || !mField) return null;
  const windows = wField.value.split(',').map((s) => s.trim()).filter(Boolean);
  const multiplier = Number(mField.value) || 1;
  const had = Boolean(model.price_schedule);
  // Nothing typed and nothing there before: sending `{}` would be a clearing
  // edit on every save of every flat-rate row.
  if (!windows.length && !had) return null;
  return { windows, multiplier, label: model.price_schedule?.label || 'пиковый тариф' };
}

function readExtraParams() {
  const field = $('edit-extra-params');
  if (!field) return null;
  return field.value.split(',').map((s) => s.trim()).filter(Boolean);
}

function readInputMap() {
  const map = {};
  for (const { value: role } of roles()) {
    const field = $(`edit-role-${role}`);
    const value = field ? field.value.trim() : '';
    if (value) map[role] = value;
  }
  return map;
}

function takesImages(model) {
  return (model.tasks || []).some((t) => t.startsWith('image-to-'));
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
    ['Задачи', (model.tasks || []).join(', ')],
    ['Группы тарификации', model.modalities.join(', ')],
    ['Контекст', model.context_length || '—'],
    ['Ценовой уровень', model.tier],
    ['Строка', caps.labels.sources[model.source]?.label || model.source],
    // Whether anything ever answered on this id. A catalog entry and a seed both
    // look identical here until one of them has served a request.
    ['Подтверждена ответом', model.confirmed_at || 'ответа не было'],
    ['Ревизия', model.revision],
  ]) {
    const tr = el('tr');
    tr.appendChild(el('th', key));
    tr.appendChild(el('td', value, { className: 'mono' }));
    body.appendChild(tr);
  }
  facts.appendChild(body);
  card.appendChild(facts);
  if (model.description) {
    card.appendChild(el('p', model.description, { className: 'hint description' }));
  }

  card.appendChild(el('h3', 'Цены'));
  card.appendChild(priceField('edit-prompt', 'Промпт', model.prompt_price));
  card.appendChild(priceField('edit-completion', 'Ответ', model.completion_price));
  // A cached prompt token costs roughly a tenth of a fresh one. Leaving this at
  // zero means accounting charges cache reads at the prompt rate and marks the
  // whole figure estimated, so the field has to be reachable.
  card.appendChild(priceField('edit-cache-read', 'Чтение кэша', model.cache_read_price));
  if (!model.cache_read_price) {
    card.appendChild(el('p',
      'Ставка чтения кэша не задана: кэшированные токены считаются по цене промпта, '
      + 'и вся сумма помечается прикидкой.',
      { className: 'hint warn' }));
  }

  // Only where it means something: a model billed per token has no compute rate
  // and a field of zeroes on every row teaches the operator to ignore the card.
  const billedByCompute = (model.modalities || []).some((m) => m === 'audio' || m === 'video')
    || model.service_provider === 'replicate'
    || model.compute_price_per_second > 0;
  if (billedByCompute) {
    card.appendChild(plainRateField(
      'edit-compute', 'Секунда расчёта', '$/с', model.compute_price_per_second));
    if (!model.compute_price_per_second) {
      card.appendChild(el('p',
        'Ставка за секунду расчёта не задана: прогон запишется как «нет методики», '
        + 'а не как бесплатный. Цену смотрите на странице модели у поставщика.',
        { className: 'hint warn' }));
    }
  }

  card.appendChild(priceScheduleFields(model));

  if (takesImages(model)) card.appendChild(inputMapFields(model));
  card.appendChild(extraParamsField(model));
  if (model.service_provider === 'openrouter') card.appendChild(providerRoutingField(model));
  card.appendChild(descriptionField(model));

  const origin = caps.labels.price_source_kinds[model.price_source_kind];
  card.appendChild(el('p', `Откуда: ${origin?.label || model.price_source_kind}`,
    { className: 'hint', title: origin?.help || '' }));
  card.appendChild(el('p',
    'Введённая вручную цена переживёт синхронизацию: защита завязана на происхождение цены, '
    + 'а строка продолжит получать обновления имени, контекста и задач.',
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
    href: `/?task=${(model.tasks || [])[0] || 'text-to-text'}`
      + `&model_id=${encodeURIComponent(model.model_id)}`,
  }));
  card.appendChild(actions);
  render();
}

// --- what the provider offers ------------------------------------------
// The registry keeps the models that were used and answered; the full list is a
// question asked on demand. Adopting a row makes it addressable; it becomes
// permanent when a request to it comes back (ADR-0008).
const offered = new Set();     // model_ids ticked in the offer table

function renderOffer(answer) {
  const host = $('offer');
  clear(host);
  offered.clear();
  if (!answer.has_catalog) {
    host.appendChild(el('p',
      `У ${answer.provider_id} нет каталога: спросить список не у кого, `
      + 'модели такого поставщика задаются вручную.',
      { className: 'hint warn' }));
    return;
  }
  host.appendChild(el('p',
    `${answer.provider_id}: предлагает ${answer.total}, в реестре ${answer.known}.`
    + (answer.scope === 'browse'
      ? ' Каталог этого поставщика не хранится целиком — отметьте нужные.'
      : ' Синхронизация хранит этот каталог полностью.'),
    { className: 'hint' }));

  const table = el('table');
  const head = el('thead');
  const hr = el('tr');
  for (const title of ['', 'model_id', 'Название', 'Задачи', 'Контекст', 'Состояние']) {
    hr.appendChild(el('th', title));
  }
  head.appendChild(hr);
  table.appendChild(head);
  const body = el('tbody');
  for (const item of answer.items) {
    const tr = el('tr');
    const pick = el('td');
    if (!item.known) {
      const box = el('input');
      box.type = 'checkbox';
      box.addEventListener('change', () => {
        if (box.checked) offered.add(item.model_id);
        else offered.delete(item.model_id);
        $('adopt').disabled = offered.size === 0;
        $('adopt').textContent = `Добавить в реестр (${offered.size})`;
      });
      pick.appendChild(box);
    }
    tr.appendChild(pick);
    tr.appendChild(el('td', item.model_id, { className: 'mono' }));
    tr.appendChild(el('td', item.display_name));
    tr.appendChild(el('td', (item.tasks || []).join(', ')));
    tr.appendChild(el('td', item.context_length || '—', { className: 'num' }));
    // Three states, three different facts: never stored, stored but nothing ever
    // answered on it, and confirmed by an answer on such a date.
    if (!item.known) {
      tr.appendChild(el('td', 'нет в реестре', { className: 'muted' }));
    } else if (item.confirmed_at) {
      tr.appendChild(el('td', `подтверждена ${item.confirmed_at}`, { className: 'ok' }));
    } else {
      tr.appendChild(el('td', item.active ? 'в реестре, ответа не было' : 'снята',
        { className: 'warn' }));
    }
    body.appendChild(tr);
  }
  table.appendChild(body);
  host.appendChild(table);

  const adopt = el('button', 'Добавить в реестр (0)', {
    id: 'adopt',
    className: 'primary',
    onClick: () => adoptPicked(answer.provider_id).catch((e) => toast(e.message, true)),
  });
  adopt.disabled = true;
  host.appendChild(adopt);
}

async function ask() {
  const provider = $('ask-provider').value;
  if (!provider) {
    toast('Выберите поставщика', true);
    return;
  }
  renderOffer(await api(`/registry/catalog/${encodeURIComponent(provider)}`));
}

async function adoptPicked(provider) {
  const model_ids = [...offered];
  await api(`/registry/catalog/${encodeURIComponent(provider)}/adopt`,
    { method: 'POST', body: { model_ids } });
  toast(`Добавлено: ${model_ids.length}`);
  await load();
  await ask();
}

// Two calls on purpose: the dry run names the rows, and only then does the
// confirmation the operator agreed to match what the second call retires.
async function prune() {
  const provider = $('ask-provider').value;
  if (!provider) {
    toast('Выберите поставщика', true);
    return;
  }
  const url = `/registry/catalog/${encodeURIComponent(provider)}/prune`;
  const plan = await api(`${url}?dry_run=true`, { method: 'POST' });
  const deleted = plan.deleted || [];
  if (!plan.retired.length && !deleted.length) {
    toast(`${provider}: сворачивать нечего`);
    return;
  }
  // Two different fates in one confirmation: retiring is reversible, deleting
  // a retired row nothing ever referenced is not — so each is named with its
  // own count and its own consequence.
  const actions = [];
  if (plan.retired.length) actions.push(`снять с учёта ${plan.retired.length} строк`);
  if (deleted.length) {
    actions.push(`удалить ${deleted.length} снятых строк, к которым не было ни одного запроса`);
  }
  const listed = [
    ...plan.retired.map((id) => `${id} — снять`),
    ...deleted.map((id) => `${id} — удалить`),
  ];
  const names = listed.slice(0, 10).join('\n');
  const more = listed.length > 10 ? `\n…и ещё ${listed.length - 10}` : '';
  if (!window.confirm(
    `${provider}: ${actions.join(', ')}?\n\n${names}${more}\n\n`
    + `Останутся: подтверждённых ответом ${plan.kept_confirmed}, `
    + `ручных и засеянных ${plan.kept_curated}, упомянутых в журнале ${plan.kept_in_log}.\n`
    + 'Снятые строки можно вернуть фильтром «снятые»; удалённые не вернуть.')) {
    return;
  }
  const done = await api(`${url}?dry_run=false`, { method: 'POST' });
  const doneDeleted = done.deleted || [];
  toast(`Снято с учёта: ${done.retired.length}`
    + (doneDeleted.length ? `, удалено: ${doneDeleted.length}` : ''));
  await load();
  await ask();
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
  const cacheRead = Number($('edit-cache-read').value);
  // Absent unless the field was rendered, and an empty field means "unchanged".
  const computeField = $('edit-compute');
  const compute = computeField && computeField.value !== ''
    ? Number(computeField.value) : null;
  const extra = readExtraParams();
  const routing = readProviderRouting();
  const schedule = readPriceSchedule(model);
  const descriptionBox = $('edit-description');
  if (!confirmJump('Промпт', model.prompt_price * 1e6, prompt)) return;
  if (!confirmJump('Ответ', model.completion_price * 1e6, completion)) return;
  const updated = await api(
    `/registry/models/${encodeURIComponent(model.service_provider)}/${model.model_id}`,
    { method: 'PATCH', body: {
      prompt_price: prompt,
      completion_price: completion,
      cache_read_price: cacheRead,
      ...(compute === null ? {} : { compute_price_per_second: compute }),
      // Only when the fields were drawn: `{}` is a real edit — it takes a wrong
      // mapping back off the row — and sending it from a chat card would clear
      // a map the operator never saw.
      ...($('edit-role-image') ? { input_map: readInputMap() } : {}),
      ...(extra === null ? {} : { extra_params: extra }),
      // Only when drawn (OpenRouter rows); an empty field clears the pinning.
      ...(routing === null ? {} : { provider_routing: routing }),
      // Only when the field was drawn; an empty field clears the text.
      ...(descriptionBox ? { description: descriptionBox.value.trim() } : {}),
      // Same rule as the roles: only when the operator could see the field, and
      // `{}` clears the schedule rather than meaning "unchanged".
      ...(schedule === null ? {} : { price_schedule: schedule }),
      revision: model.revision } });
  toast('Строка сохранена');
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
    ['add-tasks', 'Задачи', 'text-to-text'],
    ['add-prompt', 'Промпт, $/1M', '0'],
    ['add-completion', 'Ответ, $/1M', '0'],
    ['add-source', 'Ссылка на прайс', ''],
  ];
  for (const [id, label, placeholder] of fields) {
    const wrap = el('label', undefined, { className: 'grow' });
    wrap.appendChild(document.createTextNode(label));
    const input = el('input', undefined, { id });
    input.placeholder = placeholder;
    if (id === 'add-tasks') input.value = 'text-to-text';
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
          tasks: $('add-tasks').value.split(',').map((s) => s.trim()).filter(Boolean),
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

$('search').addEventListener('input', () => load().catch((e) => toast(e.message, true)));
$('reload').addEventListener('click', () => load().catch((e) => toast(e.message, true)));
$('add-model').addEventListener('click', addModelForm);
$('ask').addEventListener('click', () => ask().catch((e) => toast(e.message, true)));
$('prune').addEventListener('click', () => prune().catch((e) => toast(e.message, true)));
$('reset-facets').addEventListener('click', () => {
  writeParams({});
  $('search').value = '';
  load().catch((e) => toast(e.message, true));
});
$('select-all').addEventListener('change', (event) => {
  selection.clear();
  if (event.target.checked) {
    for (const model of rows.slice(0, 500)) {
      selection.set(key(model), {
        service_provider_id: model.service_provider, model_id: model.model_id,
      });
    }
  }
  render();
});
$('bulk-clear').addEventListener('click', () => { selection.clear(); render(); });
$('bulk-deprecate').addEventListener('click',
  () => bulkDeprecate().catch((e) => toast(e.message, true)));
$('undo-action').addEventListener('click', async () => {
  if (!lastBulk) return;
  try {
    await api('/registry/models/bulk', {
      method: 'POST', body: { action: 'restore', models: lastBulk },
    });
    toast(`Возвращено: ${lastBulk.length}`);
    lastBulk = null;
    $('undo-bar').hidden = true;
    await load();
  } catch (error) {
    toast(error.message, true);
  }
});

(async () => {
  caps = await api('/capabilities');
  for (const provider of caps.providers || []) {
    const option = el('option', provider.configured ? provider.id : `${provider.id} (не настроен)`);
    option.value = provider.id;
    $('ask-provider').appendChild(option);
  }
  const params = readParams();
  if (params.search) $('search').value = params.search;
  await load();
  if (params.model) {
    const match = rows.find((m) => m.model_id === params.model);
    if (match) select(match);
  }
})().catch((error) => toast(`Не удалось загрузить реестр: ${error.message}`, true));
