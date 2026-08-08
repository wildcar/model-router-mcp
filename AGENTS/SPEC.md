# model-router-mcp — functional & technical specification

Source of truth for *what the product does* and *how it is built*. Keep in sync
with reality (update before code when the contract changes).

## Purpose

An MCP server that routes a request to an appropriate AI model and returns the
response, across multiple *tasks* (text-to-text, image-to-text, text-to-image,
image-to-image, speech-to-text, text-to-speech, text-to-audio, text-to-video,
image-to-video, cli) and multiple providers. It maintains a registry of known/connected models
(service-provider → model-provider → model) with cost tiers, logs every request
and response keyed by an *external* user id (users are managed elsewhere; only the
id arrives, for per-user statistics), and computes per-request cost from registry
prices (with a hook for monthly-subscription amortization). Besides HTTP/API
providers it can invoke `claude`/`codex` via CLI. A web console allows testing the
service and administering the registry.

## Stack

- Python 3.11+ (developed/tested on 3.12).
- MCP: official `mcp` SDK (FastMCP) over **Streamable HTTP**.
- Web/server: FastAPI + uvicorn; Jinja2 templates + vanilla JS for the UI.
- HTTP client: httpx (async). Config: pydantic-settings. Storage: aiosqlite (raw SQL).
- Deployment target: a single long-running ASGI process.

## Architecture

```
MCP client ─┐                         ┌─ adapters/* (one per provider):
            ├─→ FastAPI app ──────────┤    openrouter (chat+image), deepseek &
Web browser ┘   ├─ /mcp  (FastMCP …)  │    claude_api (chat), codex_oauth (text/vision/image),
                │                     │    replicate (many tasks, async),
                │                     │    deepgram (stt), elevenlabs (tts),
                │                     └    codex_cli & claude_cli (cli)
                ├─ /api/v1/* (JSON API; also mounted at /api for old pages)
                ├─ /, /overview, /models, /logs, /connections (Jinja2 frames)
                └─ /blobs/{ref} (binary payload serving)
                         │
                         ▼
              ModelRouter.dispatch ──→ RegistryService (catalog+manual merge)
                         │              accounting.compute_cost
                         ▼
                  Database (aiosqlite): service_providers, registry_models,
                  subscriptions, request_logs   +   BlobStore (disk)
```

The `domain/` package holds pure types (Modality, RouterRequest/Response, Usage,
Attachment, errors). Adapters, routing, accounting, mcp and web all depend inward
on it; adapters never touch storage/mcp directly.

## Functional requirements

