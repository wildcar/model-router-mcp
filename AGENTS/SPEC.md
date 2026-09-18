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
                │                     │    deepgram (stt), elevenlabs & yandex (tts),
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
4. ✅ Routing: explicit `model_id`, or task + tier/provider hints, or a per-billing-group default. A configured `DEFAULT_*_MODEL` outranks the price scan when the request carries no hints at all (any hint returns the request to the scan; a default that cannot do the task is ignored).
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
35. ✅ A price may depend on the time of day: a row carries its base rate plus the UTC windows in which every rate is multiplied (`price_schedule`), and cost, the dry run and the fallback ladder all read the rate in force at that moment. DeepSeek's peak hours are the first case.
36. ✅ Application identity per request (`app_url` / `app_name`): the site the app runs on wins, a plain name is the fallback, and the effective identity is logged, filterable in the journal and forwarded to providers that have such a parameter (OpenRouter's `HTTP-Referer` / `X-Title`). A request with no identity is made as the router itself: `https://mr.wildcar.org` (`SELF_APP_URL`).
37. ✅ OpenRouter's dedicated images endpoint (`POST /images`): rows marked with the `images_api` capability — models absent from `GET /models` entirely, meta/muse-image first — generate and edit images there (`input_references` for reference photos), and an unmarked row follows the chat endpoint's own «use /api/v1/images» 404 instead of dying on it. The adapter now declares `image-to-image`.
38. ✅ A provider whose models are computed in this process (`local`): an adapter that calls a Python function instead of an endpoint and is otherwise indistinguishable — the same `runs`, the same error envelope, a `usage` row at exactly zero. Inference runs in a thread pool behind a semaphore (`LOCAL_MAX_CONCURRENCY`), under a deadline (`timeout`, retriable); missing weights are `model_not_loaded`. First model: UVDoc, straightening a photographed page (`local/uvdoc`). [ADR-0009]

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
| `image-to-image` | image | text | image | `image` |
| `speech-to-text` | audio | text | text | `stt` |
| `text-to-speech` | text | | audio | `tts` |
| `text-to-audio` | text | | audio | `audio` |
| `text-to-video` | text | | video | `video` |
| `image-to-video` | image | text | video | `video` |
| `cli` | text | | text | `cli` |

`domain/task.py` owns the table. `validate_task_inputs()` is the single check that
a request carries what its task needs, and it names the missing medium.

The instruction is optional for `image-to-image` because background removers and
upscalers take a picture and nothing else. A model with no prompt-like property
receiving text the task only *offered* reports it in `ignored_params` and runs;
one whose task **requires** text still refuses, because a generator with nowhere
to put a prompt cannot produce what was asked for.

### Attachment roles

A signature says what media a task needs, never how many of each or as what. Two
images with different meanings — a person and a garment, a picture and a mask, a
target and a face — are told apart by `Attachment.role`, out of the closed
vocabulary in `domain/messages.ATTACHMENT_ROLES`: `image`, `mask`, `face`,
`garment`, `reference`, `style`. `POST /api/v1/runs` takes it per attachment
(`attachments[].role`); an unknown role is a 400 with the vocabulary in it.

A role binds to the model's own input property through the registry row's
`input_map` (`{"image": "human_img", "garment": "garm_img"}`), which only a human
can write: a schema publishes `human_img` and `garm_img` and never says which is
which. Falling back, the adapter tries the role as a property name and then a
narrow per-role guess table; a role it cannot resolve is an error naming the
model's real inputs, never a positional guess. An attachment with no role binds
positionally, exactly as before roles existed. Roles bind first, so a positional
attachment cannot take the property a named one was headed for. Every role is
delivered through the same file path as the primary image, so a mask above
`DATA_URI_LIMIT` travels as a signed `PUBLIC_BLOB_BASE_URL` link. The role is
kept in the request snapshot and shown in the log inspector.

«Прогон» draws one row per chosen file — the name and a role — but only when
more than one file is chosen and the task takes an image: a single file is the
obvious one and binds positionally, and six selects on a text prompt are noise.
The roles offered are the keys of the pinned model's `input_map`
(`GET /api/v1/registry/tree` carries them on the variant), falling back to the
whole vocabulary when nothing is pinned. The first file is preselected as
`image`, which is the subject in every one of these models, and the guess is
visible rather than implied. The words come from `web/labels.ATTACHMENT_ROLES`,
which `tests/test_capabilities.py` pins to the domain vocabulary — a label for a
role the API rejects is a form that cannot be submitted.

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

`ContentFiltered(ProviderError)` — code `content_filter`, 422, not retriable —
is the provider's safety filter refusing the input or withholding the output.
`InsufficientCredits(ProviderError)` — code `insufficient_credits`, 402 — is an
empty provider account, which only an operator can fix. Both are recognised in
one place, `adapters/base.provider_error_for(message, provider_status)`: HTTP
402 is credits outright, the rest is matched on the provider's own wording
(`CONTENT_FILTER_RE`, `NO_CREDITS_RE`; the sentences come from this log —
Replicate «flagged as sensitive» / E005 / «NSFW content», OpenRouter «filtered
due to the prompt triggering our content management policy» and «Insufficient
credits», DeepSeek «Insufficient Balance»). The retry mixin classifies every
4xx through it and reads a 403 body before calling it a bad key; OpenRouter's
`finish_reason=content_filter` and Anthropic's `stop_reason=refusal` on an
empty answer raise `ContentFiltered` directly. Every failed row carries the
code in `request_logs.error_code` (null on rows written before the column),
and the run envelope's `error.code` is read from it.

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
    deleted: logs and cost history reference them. Rows with `source = 'manual'`
    are exempt: an allowlist-shaped catalog (Replicate's `REPLICATE_MODELS`)
    lists what is allowlisted, not what the provider serves, so "absent" there
    says nothing about the operator's row — curation is withdrawn by hand;
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
- `POST /api/v1/registry/catalog/{provider}/prune?dry_run=` — retires active rows
  that nothing confirmed. Kept: confirmed, `source` of `manual`/`seed`, a price a
  human vouched for, or a model_id the log mentions at all. Rows *already*
  retired that none of those four reasons protects are deleted outright
  (reported as `deleted`): «не удаляем — на строку ссылаются» is vacuous for a
  row nothing references, and such rows only clutter the «снятые» filter.
- codex-oauth has a catalog: `GET {CODEX_BASE_URL}/models?client_version=` (see
  `CODEX_CLIENT_VERSION`) lists the account's six text models with their context
  window, input media and their own reasoning levels; `visibility: hide` and
  `supported_in_api: false` entries are skipped, and the image tool model is
  appended because it is addressable but not listed. Rows are priced `unknown`:
  a subscription publishes no rate, and a zero would win every automatic pick.
- A refusal retires a row (ADR-0010). codex-oauth reads `/responses` answering
  HTTP 404 `model_not_found` or HTTP 400 «model is not supported» as "this slug
  is gone": the request fails with `model_unavailable` naming the slug (a
  fallback policy may act on it), and the row is retired at once with
  `retired_by: "backend"` — not left to a catalog sync, because on 2026-09-08
  `GET /models` kept listing gpt-5.5 for hours after `/responses` stopped
  serving it. A row retired that way stays retired through catalog syncs for
  24 h (`held` in the sync report); afterwards the catalog is believed again.
  The console's restore clears the mark.
- The text model that leads a codex-oauth image request comes from the
  registry: `CODEX_IMAGE_MAIN_MODEL` leads while its own row is active,
  otherwise the provider's active `text-to-text` rows lead, confirmed ones
  first. When the leader is refused mid-request the image call is repeated once
  with the next live leader (the first call drew nothing, `usage` empty); the
  run then carries `requested_model_id` = the image model and a
  `fallback_reason` naming both leaders, and the timeline gets a `fallback`
  step. With no live leader left the error's `hint` names the retired slugs and
  `CODEX_IMAGE_MAIN_MODEL` as the thing to change.
- The backend ignores the `image_generation` tool's `model`, `size` and
  `quality` (2026-09-10: `gpt-image-2.5-flare`, `gpt-image-2.5` and a made-up
  slug all drew, and `response.completed` echoed `gpt-image-2-codex` / `auto` /
  `auto` each time; the leading model picks size and quality when it calls the
  tool). `CODEX_IMAGE_TOOL_MODEL` therefore names the log row, not the drawing
  model. The adapter reads the stream to `response.completed` and logs what was
  applied in `usage_raw` — `backend_image_model`, `actual_size`,
  `actual_quality` — next to the requested `size` / `quality` / `main_model`.
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

### Prices that depend on the time of day (16)

A provider may charge more at some hours: DeepSeek doubled its rates for
01:00–04:00 and 06:00–10:00 UTC on 2026-08-16. One number per rate cannot hold
that — the peak figure overcharges seventeen hours a day and the off-peak figure
undercharges seven — so `registry_models.price_schedule` carries the windows
alongside the base rate:

```json
{"windows": ["01:00-04:00", "06:00-10:00"], "multiplier": 2.0,
 "label": "пиковый тариф", "tz": "UTC"}
```

The stored prices are the rate **outside** every window; inside one, all nine
rate columns are multiplied together (`RegistryModel.priced_at(moment)` returns
the row at the rates in force, and returns `self` for the flat-rate rows, which
is every other row). Windows are UTC minutes, half-open `[start, end)`, and may
wrap past midnight.

Everything that reads a rate reads it at a moment: `compute_cost(..., at=)` uses
the run's start (a long run is billed the rate it was quoted), the dry-run
estimate and the fallback ladder use now, so «дешевле» compares what two rows
would actually charge. The multiplied rates are what the breakdown shows, and
`CostResult.price_factor` / the `cost` block of the `Run` envelope carry the
factor and its label — otherwise the arithmetic in the log contradicts the price
table with no explanation. A provider-reported sum is never multiplied.

No catalog publishes any of this, so the column is human-only: the sync never
writes it (like `input_map` and `extra_params`), and typing a schedule counts as
vouching for the price, which sets `price_source_kind` to `manual` unless the
edit named another. `revert_price` clears it along with the price. A column that
cannot be parsed loads as no schedule and bills the base rate, because a registry
that will not load is worse than a rate that is wrong for a few hours.

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

## Application identity (36)

Every dispatching entry point (MCP tools, `/api/chat|image|tts|transcribe|cli`,
`/api/v1/runs`) accepts `app_url` (the site the application runs on) and
`app_name` (a plain name for apps with no site). The effective identity is
`app_url or app_name` — the site wins because it is verifiable and stable;
both are caller-supplied and untrusted, a grouping key exactly like
`external_user_id`.

- **Forwarding**: adapters that have such a parameter receive it. OpenRouter is
  the only one today: `HTTP-Referer` ← `app_url`, `X-Title` ← `app_name`, set
  per request. A request with no identity is made as the router itself —
  `HTTP-Referer: https://mr.wildcar.org` (`SELF_APP_URL`) with
  `X-Title: model-router-mcp` (`SELF_APP_NAME`); a request identified by URL
  alone sends no `X-Title` rather than mislabelling the app with ours.
- **Journal**: `request_logs.app` stores the effective identity; the `Run`
  envelope carries it as `app`, and the snapshot keeps `app_url`/`app_name` as
  they arrived. `GET /logs?app=` filters by substring (like `user`/`model`),
  `GET /logs/suggest?field=app` feeds the typeahead, and «Расход» groups by
  `app`.
- **`app_url` must be a domain the app owns.** Observed live (2026-08-26,
  posinus): OpenRouter attributes an app by the referer's *domain* and cuts
  the path — `https://dzen.ru/posinus` was recorded as dzen.ru's existing app
  («Autoposting dzen», `https://dzen.ru/`), account gone, our X-Title
  ignored. An app living under someone else's domain identifies with its own
  (posinus now sends `https://wildcar.org` + «Positive news» — recorded
  correctly). Subdomains are distinct apps to OpenRouter — verified live:
  `mr.wildcar.org` and `agentish.wildcar.org` show as two entries — so the
  house pattern is one subdomain of wildcar.org per application. Our journal
  is unaffected either way: it stores what the caller sent.

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
| `content_filter` | 422 | the provider's safety filter refused the input or withheld the output; not retriable |
| `insufficient_credits` | 402 | the provider account has no balance; an operator tops it up |
| `rate_limited` | 429 | provider refused; `retry_after_s` when it said so |
| `not_configured` | 503 | no key/binary; `hint.env_var` names the variable |
| `model_not_loaded` | 503 | a local model's weights are not on disk (`deploy/fetch-models.sh`); not retriable |
| `image_too_large` | 413 | an input frame above a local model's pixel cap, refused from the header; not retriable |
| `timeout` | 504 | a local run overran `LOCAL_RUN_TIMEOUT_SECONDS`; retriable, cost stays zero |
| `provider_timeout` | 504 | no answer inside the read timeout |
| `provider_error` / `provider_unavailable` / `access_denied` | 502 | upstream failure |

`log_id` is written before the exception leaves the router and travels on the
exception, so "open this failure in the log" works from every screen. A
failure read back from the log (the run envelope's `error`, a replayed
`client_request_id`) carries the same keys plus `replayed`. MCP tools
get the same facts appended to the message text, since FastMCP hands clients
`str(exception)`.

### Log reads

`GET /api/v1/logs` is cursor-paged by descending id (offset would repeat rows,
because the log grows from the head) and projected to the columns the table
shows — the full row carries the response text and the raw usage block.
Filters: user, status, modality, model, provider, app, channel, since/until,
`with_fallback`, `with_retries`, `unverified_cost`, `cost_mismatch`. `user`,
`model` and `app` match by substring, case-insensitive («ort-bot» finds every
ort-bot-XXXX); `model_id` stays the exact match. `GET /api/v1/logs/{id}`
returns the full row as a `Run`; its attachments carry `kind` and `mime` read
back from the blob ref's extension, so a picture drawn a month ago is a
picture and not a «file» (`views._media_of`).

`GET /api/v1/logs/{id}/neighbors` takes the same filter set and answers
`{newer_id, older_id}` — the rows on either side of the entry *under those
filters*, or null where the list ends. The entry window's ▲ ▼ step through
them, so «следующая» with `status=error` set is the next error. An unknown id
still gets an answer (the row may have been purged while the window was open).

`GET /api/v1/logs/images?cursor=&limit=` — rows that carried a picture in or
out, newest first, under the same filter set as `/logs` (one FastAPI
dependency, `log_filters`, serves all three log reads), one card per row: who asked, the words asked (snapshot
text, else the last user message, cut at 200 characters), and `images[]` with
`side` (request/response), `role`, `ref`, `url` and `thumb_url`. Request
pictures come from the snapshot's attachment list (a ref, or a plain http URL
when the caller pointed at one; a data: URL cut to 200 characters is skipped);
the answer's picture is the row's `response_ref` — only the first response
file is on the row. `total` is null on purpose: the predicate reads the
snapshot column, and counting it scans every request ever logged.

