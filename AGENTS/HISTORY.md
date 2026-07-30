# History

Newest first. Each entry ≤5 lines using the format defined in `AGENTS.md`.

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