1. ✅ Normalized provider-adapter interface (`generate(request, model)`), task-aware.
2. ✅ Model registry with 3-level mapping, cost tiers (free/cheap/expensive), manual + catalog rows.
3. ✅ OpenRouter live catalog merge into the registry (TTL-cached).
4. ✅ Routing: explicit `model_id`, or task + tier/provider hints, or a per-billing-group default.
5. ✅ Per-request/response logging by `external_user_id` with token usage and cost.
6. ✅ Cost computation from registry prices; amortized-subscription hook (table + field).
7. ✅ MCP tools over Streamable HTTP: `list_models`, `chat`, `generate_image`, `transcribe`, `synthesize_speech`, `run_cli`, `get_usage`.
8. ✅ Web UI: test console (chat/image/stt/tts) + registry admin (providers: `enabled` admin toggle + live `configured` state, manual models) + logs/usage.
9. ✅ Modality breadth: image (Codex OAuth), STT (Deepgram), TTS (ElevenLabs). [Phase 2]
10. ✅ Direct adapters: DeepSeek (OpenAI-compatible), Claude API (Anthropic Messages). [Phase 3]
11. ✅ CLI engines: `codex` (`codex exec --json`) and `claude` (`claude -p --output-format json`), via `run_cli` with a cwd whitelist and a configurable default cwd (`CLI_DEFAULT_CWD`). [Phase 4]
12. ⏳ Full billing reports / amortization rollups. [later]
13. ✅ Production deploy: systemd unit (auto-restart), nginx+TLS, `/health`, `/mcp` Bearer auth, manual `deploy.sh`.
14. ✅ Truncation is a distinct, self-explaining failure (`ResponseTruncated`), never "empty response"; `finish_reason` is logged.
15. ✅ Per-provider catalog sync on a period: adapters expose `list_catalog()`, models missing upstream are deprecated rather than deleted, seeds never resurrect a retired id.
16. ✅ Price provenance per model (`price_source`, `price_source_kind`, `price_checked_at`) and per provider (`pricing_url`); unverified prices surface as `cost_source='estimated'`.
17. ✅ Request-level fallback policy for dead/unavailable models (`same` / `cheaper` / `pricier`), with the substitution reported and logged.
18. ✅ Web contract: `/api/v1`, the `Run` envelope, the typed error envelope, `{items, total, next_cursor, facets}` lists, cursor-paged and column-projected log. [console wave 0]
19. ✅ Call provenance and execution detail in the log: channel/tool/client, HTTP retries, upstream status and request id, token breakdown (cache/reasoning), `finished_at`.
20. ✅ Response files are served safely: MIME whitelist for inline rendering, `nosniff`, immutable cache, byte ranges.
21. ✅ Capabilities: the parameter matrix is derived from adapters (`SUPPORTED_PARAMS`), served with the glossary and the server-fixed settings. [console wave 1]
22. ✅ Dry run: `POST /api/v1/plan` explains what would happen without touching the network, and answers even when nothing can run.
23. ✅ Console screens: five-item navigation, the «Прогон» form built from capabilities, one inspector with a two-level timeline over the `Run` envelope, partial price editing with provenance-based protection.
24. ✅ Accounting tells a missing rate from a zero: `unpriced`, per-character TTS, cache/reasoning tokens at their own rates, notional subscription money kept apart. [console wave 2]
25. ✅ Spend with groupings and a price-source split; the total is labelled a lower bound whenever it cannot be complete.
26. ✅ Closed health summary, live provider key probes, MCP tool inventory with call statistics.
27. ✅ Runs have identity and a lifecycle: submitted, polled, cancellable, idempotent by `client_request_id`, grouped by `batch_id`; the log row exists from the start and a restart closes it as `interrupted`. [console wave 3]
28. ✅ CLI output is read line by line and reported as it arrives; cancelling a run kills the process.
29. ✅ Retention: configurable lifetimes for log rows and response files, a preview, a purge and an orphan sweep. Unset means keep forever, which is the previous behaviour.
30. ✅ Tasks and media are two declared axes; `Modality` is the billing/endpoint group. A model declares the set of tasks it supports, so "produces images but cannot edit one" is expressible. [ADR-0006]
31. ✅ Asynchronous providers: a prediction is created, polled, cancellable upstream, and its result is fetched into the blob store before the provider's link expires. Billing per second of provider compute is its own unit and its own rate. [ADR-0007]
32. ✅ Replicate as a provider: the catalog is an env-configured allowlist, inputs are mapped through each model's own OpenAPI schema, prices arrive `unknown` and are typed in by hand.
33. ✅ A stored file can be handed to a provider by a signed, expiring link on a separate host; without a signing key nothing is served on that path.
34. ✅ A provider's model list can be asked for on demand and stored selectively: the registry keeps the models that answered a request (`confirmed_at`), what a human curated, and nothing else. A market-sized catalog is browsed, not mirrored. [ADR-0008]

## Tasks, media and billing groups (30)

Contract for requirement 30. Reasoning and rejected alternatives: `docs/adr/0006-task-and-media-axes.md`.

**`Media`** — what a payload is: `text`, `image`, `audio`, `video`, `file`. Types a
task; never routes. `file` never appears in a signature's `requires`.

**`Task`** — the operation asked for, in HuggingFace `pipeline_tag` wording. Each
task has a static signature (`requires` / `optional` / `produces` sets of media)
and belongs to exactly one billing group:

| Task | requires | optional | produces | group |
|---|---|---|---|---|
| `text-to-text` | text | | text | `chat` |
| `image-to-text` | image | text | text | `chat` |
| `text-to-image` | text | | image | `image` |
| `image-to-image` | image, text | | image | `image` |
| `speech-to-text` | audio | text | text | `stt` |
| `text-to-speech` | text | | audio | `tts` |
| `text-to-audio` | text | | audio | `audio` |
| `text-to-video` | text | | video | `video` |
| `image-to-video` | image | text | video | `video` |
| `cli` | text | | text | `cli` |

`domain/task.py` owns the table. `validate_task_inputs()` is the single check that
a request carries what its task needs, and it names the missing medium.

**`Modality`** is the billing group and the endpoint name. Tasks outnumber groups
because `text-to-image` and `image-to-image` bill identically. `accounting/`
takes only `Modality`; callers pass `task.billing_group`. That is why log rows
written before tasks existed price exactly as they did.