`GET /blobs/{ref}/thumb` — a copy of a stored image no wider than 320 px, JPEG
(PNG when there is transparency), made on demand with OpenCV when the `local`
extra is installed and held in a small in-process cache; without OpenCV, for a
format it cannot read or a picture already that small, the original bytes are
served. Never written to disk: retention and the orphan sweep know one file
per ref. Same immutable cache headers as the original; 404 for a ref that is
not an image.

`GET /api/v1/logs/suggest?field=user|model|provider|app&q=` — distinct values the
log has seen for that column, freshest first, narrowed by `q` (substring).
Feeds the journal's filter dropdowns and as-you-type подсказки; the field goes
into SQL through a closed map, anything else is a 400. The console's day
filter (◀ день ▶) is client-side sugar over since/until: the local day's
bounds travel as UTC strings in the stored `+00:00` shape, because the
comparison is lexicographic.

### Cost reconciliation

`request_logs.provider_cost_usd` holds the sum the provider itself reported
(OpenRouter's `usage.cost`; lifted out of the usage JSON, backfilled by
migration). `cost_mismatch=true` selects rows where it sits more than 2% *and*
more than $0.0001 away from `cost_usd` — excluding sources where the two are
one number by construction (`provider`, `provider_notional`, `amortized`) —
which since 2026-09-19 includes OpenRouter image tasks, booked at `usage.cost`
(see «OpenRouter images endpoint»). The console marks such rows `≠` in the log
table, the inspector prints the
provider figure beside ours, and «Обзор» counts the last week's divergences as
a work-list line. Both pricing gaps found so far (the web-search fee, the
cache rates) had exactly this signature; the point of the filter is that the
next one names itself.

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
then the request without search. A rung falls through on a *transient*
failure (`ProviderUnavailable`: 429/5xx, also when reported as a top-level
`error` object in a 200 body, timeouts, invalid JSON), and the tool rung
additionally on OpenRouter's 404 «No endpoints found that support tool use» —
the plugin asks nothing of the model, so a model without tools is the plugin's
case, not a failure. `AccessDenied` and every other `ProviderError` (bad key,
rejected prompt, refusal, retired model) propagate — a retry would only mask
which request the provider refused. The journal keeps both sides: the
flag in `request_json.params`, the mode that actually answered in the timeline
(`plan_json`) and in `raw_meta.web_search_mode`
(`server_tool` | `plugin` | `off`), which the MCP `chat` reply also carries as
`web_search_mode` — a degradation is stated, never inferred from an answer that
cites nothing.

