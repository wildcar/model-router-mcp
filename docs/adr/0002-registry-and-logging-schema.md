# ADR-0002: Registry & request-log schema

**Status:** Accepted
**Date:** 2026-05-31
**Deciders:** project owner + Claude
**Scope:** `router_mcp/storage/database.py`, `router_mcp/registry/*`, `router_mcp/accounting/cost.py`

---

## Context

The service must keep a catalog of connected models with the three-level mapping
service-provider → model-provider → model, grouped by cost tier, mixing a live
provider catalog (OpenRouter) with manually registered models. It must log every
request and response keyed by an external user id (users live elsewhere; only the
id arrives), capture token usage, and compute per-request cost — automatically
from registry prices or manually, per request or amortized from a monthly
subscription.

## Decision

SQLite (aiosqlite, raw SQL), single shared connection + write-lock (the ort_bot
pattern). Four tables:

- `service_providers(id, display_name, adapter, enabled, base_url, ...)` — the
  connection adapters; `enabled` reflects whether the adapter is configured.
- `registry_models(... service_provider_id, model_provider, model_id, modality,
  capabilities, tier, prompt_price, completion_price, image_price,
  audio_price_per_second, context_length, source['catalog'|'manual'], is_active,
  pricing_mode['per_request'|'amortized_subscription'], subscription_id, ...)`,
  UNIQUE(service_provider_id, model_id). Catalog and manual rows coexist; manual
  rows win on conflict. `model_provider` is a column (derived from the id prefix),
  not a separate table, since one service-provider fans out to many brands.
- `subscriptions(... monthly_cost, monthly_quota_units, period_start)` — the
  amortization hook; not billed in v1.
- `request_logs(... external_user_id, service_provider_id, model_provider,
  model_id, modality, status, error_message, request_text/ref, response_text/ref,
  prompt/completion/total_tokens, audio_seconds, images, usage_raw, cost_usd,
  cost_source, pricing_mode, latency_ms, created_at)`.

Cost precedence (`accounting/cost.py`): provider/manual override → amortized
subscription → per-request from registry prices (chat: tokens×price; image:
images×image_price; stt/tts: seconds×audio_price_per_second). Decimal arithmetic;
`usage_raw` is always persisted. Tier is computed via OpenRouter-consistent
thresholds (free / <$0.001-per-1k = cheap / else expensive). Binary payloads go to
the disk blob store; the DB stores only refs.

## Consequences

- One merged read path (`RegistryService.get_models` → DB) feeds both routing and
  the admin UI.
- Per-user stats are a simple GROUP BY on `external_user_id`.
- Amortization is structurally present (table + fields + cost branch) without
  committing to billing reports yet.
- Single connection is a known throughput ceiling — deferred.

## Alternatives considered

- **Separate `model_providers` table** — unnecessary normalization; the brand is a
  cheap derived attribute of the model id.
- **Store binary in SQLite BLOBs** — bloats the DB and slows queries; rejected in
  favor of the disk blob store.
- **An ORM (SQLAlchemy)** — heavier than needed; raw SQL matches the sibling repos.