Declarations: `RegistryModel.tasks` (stored in `registry_models.tasks`, comma
joined), `ProviderAdapter.supported_tasks`, `SUPPORTED_PARAMS` keyed by task.
`supported_modalities` is a derived read-only view. A request carries `task`;
`modality` is still accepted on the API and MCP and maps through `Task.from_str`,
where `chat` means `text-to-text`, `image` means `text-to-image` (the old
`generate_image` had no way to send an input image), `stt`/`tts`/`cli` map to
their obvious task. `request_logs` stores both `task` and `modality`.

## Asynchronous providers and compute billing (31–33)

Contract for 31–33. Reasoning: `docs/adr/0007-asynchronous-provider-predictions.md`.

A prediction is created with `Prefer: wait` and `Cancel-After`, and the create
call is **never retried** except on 429 — a retried create bills twice. Polling
backs off from 1 s to 10 s; timeline steps follow a coarser ladder so a long run
cannot exhaust the 500-step cap. Cancelling a run cancels it upstream, because
the provider charges by the second until told to stop.

Results are downloaded into the blob store by a client carrying no credentials
(the files are on another host). Bounded by `REPLICATE_MAX_OUTPUT_MB` and by
count; anything larger stays a link and the run still succeeds.

**Per second of provider compute** is a billing unit of its own:
`Usage.compute_seconds` metered from the provider's own figure, priced by
`RegistryModel.compute_price_per_second`, in the `audio` and `video` billing
groups. Never `audio_price_per_second`, which counts seconds of a file.

Replicate rows arrive `price_source_kind='unknown'` and `tier='expensive'`, so
they stay out of automatic selection until a rate is entered by hand, and a run
that consumes compute with no rate is `unpriced`.

`GET /public/blobs/{ref}?exp&sig` is the only route outside the auth gate: an
HMAC over the ref and the deadline, on a separate host
(`PUBLIC_BLOB_BASE_URL`), so a provider that takes file inputs by URL can fetch
one file for a few minutes. No `PUBLIC_BLOB_SIGNING_KEY`, no service at all.

## Catalog freshness, price provenance, fallback

Contract for requirements 14–17.

### Failure taxonomy (14)

`ResponseTruncated(ProviderError)` is raised when a provider returns no usable
content *and* reports that generation hit the output-token ceiling
(`finish_reason == "length"` / `stop_reason == "max_tokens"`). The message names
the ceiling and the tokens actually produced, so the caller knows to raise
`max_tokens` instead of debugging the prompt. `request_logs.finish_reason` keeps
the provider's stop reason for both successful and failed calls.

### Catalog sync (15)

- `ProviderAdapter.list_catalog()` returns `list[RegistryModel] | None`; `None`
  means "this provider has no queryable catalog" (CLI engines, TTS/STT).
- `RegistryService.sync_provider(provider_id)` / `sync_all()` reconcile one or all
  providers; both are TTL-guarded (`MODEL_CACHE_TTL_SECONDS`) and force-able.
  A background task in the app lifespan re-syncs every
  `CATALOG_SYNC_INTERVAL_SECONDS` (0 disables); `POST /api/registry/refresh`
  triggers it by hand.
- Reconciliation rules:
  - seen upstream → `last_seen_at = now`, `is_active = 1`, `deprecated_at = NULL`;
  - absent upstream → `is_active = 0`, `deprecated_at = now`. Rows are never
    deleted: logs and cost history reference them;
  - prices from a catalog that carries pricing (OpenRouter) overwrite rows whose
    `source = 'catalog'`; rows with `source = 'manual'` keep their prices,
    display name and pricing mode;
  - a catalog without pricing (DeepSeek, Anthropic) never zeroes an existing price.
- Bootstrap seeds are insert-if-absent, so a retired model id cannot be
  resurrected by a restart.

### Asking a provider, and keeping only what answered (34)

Contract for requirement 34. Reasoning and rejected alternatives:
`docs/adr/0008-catalogs-are-asked-registry-keeps-what-answered.md`.

- `ProviderAdapter.catalog_scope` — `full` (mirror the catalog, the default) or
  `browse` (refresh *active* rows only: no inserts, and no re-activating a row a
  sweep retired — in a market that lists everything always, "seen upstream" says
  nothing, and the way back is adoption). OpenRouter is `browse`: it lists 424
  ids of which five were ever addressed here, and every mirrored row joined the
  pool automatic selection sorts by price. `SyncReport.offered` counts what was
  left out.