Search is billed per call on top of the tokens. The registry stores
`web_search_price` (USD per search; from the catalog's `pricing.web_search`,
synced like every api-sourced price and editable per call, not per million),
the adapter normalizes `usage.server_tool_use_details.web_search_requests`
into `Usage.web_searches`, and accounting adds `web_searches ×
web_search_price` as its own breakdown term. A searched run on a row with no
search rate is marked `estimated` — priced by tokens alone it is understated,
and it must read that way. The plugin mode is billed per *result* and reports
no count; there the provider-reported cost remains the only complete figure.

Three more providers honour the same flag, none with a ladder — either the
switch works or the request fails loudly. codex-oauth (text tasks) puts the
native Responses tool `{"type": "web_search"}` into the payload — the same
tool `codex exec` turns on — reports `web_search_mode: "server_tool"`, and
each `response.web_search_call.*` stream event becomes a timeline step.
codex-cli appends `-c tools.web_search=true` (the `--search` flag lives on
the root command only; `exec` rejects it with exit 2 — checked live).
claude-cli appends `--allowedTools WebSearch` — explicit whitelisting: today
`--dangerously-skip-permissions` already admits the tool, and an unchecked
box stays out of the call rather than forbidding the model to search. The
subscription backends publish no search rate, so no accounting term is added
there: codex-oauth/CLI costs stay estimated or notional as before.

