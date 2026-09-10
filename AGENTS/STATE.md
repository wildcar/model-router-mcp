# State

Current snapshot. Overwrite each iteration.

## Goal

Build an MCP server (Streamable HTTP) that routes requests to AI models across
providers and modalities, with a model registry, per-external-user request/response
logging, and per-request cost accounting, plus a web test+admin console — and run
it as a managed service on this host.

## Now

- ✅ Codex image: журнал показывает, чем бэкенд рисовал на самом деле
  (2026-09-10). Проверка gpt-image-2.5 через codex-oauth показала, что бэкенд
  не читает `model`, `size` и `quality` инструмента image_generation:
  `gpt-image-2.5-flare`, `gpt-image-2.5` и выдуманный `gpt-image-9-nonexistent`
  нарисовали одинаково, а `response.completed` каждый раз вернул инструмент как
  `gpt-image-2-codex` / auto / auto. Размер и качество выбирает ведущая
  чат-модель по тексту промпта. Адаптер теперь дочитывает поток до
  `response.completed` (`_read_image_stream`, `DrawnImage`) и пишет в
  `usage.raw` и `raw_meta` поля `backend_image_model`, `actual_size`,
  `actual_quality` рядом с запрошенными `size`/`quality`/`main_model`; смена
  алиаса на стороне OpenAI станет видна в журнале сама. Живой прогон с dev
  auth.json: просили 1024×1536 medium, пришло 1536×1024 medium от
  gpt-image-2-codex. Комментарии в config.py, .env.example и SPEC говорят, что
  `CODEX_IMAGE_TOOL_MODEL` только имя строки реестра: переключать его на 2.5
  бессмысленно, строка `gpt-image-2` уйдёт в retired и закреплённые за ней
  вызовы упадут. Тесты: 494 зелёных без test_acceptance (2 новых в
  test_codex_main_model).
  Прод обновлён 2026-09-10 14:07 UTC (`95f79c7`). Живьём: картинка через
  gpt-image-2 под `codex-image-deploy-check`, строка 22771, `ok`, 48 с,
  `usage_raw`: size 1024×1536 medium запрошено, `backend_image_model`
  gpt-image-2-codex, `actual_size` 1024×1536, `actual_quality` medium. Строка
  моя проверочная, остаётся в журнале. Дальше: раз в несколько дней смотреть
  `backend_image_model` в свежих строках daypic, смена алиаса и будет сигналом,
  что Codex перевёл инструмент на Images 2.5.
- ✅ Codex image: ведущая модель из реестра, отказ бэкенда гасит строку
  (2026-09-08, ADR-0010). Утром 2026-09-08 бэкенд снял gpt-5.5 для
  ChatGPT-аккаунтов, а каталог `GET /models` продолжал его перечислять. Три
  запроса agentish-7 (строки 21752..21754) упали с HTTP 404 `model_not_found`
  про gpt-5.5, хотя просили gpt-image-2; `hint` пустой. Механизм ADR-0008 не
  сработал по определению - строка гаснет, только когда каталог убирает слаг.
  Оператор перевёл прод `.env` на `CODEX_IMAGE_MAIN_MODEL=gpt-5.6-sol` без
  деплоя. Постоянная починка в коде. Ведущая для картинки берётся из реестра:
  настроенный слаг ведёт, пока его строка активна, иначе ведут живые строки
  `text-to-text` провайдера, подтверждённые первыми. Ответ `/responses` с 404
  `model_not_found` или 400 «model is not supported» адаптер читает как «слаг
  снят», поднимает `ModelUnavailable` с именем слага и подсказкой и сразу гасит
  строку через `RegistryService.retire_model` с `retired_by=backend`. На пути
  картинки запрос повторяется один раз со следующей живой ведущей; в журнал
  идут `requested_model_id` (gpt-image-2) и `fallback_reason` с обеими
  ведущими, на таймлайн шаг `fallback`. Синк каталога не оживляет такую строку
  сутки (`held` в отчёте синка, колонка `retired_by` миграцией), потом верит
  каталогу снова. Дефолты `CODEX_IMAGE_MAIN_MODEL` и `CODEX_COMMAND` теперь
  gpt-5.6-sol. Тесты: test_codex_main_model (10), два в test_registry_sync;
  общий прогон 503 зелёных, 4 падения test_acceptance браузерные и были до
  правок (проверено на чистом HEAD).
  Прод обновлён 2026-09-08 07:19 UTC (`f2d7b40`). Живьём: чат на
  codex-oauth/gpt-5.5 под `codex-retire-check` - строка 21766, HTTP 409
  `model_unavailable`, `hint.retired=gpt-5.5`, строка реестра погасла в
  07:19:35; принудительный синк каталога после этого показал `held: 1`, строка
  осталась снятой. Картинка через gpt-image-2 под `codex-image-deploy-check` -
  строка 21767, `ok`, 41 с, `usage.images=1`, `usage.raw.main_model=gpt-5.6-sol`,
  PNG 1024×1536, то есть фраза «вертикальный кадр 2:3» с новой ведущей
  работает. Обе строки мои проверочные, остаются в журнале. Поле `retired_by`
  в API моделей появилось следующим коммитом.
  Что осталось: на стороне agentish запустить «Текст в картинку» руками и
  посмотреть daypic на ближайшем прогоне.
- ✅ Локальный провайдер `local` + модель `local/uvdoc` (2026-09-02, задачи 6–7
  постановки «Выпрямление страницы», ADR-0009): адаптер зовёт функцию в этом же
  процессе вместо HTTP — тот же `/api/v1/runs`, тот же конверт ошибок, результат
  блобом, строка `usage` с нулём как фактом (`computed`, не `unpriced`: сид с
  `image_price=0`, `price_source_kind=manual`, `tier=free`). Пул потоков +
  семафор `LOCAL_MAX_CONCURRENCY=2` (третий прогон ждёт, шаг «ждём свободный
  слот», цикл событий свободен), дедлайн `LOCAL_RUN_TIMEOUT_SECONDS=60` →
  `timeout` (504, retriable; поток дорабатывает и держит слот), нет весов →
  `model_not_loaded` (503, не retriable, подкласс NotConfigured), кадр > 50 МП
  → `image_too_large` (413, по заголовку файла, до декодирования). Веса —
  `fredcallagan/uvdoc-grid-onnx` (Apache-2.0): **31,8 МБ в двух файлах, а не
  ~8 МБ из постановки** (fp32-экспорт); качает `deploy/fetch-models.sh` с
  зашитыми sha256, его зовут `install.sh` и `deploy.sh`, они же ставят extra
  `.[local]` (CPU onnxruntime, opencv-headless, numpy — CUDA не тянут,
  `pip check` чист). У строк реестра появилась колонка `description`
  (миграция, PATCH, `model_to_dict`, карточка консоли с textarea); сид пишет
  `input_map`/`extra_params`/`description` только при вставке.
- 📏 Смоук `scripts/uvdoc_smoke.py` на этом хосте (4 ядра, 2 потока на сессию):
  загрузка модели 0,2 с; предсказание сетки ~0,45 с при любом размере входа;
  remap + кодирование: 287×398 — 5 мс, 1590×1482 — 0,14 с, 1600×1200 —
  0,13 с, 4000×3000 — 0,7 с (итого 1,3 с на 12 МП, память полосами по 256
  строк). Глазами: три реальные страницы из статьи UVDoc (лист под углом,
  страница с изгибом, журнал) выпрямлены — строки прямые, лист растянут на
  весь кадр, фон отрезан; синтетический лист с перспективой и изгибом —
  идеально. Разворот в футляре и уже плоский скан — лёгкий зум без вреда и
  без пользы. **Фото без бумаги (пейзаж) НЕ вернулось «как было»**: модель
  «нашла» лист и отдала обрезанный растянутый кадр (отклонение сетки от
  тождественной 0,10 против 0,03–0,06 у страниц). Записано в описание строки и
  SPEC §38: ИИшная должна сама проверять, что в кадре документ.