- `registry_models.confirmed_at` — when a request to that exact pair came back
  with an answer. Set once and never moved, written only by
  `confirm_registry_model` (a catalog sync cannot touch it), no revision bump,
  and a failed attempt confirms nothing. Backfilled from the log on migration.
- `GET /api/v1/registry/catalog/{provider}` — the live list, stored nowhere, each
  entry flagged `known` / `active` / `confirmed_at`. 503 when the provider has no
  credentials; `has_catalog: false` when it has no catalog endpoint at all.
- `POST /api/v1/registry/catalog/{provider}/adopt` — writes named rows from the
  provider's own answer. An adopted row is unconfirmed until something answers.
- `POST /api/v1/registry/catalog/{provider}/prune?dry_run=` — retires rows that
  nothing confirmed. Kept: confirmed, `source` of `manual`/`seed`, a price a human
  vouched for, or a model_id the log mentions at all. Retires, never deletes.
- codex-oauth has a catalog: `GET {CODEX_BASE_URL}/models?client_version=` (see
  `CODEX_CLIENT_VERSION`) lists the account's six text models with their context
  window, input media and their own reasoning levels; `visibility: hide` and
  `supported_in_api: false` entries are skipped, and the image tool model is
  appended because it is addressable but not listed. Rows are priced `unknown`:
  a subscription publishes no rate, and a zero would win every automatic pick.
- Per-model parameter options: levels a catalog publishes are stored as
  capabilities (`effort:xhigh`) and served on the tree node as `param_options`.
  A pinned model narrows the «Глубина рассуждения» select to its own levels; with
  no model pinned the form offers the union the backends accept.

### Price provenance (16)

`registry_models` gains `price_source` (URL or `api:<endpoint>`),
`price_source_kind` (`api` | `page` | `manual` | `estimate`) and
`price_checked_at`; `service_providers` gains `pricing_url`. Where a provider
publishes prices through its API (OpenRouter `/models`) the kind is `api`;
elsewhere the row points at the vendor's price page and stays `page`/`manual`
until a human confirms it. `compute_cost` reports `cost_source='estimated'`
instead of `'computed'` when the price behind the number is unverified
(`kind='estimate'`), so an invented figure is visible in logs and usage.

### Fallback policy (17)

Requests may carry `fallback = {policy, max_steps, scope}`:

- `policy`: `off` (default, current behavior) | `same` | `cheaper` | `pricier`;
- `max_steps`: how many substitutions to try (default 1);
- `scope`: `provider` (default, same service provider) | `vendor` (same
  model_provider anywhere) | `any`.

Candidates are the active models of the same **task** inside the scope, ordered by
the rate of that task's billing group (`RegistryModel.ladder_price`) — a token sum
orders nothing for image, audio or video rows, where it is zero by construction.
`same` takes the nearest neighbour, `cheaper`/`pricier` step down/up that ladder.
Fallback triggers on: unknown model id, `is_active = 0`, provider disabled or
unconfigured, and provider errors that mean "this model cannot serve the request"
(model not found, unavailable).
It never leaves the task and never retries a prompt-level rejection. The
substitution is explicit in the response (`requested_model_id`, `model_id`,
`fallback_reason`) and in `request_logs`.

## Web contract (18–20)

Wave 0 of `docs/UI_CONCEPT_v2.md`. The console is one consumer of it; MCP and
any other caller see the same shapes.

### Prefix and envelopes

- `/api/v1` is the contract. The bare `/api` prefix serves the same router so a
  page loaded before a restart keeps working; new work targets `/api/v1`.
- Lists answer `{items, total, next_cursor, facets}` — a bare array has nowhere
  to carry the total, the cursor or the facet counts.
- Money is never returned without `cost_source`.
- `/health` is public and says `{"status": "ok"}` only. Version and provider
  inventory are a map of the installation and move behind the auth gate.

### The `Run` envelope

One shape for a run whether it just executed or is read back from the log, built
by `web/views.run_from_dispatch` and `web/views.run_from_log`;
`tests/test_web_views.py` fails if their key sets diverge. Fields: `run_id`,
`log_id`, `status`, `modality`, `channel`, `tool`, `client_id`,
`model {provider, model_id, display_name}`, `requested_model`,
`fallback {kind, trigger, reason}`, `usage {prompt, completion, total,
cache_read, cache_write, reasoning, audio_seconds, images, characters, raw}`,
`cost {value, source, breakdown[]}`, `latency_ms`, `attempts`,
`provider_status`, `provider_request_id`, `finish_reason`, `created_at`,
`finished_at`, `text`, `request {…}`, `request_ref`, `steps[]`, `attachments[]`,
`ignored_params[]`, `error`.