### OpenRouter images endpoint (37)

OpenRouter serves two kinds of image models. Chat-shaped ones (the Gemini
image family) live in `GET /models` and answer on `/chat/completions` — the
path this adapter always had. Pure image models (meta/muse-image, seedream,
gpt-image-*) are listed only in `GET /images/models`, refuse the chat endpoint
with an HTTP 404 that names `/api/v1/images`, and take
`{model, prompt, aspect_ratio, resolution, n, quality, output_format, seed,
input_references[]}`; the answer is `data[].b64_json + media_type` and
`usage.cost`.

The registry row opts in with the `images_api` capability (human-set: these
models are invisible to the catalog sync, so their rows are `manual`). A row
without the mark that hits the distinctive 404 is retried once on `/images` —
the provider's own routing advice, not a retired id; a plain 404 still means
the model is gone. Reference images ride in `input_references` in the same
content-part shape as chat image parts, which is what makes `image-to-image`
(declared by the adapter since this requirement) work: editing, composition,
reference conditioning. App-identity headers (36) apply on this path as on
the chat path. Billing: the answer carries `usage.cost`, and for image tasks
(`text-to-image`, `image-to-image`) on either endpoint the adapter hands it to
accounting as `cost_override_usd`, so the row books the provider's sum with
`cost_source='provider'`. The reason is that these models bill by token (the
prompt and the input photo at one rate, the output image at another), so one
`image_price` per picture cannot fit both drawing and editing: an edit on
gpt-image-2.5 booked $0.005 against the provider's $0.016921 (log 26200). The
registry's `image_price`, entered by hand from a confirmed run, remains the
fallback for an answer without `usage.cost` and the figure the dry run quotes.
Text tasks keep the registry arithmetic: there the per-token rate is real and
the gap against `usage.cost` is the staleness signal reconciliation looks for.