- ⚠️ Побочный эффект: `local/uvdoc` — единственная бесплатная строка
  `image-to-image`, и нехинтованный запрос этой задачи теперь уходит на неё.
  Приложения пинят `model_id`; если помешает — задать `DEFAULT_IMAGE_MODEL`.

- ✅ Phases 0–3 + deploy wrapper (deployed and running on the host).
- ✅ Phase 4: CLI engines — `codex` (`codex exec --json`, ported event parsing)
  and `claude` (`claude -p --output-format json`), via the `run_cli` MCP tool and
  `/api/cli`. Shared subprocess helper (`adapters/_subprocess.py`) with exec
  resolution (PATH + nvm/.local fallbacks) and a cwd whitelist
  (`CLI_ALLOWED_CWD_ROOTS`). Claude CLI's `total_cost_usd` is used as a cost
  override. Test console gained a `cli` modality + engine selector.
- ✅ Verified live: `claude` run_cli end-to-end (text + usage + provider cost +
  session_id). Both CLI providers enabled (binaries on PATH).
- ✅ Web UX (Phase 5 start): test console now has a cascading model picker
  (поставщик сервиса → вендор → модель → версия/вариант, derived client-side from
  `model_id`); the chosen provider is sent as a routing hint. Registry price table
  shows USD per 1M tokens (uniform 3-decimal `pricePerMillion`, no exponents) with
  labelled Промпт/Ответ columns; manual-add form takes $/1M and converts to
  per-token on submit.
- ✅ Fixed a startup crash: `resolve_executable` raised `PermissionError` while
  probing `~/.nvm` once that tree appeared under the (ProtectHome-masked) home of
  the `modelrouter` user; now guarded against `OSError`.
- ✅ Deployed to prod and healthy; web console serves the new picker + per-1M prices.
- ✅ Prod claude-cli live (2026-07-14): dedicated service HOME
  `/var/lib/model-router/home` (drop-in tracked as `deploy/claude-cli.conf`),
  isolated native claude 2.1.187 install inside it, one-time subscription login +
  folder trust done under `modelrouter`. Runs execute in `$HOME/workspace`
  (`CLI_DEFAULT_CWD`, also the only `CLI_ALLOWED_CWD_ROOTS` root). Verified
  end-to-end via `/api/cli`: text + usage + session_id; under subscription the CLI
  still reports a notional nonzero `total_cost_usd`, logged with
  `cost_source=provider`.
- ✅ Prod codex-cli live (2026-07-14): codex 0.144.1 (vendored musl binary from the
  npm package) in the service HOME; its own OAuth grant via
  `codex login --device-auth` under `modelrouter` (openai-auth-sync ADR-0002: one
  grant per client, never copy auth.json). Verified end-to-end via `/api/cli`.