`request` is the question next to the answer: `text` (+ `text_truncated`),
`messages[]` (+ `dropped_messages`), `params`, the caller's `model_id`, `tier`
and `provider_hint`, the `fallback` policy, `attachments[]` with download links,
`client_request_id` and `batch_id`. It is stored as a snapshot in
`request_logs.request_json` when the row is opened, so a run that failed before
any provider was called still says what was asked, and a replay is not
re-derived from today's registry and defaults. Text is cut at 4000 characters
and the history at 50 messages, with the cut stated on screen. Rows written
before the column show the `request_text` they have and nothing invented.

A model is addressed by the pair (service provider + model id), never by the id
alone. `fallback.kind` distinguishes a substitution planned before the call
(`planned`) from one that followed a failure (`after_failure`): they cost
differently. `steps[]` is the two-level timeline — provider attempts, each with
the HTTP retries inside it, their upstream status and the wait the provider
asked for.

### Error envelope

Every failure is a `RouterError` subclass carrying `code`, `http_status`,
`retriable` and, where useful, `provider`, `model_id`, `provider_status`,
`retry_after_s` and a `hint`. One app-level handler renders
`{"error": {type, code, message, provider, model_id, log_id, retriable,
provider_status, retry_after_s, hint}}`.

| Code | Status | Raised when |
|---|---|---|
| `bad_request` | 400 | caller sent a malformed value |
| `not_found` | 404 | log entry or blob does not exist |
| `unknown_model` | 404 | no registry row for the model id |
| `model_retired` / `provider_disabled` / `model_unavailable` | 409 | the pair cannot serve the request |
| `response_truncated` | 422 | output ceiling hit before any content |
| `rate_limited` | 429 | provider refused; `retry_after_s` when it said so |
| `not_configured` | 503 | no key/binary; `hint.env_var` names the variable |
| `provider_timeout` | 504 | no answer inside the read timeout |
| `provider_error` / `provider_unavailable` / `access_denied` | 502 | upstream failure |

`log_id` is written before the exception leaves the router and travels on the
exception, so "open this failure in the log" works from every screen. MCP tools
get the same facts appended to the message text, since FastMCP hands clients
`str(exception)`.

### Log reads

`GET /api/v1/logs` is cursor-paged by descending id (offset would repeat rows,
because the log grows from the head) and projected to the columns the table
shows — the full row carries the response text and the raw usage block.
Filters: user, status, modality, model, channel, since/until, `with_fallback`,
`with_retries`, `unverified_cost`. `GET /api/v1/logs/{id}` returns the full row
as a `Run`.

### Adapter parameter declarations

`ProviderAdapter.SUPPORTED_PARAMS` maps task → the `request.params` keys the
adapter reads. The adapter builds its payload from that same tuple, and anything
the caller sent outside it comes back as `ignored_params` on the run: silently
dropping `tools` or `seed` lets a caller pay for a request that ignored half of
what they asked for.

### Web search (normalized `web_search`)

`params.web_search: true` is a normalized boolean, not a passthrough field —
callers never speak a provider's tool vocabulary, and other adapters may honour
the same flag later. OpenRouter (text tasks only) translates it into a ladder:
the `openrouter:web_search` server tool, then the legacy `{"id": "web"}` plugin,
then the request without search. A rung falls through only on a *transient*
failure (`ProviderUnavailable`: 429/5xx, also when reported as a top-level
`error` object in a 200 body, timeouts, invalid JSON); `AccessDenied` and
`ProviderError` (bad key, rejected prompt, refusal) propagate — a retry would
only mask which request the provider refused. The journal keeps both sides: the
flag in `request_json.params`, the mode that actually answered in the timeline
(`plan_json`) and in `raw_meta.web_search_mode`
(`server_tool` | `plugin` | `off`), which the MCP `chat` reply also carries as
`web_search_mode` — a degradation is stated, never inferred from an answer that
cites nothing.

### Response files

Blobs render inline only for `image/*` and `audio/*`; everything else is
`Content-Disposition: attachment`. Always `X-Content-Type-Options: nosniff`,
immutable long cache (the ref is generated per payload) and byte-range support
so an audio player can seek.

## Console contract (21–23)

Wave 1 of `docs/UI_CONCEPT_v2.md`.

### Capabilities

