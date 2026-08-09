// Load every page module against the stub DOM and the real API fixtures.
//
// What this catches, cheaply and without a browser: a mistyped element id, a
// handler bound to a node the template does not have, a page reading a field
// the API does not send, and any assignment to innerHTML. Playwright arrives in
// wave 2 for the things a stub cannot see — layout, focus, real clicks.
//
// Usage: node tests/js/run-pages.mjs

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import { installGlobals, makeDocument, parseFixture } from './dom-stub.mjs';

const here = dirname(fileURLToPath(import.meta.url));
const web = join(here, '..', '..', 'router_mcp', 'web');

const fixture = (name) =>
  JSON.parse(readFileSync(join(here, 'fixtures', `${name}.json`), 'utf8'));

const ROUTES = {
  '/api/v1/capabilities': fixture('capabilities'),
  '/api/v1/registry/tree': fixture('tree'),
  '/api/v1/registry/models': fixture('models'),
  '/api/v1/logs': fixture('logs'),
  // One route serves all three fields: the stub matches on the path alone.
  '/api/v1/logs/suggest': { items: ['web-tester', 'bot-77'], total: 2,
    next_cursor: null, facets: {} },
  '/api/v1/logs/1': fixture('log-detail'),
  '/api/v1/logs/4821': fixture('run-ok'),
  '/api/v1/usage': fixture('usage'),
  '/api/v1/health/summary': fixture('health-summary'),
  '/api/v1/retention': { log_retention_days: 90, blob_retention_days: 30,
    log_cutoff: '2026-04-27T00:00:00+00:00', blob_cutoff: '2026-06-26T00:00:00+00:00',
    logs_to_delete: 12, blobs_to_delete: 4, orphan_blobs: 1, total_logs: 8213 },
  'POST /api/v1/retention/purge': { deleted_logs: 12, deleted_blobs: 5,
    logs_to_delete: 12, blobs_to_delete: 4, orphan_blobs: 1, total_logs: 8213,
    log_retention_days: 90, blob_retention_days: 30 },
  '/api/v1/mcp/summary': fixture('mcp-summary'),
  'POST /api/v1/providers/openrouter/probe': { provider_id: 'openrouter', status: 'ok',
    detail: 'Каталог ответил, моделей 345', latency_ms: 210, age_seconds: 3 },
  'POST /api/v1/mcp/probe': { status: 'ok', detail: 'инструментов 7', tools: 7, latency_ms: 4 },
  'POST /api/v1/registry/models/bulk': { action: 'deprecate', changed: [], count: 0 },
  'POST /api/v1/plan': fixture('plan'),
  // A run is accepted, then polled. The stub answers the poll with a finished
  // run so the page module walks the whole path in one turn.
  'POST /api/v1/runs': { run_id: 'r-1', status: 'running', steps: [], result: null,
    error: null, log_id: null },
  '/api/v1/runs/r-1': { run_id: 'r-1', status: 'ok', log_id: 4821,
    steps: fixture('run-ok').steps, result: fixture('run-ok'), error: null },
  // The live catalog: asked on demand, stored only for what is adopted.
  '/api/v1/registry/catalog/codex-oauth': {
    provider_id: 'codex-oauth', scope: 'full', has_catalog: true, total: 3, known: 1,
    items: [
      { model_id: 'gpt-5.4', display_name: 'GPT-5.4', tasks: ['text-to-text'],
        capabilities: ['vision', 'effort:high'], context_length: 272000,
        prompt_price: 0, completion_price: 0, price_source_kind: 'unknown',
        known: true, active: true, confirmed_at: '2026-07-20T10:00:00+00:00' },
      { model_id: 'gpt-5.6-sol', display_name: 'GPT-5.6-Sol', tasks: ['text-to-text'],
        capabilities: ['effort:ultra'], context_length: 272000,
        prompt_price: 0, completion_price: 0, price_source_kind: 'unknown',
        known: false, active: false, confirmed_at: null },
      { model_id: 'gpt-5.6-luna', display_name: 'GPT-5.6-Luna', tasks: ['text-to-text'],
        capabilities: [], context_length: 272000,
        prompt_price: 0, completion_price: 0, price_source_kind: 'unknown',
        known: false, active: false, confirmed_at: null },
    ],
  },
  'POST /api/v1/registry/catalog/codex-oauth/adopt': {
    provider_id: 'codex-oauth', adopted: ['gpt-5.6-sol'], count: 1 },
  'POST /api/v1/registry/catalog/codex-oauth/prune': {
    provider_id: 'codex-oauth', dry_run: true, retired: ['gpt-5.4-mini'],
    deleted: ['gpt-5.2-relic'], kept_confirmed: 2, kept_curated: 1, kept_in_log: 0 },
  'POST /api/v1/registry/refresh': { providers: [
    { provider_id: 'openrouter', seen: 345, added: 2, updated: 14, deprecated: 1, skipped: null },
    { provider_id: 'deepseek', seen: 0, added: 0, updated: 0, deprecated: 0, skipped: 'not configured' },
  ] },
};

