# History

Newest first. Each entry ≤5 lines using the format defined in `AGENTS.md`.

---

## 2026-08-11 · модели картинок для agentish: 11 строк Replicate + 3 фикса
- What: заведены manual-строки §5.2–§5.6 (фон/апскейлы/инпейнт/face-swap/редакторы) с ценами page; синк не депрекирует manual; array-входы image_input; dropped_params → ignored_params; список моделей = GET /registry/models (контракт запинен тестом).
- Why: agentish ждал строк реестра для удаления фона/апскейла и списка моделей под выбор пользователя.
- Files: registry/service.py, adapters/replicate.py, routing/router.py, tests×3, AGENTS/SPEC.md
- Next: easel мёртв — face-swap через cdingram (одно лицо); real-esrgan не берёт 2048px (кап ~1440p) — сказать agentish про даунскейл; nano-banana-2 русский понимает — перевод можно снимать.

## 2026-08-11 · issue #5: output_format у обоих TTS-адаптеров
- What: параметр output_format в elevenlabs (проброс строки, алиасы mp3/opus) и yandex (префикс → контейнер MP3/OGG_OPUS/WAV); mime вложения по формату; дефолты не изменились (ogg/opus).
- Why: Safari iOS 16 не играет ogg/opus — agentish перекодировал через ffmpeg; он уже шлёт mp3_44100_128, теперь роутер отдаёт mp3 сам.
- Files: router_mcp/adapters/elevenlabs.py, router_mcp/adapters/yandex.py, тесты обоих
- Next: закрыть #5, agentish может убрать ffmpeg из деплоя.

## 2026-08-11 · дефолт TTS — SpeechKit v3
- What: DEFAULT_TTS_MODEL=speechkit-tts-v3 в dev/.env.example/прод; рестарт, живая проверка: без хинтов speechkit computed, provider=elevenlabs даёт flash (дешёвый ярус), multilingual — по model_id.
- Why: оператор: SpeechKit читает русский лучше (числительные) и дешевле ElevenLabs при любой длине.
- Files: .env.example (+ .env dev и прод вне git)
- Next: понаблюдать расходы в /usage; при жалобах на flash под provider-хинтом — обсудить per-provider дефолт.

## 2026-08-11 · SpeechKit: v1 → v3, роли голосов, честный поблочный учёт
- What: адаптер переписан на utteranceSynthesis (hints voice/role/speed, unsafeMode, сборка чанков); сид speechkit-tts-v3 с ценой $0.0000053311/симв., v1 депрекирован; usage.characters округляется до блока 250; docs/speechkit-voices.md из клиппинга.
- Why: решение оператора — только v3, без выбора версий: роли и новые голоса (saule/zhanar для kk), дешевле ElevenLabs при любой длине.
- Files: router_mcp/adapters/yandex.py, bootstrap.py, web/capabilities.py, tests/test_adapters_yandex.py, docs/speechkit-voices.md
- Next: деплой, депрекация v1 на проде, живой прогон ru+role и kk.

## 2026-08-11 · цена SpeechKit вбита со страницы тарифов
- What: yandex/speechkit-tts-v1 — $0.000011/символ (API v1 $10.99999824/1М без НДС, страница даёт цену в USD сама), tier=cheap; прод через PATCH, dev досеян вместе со строкой провайдера (FK требует provider до модели).
- Why: оператор принёс клиппинг тарифов (сайт за капчей); прогоны SpeechKit теперь computed, а не unpriced.
- Files: — (данные реестров dev и прод)
- Next: при переходе на TTS API v3 пересчитать — там тариф за запрос (250 символов = единица).

## 2026-08-11 · прод: DEFAULT_TTS_MODEL в .env, flash восстановлен
- What: оператор вписал DEFAULT_TTS_MODEL и поправил устаревший ELEVENLABS_TTS_MODEL; рестарт, bulk restore flash, живая проверка: без хинтов multilingual, по model_id — flash, обе с computed-ценой.
- Why: финал включения выбора движков — дефолт держится настройкой, flash доступен явным выбором.
- Files: — (только .env на проде и данные реестра)
- Next: у /api/tts нет поля tier — добавить при надобности; SpeechKit unpriced до появления тарифа.

## 2026-08-11 · голоса и движки TTS — через API, дефолт — через настройку
- What: voice у ElevenLabs принимает имя или id (кэш резолва); оба движка — активные строки (flash cheap 0.00018, multilingual expensive 0.00036 $/символ, происхождение page); DEFAULT_*_MODEL теперь побеждает скан при запросе без хинтов.
- Why: выбор голоса/движка per-request вместо зашивки в настройках; дефолт группы держится настройкой, а не алфавитом model_id.
- Files: routing/router.py, adapters/elevenlabs.py, bootstrap.py, tests/test_task_routing.py, tests/test_adapters_elevenlabs.py, AGENTS/SPEC.md
- Next: прод — DEFAULT_TTS_MODEL в .env (оператор), затем restore flash + цены; SpeechKit unpriced.

## 2026-08-11 · SpeechKit включён на проде, ru+kk живьём
- What: ключ (роль ai.editor) в прод-.env, рестарт, enabled=true через PATCH; живые прогоны /api/tts: marina/ru-RU и amira/kk-KK, 146–363 мс. Только реестр и .env на проде, кода не трогали.
- Why: проверка роли ключа + обещанный живой прогон; A/B-страница получила колонку SpeechKit и казахскую карточку.
- Files: — (код не менялся; данные реестра на проде)
- Next: вбить ставку за символ SpeechKit (страница цен), сверить голоса с клиппингом оператора, вписать ключ в dev-.env.

## 2026-08-11 · провайдер Yandex SpeechKit (TTS v1)
- What: адаптер yandex.py (tts:synthesize, Api-Key, oggopus, ru/kk/en/uz/de/he), сид yandex/speechkit-tts-v1, параметр emotion в капабилити, ключи YANDEX_* в конфиге.
- Why: пробуем SpeechKit ради русских ударений и казахского; ElevenLabs остаётся дефолтом (сортировка по model_id при равных ценах).
- Files: router_mcp/adapters/yandex.py, bootstrap.py, config.py, web/capabilities.py, tests/test_adapters_yandex.py
- Next: вписать YANDEX_API_KEY (dev и прод), включить провайдера, живой прогон + сверка голосов с доками.

## 2026-08-11 · TTS: flash → multilingual v2
- What: ElevenLabs TTS переключён на `eleven_multilingual_v2` (config.py, адаптер, .env, .env.example); flash-строка депрекирована в реестрах dev и прод с `replaced_by`.
- Why: качество русской озвучки — flash оптимизирован под латентность; собран A/B на 9 фразах-ловушках для сверки ушами. `language_code` у multilingual_v2 API принимает вопреки докам.
- Files: router_mcp/config.py, router_mcp/adapters/elevenlabs.py, .env.example, AGENTS/STATE.md
- Next: вбить ставку за символ (кредиты ×2 против flash), оценить A/B ушами.