`GET /api/v1/capabilities` answers with `providers`, `params`, `server`,
`labels` and a `schema_version`. `params` is keyed modality → provider and comes
from `ProviderAdapter.SUPPORTED_PARAMS` (see the ADR-0001 addendum), with
per-provider `model_overrides` for what a pair cannot express. `server` lists
what the caller cannot change — timeouts, retries, allowed cwd roots — as
derived facts plus the *name* of the environment variable, never a value or a
path. `labels` is the glossary from `web/labels.py`: one word per concept, in
one file, each with the visible explanation that belongs under a control rather
than in a tooltip.

### Dry run

`POST /api/v1/plan` returns the requested pair, the problem with it if any, the
policy in words, the candidate ladder with a verdict and a reason per model, the
pair that would be called, and a cost estimate that states its assumption. Two
invariants: it never reaches the network (`RegistryService.get_models` grew
`allow_sync=False`), and it answers 200 even when nothing can run, because
"почему запрос не ушёл" is the case the screen exists for.

### Registry edits

`GET /api/v1/registry/tree` builds the picker cascade (provider → vendor →
family → variant, with counts and prices) server-side; splitting `model_id` in
the browser guesses at a structure the database stores in its own column.

`PATCH /api/v1/registry/models/{provider}/{model_id}` writes only the columns it
is given, out of a closed set that excludes the pair, `source`, `is_active` and
the catalog dates. Prices arrive as USD per million and are stored per token.
Concurrency is guarded by a `revision` counter, not by `updated_at`: a
second-resolution timestamp cannot tell two edits inside one second apart.

Price protection is keyed on the origin of the **price**, not of the row. A
number a human vouched for (`price_source_kind` of `manual` or `page`) survives
synchronisation while the row stays a catalog row and keeps receiving name,
context and modality updates. `revert_price` hands it back to the catalog.

### Screens

Five: Обзор · Прогон · Модели · Журнал · Подключения. Filters, search, page,
sorting, the selected row, the active tab and `?log=` live in the address; the
live tail, collapsed nodes, the prompt draft and the theme do not.

The «Прогон» form is generated from capabilities, so a field is present exactly
when the adapter behind the chosen pair reads it. Model choice has two modes
(Авто with the server's reason line, and Выбрать самому with the server-built
cascade plus a `model_id` field that reports found / retired / provider-off as
it is typed). Fallback is one select in plain words, with `max_steps` under
"ещё настройки". The plan is recomputed 300 ms after any discrete change, and
the send button carries the estimate.

«Модели» carries the registry table, the price card (unit inside the field, the
previous value underneath, an order-of-magnitude jump confirmed out loud) and the
«Спросить поставщика» block: a provider select, the live catalog with per-row
adoption, and «Свернуть до подтверждённых». The card says whether anything ever
answered on the model.

One inspector component serves «Прогон» and «Журнал», over the single `Run`
envelope, with five tabs — Запрос, Ответ, Шаги, JSON, Сырьё. «Ответ» opens
first; «Запрос» carries the prompt, the message history with roles, the
attachment and the routing ask, and the JSON tab has a block per side. Its
timeline has two
levels: provider attempts, and the HTTP retries inside each one with the wait
the provider asked for.

Frontend stack: native ES modules, no htmx, no Alpine, no build step — see
ADR-0005, which supersedes §18 of the concept.

## Accounting and operations (24–26)

Wave 2 of `docs/UI_CONCEPT_v2.md`.

### Money

`Usage` has one documented contract that every adapter normalizes to:
`prompt_tokens` is the whole prompt *including* cache, and
`cache_read_tokens` / `cache_write_tokens` are the parts of it that were.
Providers disagree — DeepSeek and the OpenAI-shaped APIs count cache inside the
prompt total, Anthropic reports it alongside — so the adapter does the adding
and accounting has one rule.

`compute_cost` returns the sum by its terms, each with its own rate, and a
`cost_source` that distinguishes:

| Source | Meaning |
|---|---|
| `computed` | every rate behind the number is confirmed |
| `estimated` | some rate is a guess — including a cache rate we had to assume |
| `provider` | the provider named the sum and charged it |
| `provider_notional` | the provider named the sum, a subscription paid for it, no money moved |
| `unpriced` | the request consumed something no rate covers. Not zero: zero is a claim |
| `amortized` | spread across a subscription pool |
| `cost_unknown` | a read timeout: probably executed and billed, never reported |

A zero somebody confirmed (a `:free` model) is a price. A zero nobody filled in
is a gap — `price_is_unknown` and `is_free` turn on price provenance, not on the
digits, which is what kept three of the five modalities billing at nothing.

