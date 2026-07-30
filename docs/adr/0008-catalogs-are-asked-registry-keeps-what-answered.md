# ADR-0008: A catalog is a question, the registry keeps what answered

**Status:** Accepted
**Date:** 2026-07-30
**Deciders:** wildcar, with the agent
**Scope:** `adapters/base.py` (`catalog_scope`), `adapters/openrouter.py`,
`adapters/codex_oauth.py` (`list_catalog`), `registry/service.py`
(`browse_provider`, `adopt_models`, `prune_unconfirmed`), `registry/models.py`
(`confirmed_at`), `storage/database.py` (migration + `confirm_registry_model`),
`routing/router.py`, `web/api.py`, `web/static/js/pages/models.js`

---

## Context

The registry mirrored every catalog it could reach, and both halves of that were
wrong in opposite directions.

Too much: OpenRouter publishes 424 model ids. All 424 were stored, all were
active, and all joined the candidate pool that automatic selection sorts by price
— while the log shows that five of them were ever addressed from here. A market
was being treated as an inventory.

Too little: codex-oauth had no `list_catalog()` at all, so its registry rows were
two seeds written by hand — `gpt-5.4` and `gpt-image-2`. The account can address
six text models (`gpt-5.6-sol`, `-terra`, `-luna`, `gpt-5.5`, `gpt-5.4`,
`gpt-5.4-mini`), and nothing in the console could say so. The backend does
publish the list: `GET /backend-api/codex/models?client_version=…`, the same one
the codex CLI caches in `~/.codex/models_cache.json`. It also names, per model,
the reasoning levels that model accepts — `gpt-5.6` takes `ultra`, the
five-series stops at `xhigh` — and that fact exists nowhere else: `/responses`
rejects an unknown level with a 400 that lists values, not models.

Underneath both is one missing fact. A catalog entry says the provider lists an
id; a seed says somebody typed it. Neither says the id works. The only evidence
of that is a response.

## Decision

**`confirmed_at` on `registry_models`.** Set when a request to that exact pair
comes back with an answer, and never moved afterwards — «когда подтвердилась» has
one date. Written by `Database.confirm_registry_model` alone, deliberately absent
from `_model_to_row` so no catalog sync can touch it, and without a revision bump
so it cannot invalidate an edit form somebody has open. A failure confirms
nothing: the model that threw is still an unproven id.

The migration backfills the column from the log (`MAX(created_at)` of the
successful rows for that pair), so a model in daily use does not come out of a
restart looking unproven.

**`ProviderAdapter.catalog_scope`.** `"full"` (the default) means the catalog is
small enough to mirror — Anthropic, DeepSeek, Codex, the Replicate allowlist.
`"browse"` means it is a market: the sync refreshes only the rows that are held
*and active*, inserts none, and reports how many were `offered` and left out.
OpenRouter is the only `"browse"` provider today.

Skipping retired rows is the scope difference in one line, and it was learned on
prod: the first sweep retired 363 rows and the next forced sync brought every one
back, because "seen upstream re-activates" is a rule that means something for a
mirrored inventory and nothing for a market that lists everything always. In a
browse catalog what retired a row was the operator's sweep, and the way back is
`adopt_models`, which is explicit.

**Three operations instead of one.** `sync_provider` reconciles what is stored.
`browse_provider` asks the provider live and stores nothing, annotating each entry
with whether the registry holds it and whether anything ever answered on it.
`adopt_models` writes named rows from the provider's own answer, not from what the
browser displayed. On the console: «Спросить поставщика» on the models page, with
per-row adoption.

**`prune_unconfirmed`** retires what nothing confirmed, keeping four kinds of row:
confirmed by an answer, created or priced by a human, put there by a bootstrap
seed, or mentioned by the log at all — a failed attempt is still the row that
explains an error somebody is reading. It retires, never deletes, and the console
asks twice: a dry run names the rows, then the confirmation matches what is done.

**Per-model parameter options.** The reasoning levels a catalog publishes are
stored as capabilities (`effort:xhigh`) and served on the tree node as
`param_options`. With a model pinned, «Глубина рассуждения» offers exactly that
model's levels; without one, the union the backends accept.

## Consequences

A fresh installation starts with no OpenRouter rows: the provider is reachable,
its list is one click away, and nothing is addressable until a model is adopted.
That is the point — but it means the first OpenRouter run on a new host is two
actions, not one.

Automatic selection now picks from a curated pool. On prod that is the five ids
that were used plus whatever gets adopted, which is a different and much smaller
ladder than «cheapest of 424». Substitution has fewer places to go, and that is
the trade accepted.

A model can only be run after its row exists, so a pinned id that the provider
offers but the registry has never seen still fails with `UnknownModel`. Adoption
is the explicit step in between, on purpose: a row carries prices, and a request
routed to a row nobody created would be billed against nothing.

Codex rows are priced `unknown`, not free. A subscription publishes no per-token
rate, and a zero would make them the cheapest thing in the registry and win every
automatic pick.

## Alternatives considered

- **Keep mirroring OpenRouter, only stop it growing** — leaves 424 rows in the
  selection pool, which is the actual defect. Rejected by the user in favour of
  collapsing to what was used.
- **Delete the unused rows** — frees space and loses the price history and task
  markup a log entry may still reference. Deprecation keeps them one filter away.
- **Auto-adopt on a pinned unknown id** — one action instead of two, but it
  creates unpriced rows silently on the money path, and «дешёвая модель» that
  nobody priced is how three billing groups once billed at exactly zero.
- **Confirm on any dispatch, including failures** — cheaper to implement and
  destroys the one guarantee the column offers.
