# ADR-0007: Asynchronous predictions, fetched results, and per-second-of-compute billing

**Status:** Accepted
**Date:** 2026-07-26
**Deciders:** wildcar, with the agent
**Scope:** `adapters/replicate.py`, `adapters/base.py` (`_request_with_retry(retries=)`),
`domain/messages.py` (`Usage.compute_seconds`), `registry/models.py`
(`compute_price_per_second`), `accounting/cost.py`, `web/public_blobs.py`,
`deploy/nginx-model-router.conf`

---

## Context

Every provider here so far has answered in one HTTP call, returned either text or
bytes, and billed per token, per image, per second of audio or per character.
Replicate does none of those things:

* `POST /predictions` starts a job and returns an id. `Prefer: wait` collapses
  the fast cases into one round trip, but a video model runs for minutes.
* The result is a URL on a CDN, and those URLs expire in about an hour.
* Public models are billed by how long their hardware ran, reported as
  `metrics.predict_time`. Official models bill per run or per token instead, and
  the API publishes no price at all for any of them.
* The task is a property of the individual *model*, not of the provider:
  `flux-schnell`, `flux-kontext-pro` and `musicgen` share one key, one API shape
  and one billing unit, and differ only in what they do. This is the provider
  ADR-0006 was written for.

Each of those is a pattern the next such provider (fal.ai, Runway, Modal) will
inherit, so they are decided here rather than inside one adapter.

## Decision

**Create is not retried; polling is.** `RetryClientMixin._request_with_retry`
grows a `retries=` keyword, and the create call passes `0`. A retried POST makes
a second billable prediction nobody will collect. The one exception is HTTP 429,
which means nothing was created, and which the adapter retries itself. GETs keep
the normal retry count.

**Two clocks, deliberately different.** The poll interval starts at one second
and grows by half each time to a ceiling of ten; a ten-minute run is about 65
requests, not 600. Timeline steps follow a separate, coarser ladder — 5, 10, 20,
40, 80, 160, 320 seconds, then every five minutes — because the router caps a run
at 500 steps and a step per poll would spend a tenth of that budget saying
"still going". Each heartbeat carries the last line of the model's own log, which
for a diffusers model is a percentage and is the single most useful thing on
screen while someone waits.

**Cancellation cancels upstream.** `asyncio.CancelledError` sends
`POST /predictions/{id}/cancel` under `asyncio.shield` with a five-second budget,
swallowing errors. Replicate charges by the second until told to stop, so a
cancelled run that does not say so keeps costing money. `Cancel-After` is also
sent on create, so a prediction abandoned by a restart stops on its own.

**Results are downloaded into the blob store**, by a second httpx client with no
credentials — output files live on a different host, and httpx sends client-level
headers to absolute URLs too. Bounded by size (`REPLICATE_MAX_OUTPUT_MB`), by
count, and sequentially rather than gathered. An output too large stays a link
with no bytes and the run still *succeeds*: the money is already spent, and
failing the run would destroy the only record of having spent it.

**Compute seconds are a new billing unit, with their own column.**
`Usage.compute_seconds` and `RegistryModel.compute_price_per_second`, priced in
`accounting/cost.py` as a `compute` term. Explicitly *not* `audio_price_per_second`:
those are seconds of a file, priced by a different rate, and one column holding
both would make it impossible to tell nine seconds of synthesised speech from
nine seconds of GPU. `Modality.AUDIO` and `Modality.VIDEO` are the billing groups
that carry it.

**Synced rows are `price_source_kind='unknown'`**, with the model's page in
`price_source`. That is literally true — the API publishes no price — and it is
what keeps the row out of automatic selection and off the fallback ladder until
an operator types a rate. A run that consumes compute with no rate is logged
`unpriced`, never a confident zero. `tier` is set to `expensive` explicitly,
because `compute_tier(0, 0)` says "free" and a free video model would win every
request that asked for the cheap tier.

**The catalog is an allowlist** (`REPLICATE_MODELS`), each entry fetched by name
so its input schema and current version hash come with it. Not a crawl of
`GET /models`: thousands of rows would drown the picker and churn `deprecated_at`
across the table whenever a page fetch failed mid-crawl. An empty allowlist
returns `None`, never `[]` — an empty list tells the sync to retire every row this
provider has, including hand-added ones. A transient failure raises rather than
returning a partial list, for the same reason. Only a hard 404 omits a model.

**Inputs are mapped through the model's own schema**, by an ordered alias list
(`prompt`/`text`/`text_prompt`…, `image`/`input_image`/`img`…), with the declared
`SUPPORTED_PARAMS` filtered against the properties that model actually has.
Values are range- and enum-checked locally. A run therefore reports three
distinct facts: `ignored_params` (sent, never declared), `dropped_params`
(declared, absent from this model), and `applied_input` (what went on the wire).

**File inputs go by signed, expiring link.** `web/public_blobs.py` issues
`/public/blobs/{ref}?exp=…&sig=…` over a configured public host, with an HMAC
covering the ref and the deadline together. Without `PUBLIC_BLOB_SIGNING_KEY` no
link is ever issued and the route answers 404 — a missing setting fails closed.
The alternative, inlining as a data URI, is capped at 256 KB by the API and does
not fit a photograph.

## Consequences

Easier: a second job-shaped provider is now a subclass-sized amount of work; the
console shows real progress for a long run; a cancelled run stops costing money;
an operator can price a per-second model and have that survive every later sync.

Harder: three new failure modes to reason about (a prediction we lost track of, a
result too large to keep, a version that moved under us), each with its own
handling and its own test. And one more public surface — one path, on its own
host, signed and expiring, but a door that did not exist before.

Accepted risk: the poll trace records every HTTP try into `plan_json`, so a
15-minute run stores ~65 entries, a few KB. `REPLICATE_POLL_DEADLINE_SECONDS`
bounds it; a much longer deadline would want a cap on the trace as well.

Known gap: prices must be typed in by hand from each model's page, because
Replicate publishes none through the API. A helper that reads `GET /hardware` and
suggests a rate from the model's hardware is a reasonable follow-up.

## Alternatives considered

- **Hold the HTTP connection open for the whole prediction.** Rejected: a video
  model runs for minutes, nginx and the run registry both have their own
  timeouts, and the answer would be lost on any restart. `Prefer: wait` already
  gives the one-round-trip case.
- **Return the provider's URL and never download.** Rejected: the links expire in
  about an hour, `router.py` only blobs attachments carrying bytes, and a log
  entry opened tomorrow would point at nothing.
- **Reuse `audio_price_per_second` for compute time.** Rejected: two different
  seconds in one column make every spend report ambiguous, and no query could
  separate them afterwards.
- **Crawl `GET /models` for the catalog.** Rejected above.
- **Pass `request.params` through verbatim.** Rejected: it forfeits
  `ignored_params`, leaves the console with no form to generate, and lets a
  caller set `num_outputs: 50` or a webhook on the operator's money.
- **Serve the whole blob store on the public host.** Rejected: blobs hold what
  users asked and what models answered. One ref, one deadline, one signature.