`GET /api/v1/usage` groups by user, model, provider, channel or day and returns
the totals split along those lines, with `is_lower_bound` set whenever the
selection contains unpriced or timed-out requests. CSV export keeps the columns
separate.

### Operations

`GET /api/v1/health/summary` (behind the gate) carries uptime, disk usage,
provider state, registry counts, the last 24 hours and a work list where every
line links to where it is fixed. `POST /api/v1/providers/{id}/probe` answers
whether a key still works — `configured` only sees that one is present — and
caches for ten minutes.

`GET /api/v1/mcp/summary` lists the tools with their schemas joined to what the
log saw: calls, errors and p50 per tool, and the clients that made them. Tools
the log knows and the build no longer exposes are reported separately. There is
no sessions screen: ADR-0003 makes the server stateless, so it would be empty by
construction.

## Runs and retention (27–29)

Wave 3 of `docs/UI_CONCEPT_v2.md`.

### A run

`POST /api/v1/runs` accepts a request of any modality and answers 202 with a
`run_id`; the console polls `GET /api/v1/runs/{id}` once a second and
`DELETE`s it to cancel. No streaming: polling survives a closed tab, needs
nothing from nginx and cannot collide with the streamable-HTTP session manager
in the same process (concept §10.5, ADR-0003).

The registry is in-process and is a view of what *this* process is doing. The
durable record is `request_logs`, and the two halves of a row are now written at
two different times:

| Moment | Row |
|---|---|
| run accepted | inserted with `status='running'`, `cost_source='cost_unknown'` |
| run finished | same row updated with the outcome, usage, cost and timeline |
| terminal failure | same row updated to `error` |
| attempt failure a fallback recovered from | a row of its own, as before |
| operator cancelled | same row updated to `cancelled`, cost stays unknown |
| service restarted mid-run | `interrupt_live_logs()` on startup → `interrupted` |

Two consequences worth stating. The log is ordered by when a request *started*,
so a recovered attempt failure sorts after the run that recovered from it. And a
cancelled or interrupted run never records a cost of zero: the provider may have
executed and billed the work, and zero is a claim.

`client_request_id` is the idempotency key — looked up in the registry first and
then in the log, so it still holds after a restart or an eviction. `batch_id`
groups the N runs one operator action fanned out into; it is a log filter too.
Concurrency is capped by `MAX_CONCURRENT_RUNS` (3): one SQLite connection behind
one write lock serves the whole service.

### Progress

Adapters report through a ContextVar sink (`domain/progress.py`), the same
mechanism as the HTTP retry trace and for the same reason: `generate()` is one
signature shared by eight adapters, and threading a progress argument through
all of them would change every adapter to serve one. Emitting with no sink
installed is a no-op, so an MCP tool call is unaffected.

The CLI adapters read stdout line by line (`run_subprocess(on_line=…)`), which
is what makes a long run legible while it runs. claude uses
`--output-format stream-json --verbose`; the single-object format is still
parsed, so an older CLI or a `CLAUDE_COMMAND` carrying `--output-format json`
keeps working. Cancelling kills the subprocess.

The timeline is stored in `plan_json` for successful runs as well as failed
ones, capped at 500 steps with the truncation stated in the list itself.

### Retention

`LOG_RETENTION_DAYS` and `BLOB_RETENTION_DAYS` are separate because they cost
different things: the rows are small and are the only record of spend, the files
are almost all of the bytes. Both default to 0 — keep forever, the behaviour
before the policy existed. `GET /api/v1/retention` previews,
`POST /api/v1/retention/purge` applies, and a nightly sweep does the same when
either lifetime is set. Files go before the rows that name them: the reverse
order would leave bytes nothing points at.

## Project structure