### Models computed on this host (38)

Contract for requirement 38. Reasoning: `docs/adr/0009-local-provider.md`.

The `local` provider (`adapters/local.py`) holds a dictionary of `LocalModel`s:
each has its own inference function, weight files and input fields, and is
seeded into the registry at startup (`service_provider_id='local'`,
`model_id='local/<name>'`). The rule that only `router-client` ever reaches a
model holds: an application gets a local model the way it gets a Replicate one
— a registry row and an assignment.

- **Same path as every provider.** `POST /api/v1/runs` → 202 → poll; the result
  is a blob with `ref` and `url`; the row in `request_logs` and the `usage`
  totals are written as for any run. Attachments arrive as bytes (the blob
  store); the row's `input_map` binds roles onto the model's declared fields
  and a loose attachment binds positionally, exactly as at Replicate. Text is
  optional and, since no local model reads one, lands in `ignored_params` as
  `prompt`. Parameters reach the function only if the row's `extra_params`
  allows them **and** the model declares them; the adapter itself declares none.
- **Price is zero as a fact.** The seed carries `image_price: 0` with
  `price_source_kind='manual'`, so `price_is_unknown` is false, `is_free` is
  true, and a run is `computed` at $0 — «бесплатно», never «нет методики».
- **CPU is shared with the router.** At most `LOCAL_MAX_CONCURRENCY` (2) runs
  compute at once, in a thread pool of that size behind a semaphore; a third
  waits (a «ждём свободный слот» step on its timeline) and the event loop keeps
  answering `chat`. Each session gets `cores // concurrency` threads.
- **Deadline.** `LOCAL_RUN_TIMEOUT_SECONDS` (60) → `timeout`, 504, retriable.
  A thread cannot be killed: the overrun computation finishes on its own and
  holds its slot until it does, so the cap is never exceeded.
- **Weights are an installation matter.** `deploy/fetch-models.sh` downloads
  them into `LOCAL_MODELS_DIR` (`data/models/<model>/…`) and checks pinned
  sha256 hashes; `install.sh` and `deploy.sh` both call it. A missing file
  answers `model_not_loaded`, 503, not retriable — a `NotConfigured` subclass,
  so a fallback policy may still choose a hosted model. `is_configured()` is
  "the runtime libraries are importable" (`pip install -e '.[local]'`: CPU
  `onnxruntime`, `opencv-python-headless`, `numpy` — no CUDA); the weights are
  checked per model at run time.
- **Registry rows have a `description`** (new column, patchable, in
  `model_to_dict` and the console card): what a model cannot do is not in any
  catalog, and the applications that let a user pick a model show it.

