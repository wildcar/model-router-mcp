# ADR-0010: A refusal retires the row, and a leader is chosen from live rows

**Status:** Accepted
**Date:** 2026-09-08
**Deciders:** wildcar, with the agent
**Scope:** `adapters/codex_oauth.py` (`_main_model_ladder`, `_model_gone`,
`_retire`, one retry on the image path), `adapters/base.py` (`bind_registry`),
`registry/service.py` (`retire_model`, `BACKEND_RETIREMENT_HOLD_SECONDS`,
`SyncReport.held`), `registry/models.py` / `storage/database.py` (`retired_by`),
`routing/router.py` (an adapter's own `fallback_reason` reaches the log),
`bootstrap.py`, `config.py`, `.env.example`

---

## Context

ADR-0008 settled what a catalog entry is worth: a question, not proof. The
proof that a model id works is an answer, recorded as `confirmed_at`. Nothing
was said about the opposite direction — what proves an id is *gone* — and the
implicit answer was "the catalog stops listing it, the next sync retires the
row". Codex broke that twice in three days.

**2026-09-05, gpt-5.4.** The backend stopped serving the slug to ChatGPT
accounts; `/responses` answered HTTP 400 «The 'gpt-5.4' model is not supported
when using Codex with a ChatGPT account». The catalog dropped the slug the same
day, so the sync retired the row as designed. It did not help: image generation
does not go through the registry row of the model that leads it.
`CODEX_IMAGE_MAIN_MODEL` was sent verbatim on every image request, and 26 image
requests died before the operator changed the variable.

**2026-09-08, gpt-5.5.** The backend stopped serving the slug — HTTP 404,
`code: model_not_found`, «The model gpt-5.5 does not exist or you do not have
access to it» — while `GET /models` kept listing it (`visibility: list`,
`supported_in_api: true`; the codex CLI's own cache agreed). ADR-0008's
mechanism could not fire: the row cannot go dark until the catalog says so, and
the catalog was wrong. Three user clicks on «Текст в картинку» failed (log
21752..21754); the caller had asked for `gpt-image-2` and read a 404 about a
model it never named, with `hint: null`.

Two facts fall out of this. The picture is drawn by the tool model; the leading
text model only calls `image_generation`, so *which* live slug leads is nearly
irrelevant and any live one will do. And a definitive refusal from the backend is
better evidence than a catalog entry — the same asymmetry as ADR-0008, seen from
the other side.

## Decision

**The leader comes from the registry.** `CODEX_IMAGE_MAIN_MODEL` leads only while
its own `codex-oauth` row is active. Otherwise the provider's active
`text-to-text` rows lead — the ones an answer has confirmed first (ADR-0008),
then by id. The adapter reads them through the registry it is handed at startup
(`ProviderAdapter.bind_registry`, a no-op everywhere else); without one, as in
a bare unit test, the configured slug is the whole ladder.

**A refusal is evidence, and acts at once.** `/responses` answering HTTP 404 with
`model_not_found`, or HTTP 400 «model is not supported», is read by the adapter
as "this slug is gone" and raised as `ModelUnavailable` — the one 4xx a fallback
policy may answer with another model — carrying the slug that died and a hint.
Before raising, the adapter retires the row: `RegistryService.retire_model`
writes `is_active = 0`, `deprecated_at = now`, `retired_by = 'backend'` and,
when the caller knows it, `replaced_by`. This applies to text requests too (log
21759 was a chat pinned to gpt-5.5).

**The image path retries once.** The first call died before any drawing began
(`usage` empty), so repeating it with the next live leader is not a second
generation. At most two leaders per request: a second refusal in a row is not a
coincidence worth a third call. The substitution is not silent — the response
carries `fallback_reason` in `raw_meta`, and the router copies it into the same
`requested_model_id` / `fallback_reason` fields a ladder fallback uses, plus a
`fallback` step on the timeline. When no live leader is left the error names the
retired slugs and the variable to change.

**The catalog does not undo a refusal for a day.** A sync would have revived the
gpt-5.5 row every 30 minutes on the catalog's word, and the next request would
have retired it again — one wasted call per half hour and the automatic pick
landing on a dead id in between. A row with `retired_by = 'backend'` stays
retired through syncs for `BACKEND_RETIREMENT_HOLD_SECONDS` (24 h), reported as
`held` in the sync report. After that the catalog is believed again; if the
backend still refuses, the first request retires the row for another day.
Restoring by hand clears the mark: the button outranks both.

**Defaults follow.** `CODEX_IMAGE_MAIN_MODEL` and `CODEX_COMMAND` default to
`gpt-5.6-sol`, the slug that answered on 2026-09-08. The fallback costs one
refused call per retired leader, so the variable still deserves a live value;
the difference is that a stale one now degrades a request by a few seconds
instead of failing it.

## Consequences

- An image request survives the retirement of its leader without a deploy or a
  restart; the log says which leader drew and why not the configured one.
- The registry can now be written from a request path, not only from a sync or
  a hand. The write is deliberately narrow — one method, one author value, one
  trigger (a typed refusal) — and never raises into the request that caused it.
- A false positive costs a day of one slug being unavailable through the
  registry, undone by the restore button. The two patterns matched are the two
  seen live plus the generic "model … not found" family shared with the other
  OpenAI-shaped providers; a plain 404 that does not mention a model is not one.
- `retired_by` is a new nullable column; every existing retired row reads as
  retired by the catalog or a hand, which is what all of them were.
- Only codex-oauth retires rows on a refusal today. The OpenAI-shaped adapters
  raise `ModelUnavailable` on any 404 (`RetryClientMixin`), which is too broad
  to retire on — and for a browse-scope catalog like OpenRouter a retirement is
  permanent until adopted again, so that side is left as it was.

## Alternatives considered

- **Keep the leader in `.env` and document «поставьте живой слаг»** — the state
  of things before. Two outages in three days, each ending in an operator
  editing a file by hand; the third would have gone the same way.
- **Retire rows from the router on every `ModelUnavailable`** — one place, all
  providers. Rejected: `RetryClientMixin` raises it on any 404, a spurious
  retirement of an OpenRouter row is permanent (browse scope skips retired
  rows), and the router has no way to tell a typed refusal from a bad URL.
- **Let the sync revive the row and pay one wasted call per half hour** —
  simpler, no new column. Rejected because the automatic pick would land on the
  dead id between syncs, which fails a request outright with no fallback policy.
- **Retry with every live leader, not just one** — more resilient on paper. A
  second refusal in a row means the account or the backend is the problem, and
  a third call would only delay the error.