## 2026-08-09 · у всех моделей Replicate появилась цена
- What: flux-schnell 0.003 за картинку ($3 за 1000), flux-kontext-pro 0.04 за картинку, musicgen 0.0014 $/с ($0.065 / 47 с, A100 80GB), примерочная 0.0014 $/с — происхождение `page`, арифметика в `price_source`. Только данные реестра на проде, кода не трогали.
- Why: API Replicate цен не публикует, строки приходили с `price_source_kind=unknown`, и расход по провайдеру в отчётах был занижен и помечен нижней границей.
- Files: — (PATCH `/api/v1/registry/models/replicate/*` на проде)
- Verified: сухие прогоны всех четырёх дают `computed` с правильной единицей; `compute_cost` на реальных цифрах — 0.003 за картинку, 0.04 за картинку, $0.0225 за прогон 10575, $0.0658 за 47 с musicgen. Ставка A100 сошлась по двум независимым моделям (0.0013830 и 0.0014118). «Обзор»: моделей без цены 14 -> 11, все оставшиеся не Replicate.
- Next: исторические строки журнала пересчитать нельзя — сумма фиксируется при записи.

## 2026-08-09 · сухой прогон и журнал больше не расходятся в цене
- What: правило «своя ставка группы, иначе секунды расчёта» вынесено в `accounting.cost.billing_unit` и применяется и в `compute_cost`, и в `_estimate`; оценка для такой модели называет ставку и говорит, что длительность прогона заранее неизвестна.
- Why: `POST /plan` для группы `image` смотрел только `image_price`, поэтому примерочная на Replicate со ставкой за секунду расчёта в сухом прогоне читалась как «ставки нет», а в журнале считалась нормально — экран противоречил исходу.
- Files: router_mcp/accounting/cost.py, router_mcp/routing/router.py, AGENTS/SPEC.md, tests/test_plan.py
- Verified: pytest 397 прошёл, node-харнесс зелёный; на проде оценка примерочной стала $0.0014 за секунду расчёта вместо «ставки нет».
- Next: —

