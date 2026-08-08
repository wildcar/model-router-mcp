# State

Current snapshot. Overwrite each iteration.

## Goal

Build an MCP server (Streamable HTTP) that routes requests to AI models across
providers and modalities, with a model registry, per-external-user request/response
logging, and per-request cost accounting, plus a web test+admin console — and run
it as a managed service on this host.

## Now

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
  Не проверено живьём: нужен запрос с реальным ключом OpenRouter.

## Next

- ✅ Развёрнуто на прод (2026-07-26 22:3x UTC, `f8a94a2`) и проверено живьём;
  подробности в верхней записи `HISTORY.md`. `blob.wildcar.org` поднят с
  сертификатом, Replicate работает по всем трём моделям, консоль не задета.
- Вбить ставки за секунду расчёта для трёх моделей Replicate: API их не
  публикует, цены на страницах моделей. Сейчас прогоны честно пишутся как
  «нет методики», а не как бесплатные — то есть расход по Replicate в отчётах
  занижен и помечен нижней границей.
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