**`local/uvdoc`** (`adapters/local_uvdoc.py`; UVDoc, Verhoeven–Magne–
Sorkine-Hornung, SIGGRAPH Asia 2023, Apache-2.0; weights: the grid-output ONNX
export `fredcallagan/uvdoc-grid-onnx`, 31.8 MB in two files). Task
`image-to-image`, `input_map {"image": "image"}`, no parameters. The network
sees an RGB 720×496 copy in [0, 1] and predicts a 2×45×31 inverse map in
[-1, 1]; the map is stretched bilinearly to the output size with the grid's
corners pinned to the image corners (as in the authors' code), converted to
pixel coordinates of the full-resolution original and applied with
`cv2.remap` (linear, border replicated) in horizontal bands so the two
coordinate maps never exist at full size. Output size equals input size; JPEG
in → JPEG quality 92, PNG in → PNG, anything else → PNG; no EXIF is written
(orientation is applied on decode). Above 50 megapixels the run is refused
from the file header as `image_too_large` (413, not retriable) before a pixel
is decoded. Limits, stated in the row's description: one sheet in frame with
its edges at least partly visible; a two-page spread, a stack, a sheet cropped
on three sides — unpredictable; lighting, shadows and focus are untouched; and — measured, not assumed — with
**no document in frame the model still finds one**: a landscape photo came back
as an arbitrarily cropped, stretched frame (grid deviation 0.10 against
0.03–0.06 on real pages), so a caller must check that a document is in the shot
rather than rely on the model to leave a non-document alone.
`scripts/uvdoc_smoke.py` runs the model on photographs with per-stage timings.

### Response files

Blobs render inline only for `image/*` and `audio/*`; everything else is
`Content-Disposition: attachment`. Always `X-Content-Type-Options: nosniff`,
immutable long cache (the ref is generated per payload) and byte-range support
so an audio player can seek.

### Request files a provider has to fetch (required config)

An adapter declares `inline_file_limit_bytes` when it takes file inputs by URL
above some inline cap — Replicate's is 256 KB (`DATA_URI_LIMIT`). Above it the
file must be reachable from outside, which needs **both** `PUBLIC_BLOB_BASE_URL`
and `PUBLIC_BLOB_SIGNING_KEY`: no key, no link is ever issued, so a base URL on
its own delivers nothing.

Unset, this is silent until the first real request from a phone (2–5 MB), long
after the key and the model are in place. So the installation says so itself,
in the three places an operator is already looking:

- `GET /api/v1/capabilities` — the provider's own `warnings[]`, and «Подключения»
  reads «настроен наполовину» rather than «рабочий»;
- `GET /api/v1/health/summary` — one line in `work`, per affected provider;
- `POST /api/v1/plan` — `warnings[]`, for the provider that would actually be
  called and only for a task whose signature carries a file. A clean plan for a
  request that cannot deliver its file is a confident wrong answer, and the dry
  run exists to say «сработает ли».

`deploy/DEPLOY.md` and `.env.example` state both variables as required whenever
Replicate is enabled.

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
pair that would be called, a cost estimate that states its assumption, and
`warnings[]`. Two invariants: it never reaches the network
(`RegistryService.get_models` grew `allow_sync=False`), and it answers 200 even
when nothing can run, because "почему запрос не ушёл" is the case the screen
exists for.

The estimate applies the same precedence a finished run does
(`accounting.cost.billing_unit`, shared with `compute_cost`): a per-unit rate for
the group wins, and `compute_price_per_second` is what is left when the group has
none. Replicate hosts image models and bills the seconds its hardware ran, so
reading `image_price` alone made the dry run answer «ставки нет» about a run the
log then charged for — a screen contradicting the outcome is worse than either
answer on its own.

### Registry edits

`GET /api/v1/registry/tree` builds the picker cascade (provider → vendor →
family → variant, with counts and prices) server-side; splitting `model_id` in
the browser guesses at a structure the database stores in its own column.

`GET /api/v1/registry/models?task=…&state=active` is also the *client-facing*
model list, not just the console's: an external caller (agentish) reads it to
offer per-task model choice to its users, behind the same gate as the rest of
`/api/v1/*`. The row shape it relies on: `model_provider`, `model_id`,
`display_name`, `tasks`, `tier`, the price columns plus `price_source_kind` /
`price_is_unknown`, `extra_params` (the knobs a client may show), and
`input_map` — whose *keys* are the attachment roles the model expects, which is
how a client knows to collect a mask or a second photo. Keys of `model_to_dict`
are therefore a contract; `tests/test_web_views.py` already fails on drift.

`PATCH /api/v1/registry/models/{provider}/{model_id}` writes only the columns it
is given, out of a closed set that excludes the pair, `source`, `is_active` and
the catalog dates. Prices arrive as USD per million and are stored per token.
Concurrency is guarded by a `revision` counter, not by `updated_at`: a
second-resolution timestamp cannot tell two edits inside one second apart.

`input_map` and `extra_params` are patchable and, like `confirmed_at`, are never
written by a catalog sync: only a human knows that `garm_img` is the garment or
that this upscaler's `scale` is worth exposing, and the empty defaults would
erase that on every refresh. The model card draws one field per role for rows
that declare an `image-to-*` task and one text field for the parameter
allowlist; `{}` and `[]` clear them.

`extra_params` is a per-model allowlist of parameter names, merged with the
adapter's `SUPPORTED_PARAMS` for that row alone (`adapters/base.merge_params`).
It is not a passthrough and does not widen the gate: a name the model's own
schema has no property for is still dropped and reported, and the value is still
held against that property's enum and range before anything is sent. It counts
as declared for `ignored_params`, so the run that honoured it does not also warn
that it ignored it, and `GET /api/v1/registry/tree` carries it on the variant so
the «Прогон» form can draw a field the pair-level matrix cannot know about.
Adding an upscaler is then a registry edit, not an adapter change.

Price protection is keyed on the origin of the **price**, not of the row. A
number a human vouched for (`price_source_kind` of `manual` or `page`) survives
synchronisation while the row stays a catalog row and keeps receiving name,
context and modality updates. `revert_price` hands it back to the catalog.

### Screens

Seven: Обзор · Прогон · Модели · Журнал · Расход · Картинки · Подключения.
Filters, search, page, sorting, the selected row, the active tab and `?log=`
live in the address; the live tail, collapsed nodes, the prompt draft and the
theme do not. «Обзор» carries, above the recent errors, an «Оповещения» card:
channels or «выключены», the mute lists, the counters and the fate of the last
five failures, and «Отправить пробное» (see Notifications (39)).

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

«Журнал» filters by user (substring, with as-you-type подсказки from
`/logs/suggest`), provider, model (substring, datalist), status, task, group,
channel and day (◀ ▶ page by local days; empty date is all days). The table
shows the provider column before the model, tokens split into four columns
(Промпт / Кэш / Ответ / Рассужд.; a row billed by seconds, characters or
images shows its measure in the first of them), and «Запрос» shows the words
that were asked — when `request_text` holds a message list, the cell extracts
the last user message and the JSON stays in the tooltip. The journal opens
straight on the rows: there is no tab in front of them.

An entry opens in a window over the table (a native `<dialog>`; `?log=` in the
address opens it on load, Esc, «Закрыть» and a click on the backdrop close it
and take `log` out of the address). The head is ▲ · «Запись #N» · ▼: the arrows
and the ↑ ↓ keys step to the neighbouring rows under the page's filters
(`/logs/{id}/neighbors`), the button at the end of the list is disabled and
says so. Under the head, every picture the run carried as a strip of
thumbnails labelled with its side (запрос / ответ, plus the role when there
was one), each opening full size in a new tab.

«Расход» is a screen of its own (`/spend`): the same rows as money, with the
period and grouping in the address (`?period=7|30|1|all&group_by=`). It keeps
the token total and splits the same four kinds into their own columns. The
old `/logs?tab=spend` redirects to it (301).

«Картинки» (`/images`) is every picture the log carried, newest first, one card
per picture: the thumbnail (full size in a new tab), the entry number as a
link into the journal's entry window, the side chip, the time, the user label
and model, and the words that were asked. The next page loads as the bottom
comes into view (IntersectionObserver, with an «Ещё» button as the fallback),
and the counter says when the log is out of pictures. The journal's filter
row sits above the grid — user, provider, model, app, status, task, channel,
day — minus the billing group (the task already tells drawn from edited) and
the money and routing checkboxes, which say nothing about a picture. Any
change restarts the feed from the head, and the choice lives in the address.
Both screens mount the row from one component
(`static/js/components/log-filters.js`): the set of boxes is whatever the
template carries, the wiring (подсказки, day pager, glossary lists, restore
from the address) is written once.

One inspector component serves «Прогон» and «Журнал», over the single `Run`
envelope, with five tabs — Запрос, Ответ, Шаги, JSON, Сырьё. «Ответ» opens
first; «Запрос» carries the prompt, the message history with roles, the
attachment (a picture is shown, not only linked) and the routing ask, and the
JSON tab has a block per side. Its timeline has two levels: provider attempts,
and the HTTP retries inside each one with the wait the provider asked for.

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

### Notifications (39)

A run that fails reaches the operator without the operator watching the log.
The router hands every failure that closes a run's own row (not an attempt a
fallback recovered from, not a cancellation) to `notify.Notifier.failed`,
which never raises and never makes the request wait: delivery runs as a task
of its own, and a channel error is logged and counted, not thrown.