// The page's markup, minus Jinja: the template body is what the module drives.
function template(name) {
  const raw = readFileSync(join(web, 'templates', `${name}.html`), 'utf8');
  const start = raw.indexOf('{% block content %}');
  const end = raw.lastIndexOf('{% endblock %}');
  return raw.slice(start, end)
    // Comments go first: now that the parser keeps inter-tag text, a Jinja
    // comment left in place would surface as visible page text.
    .replace(/\{#[\s\S]*?#\}/g, '')
    .replace(/\{[%{][^}]*[%}]\}/g, '');
}

const failures = [];
const notes = [];

/** Let the module's promise chain run to completion. */
async function settle(turns = 30) {
  for (let i = 0; i < turns; i += 1) await new Promise((r) => setImmediate(r));
}

function check(name, condition, detail = '') {
  if (condition) notes.push(`  ok   ${name}`);
  else failures.push(`  FAIL ${name}${detail ? ` — ${detail}` : ''}`);
}

async function runPage(page, templateName, assertions) {
  const doc = makeDocument();
  parseFixture(template(templateName), doc);
  const calls = installGlobals(doc, ROUTES);
  const errors = [];
  process.on('unhandledRejection', (reason) => errors.push(String(reason)));
  try {
    // Cache-bust so each page gets a fresh module instance.
    await import(`${join(web, 'static', 'js', 'pages', `${page}.js`)}?t=${Date.now()}`);
  } catch (error) {
    failures.push(`  FAIL ${page} threw on load — ${error.message}`);
    return;
  }
  await settle();
  if (errors.length) failures.push(`  FAIL ${page} rejected — ${errors[0]}`);
  console.log(`\n${page}: ${calls.length} запросов к API`);
  // Assertions may click something that loads asynchronously, so they get
  // `settle` and may be async themselves.
  await assertions(doc, calls, settle);
  console.log(notes.splice(0).join('\n'));
}

const text = (doc, id) => doc.getElementById(id)?.textContent ?? '';
const rowsOf = (doc, id) => doc.querySelector(`#${id} tbody`)?.children.length ?? 0;

await runPage('overview', 'overview', (doc) => {
  check('обзор: плитка поставщиков заполнена', /\d+\/\d+/.test(text(doc, 'providers-count')),
    text(doc, 'providers-count'));
  check('обзор: плитка реестра заполнена', text(doc, 'models-count') !== '—');
  check('обзор: uptime и размеры показаны', text(doc, 'service-detail').includes('МБ'),
    text(doc, 'service-detail'));
  // A fresh install must get steps, and a running one a work list — never a
  // wall of zeroes (§14).
  const hasWork = doc.getElementById('work').children.length > 0
    || doc.getElementById('work-empty').hidden === false;
  check('обзор: список работы или явное «ничего не требует внимания»', hasWork);
  check('обзор: ошибки перечислены или названы отсутствующими',
    rowsOf(doc, 'errors') > 0 || doc.getElementById('no-errors')?.hidden === false);
  // Retention: the policy in words, and what a purge would take with it.
  check('обзор: политика хранения названа',
    text(doc, 'retention-policy').includes('90 дн.'), text(doc, 'retention-policy'));
  check('обзор: показано, что будет удалено',
    doc.getElementById('retention').children.length === 4);
  check('обзор: файлы без записи посчитаны отдельно',
    doc.querySelector('#retention').textContent.includes('Файлов без записи'));
});

await runPage('logs', 'logs', async (doc, _calls, settleNow) => {
  check('журнал: строки отрисованы', rowsOf(doc, 'logs') > 0);
  check('журнал: подпись часового пояса на месте', text(doc, 'tz').includes('UTC'));
  check('журнал: счётчик показан', text(doc, 'count').includes('показано'));
  // The error row opens under its entry — the whole point of §13.
  const body = doc.querySelector('#logs tbody');
  check('журнал: текст ошибки виден', body.textContent.includes('UnknownModel'),
    body.textContent.slice(0, 80));

  // Provider before model, tokens split into their four kinds.
  const header = doc.querySelector('#logs thead').textContent;
  check('журнал: колонка поставщика перед моделью',
    header.includes('Поставщик') && header.indexOf('Поставщик') < header.indexOf('Модель'));
  check('журнал: токены разбиты на четыре колонки',
    ['Промпт', 'Кэш', 'Ответ', 'Рассужд.'].every((t) => header.includes(t)), header);
  // children: [error row, its error line, ok row] — the ok row carries the JSON request.
  const okRow = body.children[2];
  check('журнал: поставщик показан в строке', okRow.textContent.includes('openrouter'),
    okRow.textContent.slice(0, 120));
  check('журнал: в ячейке запроса текст, не JSON',
    okRow.textContent.includes('сколько будет 2+2') && !okRow.textContent.includes('"role"'),
    okRow.textContent.slice(-80));
  const requestCell = okRow.children[okRow.children.length - 1];
  check('журнал: JSON запроса остался в тултипе',
    String(requestCell.title || '').includes('"role"'));
  // Row 3 carries a history cut mid-message at the 4000-character column
  // limit: broken JSON must still surface as words, not punctuation.
  const cutRow = body.children[3];
  check('журнал: обрезанный JSON тоже отдаёт слова',
    cutRow.textContent.includes('Заголовок: почем') && !cutRow.textContent.includes('"role"'),
    cutRow.textContent.slice(-80));
  check('журнал: счётчики токенов по своим колонкам',
    okRow.textContent.includes('1200') && okRow.textContent.includes('900')
      && okRow.textContent.includes('340') && okRow.textContent.includes('64'));

  // Opening an entry has to answer «что спросили», not only «что ответили»:
  // before the «Запрос» tab the request was nowhere on this screen.
  body.children[0].dispatch('click');
  await settleNow();
  const detail = doc.getElementById('detail');
  check('журнал: подробности записи открываются', detail.hidden === false);
  const tabNames = detail.querySelectorAll('.tab').map((t) => t.textContent);
  check('журнал: закладка Запрос на месте', tabNames.includes('Запрос'), tabNames.join(' · '));
  detail.querySelectorAll('.tab').find((t) => t.textContent === 'Запрос').dispatch('click');
  await settleNow();
  const pane = detail.querySelector('.tab-pane');
  check('журнал: в запросе виден текст и как спросили',
    pane.textContent.includes('привет') && pane.textContent.includes('Запрошенная модель'),
    pane.textContent.slice(0, 120));
  detail.querySelectorAll('.tab').find((t) => t.textContent === 'JSON').dispatch('click');
  await settleNow();
  check('журнал: в JSON есть и запрос, и ответ',
    detail.querySelector('.tab-pane').textContent.includes('JSON (запрос)')
      && detail.querySelector('.tab-pane').textContent.includes('JSON (ответ /api)'));

  // The spend tab renders from the same page module, and loads asynchronously.
  doc.getElementById('tab-spend').dispatch('click');
  await settleNow();
  check('расход: вкладка открывается', doc.getElementById('pane-spend').hidden === false);
  const totals = text(doc, 's-totals');
  check('расход: подтверждённые и неподтверждённые ставки разведены',
    totals.includes('подтверждённые ставки') && totals.includes('неподтверждённые ставки'),
    totals.slice(0, 120));
  check('расход: условная стоимость подписки названа отдельно',
    totals.includes('не списывалась'));
  check('расход: итог 12.00, без условных 0.84',
    totals.includes('12.000000'), totals.slice(0, 200));
  check('расход: строки сгруппированы', rowsOf(doc, 'spend') > 0);
  const spendHeader = doc.querySelector('#spend thead').textContent;
  check('расход: итог токенов остался и колонки разбиты',
    spendHeader.includes('Токенов')
      && ['Промпт', 'Кэш', 'Ответ', 'Рассужд.'].every((t) => spendHeader.includes(t)),
    spendHeader);
  check('расход: ссылка на выгрузку ведёт на CSV',
    (doc.getElementById('s-export').attributes.href || '').includes('/usage/export'));
});

await runPage('models', 'models', async (doc, calls, settleNow) => {
  check('модели: таблица заполнена', rowsOf(doc, 'models') > 0);
  check('модели: счётчик показан', text(doc, 'count') !== '');
  // Facet counts come back with the rows, so the sidebar cannot disagree.
  check('модели: фасеты со счётчиками построены',
    doc.getElementById('facet-groups').children.length > 0);
  // Opening a row must build the price card with the unit inside the field.
  doc.querySelector('#models tbody').children[0].dispatch('click');
  const card = doc.getElementById('card');
  check('модели: карточка открывается', card.hidden === false);
  check('модели: единица внутри поля', card.textContent.includes('$/1M'));
  check('модели: прежнее значение показано', card.textContent.includes('было'));
  check('модели: карточка говорит, подтверждена ли модель ответом',
    card.textContent.includes('Подтверждена ответом'));

  // «Спросить поставщика»: the live list, and adopting from it.
  const ask = doc.getElementById('ask-provider');
  check('модели: поставщики предложены для опроса', ask.children.length > 1);
  ask.value = 'codex-oauth';
  doc.getElementById('ask').dispatch('click');
  await settleNow();
  const offer = doc.getElementById('offer');
  check('каталог: живой список запрошен, ничего не сохранено',
    calls.some((c) => c.url.includes('/registry/catalog/codex-oauth') && c.method === 'GET'));
  check('каталог: модель вне реестра помечена',
    offer.textContent.includes('нет в реестре'), offer.textContent.slice(0, 200));
  check('каталог: подтверждённая модель названа подтверждённой',
    offer.textContent.includes('подтверждена 2026-07-20'));
  const adopt = doc.getElementById('adopt');
  check('каталог: без выбора добавлять нечего', adopt.disabled === true);
  const box = offer.querySelector('tbody').children[1].querySelector('input');
  box.checked = true;
  box.dispatch('change');
  check('каталог: кнопка считает выбранные',
    adopt.textContent.includes('(1)'), adopt.textContent);
});

await runPage('connections', 'connections', async (doc, _calls, settleNow) => {
  check('подключения: поставщики перечислены', rowsOf(doc, 'providers') > 0);
  const body = doc.querySelector('#providers tbody');
  check('подключения: имя переменной окружения показано',
    body.textContent.includes('_API_KEY') || body.textContent.includes('_COMMAND'));
  check('подключения: значение ключа не показано', !body.textContent.includes('sk-'));
  check('подключения: колонка пробы ключа есть', body.textContent.includes('Проверить'));

  doc.getElementById('tab-mcp').dispatch('click');
  await settleNow();
  check('MCP: вкладка открывается', doc.getElementById('pane-mcp').hidden === false);
  check('MCP: инструменты перечислены', rowsOf(doc, 'tools') > 0);
  const tools = doc.querySelector('#tools tbody').textContent;
  check('MCP: статистика вызовов из журнала подтянута', tools.includes('chat'));
  check('MCP: клиенты перечислены', rowsOf(doc, 'clients') > 0);
  check('MCP: фрагмент конфигурации собран',
    text(doc, 'mcp-snippet').includes('mcpServers'));
  doc.getElementById('mcp-probe').dispatch('click');
  await settleNow();
  check('MCP: самопроба отвечает', text(doc, 'mcp-probe-result').includes('ok'));
});

await runPage('run', 'run', async (doc, calls, settleNow) => {
  // Tasks arrive grouped by billing group, so the children are <optgroup>s.
  const taskSelect = doc.getElementById('task');
  check('прогон: задачи загружены', taskSelect.children.length > 1);
  check('прогон: задача без настроенного поставщика помечена',
    taskSelect.textContent.includes('поставщик не настроен'));
  check('прогон: параметры построены по матрице',
    doc.getElementById('params').children.length > 0);
  check('прогон: у параметра есть видимое пояснение',
    doc.getElementById('params').textContent.length > 20);
  // The regression: an <option> without a value takes its label as its value, so
  // «— по умолчанию —» travelled to the Codex backend as reasoning.effort and
  // came back HTTP 400. A placeholder must select to the empty string.
  const selects = doc.getElementById('params').querySelectorAll('select');
  check('прогон: у пустого варианта пустое значение',
    selects.length > 0 && selects.every((s) => s.children[0].value === ''),
    selects.map((s) => s.children[0].value).join('|'));
  check('прогон: подпись «по умолчанию» не уезжает в запрос',
    !JSON.stringify(calls.filter((c) => c.body).map((c) => c.body))
      .includes('по умолчанию'));
  // A boolean parameter is a checkbox: unchecked means «не задавать», so the
  // flag must stay out of the call, never travel as "on"/"false".
  doc.getElementById('auto-provider').value = 'openrouter';
  doc.getElementById('auto-provider').dispatch('change');
  await settleNow();
  const webSearch = doc.getElementById('param-web_search');
  check('прогон: web_search рисуется чекбоксом',
    !!webSearch && webSearch.type === 'checkbox');
  doc.getElementById('auto-provider').value = '';
  doc.getElementById('auto-provider').dispatch('change');
  await settleNow();
  check('прогон: настройки сервера показаны именами переменных',
    text(doc, 'server-settings').includes('HTTP_TIMEOUT_SECONDS'));
  check('прогон: варианты фолбэка человеческими словами',
    doc.getElementById('fallback').textContent.includes('подставить'));
  check('прогон: план запрошен без отправки',
    calls.some((c) => c.url.includes('/plan') && c.method === 'POST'));
  check('прогон: ни одного вызова модели при загрузке',
    !calls.some((c) => c.method === 'POST' && /\/(chat|image|tts|cli|runs)$/.test(c.url)));

  // The regression from the screenshot: pick a model whose family has one
  // variant («базовая»), and model_id must fill itself — it used to stay empty
  // until the variant was clicked by hand, so «выбрана gpt-5.6-sol» left the
  // form as a bare provider hint and a different model answered.
  doc.getElementById('mode-pick').dispatch('click');
  await settleNow();
  doc.getElementById('sel-provider').value = 'deepseek';
  doc.getElementById('sel-provider').dispatch('change');
  doc.getElementById('sel-vendor').value = 'deepseek';
  doc.getElementById('sel-vendor').dispatch('change');
  doc.getElementById('sel-family').value = 'deepseek-v4-flash';
  doc.getElementById('sel-family').dispatch('change');
  await settleNow();
  check('выбор: единственный вариант семьи заполняет model_id сам',
    doc.getElementById('model-id').value === 'deepseek-v4-flash',
    `model-id="${doc.getElementById('model-id').value}"`);
  // And walking away from the pick does not silently pin the old id.
  doc.getElementById('sel-family').value = '';
  doc.getElementById('sel-family').dispatch('change');
  await settleNow();
  check('выбор: уход с выбора очищает подставленный id',
    doc.getElementById('model-id').value === '',
    `model-id="${doc.getElementById('model-id').value}"`);

  doc.getElementById('show-plan').dispatch('click');

  // Multi-model mode: chips in, one run per chip, one table row per run.
  doc.getElementById('mode-multi').dispatch('click');
  await settleNow();
  check('мультипрогон: список моделей заполнен',
    doc.getElementById('multi-pick').children.length > 1);
  const pick = doc.getElementById('multi-pick');
  pick.value = pick.children[1].value;
  doc.getElementById('multi-add').dispatch('click');
  check('мультипрогон: чип добавлен', doc.getElementById('chips').children.length === 1);
  check('мультипрогон: кнопка считает прогоны',
    doc.getElementById('send').textContent.includes('Запустить 1'),
    doc.getElementById('send').textContent);

  doc.getElementById('send').dispatch('click');
  await settleNow();
  check('мультипрогон: прогон отправлен через реестр',
    calls.some((c) => c.method === 'POST' && c.url.endsWith('/runs')));
  check('мультипрогон: ключ идемпотентности приложен',
    calls.some((c) => c.method === 'POST' && c.url.endsWith('/runs')
      && JSON.parse(c.body).client_request_id));
  check('мультипрогон: состояние опрошено',
    calls.some((c) => c.url.includes('/runs/r-1')));
  const table = doc.querySelector('#multi tbody');
  check('мультипрогон: строка результата заполнена',
    table.children.length === 1 && table.textContent.includes('8.42 с')
      && table.textContent.includes('0.000412'),
    table.textContent.slice(0, 120));
});

// The inspector and the timeline are exercised through a run with retries.
{
  const doc = makeDocument();
  installGlobals(doc, ROUTES);
  const { inspector } = await import(`${join(web, 'static', 'js', 'components', 'inspector.js')}?t=${Date.now()}`);
  const host = doc.createElement('div');
  const run = fixture('run-ok');
  const caps = fixture('capabilities');
  inspector(host, run, { labels: caps.labels });
  const dump = host.textContent;
  console.log('\ninspector:');
  check('инспектор: пять вкладок', host.querySelectorAll('.tab').length === 5,
    host.querySelectorAll('.tab').map((t) => t.textContent).join(' · '));
  check('инспектор: подмена названа', dump.includes('запрошена deepseek-v4-pro'));
  check('инспектор: игнорируемый параметр назван', dump.includes('tools'));
  check('инспектор: скрипт в ответе остался текстом', dump.includes('<script>alert(1)</script>'));

  // Switch to Шаги and check the second level of the timeline is there.
  const stepsTab = host.querySelectorAll('button').find((b) => b.dataset.tab === 'steps');
  stepsTab.dispatch('click');
  const steps = host.textContent;
  check('таймлайн: повторы видны', steps.includes('провайдер просил 5 с'), steps.slice(0, 200));
  check('таймлайн: обе попытки видны',
    steps.includes('deepseek-v4-pro') && steps.includes('deepseek-chat'));

  const rawTab = host.querySelectorAll('button').find((b) => b.dataset.tab === 'raw');
  rawTab.dispatch('click');
  check('сырьё: причина остановки расшифрована',
    host.textContent.includes('обрыв по лимиту токенов'));
  check('сырьё: id запроса у провайдера показан', host.textContent.includes('req-77f0'));

  // The request side: the history with its roles, the knobs, and the policy in
  // the same words the «Прогон» select uses.
  host.querySelectorAll('.tab').find((b) => b.dataset.tab === 'request').dispatch('click');
  const asked = host.textContent;
  check('запрос: роли и текст сообщений видны',
    asked.includes('system') && asked.includes('Отвечай по-русски'), asked.slice(0, 160));
  check('запрос: параметры перечислены',
    asked.includes('temperature') && asked.includes('max_tokens'));
  check('запрос: запрошенная модель отделена от отработавшей',
    asked.includes('deepseek-v4-pro'));
  check('запрос: политика подмены человеческими словами',
    asked.includes('подставить'), asked.slice(0, 400));
  check('запрос: ключ идемпотентности показан', asked.includes('web-9f31'));
  console.log(notes.splice(0).join('\n'));
}

if (failures.length) {
  console.error(`\n${failures.length} проверок не прошло:\n${failures.join('\n')}`);
  process.exit(1);
}
console.log('\nвсе проверки прошли');