- ✅ codex-oauth (image adapter) now shares the service codex CLI's
  `/var/lib/model-router/home/.codex/auth.json` via `CODEX_AUTH_PATH` (dev `.env`
  points at keeper's `~/.codex/auth.json`). One grant, one file: the adapter
  re-reads it per request and re-checks it before refreshing, so both writers stay
  on one rotation chain. The dead Jun-16-family files (`/opt` auth.json + .bak,
  repo-root auth.json) were deleted with user approval on 2026-07-14.
- ✅ Providers now expose live `configured` (adapter key/binary check) next to the
  `enabled` admin toggle: `/api/registry/providers` + «Доступен» column in the
  registry console. `enabled` is seeded from configured state on first insert only
  and never downgraded by restarts; dispatch re-checks `is_configured()` live.
- ✅ Registry freshness pack (2026-07-25, from user feedback after the DeepSeek
  v4-pro incident), four changes in the agreed order:
  1. Truncation is its own error: `ResponseTruncated` when a provider returns no
     content with `finish_reason=length` / `stop_reason=max_tokens`, message names
     the ceiling and the tokens produced. `request_logs.finish_reason` added.
  2. Per-provider catalog sync: `ProviderAdapter.list_catalog()` (OpenRouter with
     prices, DeepSeek and Anthropic ids-only), `RegistryService.sync_all/sync_provider`,
     background timer (`CATALOG_SYNC_INTERVAL_SECONDS`, default 6h) plus
     `POST /api/registry/refresh`. Missing models are deprecated, never deleted;
     seeds are insert-if-absent; the hardcoded `deepseek-chat`/`deepseek-reasoner`
     rows are gone.
  3. Price provenance: `price_source`/`price_source_kind`/`price_checked_at` on
     models, `pricing_url` on providers, unverified prices logged as
     `cost_source='estimated'` and marked in the console.
  3b. Guard found during live verification: a model from a priceless catalog
     stores zeros, which made it look like the cheapest row in the registry.
     `RegistryModel.price_is_unknown` now pushes such models last in automatic
     selection and keeps them off the fallback price ladder.
  4. Fallback policy per request: `{policy: same|cheaper|pricier, max_steps, scope}`
     on chat/image/tts/stt (API + MCP), substitutions reported via
     `requested_model_id` / `fallback_reason` in the response and the log.
- ✅ UI/UX concept for the console, v2 (2026-07-26): `docs/UI_CONCEPT.md` (first
  pass, superseded), three reviews in `docs/reviews/` (API/MCP, frontend
  architecture, unprepared operator), and `docs/UI_CONCEPT_v2.md` merging them.
  Five screens instead of seven, a glossary with visible field captions, a
  six-state money model, output-escaping rules, an in-process run registry with
  polling instead of SSE. Backend asks are split into wave 0..3 with contracts;
  nothing implemented yet.
- ✅ Two P1 defects found by the reviews, both fixed (2026-07-26):
  1. Stored XSS: the log/registry/test pages built rows by interpolating
     `external_user_id`, `request_text`, `model_id` and `fallback_reason` into
     `innerHTML`. `el()`/`row()` helpers in `static/app.js` now set text as text;
     `tests/test_escaping.py` fails the build if `innerHTML` is ever assigned
     anything but an empty literal. Verified by running the real template scripts
     against a stub DOM with `<img onerror>` payloads.
  2. nginx `location /` had no `proxy_read_timeout`, so `POST /api/cli` answered
     504 after 60s while the subprocess kept running (adapter limit 1800s). A
     dedicated `location ~ ^/api/(cli|runs)` gets 3600s with buffering off; the
     rest of the console gets 600s (image generation allows 180s plus a retry).
     Needs `cp` + `nginx -t` + reload on the prod host, see `deploy/DEPLOY.md`.
- ✅ Console wave 0 shipped (2026-07-26), everything in §21 of the concept except
  vendoring htmx/Alpine (deferred to wave 1, when the first fragment needs them):
  1. `/api/v1` is the contract; the bare `/api` prefix serves the same router so
     a page loaded before a restart keeps working. Lists answer
     `{items, total, next_cursor, facets}`.
  2. One `Run` envelope (`web/views.py`) built from a dispatch and from a log
     row, with `tests/test_web_views.py` failing on key drift.
  3. Typed errors: each class carries `code`, `http_status`, `retriable`, `hint`;
     the router attaches `log_id` before the exception leaves, one app handler
     renders the envelope, MCP tools append the same facts to the message.
  4. `request_logs` migration in one pass: channel/tool/client_id/
     client_request_id, attempts, provider_status, provider_request_id,
     session_id, return_code, cache/write/reasoning tokens, characters,
     plan_json, finished_at.
  5. Log reads: column projection, cursor paging by descending id, filters
     (user/status/modality/model/channel/period, with_fallback, with_retries,
     unverified_cost), `GET /logs/{id}` as a `Run`, indexes behind all of it.
  6. `params` reach `/api/tts` and `/api/cli` (TTS language, claude-cli model
     were unreachable from the web API).
  7. Blobs: inline only for image/audio, `nosniff`, immutable cache, byte ranges.
     `/health` trimmed to `{"status": "ok"}`.
  8. Static skeleton: `css/tokens.css`, `js/core/{api,dom,format,state,keys}.js`,
     page logic as ES modules in `js/pages/`, `?v=<version>` on every link.
     `static/app.js` and all inline template scripts are gone.
  9. `ProviderAdapter.SUPPORTED_PARAMS` feeds both the payload and the new
     `ignored_params` on a run; the retry loop records each HTTP try, so the
     two-level timeline has data; a read timeout logs `cost_source='cost_unknown'`.
- ✅ Console wave 1 shipped (2026-07-26), all nine items of §21:
  1. `/api/v1/capabilities` — parameter matrix from `ProviderAdapter.SUPPORTED_PARAMS`
     (appended to ADR-0001), per-provider model overrides, server-fixed settings by
     env-var name, and the glossary from the new `web/labels.py`.
  2. `POST /api/v1/plan` — never reaches the network (`get_models(allow_sync=False)`),
     always answers 200, and gives a verdict plus a reason for every rejected
     candidate. The estimate states its assumption and refuses to invent a zero.
  3. `GET /api/v1/registry/tree` — the picker cascade built server-side.
  4. `PATCH /api/v1/registry/models/{provider}/{model_id}` — named columns only,
     `revision` counter as the optimistic lock, price protection keyed on price
     provenance so a hand-entered price survives the next sync, `revert_price`.
  5. Five-item navigation (Обзор · Прогон · Модели · Журнал · Подключения) with
     filters, selection and `?log=` in the address; `/registry` redirects to `/models`.
  6. «Прогон»: form generated from capabilities, Авто / Выбрать самому, cascade from
     the server, `model_id` field reporting found/retired/provider-off as typed,
     fallback as one select, plan on a 300 ms debounce, estimate on the button,
     session feed, prompt draft, `?log=` deep link.
  7. JSON viewer (token nodes, never string replacement), and one inspector with four
     tabs and the two-level timeline, shared by «Прогон» and «Журнал».
  8. «Модели» with the price card: unit inside the field, previous value under it,
     confirmation on an order-of-magnitude change. «Подключения» carries the
     providers and the sync report; the MCP tab names itself as wave 2.
  9. ADR-0005 supersedes §18 of the concept: plain ES modules, no htmx/Alpine, no
     build step. Reasoning and reversal conditions are in the ADR.
- ✅ Console wave 2 shipped (2026-07-26), all eight items of §21:
  1. The three accounting defects of §7 are closed. A missing rate is `unpriced`,
     not zero; a zero somebody confirmed is still free (`price_is_unknown` and
     `is_free` turn on price provenance). TTS is billed per character.
     Cache/reasoning tokens have their own rates, with `Usage` carrying one
     documented contract every adapter normalizes to.
  2. `compute_cost` returns the sum by its terms, so the inspector can print the
     arithmetic. `provider_notional` keeps subscription money out of real totals
     (`CLAUDE_CLI_SUBSCRIPTION`); OpenRouter's own `usage.cost` is captured
     beside the registry figure as the staleness signal.
  3. `GET /api/v1/usage` groups five ways, splits the totals by price source,
     sets `is_lower_bound`, and exports CSV with the columns kept apart.
     «Расход» is a tab of «Журнал», with a sparkline and links into the log.
  4. `GET /api/v1/health/summary` behind the gate; «Обзор» is built on it —
     tiles, an actionable work list, first-run steps.
  5. `POST /api/v1/providers/{id}/probe` answers whether a key still works,
     cached ten minutes.
  6. `GET /api/v1/mcp/summary` — tool schemas joined with per-tool and
     per-client statistics from the log; MCP tab with a self-probe and a config
     snippet. No sessions screen (ADR-0003 makes it empty by construction).
  7. «Модели»: facet counts returned with the rows, mass deprecation with the
     affected models listed and a real undo behind the window.
  8. Twelve acceptance scenarios of §20 in a real Chromium
     (`tests/test_acceptance.py`, `pip install -e ".[e2e]"`), driving a uvicorn
     subprocess. They found the «Прогон» form rendering no parameters at all
     when no provider is configured.
- ✅ Console wave 3 shipped (2026-07-26), all seven items of §21:
  1. Run registry (`web/runs.py`) in process: `POST/GET/DELETE /api/v1/runs`,
     202 plus polling once a second, `client_request_id` idempotency (checked in
     memory and then against the log, so a key survives a restart), `batch_id`,
     and a concurrency cap of three (`MAX_CONCURRENT_RUNS`) — the answer to open
     question 5, chosen because one SQLite write lock serves everything.
  2. The log row is opened when a run starts and closed when it ends. A run in
     flight is visible to a second tab; a terminal failure closes the run's own
     row instead of adding a second one; an intermediate attempt failure that a
     fallback recovered from is still its own row, as before. Consequence worth
     knowing: the log is ordered by when a request *started*, so a recovered
     attempt failure sorts after the run that recovered from it.
  3. `interrupt_live_logs()` on startup turns leftover `running` rows into
     `interrupted` with `cost_unknown` — the provider may have billed the work.
     Cancellation does the same as an outcome, never a confident zero.
  4. CLI stdout is read line by line (`run_subprocess(on_line=…)`, 8 MiB line
     buffer) and each event becomes a timeline step through a ContextVar sink
     (`domain/progress.py`) — the same trick as the HTTP retry trace, because
     `generate()` is shared by eight adapters. claude switched to
     `--output-format stream-json --verbose`, with the single-object format
     still parsed; its cache tokens are now normalized like the API adapter's.
     Cancelling kills the subprocess.
  5. `plan_json` is stored for successful runs too (capped at 500 steps, the
     truncation stated in the timeline), so the inspector replays the same steps
     a week later.
  6. «Прогон» polls instead of waiting: elapsed clock, live timeline,
     «Прервать». Third selection mode «Несколько» — chips, N runs under one
     batch, a row per run, confirmation above three runs stating the money.
     Attachments go by reference (`POST /api/v1/blobs`), which is also what
     makes «Повторить распознавание» work from a log entry.
  7. Retention exists (`web/retention.py`): `LOG_RETENTION_DAYS` and
     `BLOB_RETENTION_DAYS`, both 0 = keep forever, a preview endpoint, a purge
     endpoint, orphan-file collection and a nightly sweep that does nothing
     until configured. The control sits on «Обзор» next to the size it explains.
- ✅ The request side of an entry (2026-07-26, from user feedback): the inspector
  had four tabs about the answer and nothing about the question. The `Run`
  envelope now carries `request` (text, message history with roles, params, the
  caller's model/tier/provider hint, the fallback policy, attachments,
  idempotency key, batch), stored as a snapshot in the new
  `request_logs.request_json` when the row is opened — so a run that failed
  before any provider was reached still says what was asked. The inspector gained
  a «Запрос» tab and a second JSON block; «Ответ» still opens first. Text is cut
  at 4000 characters, the history at 50 messages, both stated on screen. Rows
  written before the column show their `request_text` and nothing invented.
  Deployed and verified on prod (2026-07-26 15:45 UTC, `2dc723a`): the column
  came by migration on the first restart, a failure that never reached a provider
  keeps its request (#8221), a message history round-trips (#8222, #8223), and
  #8218 from before the change opens with its text and nulls. Those three rows
  under `request-tab-check` are verification, not traffic.
- ✅ Задача как ключ маршрутизации (ADR-0006, 2026-07-26). Одна `Modality`
  тянула шесть ролей сразу, и по ней нельзя было выразить «умеет нарисовать, но
  не умеет поправить» — с этого начался разговор про codex-oauth. Теперь две оси:
  `Media` (чем является полезная нагрузка) и `Task` (что просят сделать, словарём
  HuggingFace `pipeline_tag`, им же названы коллекции у Replicate). У каждой
  задачи статичная подпись в терминах сред, поэтому проверка запроса, сборка
  формы и работа с вложениями читают одну таблицу вместо ветки на задачу.
  `Modality` осталась вторым, грубым уровнем — группой тарификации, она же имя
  ручки; добавились `audio` и `video`. Бухгалтерия про задачи не знает вообще,
  поэтому исторические строки журнала считаются ровно как раньше. Восемь волн,
  каждая с зелёным прогоном:
  A. `domain/task.py`, `Modality` как группа тарификации, русские подписи.
  B. Колонки `registry_models.tasks` и `request_logs.task` миграцией с обратным
     заполнением; `modality` не переписывается. Проверено на копии живой базы:
     колонки денег выходят байт в байт. Плюс `compute_price_per_second`.
  C. Все девять адаптеров объявляют `supported_tasks`, `SUPPORTED_PARAMS`
     ключуется задачей, `supported_modalities` стало производным свойством.
     `RouterRequest.task` с `modality` как свойством над ним — из пятнадцати мест
     в router.py настоящих правок осталось шесть.
  D. Маршрутизация и лестница подстановок фильтруются задачей; `ladder_price`
     упорядочивает по ставке группы; `BILLING_UNITS` вместо словаря из четырёх
     ставок в `_estimate`.
  E. `/capabilities` до schema_version 2 (`tasks`, `params_by_task`, старый
     `params` живёт один выпуск), `task` на `/runs` и `/plan`, список
     `attachments`, инструмент `edit_image`, картинки на входе `chat`.
  F. Консоль: селектор задач с группировкой, поля из подписи задачи, текст и
     файл больше не взаимоисключающие, фильтры журнала из `/capabilities`.
  G. codex-oauth целиком: текст и vision через тот же `/responses`, две
     засеянные модели вместо одной, `size` и `quality` в параметрах.
     `image-to-image` он не умеет и получает внятный отказ.
  H. Новый провайдер Replicate (ADR-0007).
- ✅ Провайдер Replicate (2026-07-26). Первый асинхронный: создание предсказания
  не повторяется (кроме 429), опрос с отступом и отдельной, более редкой
  лестницей шагов в таймлайн, отмена отменяет и наверху. Результаты скачиваются
  в blob-store вторым клиентом без токена, потому что ссылки провайдера живут
  около часа. Секунда расчёта у провайдера — своя единица тарификации, своя
  колонка ставки и своя ветка в бухгалтерии. Каталог это allowlist
  `REPLICATE_MODELS`, у каждой модели читается её OpenAPI-схема: по ней
  раскладываются поля и проверяются значения. Цены приезжают `unknown`.
  Проверено живьём на dev: каталог из трёх моделей, схемы прочитаны, версии
  получены; flux-schnell отработал через `Prefer: wait`, 0.54 секунды расчёта,
  webp на 36 КБ осел в blob-store, строка журнала записана как `unpriced`.
- ✅ Входные файлы уходят провайдеру подписанной ссылкой с коротким сроком
  жизни (`web/public_blobs.py`, отдельный серверный блок в nginx). Без
  `PUBLIC_BLOB_SIGNING_KEY` путь не отвечает вообще: пропущенная настройка
  закрывает дверь, а не открывает.
- ✅ Четыре дефекта, найденных по дороге и закрытых там же: OpenRouter помечал
  каждую vision-модель каталога как рисующую (и она попадала в кандидаты
  `generate_image` с нулевой `image_price`); лестница подстановок для четырёх
  групп из пяти была отсортирована по `model_id`; строка с подтверждённой
  ставкой для одной группы выдавала уверенный ноль для другой; тесты ходили в
  живой ChatGPT, потому что `CODEX_AUTH_PATH` не вычищался — это путь, а не ключ.
- ✅ Тесты зелёные: `pytest` (298 прошло, было 173). Новые файлы:
  `test_taxonomy`, `test_migrations`, `test_task_routing`, `test_task_api`,
  `test_adapters_replicate` (33 проверки на MockTransport), `test_public_blobs`.
  Node-харнесс и пятнадцать сценариев Playwright тоже зелёные.
- ✅ Tests green: `pytest` (173 passed). Three harnesses: pytest for the service,
  `tests/js/` for the page modules under node against a stub DOM, and Playwright
  for the acceptance list. The last two skip when node or the browser is absent —
  the production host needs neither.
- ✅ Каталог спрашивают, а храним подтверждённое (ADR-0008, 2026-07-30, по двум
  правкам пользователя):
  1. Подпись «— по умолчанию —» уезжала в API как значение параметра: `<option>`
     без `value` берёт значением свою подпись, и бэкенд Codex отвечал
     `HTTP 400 Invalid value: '— по умолчанию —'` на `reasoning.effort`. У пустого
     варианта теперь пустое значение — и в блоке параметров, и в каскаде выбора.
  2. У codex-oauth есть каталог: `GET {CODEX_BASE_URL}/models?client_version=`
     (тот же, что кэширует codex CLI). Аккаунту доступны шесть текстовых моделей,
     а не одна засеянная: gpt-5.6-sol/terra/luna, gpt-5.5, gpt-5.4, gpt-5.4-mini.
     Оттуда же приезжают уровни рассуждения по каждой модели, и они разные —
     пятёрка до `xhigh`, gpt-5.6 до `max` (каталожный `ultra` — режим CLI,
     `/responses` его отвергает, отфильтрован на разборе). Хранятся возможностями
     (`effort:max`), отдаются на узле дерева, и выбранная модель сужает список
     в форме до своего.
  3. `registry_models.confirmed_at` — когда на этой паре пришёл ответ. Ставится
     один раз, только успешной отправкой, миграция заполняет из журнала.
  4. `catalog_scope`: у OpenRouter `browse` — синхронизация обновляет known-строки
     и не вставляет новых (было 424 строки, из них использовались 5, и все 424
     участвовали в автовыборе по цене). Новые ручки: живой список
     (`GET /registry/catalog/{provider}`, ничего не сохраняет), принятие
     (`.../adopt`) и свёртка неподтверждённых (`.../prune?dry_run=`), плюс блок
     «Спросить поставщика» на «Моделях».
  5. Проверено живьём на dev: каталог codex-oauth отвечает семью строками,
     `gpt-5.6-sol` с `reasoning_effort=xhigh` ответил «Астана», строка реестра
     получила `confirmed_at`, расход записан как `unpriced` (подписка ставок не
     публикует). Синхронизация OpenRouter на чистой базе: `seen=367, added=0,
     offered=367`.


- ✅ ADR-0008 на проде (2026-07-30). Миграция `confirmed_at` заполнилась из
  журнала на первом перезапуске; каталог codex-oauth подтянулся сам — все семь
  строк, `gpt-5.4` и `gpt-image-2` подтверждены. Каталог OpenRouter свёрнут:
  снято 363, активны 4 подтверждённых (`deepseek-v4-pro`, `gpt-5.4-mini`,
  `ui-tars-1.5-7b`, `openrouter/auto`). Живая проверка тут же нашла дефект:
  следующий принудительный `refresh` воскресил все 363 — «замечен у
  поставщика — активен» осмысленно для зеркала и бессмысленно для рынка.
  Исправлено (browse-синхронизация обновляет только активные строки), выкачено,
  свёртка повторена, повторный `refresh` даёт `updated=4, offered=363` и
  активных остаётся 4. Резервная копия базы до миграции:
  `data/router.db.bak-2026-07-30`.

- ✅ Нормализованный `web_search` (2026-08-08): булев `params.web_search` у
  OpenRouter на текстовых задачах. Внутри адаптера восстановлена лесенка из
  ort_bot: серверный инструмент `openrouter:web_search` → legacy-плагин
  `{"id": "web"}` → запрос без поиска; ступенька срабатывает только на
  временном отказе (`ProviderUnavailable`), 401 и отказ модели наружу как есть.
  Журнал хранит флаг в `request_json.params` и фактический режим в таймлайне;
  ответ MCP `chat` несёт `web_search_mode` (`server_tool`/`plugin`/`off`).
  В консоли параметр рисуется чекбоксом (новый тип `boolean` в матрице).
  Живая проверка на dev с реальным ключом: gpt-5.4-mini → `server_tool`
  (ответ со ссылкой, $0.025), ui-tars-1.5-7b → 404 «No endpoints found that
  support tool use» → уточнение: этот 404 тоже уводит на плагин (модель без
  tools — случай плагина, а не отказ), проверено живьём → `plugin`.
  Выкачено на прод (9af101a), контрольный прогон через прод-MCP: строка 10234,
  режим server_tool, флаг и режим в журнале.

- ✅ Цена веб-поиска в расходе (2026-08-08): прогон 10234 показал занижение —
  провайдер списал $0.028 против расчётных $0.011 (два поиска по $0.01 мимо
  токенных ставок). Добавлена `web_search_price` (за один вызов поиска, из
  каталожного `pricing.web_search`; синк и правка руками как у прочих ставок),
  `Usage.web_searches` из `server_tool_use_details.web_search_requests`,
  слагаемое «веб-поиск» в разбивке; без ставки итог помечается `estimated`.
  Заодно закрыт смежный разрыв: каталожные `input_cache_read`/`input_cache_write`
  не разбирались вовсе, кэш шёл по полной ставке промпта «допущением» — теперь
  расчётная сумма поискового прогона сходится с провайдерской копейка в копейку
  (прод, строка 10242 → пересчёт вручную $0.01465765 = `usage.cost`).
  Плагинный режим оплачивается за результат и счётчика не даёт — там полная
  цифра только у провайдера (`usage.cost`).

- ✅ Сверка с суммой поставщика (2026-08-08): `provider_cost_usd` — своя
  колонка журнала (OpenRouter отдаёт `usage.cost` в каждом ответе; миграция
  подняла его из JSON у старых строк). Фильтр `cost_mismatch` (>2% и >$0.0001,
  кроме provider/notional/amortized), метка `≠` в таблице журнала, сумма
  поставщика в инспекторе, счётчик расхождений за неделю на «Обзоре».
  DeepSeek per-request суммы не публикует (только `GET /user/balance` по
  аккаунту) — там точность держится каталожными ставками и точной кэш-разбивкой.

- ✅ Правки журнала и реестра по списку пользователя (2026-08-09):
  1. Свёртка каталога добирает мусор: снятая строка, которую ничто не
     подтверждало, никто не курировал и которой нет в журнале, удаляется насовсем
     (`PruneReport.deleted`; активные неподтверждённые по-прежнему только
     снимаются). Диалог в консоли называет оба действия и их счётчики;
     «сворачивать нечего» — только когда нет ни того, ни другого.
  2. Журнал: фильтр по пользователю ищет подстрокой без регистра («ort-bot»
     находит все ort-bot-XXXX), с подсказками из нового
     `GET /api/v1/logs/suggest?field=user|model|provider&q=` (значения из самого
     журнала, свежие первыми, закрытая карта колонок). Фильтры по поставщику
     (select) и модели (подстрока + datalist), фильтр по дню с листанием ◀ ▶
     (локальные сутки уезжают как since/until в хранимом формате `+00:00`).
  3. Таблица журнала: колонка «Поставщик» перед моделью; токены разбиты на
     четыре колонки Промпт/Кэш/Ответ/Рассужд. (строка с секундами/символами/
     изображениями показывает свою меру в первой); «Запрос» показывает слова, а
     не JSON — из списка сообщений берётся последняя user-реплика, полный JSON
     остаётся в тултипе. «Расход»: итог токенов остался, те же четыре вида —
     отдельными колонками.
  4. Стаб-DOM научился тексту между тегами (заголовки таблиц из статической
     разметки теперь проверяемы), `children` отдаёт только элементы, как в
     браузере; Jinja-комментарии вычищаются из шаблона до разбора.
  5. Найдено первым же взглядом на прод: браузер держал старый logs.js — `?v=`
     это `__version__`, навсегда 0.1.0. Версия статики теперь со штампом старта
     процесса, `/static` отдаётся с `Cache-Control: no-cache` (ревалидация
     чинит и незверсованные относительные import'ы внутри модулей); кнопки
     листания дня собраны в горизонтальный `.day-nav`.

- ✅ `params.web_search` на всех движках с поиском (2026-08-09): к OpenRouter
  добавились codex-oauth (родной инструмент `{"type": "web_search"}` в
  /responses — тот же, что включает `codex exec`; `web_search_mode:
  server_tool`; события `response.web_search_call.*` — шаги таймлайна),
  codex-cli (`-c tools.web_search=true`: флаг `--search` есть только у корневой
  команды, exec отвечает exit 2 — поймано живой проверкой) и claude-cli
  (`--allowedTools WebSearch`, явный вайтлист поверх skip-permissions).
  Лесенки нет — плагинная деградация специфична для OpenRouter. Подписки
  ставок поиска не публикуют, слагаемого в расходе не прибавилось. Живьём:
  codex-oauth и codex-cli на dev, claude-cli на проде (строка 10423,
  «инструмент: WebSearch» в таймлайне) — все назвали текущий день недели и
  свежую новость со ссылкой.
  Тесты: pytest 351 прошёл; `test_acceptance.py::test_mcp_tab_lists_tools_with_schemas`
  падает и на чистом `main` до правок — это существовавшая проблема, вынесена
  отдельной задачей. Node-харнесс зелёный целиком.

- ✅ codex-oauth принимает запрос из одних `messages` (2026-08-09): контракт
  `chat` разрешает «либо `text`, либо `messages`», deepseek так и отвечал, а
  codex-oauth считал `messages` только историей и падал с «Request carries
  neither text nor an image». Теперь при пустом `text` последний
  пользовательский ход поднимается из истории в содержимое `/responses`.
  Нашлось живой проверкой posinus: «Картина дня» на gpt-5.5 с поиском уходила в
  запасной шаблон при каждом прогоне.

- ✅ `image-to-image` без промпта и роли вложений (2026-08-09, задачи 1 и 2 от
  ИИшной): подпись задачи требовала текст, а адаптер отказывался слать его
  модели без prompt-свойства, так что удаление фона и апскейл были недоступны с
  обеих сторон. Теперь текст у `image-to-image` необязателен; модель без
  prompt-свойства получает такой текст в `ignored_params` и работает, а
  задача, которой текст обязателен (`text-to-image`), по-прежнему отказывает.
  Второе: у вложения появилась роль (`image`, `mask`, `face`, `garment`,
  `reference`, `style`) и строка реестра научилась карте `input_map`
  (`{"image": "human_img", "garment": "garm_img"}`). Позиционная привязка
  осталась для вложений без роли; нераспознанная роль это ошибка с перечнем
  настоящих входов модели, а не догадка. Маска идёт тем же путём, что и
  картинка, поэтому больше 256 КБ уезжает подписанной ссылкой. Синхронизация
  каталога `input_map` не трогает: что `garm_img` это одежда, знает только
  человек. Роли видно в инспекторе журнала, карта правится в карточке модели.
  Тесты: pytest 375 прошёл, node-харнесс зелёный.
  Живьём не проверялось: на проде нет ни одной модели с двумя картинками.

- ✅ Свои параметры модели в реестре (2026-08-09, задача 3 от ИИшной): у строки
  появился `extra_params`, список имён через запятую, который складывается с
  `SUPPORTED_PARAMS` адаптера только для этой модели. Ворота шире не стали:
  имени, которого нет в схеме модели, всё равно не уйдёт, значение по-прежнему
  проверяется по enum и диапазону, а сам список пишет человек. В
  `ignored_params` такой параметр больше не попадает, а `/registry/tree` отдаёт
  его форме «Прогон», так что поле рисуется само. Апскейлер теперь заводится
  правкой строки, а не адаптера.

- ✅ Публичная выдача файлов перестала молчать (2026-08-09, задача 4 от
  ИИшной): адаптер объявляет `inline_file_limit_bytes` (у Replicate это 256 КБ),
  и если при этом не заданы `PUBLIC_BLOB_BASE_URL` с `PUBLIC_BLOB_SIGNING_KEY`,
  об этом говорят «Обзор» (строка в списке дел), «Подключения» («настроен
  наполовину» вместо «рабочий») и `POST /plan` (поле `warnings` для задач с
  файлом). Обе переменные, не одна: без ключа ссылка не выдаётся вовсе.
  `deploy/DEPLOY.md` и `.env.example` называют их обязательными при включённом
  Replicate.
  На проде обе переменные заданы давно (`blob.wildcar.org`), так что там это
  чистая профилактика: предупреждение промолчит. А вот в dev-`.env` их нет, и
  консоль в деве честно скажет «настроен наполовину».

- ✅ Сухой прогон и журнал перестали расходиться в цене (2026-08-09): правило
  «своя ставка группы, иначе секунды расчёта» вынесено в
  `accounting.cost.billing_unit` и используется и `compute_cost`, и `_estimate`.
  До этого `POST /plan` для группы `image` смотрел только `image_price`, поэтому
  модель Replicate со ставкой за секунду расчёта читалась как «ставки нет», хотя
  журнал ту же модель считал нормально.

- ✅ Форма «Прогон» научилась ролям (2026-08-09): после выбора файлов рисуется
  строка на файл с выпадающим списком, но только когда файлов больше одного и
  задача берёт картинку. Роли предлагаются из `input_map` закреплённой модели,
  первый файл предугадан как основное изображение. Слова в одном месте
  (`web/labels.ATTACHMENT_ROLES`), карточка модели читает оттуда же.

- ✅ Примерочная на проде (2026-08-09): `cuuupid/idm-vton` добавлен в
  `REPLICATE_MODELS` прод-`.env` (бэкап рядом), `input_map` =
  `{image: human_img, garment: garm_img, mask: mask_img}`, `extra_params` =
  `category, garment_des, steps, crop, seed`. Один прогон закрыл три
  задачи сразу (строка 10575, predict_time 16 с):
  задача 1 — текст был отправлен, prompt-свойства у модели нет,
  прогон всё равно прошёл (раньше была бы ProviderError);
  задача 2 — человек и одежда сели на свои входы, на результате та
  же девушка в красном худи вместо белой футболки;
  задача 3 — `category`, `garment_des`, `steps` ушли в модель,
  `ignored_params` пустой (без аллоулиста все три туда бы попали).
  Исходники сгенерированы тем же роутером через flux-schnell.
  Цена вбита (2026-08-09): страница называет $0.024 за прогон, а колонка в
  реестре хранит ставку за секунду расчёта. Со страницы же взяты железо
  (A100 80GB) и типичная длительность (17 с), отсюда 0.024 / 17 = 0.0014 $/с,
  происхождение `page`, арифметика записана в `price_source`. Наш прогон на
  16.043 с считается как $0.0225, `cost_source=computed`.
  Следом закрыты остальные три строки Replicate (2026-08-09), у каждой своя
  форма цены: flux-schnell $3 за 1000 картинок = 0.003 за штуку,
  flux-kontext-pro 0.04 за картинку — обе за единицу, а не за время;
  musicgen $0.065 / 47 с = 0.0014 $/с. Последнее сошлось с примерочной до
  четвёртого знака (0.0013830 против 0.0014118), то есть A100 80GB у
  Replicate стоит 0.0014 $/с, и это уже не одиночный замер. Ни одной
  строки Replicate без цены не осталось.
- ✅ TTS переключён с `eleven_flash_v2_5` на `eleven_multilingual_v2` (2026-08-11):
  дефолты в `config.py` и адаптере, оба `.env`; flash-строка реестра
  депрекирована с `replaced_by`. Причина — качество русской озвучки: flash
  сделан под латентность 75 мс, multilingual v2 у ElevenLabs старшая по
  качеству модель. A/B-прогон 9 фраз-ловушек (омографы, числительные,
  казахские имена/топонимы) собран для сверки ушами. Замечено: `language_code`
  по докам поддерживают только flash/turbo, но API принял его и у
  multilingual_v2 (HTTP 200) — адаптер не трогали. Плата за это: латентность
  ~0.2 с → ~1.1 с на фразу, кредиты ×2 (1 кредит/символ против 0.5); ставка
  за символ в реестре как не была вбита, так и не вбита.
- ✅ Провайдер Yandex SpeechKit (TTS v1) добавлен (2026-08-11): адаптер
  `adapters/yandex.py` (form-POST `tts:synthesize`, Api-Key, oggopus; язык
  ru/kk/en/uz/de/he маппится в локаль, лимит 5000 символов), сид
  `yandex/speechkit-tts-v1` (unpriced, expensive). Ключа пока нет — провайдер
  сидится выключенным; после вписывания ключа нужен рестарт + PATCH
  `/api/registry/providers/yandex {"enabled": true}`. ElevenLabs остаётся
  дефолтом TTS: при двух беспрайсовых строках сортировка падает до `model_id`,
  и `eleven…` < `speechkit…` — Yandex зовётся хинтом `provider=yandex`.
  Голос по умолчанию `marina`; список голосов не сверен с доками (капча),
  ждём файл-клиппинг от оператора.
- ✅ SpeechKit включён на проде и проверен живьём (2026-08-11): ключ с ролью
  `ai.editor` вписан оператором только в прод-`.env` (dev пуст), после
  рестарта `configured=true`, PATCH enabled=true. Русский (marina, 146–363 мс
  на фразу) и казахский (amira, kk-KK) синтезируются через `/api/tts` с
  хинтом `provider=yandex`; дефолт TTS не сместился — без хинта роутер
  по-прежнему берёт `eleven_multilingual_v2`. Все 9 A/B-фраз прогнаны через
  прод, третья колонка на странице прослушивания. Ставка за символ не вбита —
  прогоны `unpriced`.
- ✅ Голоса и движки TTS выбираются через API (2026-08-11). `voice` у ElevenLabs
  принимает и имя («Nadia» — резолв через /v2/voices с кэшем), и voice_id
  (шаблон: 16–40 alnum). Оба движка ElevenLabs — активные строки реестра
  (flash tier=cheap $0.00018/символ, multilingual tier=expensive
  $0.00036/символ; арифметика со страницы цен: доп. минута Free $0.36 ≈ 1000
  кредитов), выбор движка — `model_id` в запросе. Контракт роутинга уточнён:
  `DEFAULT_*_MODEL` побеждает скан, когда в запросе нет ни одного хинта
  (SPEC п.4, тесты в test_task_routing). DEFAULT_TTS_MODEL вбит и в dev, и на
  проде (оператор поправил .env 2026-08-11); flash на проде восстановлен,
  проверено живьём: без хинтов — multilingual ($0.00036/симв., computed),
  `model_id=eleven_flash_v2_5` — flash. Учтено: у `/api/tts` нет поля `tier`
  (в отличие от chat), ярусный хинт для TTS доступен только через MCP,
  если инструмент его принимает. Цена Free-плана —
  оценка сверху: первые 10k кредитов/мес по факту бесплатны. SpeechKit вбит
  (2026-08-11) со страницы тарифов, принесённой оператором: API v1
  $10.99999824/1М символов без НДС → $0.000011/символ, tier=cheap — в ~33 раза
  дешевле multilingual_v2. Проверено на проде: 44 символа → $0.000484
  computed.
- ✅ SpeechKit переведён на TTS API v3 (2026-08-11, по решению оператора — без
  выбора версий): адаптер шлёт `utteranceSynthesis` (JSON, hints voice/role/
  speed, `unsafeMode` от 250 символов, сборка NDJSON-чанков), реестр —
  `speechkit-tts-v3` ($0.0000053311/символ = $0.0013327867 за блок 250),
  v1-строка депрекирована с `replaced_by`. Учёт честный: `usage.characters`
  округляется вверх до блока (фактическая длина в `raw`). Роли по голосам —
  `docs/speechkit-voices.md` (из клиппинга оператора); в v3 языкового
  параметра нет, язык определяется голосом. Параметр `emotion` в консоли
  заменён на `role`.
- ✅ SpeechKit — дефолт TTS (2026-08-11, решение оператора: читает русский
  лучше, особенно числительные, и дешевле при любой длине).
  `DEFAULT_TTS_MODEL=speechkit-tts-v3` в dev/.env.example/прод (прод — рукой
  оператора). Проверено: без хинтов — speechkit-tts-v3 computed. Нюанс:
  `provider=elevenlabs` без model_id даёт flash (хинт отключает дефолт, скан
  предпочитает дешёвый ярус) — качество ElevenLabs только по явному
  `model_id=eleven_multilingual_v2`.

- ✅ Модели картинок для agentish (SPEC agentish §5.2–§5.6) заведены на проде
  (2026-08-11): 11 manual-строк Replicate, все `image-to-image`, все с ценами
  (`page`, у bria/eraser — `estimate`: Replicate свою цену не публикует), все
  подтверждены живыми прогонами через `/api/v1/runs` (фото 1024px PNG >1МБ —
  путь подписанной ссылки). Обязательные: 851-labs/background-remover
  (T4 0.000225 $/с; текст → `ignored_params`), recraft-crisp-upscale
  ($0.004/шт, выход ×4), real-esrgan (T4; `scale=2` проверен, чужой параметр
  отброшен), clarity-upscaler (0.0011667 $/с), flux-fill-pro ($0.05/шт — за
  картинку, не за МП; image+mask по ролям), cdingram/face-swap (0.0014 $/с,
  `input_map` image→input_image, face→swap_image). Опциональные: kontext-max
  $0.08, nano-banana-2 $0.08 (4K=$0.16, ставка одна — учёт 4K занижен вдвое),
  seedream-4 $0.03, qwen-image-edit $0.03, bria/eraser (маска без промпта).
  Все четыре редактора выполнили русские инструкции без перевода («Убери
  солнце с неба» и др.) — agentish может убирать перевод-костыль минимум для
  nano-banana-2.
- Замена по §5.6: easel/advanced-face-swap с Replicate снят (API 404, в
  коллекции face-swap его нет; у easel остался только ai-avatars) —
  cdingram/face-swap: одно лицо, `hair_source`/второго лица нет.
- Ограничение real-esrgan: вход капится ~2.1М пикселей (≈1448×1448) — фото
  agentish, нормализованные до 2048px, не пролезают (проверено: 4096-вход дал
  ProviderError с внятным текстом). Клиенту нужен даунскейл до ≤1440p перед
  этой моделью; crisp/clarity берут 1024–2048 нормально.
- Три сопутствующих фикса кодом: (1) синк каталога больше не депрекирует
  manual-строки (иначе allowlist-каталог Replicate снимал их через 6 часов;
  проверено принудительным `refresh` на проде — 11 строк живы); (2) массивные
  файловые входы: `image_input` (nano-banana-2, seedream-4) — список URL, а не
  строка; (3) `raw_meta.dropped_params` адаптера вливается в `ignored_params`
  конверта — клиент видит, что текст к безпромптовой модели не дошёл.
- Список моделей для клиента (agentish, выбор модели по задаче):
  `GET /api/v1/registry/models?task=image-to-image&state=active` объявлен
  клиентским контрактом (SPEC «Registry edits», ключи `model_to_dict` запинены
  тестом): роли вложений клиент читает ключами `input_map`, разрешённые ручки —
  `extra_params`.

- ✅ Пиковые часы в ценах (2026-08-16, требование 35). DeepSeek с 16:00 UTC
  берёт двойной тариф в 01:00–04:00 и 06:00–10:00 UTC, а в реестре на ставку
  одно число: пиковая цифра завышала бы 17 часов в сутки, внепиковая занижала
  бы 7. Строка теперь несёт базовую ставку плюс окна, в которых всё умножается
  (`registry_models.price_schedule`, JSON, окна в UTC, полуинтервал, переход
  через полночь допустим). `RegistryModel.priced_at(момент)` отдаёт строку по
  действующим ставкам — и саму себя для всех остальных строк, у которых цена
  круглосуточная. Учёт считает по времени старта прогона, прикидка «Прогона» и
  лестница подмены — по «сейчас», так что «дешевле» сравнивает то, что реально
  спишется. Множитель едет вместе с суммой (`CostResult.price_factor`, блок
  `cost` конверта, шаг таймлайна) — иначе арифметика в журнале противоречит
  таблице цен без объяснений. Колонку пишет только человек: синк её не трогает,
  как `input_map`; введённое расписание считается ручной ценой.
- ✅ Цены DeepSeek заведены по объявлению от 2026-08-16 (базовые, внепиковые,
  $/1M): v4-flash 0.22 / 0.66, кэш-хит 0.007; v4-pro 0.66 / 1.98, кэш-хит
  0.022; обе строки — `page`, множитель ×2. Ярус v4-pro поднялся до
  `expensive` (2.64 $/1M против порога 1.0).
- `deepseek-chat` и `deepseek-reasoner` расписания не получили и не должны:
  таких моделей у поставщика больше нет, синк снял обе строки 2026-07-25.
  Лежат депрекированными с ценами того времени, из маршрутизации и автоподбора
  исключены. Не удаляем: обе подтверждены ответами, на них смотрит журнал
  (ADR-0008).

- ✅ Идентификация приложения (2026-08-26, требование 36). У каждого
  диспетчеризующего входа (MCP-инструменты, /api/chat|image|tts|transcribe|cli,
  /api/v1/runs) появились `app_url` и `app_name`: сайт приложения — главная
  идентификация, название — запасная. Действующая идентичность
  (`app_url or app_name`) пишется в `request_logs.app`, обе половины — в
  снапшот запроса; конверт `Run` несёт `app` (schema_version 3). Журнал:
  колонка «Приложение», фильтр подстрокой (`GET /logs?app=`), подсказки
  (`suggest?field=app`), группировка расхода `group_by=app`. Провайдерам с
  таким параметром идентичность транслируется — сегодня это только OpenRouter:
  `HTTP-Referer` ← app_url, `X-Title` ← app_name, на каждый запрос (раньше в
  клиенте были зашиты `https://localhost` / `model-router-mcp`). Без
  идентификации запрос уходит от самого роутера: `SELF_APP_URL`
  (https://mr.wildcar.org) + `SELF_APP_NAME`; запрос с одним URL не получает
  нашего X-Title — чужое приложение не подписывается нашим именем. Кириллица
  в X-Title транслитерируется («Проверка идентификации» → Proverka
  identifikatsii, включая казахские буквы): первая версия слала проценты, и
  дашборд OpenRouter показал их как есть — %D0%9F… (замечено оператором
  2026-08-26). Не-кириллица без таблицы и кириллические URL в HTTP-Referer
  остаются в процентах — для URL это родная запись. Тесты: test_app_identity
  (16 проверок), общий прогон 456; попутно починен test_health_mcp —
  захардкоженная дата выпала из окна 720 часов. Сверено глазами оператора на
  дашборде OpenRouter (2026-08-26): «Proverka identifikatsii» и
  https://mr.wildcar.org читаются; прогоны 16431 (процентная версия) и 16432 —
  проверочные, остаются в журнале.

- ✅ meta/muse-image заведён, адаптер OpenRouter научился ручке /images
  (2026-08-26, требование 37, просьба пользователя). Модель скрытая: страница
  на сайте есть, в GET /models её нет, а /chat/completions отвечает 404
  «Use the /api/v1/images endpoint». Заведена manual-строкой (openrouter,
  задачи text-to-image + image-to-image, capability `images_api`); адаптер
  ходит на POST /images (prompt + референсы в `input_references`, ответ
  base64), непомеченная строка уезжает туда по 404-подсказке провайдера,
  обычный 404 по-прежнему «модели нет». Адаптер объявил image-to-image;
  матрица параметров: aspect_ratio/resolution/n/quality/output_format/seed.
  Проверено на проде: генерация (строка 16436, 8.9 с) и правка той же
  картинки «сделай небо звёздным» (16437, референс сел в input_references,
  закат честно стал звёздной ночью). Цена вбита замером: провайдер списал
  $0.01 за картинку → image_price=0.01 (manual; каталожная ставка
  $0.0000024/токен × 767 токенов дала бы $0.0018 — списанию не верить
  нельзя). Строка 16437 посчиталась computed $0.01 = провайдерская сумма.
  Строки 16434 (404 до фикса), 16436, 16437 под `muse-image-check` — мои
  проверки, остаются. Тесты: test_openrouter_images (6), общий прогон 462.

## Next

- Задачи 8–9 в `agentish`: строка `local/uvdoc` уже в реестре роутера — нужны
  назначение в админке и вызов через `router-client`; проверка «в кадре
  документ» на стороне ИИшной (см. смоук). После закрытия — SPEC agentish
  §4.6 и §5.13 и список «Что нужно доработать в model-router».
- ✅ Прод обновлён 2026-09-02 19:32 UTC (`3181508`): `deploy.sh` поставил
  `.[local]` (`pip check` чист, CUDA нет), скачал веса в
  `/opt/model-router-mcp/data/models/uvdoc/`, сервис здоров, память процесса
  после двух прогонов 181 МБ. Живьём через `/api/v1/runs`: строки 19393
  (1590×1482 JPEG, 0,84 с расчёта с загрузкой модели) и 19394 (285×398,
  0,49 с) под `uvdoc-deploy-check` — `cost 0 computed`, `ignored_params:
  [prompt]`, JPEG→JPEG, usage 2 запроса / $0. Это мои проверочные строки, они
  остаются в журнале.

- ✅ Развёрнуто на прод (2026-07-26 22:3x UTC, `f8a94a2`) и проверено живьём;
  подробности в верхней записи `HISTORY.md`. `blob.wildcar.org` поднят с
  сертификатом, Replicate работает по всем трём моделям, консоль не задета.
- Исторические прогоны Replicate пересчитать нельзя: строка журнала хранит ту
  сумму, с которой её записали. Первые прогоны примерочной и генерации
  исходников остались с нулём и `unpriced`; с ценами считаются только новые.
- Проверить `image-to-image` из самой консоли на большом файле: механика
  подписанной ссылки проверена через API, но браузерная загрузка > 256 КБ
  ещё не прогонялась руками.
- Deploy the registry-freshness pack to prod, run `POST /api/registry/refresh`
  there, and fill real prices for the live DeepSeek models in `/registry`
  (they arrive from the catalog with `price_source_kind='unknown'`).
- ✅ Waves 0-2 deployed to prod (2026-07-26). DeepSeek prices corrected from the
  vendor page with `page` provenance: v4-pro 0.435/0.87 + cache read 0.003625
  per 1M (it had been carrying deepseek-chat's 0.27/1.10), v4-flash
  0.14/0.28/0.0028 (was unpriced). Historical rows keep what they were logged
  with and cannot be recomputed — before wave 2 the cache split was not
  recorded, so the 30-day DeepSeek figure of $6.24 is only known to lie between
  ~$2.40 (all cache hits) and ~$7.61 (all misses).
- Still unpriced on prod: the image, stt and tts seed rows, and Anthropic's
  catalog rows. The «Обзор» work list names them. Anthropic cache rates are
  worth entering for the same reason as DeepSeek's.
- ✅ Wave 3 deployed (2026-07-26 14:56 UTC, `d05b915`) and verified on prod:
  the `batch_id` column and the `client_request_id` index arrived by migration
  on the first restart; `run_cli` through claude parses the new
  `stream-json` output (steps arrived live, cache tokens normalized —
  prompt 12354 = 2 input + 7669 cache read + 4683 cache write, cost
  `provider_notional`); a run deliberately caught by a restart came back as
  `interrupted` with `cost_unknown` and its subprocess died with the cgroup.
  The verification left rows 8219 (ok, notional $0.051) and 8220 (interrupted)
  under `wave3-deploy-check` / `wave3-interrupt-check` — mine, not traffic.
- The nginx delta from wave 2 turns out to be already applied: the live config
  carries `location ~ ^/api/(v1/)?(cli|runs)` with the 3600s timeouts. Nothing
  left to do there.
- Decide the retention policy itself. The code is there and disabled: with
  `LOG_RETENTION_DAYS`/`BLOB_RETENTION_DAYS` unset nothing is ever deleted,
  exactly as before. The log is the only record of spend, so the number is a
  decision, not a default — the files are the bulk of the bytes and can go
  earlier than the rows that name them.
- Phase 5 (cont.): subscription/amortization UI; tune seed prices.
- Phase 6: hardening, optional streaming for long CLI/chat runs.

## Open questions

- Subdomain + TLS cert name for nginx (placeholder `router.example.com`).
- Real end-to-end chat still needs a live `OPENROUTER_API_KEY` (chain proven via
  stub tests).

## Deferred

- Automated update triggers (git-poll timer / inotify `.path` / webhook) — for now
  updates are the manual `deploy.sh`.
- Streaming MCP responses (CLI/long chat); full billing/amortization rollups;
  multi-connection concurrency.