Channels come from `.env` and are on only when fully configured: Telegram
(`NOTIFY_TELEGRAM_BOT_TOKEN` + `NOTIFY_TELEGRAM_CHAT_IDS`, comma separated,
plain Bot API `sendMessage` in HTML parse mode with the first line bold and
everything escaped) and e-mail (`NOTIFY_EMAIL_TO` + `NOTIFY_SMTP_HOST`, plus
port/user/password/STARTTLS/from; stdlib `smtplib` in a worker thread). No
channel configured means nothing is ever sent; the console says so.

Three filters stand between a failure and a message, in this order:

1. `NOTIFY_MUTE_CODES` — error codes that stay in the log. The default is
   `content_filter,response_truncated,bad_request,image_too_large`: failures
   that belong to the request, not to the service. `insufficient_credits` is
   deliberately not muted — it is what the channel is for.
2. `NOTIFY_MUTE_APPS` / `NOTIFY_MUTE_USERS` — `request_logs.app` values and
   caller labels whose failures are not news (a test bench).
3. One message per (code, provider, model) per `NOTIFY_COOLDOWN_SECONDS`
   (default 600). Repeats inside the window are counted; the count rides on
   the next message («Ещё N таких за последние 10 мин»).

The message: `⛔ <SELF_APP_NAME>: <code>`, then provider · model · task, then
приложение · метка · канал, then `HTTP <status> · <Type>: <message>`, then
`Запись #<id>: <SELF_APP_URL>/logs?log=<id>`. The e-mail subject is
`[<name>] <code> · provider · model · task`.