```
router_mcp/
  config.py            pydantic-settings Settings
  app.py               build_app(): FastAPI + FastMCP mount + lifespan
  main.py              uvicorn entrypoint (module-level `app`)
  bootstrap.py         build_core(): wires DB, adapters, registry, router
  runtime.py           process-wide AppCore handle for MCP tools
  logging_config.py
  domain/              modality, messages, errors, progress (pure types and
                       the ContextVar sink work in flight reports through)
  adapters/            base (ABC + RetryClientMixin), _subprocess (CLI helper),
                       openrouter, replicate, deepseek, claude_api, codex_oauth, deepgram,
                       elevenlabs, codex_cli, claude_cli
  registry/            models (RegistryModel), service (per-provider catalog sync)
  routing/router.py    ModelRouter.resolve + dispatch
  accounting/cost.py   compute_cost
  storage/database.py  aiosqlite schema + CRUD + logging + usage
  blobs/store.py       on-disk blob store
  mcp_server/          tools (FastMCP), serialize
  web/                 api (JSON, mounted at /api/v1 and /api), views (Run and
                       error envelopes), capabilities (matrix from adapters),
                       labels (the glossary), health (summary + key probes),
                       mcp_summary (tool inventory), runs (the in-process run
                       registry), retention (lifetimes, preview, purge),
                       pages (Jinja2 frames), templates/,
                       static/{app.css, css/tokens.css,
                       js/core/{api,dom,format,state,keys,runs}.js,
                       js/components/{json-view,inspector,timeline}.js,
                       js/pages/{run,models,logs,spend,overview,connections}.js}
tests/                 conftest (one app per session), test_smoke (chain),
                       test_web (app), test_web_views (envelope parity),
                       test_api_filters (log filters/cursor), test_blobs
                       (file-serving headers), test_escaping (no innerHTML),
                       test_capabilities (matrix matches the adapters),
                       test_plan (dry run stays offline), test_registry_patch
                       (partial edit, price survives sync), test_accounting
                       (a gap is not a zero), test_spend (kinds of money stay
                       apart), test_health_mcp (summary, probe, inventory),
                       test_pages_js (page modules against a stub DOM, skipped
                       without node), test_acceptance (§20 in a real browser,
                       skipped without playwright),
                       test_modalities, test_adapters_parse, test_cli,
                       test_fallback, test_registry_sync, test_auth;
                       js/ (DOM stub + API fixtures), mcp_probe.py (manual)
```

Frontend rule: templates are markup only; behaviour lives in native ES modules
under `static/js/`, pages get their module from `page_script`, and static links
carry `?v=<app version>` so a restart cannot leave an old client talking to a
new API.

## Testing the console

Two harnesses, because a browser is not always available and a stub cannot see
everything.

- `tests/js/` runs each page module against a hand-written DOM stub with real
  API fixtures under node. It catches mistyped element ids, handlers bound to
  missing nodes, and pages reading fields the API does not send. Skipped when
  node is absent — the production host has none.
- `tests/test_acceptance.py` drives Chromium through the twelve acceptance
  points of §20 against a uvicorn in its own subprocess (the session manager
  runs once per process, and the rest of the suite already has an app in this
  one). Install with `pip install -e ".[e2e]" && playwright install chromium`;
  skipped when playwright is missing.

## Deployment

- Local dev: `uvicorn router_mcp.main:app --host $HOST --port $PORT` (or `python -m router_mcp.main`).
- Production (this host): runs as systemd service `model-router-mcp.service` under a
  dedicated user `modelrouter`, from `/opt/model-router-mcp`, bound to `127.0.0.1:8088`,
  behind nginx + TLS on a subdomain. `Restart=always`; updates via `deploy/deploy.sh`.
  Full runbook: `deploy/DEPLOY.md`. Artifacts: `deploy/model-router-mcp.service`,
  `deploy/install.sh`, `deploy/deploy.sh`, `deploy/nginx-model-router.conf`.
- Secrets/config live in `.env` (see `.env.example`); never committed.
- Runtime data (`data/router.db`, `data/blobs/`) and `logs/` are gitignored.
- Endpoints: `GET /health` (open, status only); `/mcp` (Streamable HTTP, requires
  `Authorization: Bearer $AUTH_TOKEN` when set, enforced by a pure-ASGI middleware);
  web console `/`, `/registry`, `/logs` (guarded by nginx HTTP Basic auth).

## Current state

- ✅ Phases 0–4 + deploy wrapper complete and tested: OpenRouter chat, image
  (Codex), STT (Deepgram), TTS (ElevenLabs), direct DeepSeek + Claude API, and
  CLI engines (`codex`/`claude`).
- ⏳ Phases 5–6 per the plan: registry admin polish, hardening, optional streaming.

See `AGENTS/STATE.md` for the live Now / Next snapshot.

## Data sources & dependencies

- OpenRouter `/models` + `/chat/completions` (requires `OPENROUTER_API_KEY`).
- Reused connection patterns originate in sibling repos: `ort_bot` (OpenRouter,
  Deepgram, ElevenLabs, Codex-image, model cache, DB/config patterns),
  `telecodex_bot` (Codex CLI runner), `openai-auth-sync` (OAuth token sync).
- Codex OAuth shares `auth.json` with those repos — use a dedicated copy to avoid
  refresh-token rotation conflicts.