## 2026-08-09 · роли вложений в форме «Прогон»
- What: после выбора файлов форма рисует строку на файл (имя + роль), но только когда файлов больше одного и задача берёт картинку; предлагаются роли из `input_map` закреплённой модели (`/registry/tree` отдаёт их на варианте), первый файл предугадан как `image`; слова живут в `web/labels.ATTACHMENT_ROLES` и прибиты тестом к словарю домена, карточка модели читает оттуда же.
- Why: браузер отдаёт плоский список, а список умеет только порядок — примерочную из консоли было не запустить, только через API.
- Files: router_mcp/web/labels.py, router_mcp/web/api.py, router_mcp/web/templates/run.html, router_mcp/web/static/js/pages/run.js, router_mcp/web/static/js/pages/models.js, router_mcp/web/static/app.css, tests/*
- Verified: pytest 395 прошёл, node-харнесс зелёный; на проде заведена примерочная `cuuupid/idm-vton` (в `REPLICATE_MODELS`, `input_map` и `extra_params` через PATCH) и прогнана на двух картинках — строка 10575, predict_time 16 с: текст без prompt-свойства не уронил прогон (задача 1), человек и одежда сели на свои входы (задача 2), `category`/`garment_des`/`steps` ушли в модель с пустым `ignored_params` (задача 3).
- Next: ставка примерочной вбита следом (0.0014 $/с — A100 80GB, $0.024 за прогон / типично 17 с со страницы модели, происхождение `page`), а найденное попутно расхождение оценки и журнала починено отдельной записью ниже.

## 2026-08-09 · свои параметры модели и громкий отказ без публичной выдачи
- What: у строки реестра появился `extra_params` — список имён, который складывается с `SUPPORTED_PARAMS` адаптера только для этой модели (значение по-прежнему проверяется по схеме, в `ignored_params` не попадает, поле рисуется формой из `/registry/tree`); адаптер объявляет `inline_file_limit_bytes`, и незаданные `PUBLIC_BLOB_BASE_URL`/`PUBLIC_BLOB_SIGNING_KEY` теперь видны в `/capabilities`, `/health/summary` и `/plan`.
- Why: задачи 3 и 4 от ИИшной: апскейлер нельзя было завести без правки адаптера, а без публичной выдачи молча падал любой запрос с фотографией — сообщение адаптера появлялось только на первом настоящем прогоне, когда ключ и модель уже оплачены.
- Files: router_mcp/registry/models.py, router_mcp/storage/database.py, router_mcp/adapters/base.py, router_mcp/adapters/replicate.py, router_mcp/routing/router.py, router_mcp/web/api.py, router_mcp/web/public_blobs.py, router_mcp/web/health.py, router_mcp/web/capabilities.py, router_mcp/web/static/js/*, deploy/DEPLOY.md, .env.example, tests/*
- Verified: pytest 393 прошёл, node-харнесс зелёный; на проде обе переменные заданы давно (`blob.wildcar.org`), правки конфигурации не потребовалось — в dev-`.env` их нет, и предупреждение там сработает по делу.
- Next: —

## 2026-08-09 · роли вложений и `image-to-image` без промпта
- What: у `image-to-image` текст стал необязательным, а модель без prompt-свойства кладёт лишний текст в `ignored_params` вместо отказа (задача, которой текст обязателен, отказывает как раньше); у вложения появилась роль (`image`/`mask`/`face`/`garment`/`reference`/`style`), строка реестра — карта `input_map`, роли привязываются раньше позиционных, нераспознанная роль это ошибка с перечнем входов модели.
- Why: задачи 1 и 2 от ИИшной: удаление фона и апскейл были недоступны с обеих сторон, а примерочная, замена лица и инпейнт получали картинки по порядку кортежа, то есть какая из двух фотографий одежда решалось подбрасыванием монеты; маску больше 256 КБ доставить было нечем.
- Files: router_mcp/domain/task.py, router_mcp/domain/messages.py, router_mcp/adapters/replicate.py, router_mcp/registry/models.py, router_mcp/storage/database.py, router_mcp/web/api.py, router_mcp/routing/router.py, router_mcp/web/views.py, router_mcp/mcp_server/serialize.py, router_mcp/web/static/js/pages/models.js, tests/*
- Verified: pytest 375 прошёл, node-харнесс зелёный; живого прогона с двумя картинками не было — на проде нет такой модели.
- Next: завести на проде примерочную или инпейнт, заполнить `input_map` и прогнать две картинки; форма «Прогон» роли пока не отправляет.

## 2026-08-09 · codex-oauth принимает запрос из одних `messages`
- What: адаптер codex-oauth больше не требует `text` — последний пользовательский ход поднимается из истории в содержимое запроса `/responses`, остальное остаётся историей; с `text` поведение прежнее.
- Why: контракт `chat` разрешает «либо `text`, либо `messages`», deepseek так и отвечает, а codex-oauth падал с «Request carries neither text nor an image»; на этом сломалась «Картина дня» posinus, переведённая на gpt-5.5 с веб-поиском.
- Files: router_mcp/adapters/codex_oauth.py, tests/test_adapters_parse.py
- Verified: pytest весь зелёный (358); живьём — вызов posinus daypic на gpt-5.5 (medium, поиск) после выката.
- Next: —

---

## 2026-08-09 · web_search у codex-oauth и обоих CLI-движков
- What: тот же нормализованный `params.web_search` теперь читают codex-oauth (`tools: [{"type": "web_search"}]` в /responses, режим `server_tool`, шаги `web_search_call` в таймлайне), codex-cli (`-c tools.web_search=true`) и claude-cli (`--allowedTools WebSearch`); лесенки нет — либо работает, либо падает вслух.
- Why: пользователь попросил прокинуть параметр во все движки, где поиск есть; у OpenRouter он уже был.
- Files: router_mcp/adapters/{codex_oauth,codex_cli,claude_cli}.py, tests/test_web_search.py, AGENTS/SPEC.md
- Verified: живьём все три — codex-oauth и codex-cli на dev (строки 1 и 3), claude-cli на проде после выката (строка 10423: «инструмент: WebSearch» в таймлайне, флаг в request_json, стоимость provider_notional); `codex exec --search` не существует — exit 2, заменено конфиг-ключом; dev-логин claude у кипера протух (401) — к обвязке отношения не имеет.
- Next: —

---

## 2026-08-09 · Разделение разрядов у счётчиков токенов
- What: `format.count()` — единый локализованный счётчик (ru-RU, 12 345); четыре колонки токенов журнала, разбивка «Расхода» и сводка инспектора считают разряды через него.
- Why: пользователь: «цифры в запросах сделай разделение разрядов» — пятизначные счётчики токенов без группировки не читаются.
- Files: router_mcp/web/static/js/{core/format.js,pages/{logs,spend}.js}, tests/js/run-pages.mjs
- Verified: node-харнесс зелёный («1 200» с неразрывным пробелом в ячейке), pytest страниц зелёный.
- Next: —

---

## 2026-08-09 · Сид acceptance-тестов гнил на таймере — даты стали относительными
- What: литеральные `created_at` в сиде (`2026-07-26…`) заменены на `_stamp(minutes_ago)` от текущего момента.
- Why: таблица MCP-клиентов читает последние 24 часа — `test_mcp_tab_lists_tools_with_schemas` падал со следующего дня после написания; вкладка «Расход» (период 30 дней) сгнила бы к 25.08.
- Files: tests/test_acceptance.py
- Verified: acceptance 15/15, полный pytest 352 прошло.
- Next: —

---

## 2026-08-09 · Обрезанный JSON отдаёт слова; календарь виден в тёмной теме
- What: ячейка «Запрос» достаёт последнюю user-реплику регуляркой из битого JSON (`salvagedText`), когда история длиннее 4000 символов и `JSON.parse` падает на срезанном хвосте; `input[type=date]` получил `color-scheme: dark`.
- Why: после починки кэша пользователь показал строки, где в «Запросе» так и стоял `[{"role": …` — это ровно длинные истории, срезанные лимитом колонки; значок календаря рисовался чёрным по чёрному.
- Files: router_mcp/web/static/{js/pages/logs.js,app.css}, tests/js/{fixtures/logs.json,run-pages.mjs}
- Verified: node-харнесс зелёный, включая новую строку с историей, срезанной посреди сообщения.
- Next: —

---

## 2026-08-09 · Кэш статики пережил деплой — ?v= не менялся никогда
- What: версия статики несёт штамп старта процесса (`pages.STATIC_VERSION`), `/static` отвечает `Cache-Control: no-cache` (ревалидация — 304, не перекачка; чинит и незверсованные относительные import'ы); кнопки листания дня — в горизонтальном `.day-nav`.
- Why: первый взгляд на прод после выката журнала — шаблон новый, logs.js из кэша: 11 старых ячеек под 15 новых заголовков, день не работает; `?v=` — это `__version__`, а он 0.1.0 навсегда.
- Files: router_mcp/{web/pages.py,app.py,web/templates/logs.html,web/static/app.css}
- Verified: прод — `?v=0.1.0.1786293474`, `cache-control: no-cache` на /static, day-nav в разметке; node-харнесс и pytest страниц зелёные.
- Next: —

---

## 2026-08-09 · Журнал по списку пользователя; свёртка добирает мусор реестра
- What: свёртка удаляет снятые строки без единого упоминания (`PruneReport.deleted`); журнал — поиск пользователя подстрокой с подсказками (`GET /logs/suggest`), фильтры поставщик/модель/день (◀ ▶ по дням), колонка «Поставщик» перед моделью, токены четырьмя колонками Промпт/Кэш/Ответ/Рассужд. (та же разбивка в «Расходе» при живом итоге), «Запрос» — текст в ячейке, JSON в тултипе.
- Why: снятые OpenRouter-строки засоряли список и «сворачивать нечего» врало; фильтр по метке требовал точного совпадения; единая колонка токенов прятала, за какой вид ушли деньги; в «Запросе» стоял JSON вместо слов.
- Files: router_mcp/{registry/service.py,storage/database.py,web/api.py,web/templates/logs.html,web/static/js/pages/{logs,spend,models}.js}, tests/{test_api_filters,test_registry_sync}.py, tests/js/{dom-stub,run-pages}.mjs, AGENTS/SPEC.md
- Verified: pytest 351 прошло (давний test_mcp_tab_lists_tools_with_schemas падает и на чистом HEAD, вынесен отдельной задачей); node-харнесс зелёный, стаб-DOM научился тексту между тегами ради проверок заголовков.
- Next: прогнать свёртку OpenRouter на проде — снятые 363 строки без запросов должны уйти из списков насовсем.

---

## 2026-08-08 · Сверка с суммой поставщика — расхождение называет себя само
- What: `request_logs.provider_cost_usd` (из `usage.cost` OpenRouter; миграция поднимает его из JSON у старых строк); фильтр `cost_mismatch` (>2% и >$0.0001, кроме provider/notional/amortized); в журнале метка `≠`, в инспекторе сумма поставщика рядом с расчётной, на «Обзоре» счётчик за неделю.
- Why: обе дыры в ставках (цена поиска, кэш-ставки) проявлялись одинаково — расчёт врозь с суммой провайдера — и обнаруживались только ручной сверкой; теперь это строка в списке дел.
- Files: router_mcp/{storage/database.py,routing/router.py,web/{api,views,health}.py,web/templates/logs.html,web/static/js/{pages/logs.js,components/inspector.js}}, tests/{test_cost_reconciliation,test_migrations,test_web_search}.py, AGENTS/SPEC.md
- Verified: pytest 346 прошло, node-харнесс зелёный; на проде после выката фильтр должен показать строки 10234 и 10242 (обе — вчерашние находки), 10246 — тихая.
- Next: DeepSeek per-request суммы не даёт (только баланс аккаунта) — сверка там остаётся точной арифметикой по каталожным ставкам.

---

## 2026-08-08 · Цена веб-поиска в реестре и в расходе
- What: колонка `web_search_price` (USD за один поиск, из каталожного `pricing.web_search`, синк и ручная правка как у остальных ставок); адаптер кладёт `server_tool_use_details.web_search_requests` в `Usage.web_searches`; расход получает слагаемое «веб-поиск», без ставки — весь итог `estimated`.
- Why: прод-прогон 10234 — провайдер списал $0.028, реестр насчитал $0.011: два поиска по $0.01 не входили в токенные ставки, расход по поисковым запросам занижался.
- Files: router_mcp/{registry/models.py,storage/database.py,adapters/openrouter.py,domain/messages.py,accounting/cost.py,registry/service.py,web/api.py,mcp_server/serialize.py}, tests/{test_accounting,test_web_search,test_adapters_parse}.py, AGENTS/SPEC.md
- Verified: pytest 339 прошло; прод, строка 10242 — сначала `estimated` $0.0172 против $0.0147 провайдера, причина ниже; после кэш-ставок арифметика сходится с суммой провайдера копейка в копейку.
- Fixes-on-the-fly: каталожные `input_cache_read/input_cache_write` никогда не разбирались — кэшированные токены шли по полной ставке промпта «допущением»; теперь обе кэш-ставки берутся из каталога.
- Next: плагинный режим оплачивается за результат и счётчика не сообщает — там полной цифрой остаётся сумма от провайдера.

---

## 2026-08-08 · Модель без tools уводит поиск на плагин (найдено живой проверкой)
- What: первая ступенька лесенки web_search дополнительно падает на 404 «No endpoints found that support tool use» — плагин ничего не требует от модели; остальные ProviderError по-прежнему наружу (снятая модель не гоняется по плагину).
- Why: живая проверка на dev с реальным ключом: gpt-5.4-mini → `server_tool` (ссылка на источник, $0.025), а ui-tars-1.5-7b умирал целиком вместо деградации — самый частый практический случай плагина.
- Files: router_mcp/adapters/openrouter.py, tests/test_web_search.py, AGENTS/SPEC.md
- Verified: живьём — ui-tars теперь `plugin`, в таймлайне обе ступеньки; pytest 334 прошло (давний test_mcp_tab_lists_tools_with_schemas падает и на чистом HEAD).
- Next: выкат на прод (`deploy/deploy.sh` требует sudo — запуск за оператором) и контрольный `chat` с `web_search=true` там.

---

## 2026-08-08 · Нормализованный `web_search` у OpenRouter
- What: булев `params.web_search` (текстовые задачи); внутри адаптера лесенка «серверный инструмент → плагин web → без поиска», ступенька только на `ProviderUnavailable`; 401 и отказ модели не маскируются. Флаг — в `request_json.params`, фактический режим — в таймлайне и в `web_search_mode` ответа `chat`; в консоли чекбокс (тип `boolean`).
- Why: боту нужен веб-поиск, а OpenRouter-специфика (словарь tools/plugins) не должна возвращаться в клиентов MCP — нормализованный флаг оставляет место другим поставщикам.
- Files: router_mcp/adapters/openrouter.py, router_mcp/web/capabilities.py, router_mcp/web/static/js/pages/run.js, router_mcp/mcp_server/tools.py, tests/test_web_search.py, tests/js/{run-pages.mjs,fixtures/capabilities.json}, AGENTS/SPEC.md
- Verified: pytest — 332 прошло (падает только давний `test_mcp_tab_lists_tools_with_schemas`, он падал и на чистом HEAD); node-харнесс зелёный, чекбокс рисуется. Живого запроса с ключом не было.
- Next: живая проверка на проде: `chat` с `web_search=true` через модель с поддержкой поиска, глазами посмотреть таймлайн.

---

## 2026-07-30 · Выбранная в каскаде модель доезжает до model_id
- What: семья с единственным вариантом заполняет `model_id` сама; id, подставленный каскадом, помечается и очищается при уходе с выбора, набранный руками — не трогается.
- Why: скриншот пользователя — «Выбрать самому», модель gpt-5.6-sol выбрана, а запрос ушёл с `model_id: null` и одной подсказкой поставщика, ответила gpt-5.4. `model_id` заполнялся только из «Варианта», а у codex/anthropic вариант всегда один («базовая») и его надо было ткнуть отдельно.
- Files: router_mcp/web/static/js/pages/run.js, tests/js/run-pages.mjs
- Verified: node-харнесс — выбор deepseek-v4-flash заполняет id, уход с выбора очищает; Playwright зелёный.
- Next: хвосты прежние — ставки Replicate, непроцененные seed-строки.

---

## 2026-07-30 · `ultra` — режим CLI, а не значение `reasoning.effort`
- What: разбор каталога codex-oauth фильтрует уровни рассуждения по тому, что реально принимает `/responses` (`none…max`, его собственный 400 их перечисляет); каталожный `ultra` («максимум рассуждения с автоделегированием») в форму больше не попадает.
- Why: живая проверка после деплоя ADR-0008 — запрос к `gpt-5.6-sol` с `reasoning_effort=ultra` вернул тот же `HTTP 400 invalid_value`, от которого всё началось (prod log #8664): каталог говорит от имени CLI, а не API.
- Files: router_mcp/adapters/codex_oauth.py, router_mcp/web/capabilities.py, tests/test_adapters_parse.py, docs/adr/0008-*.md
- Verified: prod после выката — `refresh?provider=codex-oauth` переписал возможности, `gpt-5.6-sol` с `max` ответил, дерево отдаёт уровни без `ultra`.
- Next: хвосты прежние — ставки Replicate, непроцененные seed-строки.

---

## 2026-07-30 · Синхронизация не воскрешает свёрнутое (найдено на проде)
- What: в браузном каталоге синхронизация обновляет только активные строки: снятую свёрткой не оживляет, дорога назад — явное принятие. Правка в одну строку в `_sync_one` плюс регрессионный тест.
- Why: живая проверка на проде. Свёртка сняла 363 строки OpenRouter, следующий же принудительный `refresh` вернул все: «замечен у поставщика — активен» осмысленно для зеркала и бессмысленно для рынка, который перечисляет всё и всегда.
- Files: router_mcp/registry/service.py, tests/test_registry_sync.py, docs/adr/0008-*.md, AGENTS/SPEC.md
- Verified: prod. Повторная свёртка после деплоя, затем `refresh?provider=openrouter`: seen=367, updated=4, offered=363, активных осталось 4.
- Next: хвосты прежние — ставки Replicate, непроцененные seed-строки.

---

## 2026-07-30 · Каталог спрашивают, а храним тот, который ответил (ADR-0008)
- What: подпись «— по умолчанию —» больше не уезжает в API значением параметра (`<option>` без `value` берёт значением свою подпись — отсюда `HTTP 400 Invalid value` на `reasoning.effort` у Codex). У codex-oauth появился каталог: `GET {CODEX_BASE_URL}/models?client_version=` отдаёт шесть текстовых моделей вместо одной засеянной и уровни рассуждения по каждой (пятёрка до `xhigh`, gpt-5.6 до `ultra`) — они хранятся возможностями и сужают список в форме, когда модель выбрана. `registry_models.confirmed_at` ставится успешной отправкой и заполняется из журнала миграцией. `catalog_scope='browse'` у OpenRouter: синхронизация обновляет известные строки и не вставляет новых; живой список, принятие и свёртка неподтверждённых доступны ручками и блоком «Спросить поставщика» на «Моделях».
- Why: две правки пользователя. Фраза-заглушка ломала каждый запрос с глубиной рассуждения, а реестр одновременно хранил лишнее (424 строки OpenRouter, использовано 5, и все 424 участвовали в автовыборе по цене) и недостаточное (у codex-oauth две строки, написанные руками).
- Files: router_mcp/{adapters/{base,openrouter,codex_oauth}.py, registry/{models,service}.py, storage/database.py, routing/router.py, web/{api,capabilities}.py, web/templates/models.html, web/static/js/pages/{models,run}.js, config.py, bootstrap.py, mcp_server/serialize.py}, .env.example, tests/{test_adapters_parse,test_registry_sync,test_registry_patch,test_fallback,test_migrations}.py, tests/js/run-pages.mjs, docs/adr/0008-*.md, AGENTS/SPEC.md
- Verified: dev, живьём. Каталог codex-oauth отвечает семью строками; `gpt-5.6-sol` с `reasoning_effort=xhigh` ответил «Астана», строка реестра получила `confirmed_at`, расход записан `unpriced`. Синхронизация OpenRouter на чистой базе: `seen=367, added=0, offered=367`. `pytest` 316 прошло, падает только `test_mcp_tab_lists_tools_with_schemas` — он падал и до правок.
- Next: деплой на прод и там же свёртка каталога OpenRouter до использованных (сначала `dry_run=true` глазами).

---

## 2026-07-26 · Деплой на прод, `blob.wildcar.org` и четыре дефекта живой проверки
- What: восемь волн выкачены на прод (`f8a94a2`). Миграция прошла на первом перезапуске: 8293 строки журнала получили `task`, колонки денег вышли байт в байт против резервной копии, сумма расхода не изменилась ($6.541389). Синхронизация каталога вылечила разметку OpenRouter — рисующих моделей 13 вместо 183, 172 vision-модели уехали в `text-to-text,image-to-text`. Поднят второй хост `blob.wildcar.org` (свой серверный блок в nginx, сертификат Let's Encrypt, редирект с 80), в прод `.env` добавлены `REPLICATE_MODELS`, `PUBLIC_BLOB_BASE_URL`, `PUBLIC_BLOB_SIGNING_KEY`.
- Why: пользователь попросил развернуть и настроить домен для передачи файлов провайдеру.
- Files: /opt/model-router-mcp (деплой), /etc/nginx/sites-available/model-router, /opt/model-router-mcp/.env, router_mcp/{web/api.py,routing/router.py,adapters/{openrouter,claude_api}.py}, deploy/nginx-model-router.conf, tests/{test_public_blobs,test_task_routing,test_adapters_parse}.py
- Fixes-on-the-fly: живая проверка нашла четыре дефекта, все закрыты и выкачены. Подписанная ссылка отдавалась с двумя противоречащими `Cache-Control` (приложение говорило `immutable`, nginx дописывал `no-store`). Запросные вложения сохранялись только первое и заново для уже лежащего в хранилище файла — прогон по трём моделям писал три лишние копии; правку «сохранять все» я записал в волне D в сообщение коммита, но не в код. Картинка не доезжала до OpenRouter и Anthropic: `image-to-text` был объявлен, а сборщик сообщений вложения не читал, и модель отвечала «Пришлите изображение». Сухой прогон говорил «не поддерживает модальность image» вместо «не умеет image-to-image».
- Verified: `flux-schnell` #8294 и `flux-kontext-pro` #8295 (исходник Replicate забрал снаружи по подписанной ссылке, видно в логе nginx с внешнего адреса), vision #8297 ответил «Утка», отказы #8298-8299 и три отказа проверки подписи. Строки 8294-8299 под `*-deploy-check` мои, не трафик.

## 2026-07-26 · Задача как ось, отдельная от группы тарификации (ADR-0006, ADR-0007)
- What: `domain/task.py` вводит `Media` и `Task` (словарь HuggingFace `pipeline_tag`) с подписью на каждую задачу; `Modality` остаётся группой тарификации и получает `audio` и `video`. Колонки `registry_models.tasks` и `request_logs.task` приезжают миграцией с обратным заполнением, `modality` не трогается. Все девять адаптеров объявляют `supported_tasks`, `SUPPORTED_PARAMS` ключуется задачей. codex-oauth научился тексту и vision через тот же `/responses` (модуль переименован в `codex_oauth.py`, засеяны обе модели), `image-to-image` он по-прежнему не умеет и честно отказывает. Новый провайдер Replicate: асинхронные предсказания, скачивание результатов в blob-store, оплата за секунду расчёта (`compute_price_per_second`), каталог по allowlist, входные файлы по подписанной ссылке (`web/public_blobs.py`). Восемь волн, каждая с зелёным прогоном.
- Why: одна пятизначная `Modality` тянула шесть ролей, и по ней нельзя было выразить «умеет нарисовать, но не умеет поправить» — с этого начался разговор про codex-oauth. Replicate это тот провайдер, у которого задача является свойством модели, а не поставщика.
- Files: router_mcp/domain/{task,modality,messages}.py, adapters/{base,replicate,codex_oauth,openrouter,deepseek,claude_api,claude_cli,codex_cli,deepgram,elevenlabs}.py, registry/{models,service}.py, storage/database.py, routing/router.py, accounting/cost.py, mcp_server/{tools,serialize}.py, web/{api,capabilities,labels,views,runs,public_blobs}.py, web/templates/{run,logs}.html, web/static/js/**, config.py, bootstrap.py, deploy/nginx-model-router.conf, .env.example, tests/{test_taxonomy,test_migrations,test_task_routing,test_task_api,test_adapters_replicate,test_public_blobs}.py + обновлённые, docs/adr/000{6,7}-*.md, AGENTS/SPEC.md
- Fixes-on-the-fly: `openrouter._extract_tasks` перестал помечать vision-модели как рисующие (они попадали в кандидаты `generate_image` с нулевой `image_price`); лестница подстановок сортируется по ставке своей группы, а не по сумме токенных, которая ноль у четырёх групп из пяти; строка с подтверждённой ставкой для одной группы больше не выдаёт уверенный ноль для другой; тесты ходили в живой ChatGPT, потому что `CODEX_AUTH_PATH` не чистился (это путь, а не ключ); форма прогона без выбранного поставщика показывает объединение параметров вместо пустого пересечения.
- Next: деплой на прод волнами, затем ставки за секунду расчёта для трёх моделей Replicate и DNS с сертификатом на `blob.wildcar.org`.

## 2026-07-26 · The entry says what was asked, not only what came back
- What: `request_logs.request_json` (migration, one column) stores a snapshot of the request when the row is opened — text and message history (cut at 4000 chars / 50 messages, truncation stated), params, the caller's model/tier/provider hint, the fallback policy, attachment metadata, idempotency key, batch; the `Run` envelope carries it as `request` from both constructors, the inspector gained a «Запрос» tab (prompt, roles, attachment player or download, «как спросили», params) and the JSON tab now has a block per side, as §10.4 of the concept always specified.
- Why: user feedback — the log detail had four tabs about the answer and nothing about the question, so «почему ответ такой» had to be guessed. A snapshot rather than a re-derivation: the registry, defaults and policy move, and a request rebuilt from today's settings is a different request.
- Files: router_mcp/storage/database.py, routing/router.py, web/views.py, web/static/js/components/inspector.js, web/static/app.css, tests/{test_web_views.py,js/run-pages.mjs,js/fixtures/{run-ok,log-detail}.json}, AGENTS/SPEC.md, docs/UI_CONCEPT_v2.md
- Fixes-on-the-fly: envelope `SCHEMA_VERSION` bumped to 2 (nothing validates it yet, but the field is documented as tracking the shape).
- Deployed: prod at `2dc723a` (2026-07-26 15:45 UTC). `request_json` arrived by migration on the first restart (43 columns); rows 8221-8223 under `request-tab-check` are mine — a `ModelRetired` failure that never reached a provider still carries its request, a `ResponseTruncated` one round-trips the message history, and the ok run (#8223, $0.0000161 on deepseek-v4-flash) carries params and the history. Entry #8218 from before the change shows its `request_text` and nulls, as designed.
- Next: nothing pending on this; retention numbers and the unpriced seed rows are still the open items.

## 2026-07-26 · Console wave 3: a run is a thing with an id, a timeline and a stop button
- What: in-process run registry (`POST/GET/DELETE /api/v1/runs`, 202 + polling, `client_request_id` idempotency checked in memory then against the log, `batch_id`, concurrency capped at three); the log row is opened when a run starts and closed when it ends, with `interrupted` recovery on restart and `cancelled` as an outcome carrying `cost_unknown`; CLI stdout read line by line into timeline steps through a ContextVar sink (`domain/progress.py`), claude switched to `--output-format stream-json` with its cache tokens normalized; `plan_json` stored for successful runs (capped, truncation stated); «Прогон» polls with a clock and «Прервать», gains the multi-model mode (chips, N runs per batch, a row each, confirmation above three); attachments by reference via `POST /api/v1/blobs`, which also gives «Повторить распознавание» in the log; retention policy, preview, purge, orphan collection and a nightly sweep, all inert until configured.
- Why: Wave 3 of `docs/UI_CONCEPT_v2.md`. A five-minute CLI run was invisible until it ended, could not be stopped, and left «выполняется» forever if the service restarted; nothing on disk was ever deleted.
- Files: router_mcp/{app,bootstrap,config}.py, domain/{progress,messages}.py, routing/router.py, storage/database.py, blobs/store.py, adapters/{_subprocess,codex_cli,claude_cli}.py, web/{runs,retention,api,views}.py, web/templates/{run,logs,overview}.html, web/static/{app.css,js/core/runs.js,js/pages/{run,logs,overview}.js}, tests/{test_runs,test_runs_api,test_cli,test_acceptance,test_fallback,test_registry_patch}.py, tests/js/*
- Fixes-on-the-fly: the cache-rate test added the session before left a deepseek row in the session-scoped database and broke the test that asserts deepseek contributes nothing without a key — it cleans up after itself now; `_terminate` closes the subprocess pipes so a cancelled run does not warn about a closed event loop.
- Deployed: prod at `d05b915` (2026-07-26 14:56 UTC). Verified there — migration added `batch_id`, claude's `run_cli` parses the stream-json output with cache tokens normalized (12354 = 2 + 7669 + 4683), and a run caught by a restart came back `interrupted` with `cost_unknown`. The wave-2 nginx delta turned out to be already applied.
- Next: decide the retention numbers (the code ships disabled); price the image/stt/tts seeds and the Anthropic catalog rows, cache rates included.

## 2026-07-26 · Real DeepSeek prices off the vendor page, cache rates editable
- What: `cache_read_price` / `cache_write_price` / `character_price` reachable through `PATCH` and the model card (they were columns from wave 2 that only SQL could set); real DeepSeek rates entered on prod from https://api-docs.deepseek.com/quick_start/pricing with `page` provenance — v4-pro 0.435/0.87 and cache read 0.003625 per 1M (was 0.27/1.10, copied from deepseek-chat), v4-flash 0.14/0.28/0.0028 (was unpriced).
- Why: the console reported $6.24 of DeepSeek spend over 30 days computed from prices belonging to a different model, and no cache rate meant cached tokens were charged at the full prompt rate.
- Files: router_mcp/web/api.py, web/static/js/pages/models.js, tests/test_registry_patch.py; prod registry rows via PATCH
- Fixes-on-the-fly: the first PATCH run silently left the cache rate at zero because the commit had not been pushed and `deploy.sh` pulled nothing — the deploy script reports the revision it moved to, worth reading.
- Next: wave 3 — run registry with progress and cancellation, line-by-line CLI output, `running`/`interrupted` recovery, multi-model mode, retention.

## 2026-07-26 · Console wave 2: honest money, operations, acceptance in a browser
- What: accounting stopped calling a missing rate a zero — `unpriced`, per-character TTS, cache/reasoning tokens at their own rates with the sum returned by its terms, notional subscription money kept out of real totals, and OpenRouter's own charge captured beside the registry figure; `GET /usage` groups spend five ways and splits the totals by price source with `is_lower_bound`, plus CSV export and the «Расход» tab; `GET /health/summary` and `POST /providers/{id}/probe` behind the gate with «Обзор» rebuilt on them; `GET /mcp/summary` with tool schemas and per-tool/per-client statistics from the log, and the MCP tab; «Модели» gained facet counts returned with the rows, mass deprecation with a listed confirmation and a working undo.
- Why: Wave 2 of `docs/UI_CONCEPT_v2.md`. §7 says the three accounting defects have to be fixed before the spend screen is drawn, or it shows confidently wrong numbers.
- Files: accounting/cost.py, registry/models.py, adapters/{base,openrouter,deepseek,claude_api,elevenlabs,claude_cli}.py, storage/database.py, config.py, bootstrap.py, web/{api,health,mcp_summary,views}.py, web/templates/*, web/static/js/pages/{overview,models,connections,logs,spend}.js, tests/{test_accounting,test_spend,test_health_mcp,test_acceptance}.py, tests/js/*, pyproject.toml
- Fixes-on-the-fly: the browser tests found the «Прогон» form showing no parameters at all when no provider is configured (the matrix was filtered to configured providers with no fallback); the node harness found a duplicate declaration and two helpers lost in a scripted refactor of the models page.
- Next: wave 3 — run registry with progress and cancellation, line-by-line CLI output, `running`/`interrupted` recovery, multi-model mode, retention.

## 2026-07-26 · Console wave 1: capabilities, dry run, five screens
- What: `/api/v1/capabilities` derives the parameter matrix from adapters' new `SUPPORTED_PARAMS` (ADR-0001 addendum) and serves the glossary from `web/labels.py` plus the server-fixed settings by env-var name; `POST /api/v1/plan` explains the routing without touching the network and answers even when nothing can run, with a verdict per rejected candidate; `GET /registry/tree` builds the picker cascade server-side; `PATCH /registry/models/{pair}` edits named columns only, guarded by a revision counter, with price protection re-keyed from row origin to price origin; five-item navigation with state in the address; «Прогон» generated from capabilities with two selection modes, fallback as one select and the estimate on the button; JSON viewer, and one inspector with a two-level timeline serving both «Прогон» and «Журнал».
- Why: Wave 1 of `docs/UI_CONCEPT_v2.md` — the console could show a result but not the path to it, and the form described fields no adapter read.
- Files: router_mcp/web/{capabilities,labels,api,views,pages}.py, web/templates/{base,run,models,logs,overview,connections}.html, web/static/{app.css,js/components/*,js/pages/*,js/core/format.js}, routing/router.py, registry/{service,models}.py, storage/database.py, adapters/*.py, domain/errors.py, docs/adr/{0001,0005}.md, tests/{test_capabilities,test_plan,test_registry_patch,test_pages_js,test_web}.py, tests/js/*
- Fixes-on-the-fly: the optimistic lock used `updated_at`, and two edits inside one second slipped through it — replaced by a `revision` column; sync's price guard was keyed on `source`, so a price typed into a catalog row was wiped by the next sync.
- Next: wave 2 — accounting (cache/reasoning tokens, `unpriced`, `provider_notional`), «Модели» with facets, «Обзор» health summary, the MCP tab, «Расход», Playwright.

## 2026-07-26 · Console wave 0: contracts under the UI
- What: `/api/v1` (bare `/api` kept for in-flight pages); one `Run` envelope built two ways in `web/views.py` with a key-parity test; typed error hierarchy (code + HTTP status + hint + `log_id` attached by the router) behind one handler, MCP tools get the same facts in the message; `request_logs` migration for channel/tool/client, HTTP attempts, provider status and request id, cache/reasoning tokens, `finished_at`; log list projected to table columns with cursor paging and filters, plus `GET /logs/{id}`; `params` reach `/api/tts` and `/api/cli`; blobs answer with a MIME whitelist, nosniff, immutable cache and byte ranges; `/health` trimmed to status; static skeleton (tokens.css, `core/*.js`, page ES modules, `?v=`), `app.js` and inline template scripts gone; `SUPPORTED_PARAMS` per adapter feeds both the payload and `ignored_params`.
- Why: The concept's wave 0 — without these the first UI wave ships on a shape that has to be rewritten by the third screen, and the console keeps hiding the path behind a result (latency, retries, error text and the failing log id all existed and never reached the eye).
- Files: router_mcp/domain/{errors,messages,__init__}.py, adapters/{base,openrouter,deepseek,claude_api,deepgram,elevenlabs,codex_image,codex_cli,claude_cli}.py, routing/router.py, registry/service.py, storage/database.py, mcp_server/tools.py, web/{api,views,pages}.py, web/templates/*, web/static/{app.css,css/tokens.css,js/**}, app.py, tests/{conftest,test_web_views,test_api_filters,test_blobs,test_escaping,test_web,test_modalities}.py, deploy/nginx-model-router.conf, AGENTS/SPEC.md, docs/UI_CONCEPT_v2.md
- Fixes-on-the-fly: the retry loop now records each try, so a 429 with the wait the provider asked for is data instead of a mystery in the latency; a read timeout logs `cost_source='cost_unknown'` rather than a confident zero; one app per test session (FastMCP's session manager runs once per process).
- Next: wave 1 — capabilities from `SUPPORTED_PARAMS`, `POST /plan`, the five-item navigation, the inspector with the two-level timeline.

## 2026-07-26 · Console UI concept v2 plus two P1 fixes it surfaced
- What: `docs/UI_CONCEPT.md` (v1, superseded) plus three reviews in `docs/reviews/` merged into `docs/UI_CONCEPT_v2.md`; fixed the two defects the reviews found in existing code - stored XSS in the console tables (`el()`/`row()` helpers, no `innerHTML` with data, regression test) and the missing nginx read timeout that answered 504 on long CLI runs.
- Why: The console showed the result and hid the path (plan, retries, raw JSON, latency, error text all exist in the data and never reached the screen); the two fixes are prerequisites for any UI work.
- Files: docs/UI_CONCEPT.md, docs/UI_CONCEPT_v2.md, docs/reviews/*, router_mcp/web/static/app.js, router_mcp/web/templates/{logs,registry,test}.html, tests/test_escaping.py, deploy/{nginx-model-router.conf,DEPLOY.md}, AGENTS/STATE.md
- Next: copy the nginx config to prod and reload; decide whether wave 0 of the concept starts now.

## 2026-07-25 · Registry freshness: sync, price provenance, fallback
- What: `ResponseTruncated` + logged `finish_reason`; per-provider `list_catalog()` with TTL/background/manual sync that deprecates (never deletes) retired models and stops seeds resurrecting them; price provenance fields with `cost_source='estimated'` for unconfirmed prices; per-request fallback policy (same/cheaper/pricier) with the substitution reported in the response and the log.
- Why: User feedback after the DeepSeek v4-pro switch: the registry served retired ids, a max_tokens cutoff read as "empty response", and the v4-pro price was copied from deepseek-chat.
- Files: adapters/{base,deepseek,claude_api,openrouter}.py, registry/{models,service}.py, routing/router.py, storage/database.py, accounting/cost.py, bootstrap.py, app.py, config.py, web/{api.py,templates/*}, mcp_server/*, tests/{test_fallback,test_registry_sync,test_web,test_smoke,test_adapters_parse}.py, AGENTS/SPEC.md, .env.example
- Fixes-on-the-fly: models from priceless catalogs stored zeros and looked cheapest — `price_is_unknown` now ranks them last and keeps them off the fallback ladder; `tests/test_web.py` cleared only the OpenRouter key, so the sync was calling the live DeepSeek API during tests.
- Next: deploy, refresh catalogs in prod, fill real DeepSeek prices in /registry.

## 2026-07-14 · Purge dead-family auth.json leftovers
- What: Deleted /opt/model-router-mcp/auth.json (+.bak) and repo-root auth.json — dead Jun 16 token family; nothing references them since the CODEX_AUTH_PATH repoint. Service healthy after removal.
- Why: User confirmed; dead OAuth credentials on disk are pure risk (ADR-0002 reuse-revocation).
- Files: host cleanup only; AGENTS/STATE.md
- Next: Phase 5 cont. — subscription/amortization UI; tune seed prices.

## 2026-07-14 · codex-cli live; codex-oauth shares the CLI grant
- What: `codex login --device-auth` done under modelrouter (verified via /api/cli); CODEX_AUTH_PATH repointed to that same auth.json (dev: keeper's ~/.codex); the image adapter now re-reads the file before refreshing so parallel writers stay on one rotation chain; stale "dedicated copy" docstring advice replaced.
- Why: One OAuth grant per client (openai-auth-sync ADR-0002); the /opt file held a dead Jun 16 token family.
- Files: router_mcp/adapters/codex_image.py, .env.example, AGENTS/{STATE,ENV,HISTORY}.md
- Next: delete dead auth.json leftovers (/opt auth.json + .bak, repo auth.json) after user confirms.

## 2026-07-14 · codex-cli infra + live `configured` flag
- What: codex 0.144.1 (vendored musl binary) installed into the service HOME and verified end-to-end via /api/cli; live `configured` field added to /api/registry/providers + «Доступен» column in the console; bootstrap comment fixed. Temporary auth.json copy removed — per openai-auth-sync ADR-0002 shared token families get revoked, each client logs in itself.
- Why: User asked to bring codex-cli up the same way and fix the misleading stale enabled flag.
- Files: router_mcp/web/api.py, router_mcp/web/templates/registry.html, router_mcp/bootstrap.py, tests/test_web.py, AGENTS/{SPEC,STATE,ENV}.md
- Next: user runs `codex login --device-auth` under modelrouter; re-auth the codex_image /opt token (dead family from Jun 16).

## 2026-07-14 · Prod claude-cli live (login + deploy + verify)
- What: Deployed 295fb28 (CLI_DEFAULT_CWD wiring); user completed the one-time subscription login and folder trust under modelrouter; verified /api/cli end-to-end (pwd → $HOME/workspace, usage, session_id, provider-reported cost).
- Why: Finish the prod claude-cli fix started the same day.
- Files: AGENTS/STATE.md (no code)
- Next: same install recipe for codex-cli; Phase 5 cont. — subscription/amortization UI.

## 2026-07-14 · Prod claude-cli: dedicated HOME + workspace cwd
- What: systemd drop-in (deploy/claude-cli.conf) gives the service HOME=/var/lib/model-router/home (+ReadWritePaths); isolated native claude 2.1.187 install inside it; new CLI_DEFAULT_CWD setting wired into both CLI adapters; prod runs in $HOME/workspace (also the cwd whitelist root).
- Why: ProtectHome=true masked /home — claude-cli was enabled in DB but undispatchable; runs must not execute in /opt (repo CLAUDE.md pickup, read-only fs, secrets nearby).
- Files: router_mcp/config.py, router_mcp/bootstrap.py, .env.example, deploy/claude-cli.conf, AGENTS/ENV.md, AGENTS/SPEC.md, AGENTS/STATE.md
- Next: one-time subscription login under modelrouter; live verify via /api/cli; same install recipe for codex-cli.

## 2026-07-14 · Mandate humanizer-ru skill for Russian prose
- What: Vendored https://github.com/smixs/humanizer-ru (SKILL.md + LICENSE) into .claude/skills/humanizer-ru/, un-ignored .claude/skills/ in .gitignore, added a mandatory Language Rule to AGENTS.md.
- Why: User request — every agent must run its Russian output through the humanizer-ru patterns.
- Files: .claude/skills/humanizer-ru/*, .gitignore, AGENTS.md, AGENTS/STATE.md
- Next: Phase 5 cont.; expose claude/codex binaries + auth to the prod service user (claude-cli currently unusable in prod despite enabled flag).

## 2026-06-02 · Fix — startup crash on masked service-user home
- What: resolve_executable now swallows OSError when probing ~/.nvm / candidate paths (PermissionError under systemd ProtectHome=true was crashing startup once /home/modelrouter/.nvm appeared). Added a regression test.
- Why: Prod deploy failed — service exited 3 with PermissionError on /home/modelrouter/.nvm/versions/node.
- Files: router_mcp/adapters/_subprocess.py, tests/test_cli.py
- Next: decide how to actually expose codex/claude to the prod service (PATH/ReadWritePaths vs ProtectHome).

## 2026-06-02 · Web UX — model picker + readable prices
- What: Test console gained a cascading model picker (provider→vendor→model→version, client-side from model_id) and now sends the provider hint; registry price table shows USD/1M tokens (uniform decimals, no exponents) with labelled Промпт/Ответ; manual-add form takes $/1M.
- Why: User: service provider wasn't selectable, OpenRouter list too long, prices shown in exponent form with no unit/label.
- Files: web/templates/test.html, web/templates/registry.html, web/static/app.js, web/static/app.css
- Next: Phase 5 cont. — subscription/amortization UI; tune seed prices.

## 2026-05-31 · Phase 4 — CLI engines (codex, claude)
- What: Added codex/claude CLI adapters (one-shot subprocess + JSON parsing), shared _subprocess helper with cwd whitelist, run_cli MCP tool + /api/cli, cli modality in the test console. Claude CLI total_cost_usd used as cost override.
- Why: Phase 4 — invoke local CLI agents alongside HTTP providers.
- Files: router_mcp/adapters/{_subprocess,codex_cli,claude_cli}.py, bootstrap.py, mcp_server/tools.py, web/{api.py,templates/test.html}, tests/test_cli.py
- Next: Phase 5 — registry admin polish, subscription/amortization UI.

## 2026-05-31 · Phase 3 — direct DeepSeek + Claude API adapters
- What: Added DeepSeek (OpenAI-compatible chat) and Claude (Anthropic Messages, raw httpx) adapters, seeded chat models with approximate prices; verified DeepSeek live end-to-end.
- Why: Phase 3 — direct provider connections beyond OpenRouter.
- Files: router_mcp/adapters/{deepseek,claude_api}.py, bootstrap.py, tests/test_adapters_parse.py
- Next: Phase 4 — CLI engines (codex via CodexRunner, claude).

## 2026-05-31 · Phase 2 — image / STT / TTS modalities
- What: Added Codex-image (OAuth), Deepgram (STT), ElevenLabs (TTS) adapters, seeded into the registry; MCP tools generate_image/transcribe/synthesize_speech; web API /api/image,/api/transcribe,/api/tts; test console modality UI; binary outputs via blob store.
- Why: Phase 2 — broaden modalities beyond chat.
- Files: router_mcp/adapters/{codex_image,deepgram,elevenlabs}.py, bootstrap.py, mcp_server/{tools,serialize}.py, web/{api.py,templates/test.html}, tests/test_modalities.py, tests/test_web.py
- Next: Phase 3 — direct DeepSeek + Claude API adapters.

## 2026-05-31 · Deploy wrapper — systemd + nginx/TLS + deploy.sh
- What: Added `/health`, optional `/mcp` Bearer auth (pure-ASGI, SSE-safe), and deploy artifacts (systemd unit, install.sh, deploy.sh, nginx conf, DEPLOY.md).
- Why: Run model-router-mcp as a managed, auto-restarting, HTTPS-exposed service on this host.
- Files: router_mcp/app.py, router_mcp/config.py, .env.example, deploy/*, docs/adr/0004-deployment-systemd-nginx.md, tests/test_auth.py, tests/test_web.py
- Next: Execute the install on the host; then Phase 2 (image/STT/TTS adapters).

## 2026-05-31 · Phase 0+1 — skeleton & OpenRouter chat slice
- What: Bootstrapped the project and shipped an end-to-end chat slice (OpenRouter → registry → router → cost → request_logs → MCP tools + web UI).
- Why: First iteration of the model-router MCP server per the approved plan.
- Files: router_mcp/* (config, domain, adapters, registry, routing, accounting, storage, blobs, mcp_server, web, app, main, bootstrap), tests/*, pyproject.toml, docs/adr/000{1,2,3}-*.md
- Next: Phase 2 — image (Codex), STT (Deepgram), TTS (ElevenLabs) adapters + tools.