`GET /api/v1/notifications` returns the channels, the three mute lists, the
window, counters since start (sent / muted / throttled / failed) and the last
twenty failures with what became of each (`sent`, `muted_code`, `muted_app`,
`muted_user`, `no_channels`, `throttled`, `failed` + the channel's error text).
`POST /api/v1/notifications/test` sends a test message naming the channels
and the filters and answers per-channel results (200 even when a channel
failed; 503 `not_configured` when there is no channel). «Обзор» shows all of
it in a card above the recent errors, with the test button.

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
then in the log, so it still holds after a restart or an eviction. A repeated
key answers with the stored outcome: the result envelope for a run that
succeeded, and for one that failed the *original* error — `code`, `type`,
`provider`, `model_id`, `provider_status`, `retriable` rebuilt from the row by
`views.error_from_log` (the code from `error_code`; on rows older than that
column, from the class name at the head of `error_message`) — with
`replayed: true` added. There is no `replayed` code: a client that branches on
`content_filter` must see it on the second submit as on the first. `batch_id`
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
                       elevenlabs, yandex (SpeechKit TTS v3), codex_cli, claude_cli,
                       local (models computed in-process: pool + semaphore +
                       deadline), local_uvdoc (page unwarping, ONNX/OpenCV)
  registry/            models (RegistryModel), service (per-provider catalog sync)
  routing/router.py    ModelRouter.resolve + dispatch
  accounting/cost.py   compute_cost
  storage/database.py  aiosqlite schema + CRUD + logging + usage
  blobs/store.py       on-disk blob store
  mcp_server/          tools (FastMCP), serialize
  notify/              service (Notifier: mute lists, cooldown, wording),
                       telegram (Bot API), email (smtplib) — failures reach the operator
  web/                 api (JSON, mounted at /api/v1 and /api), views (Run and
                       error envelopes), capabilities (matrix from adapters),
                       labels (the glossary), health (summary + key probes),
                       mcp_summary (tool inventory), runs (the in-process run
                       registry), retention (lifetimes, preview, purge),
                       pages (Jinja2 frames), templates/,
                       static/{app.css, css/tokens.css,
                       js/core/{api,dom,format,state,keys,runs}.js,
                       js/components/{json-view,inspector,timeline}.js,
                       js/pages/{run,models,logs,spend,images,overview,connections}.js},
                       thumbs (previews of stored images, on demand)
scripts/               uvdoc_smoke.py (local UVDoc on photographs, with timings)
deploy/                install.sh, deploy.sh, fetch-models.sh (local model weights,
                       sha256-pinned), the unit, the nginx template, DEPLOY.md
tests/                 conftest (one app per session), test_smoke (chain),
                       test_web (app), test_web_views (envelope parity),
                       test_api_filters (log filters/cursor), test_log_gallery
                       (pictures feed, neighbours, thumbnails), test_blobs
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
                       test_fallback, test_registry_sync, test_auth,
                       test_adapters_local (slots, deadline, roles, zero price
                       with a stand-in model), test_local_uvdoc (header cap,
                       remap geometry; the model when weights are installed),
                       test_local_runs (blobs → runs → blob → usage over HTTP);
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
- Runtime data (`data/router.db`, `data/blobs/`, `data/models/`) and `logs/` are gitignored.
- Local model weights: `deploy/fetch-models.sh` (called by `install.sh` and
  `deploy.sh`) downloads them into `LOCAL_MODELS_DIR` and verifies pinned
  sha256 hashes; the runtime libraries are the `local` extra
  (`pip install -e '.[anthropic,local]'`, as the scripts do).
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
- Local models: UVDoc (Verhoeven, Magne, Sorkine-Hornung, SIGGRAPH Asia 2023;
  https://github.com/tanguymagne/UVDoc, **Apache-2.0**) via the grid-output ONNX
  export https://huggingface.co/fredcallagan/uvdoc-grid-onnx (Apache-2.0,
  `UVDoc_grid.onnx` + `.data`, sha256 pinned in `deploy/fetch-models.sh`).
  Runtime: `onnxruntime` (CPU build), `opencv-python-headless`, `numpy` — the
  `local` extra in `pyproject.toml`; no CUDA, no GUI toolkit.
