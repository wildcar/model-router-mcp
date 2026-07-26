# ADR-0001: Provider-adapter abstraction

**Status:** Accepted
**Date:** 2026-05-31
**Deciders:** project owner + Claude
**Scope:** `router_mcp/adapters/*`, `router_mcp/domain/messages.py`, `router_mcp/routing/router.py`

---

## Context

We must route requests to many providers (OpenRouter, DeepSeek, Claude, Deepgram,
ElevenLabs, Codex) spanning different modalities (chat, image, STT, TTS, CLI).
Each upstream has a different request/response shape, auth, and usage block. The
sibling repos already implement several of these as standalone service classes
with a near-identical retry/error skeleton. We want one routing core that does not
branch per provider, and a clean path to add providers later.

## Decision

A single `ProviderAdapter` ABC with one entrypoint:

```python
class ProviderAdapter(abc.ABC):
    name: str
    supported_modalities: set[Modality]
    def is_configured(self) -> bool: ...
    async def generate(self, request: RouterRequest, model: RegistryModel) -> RouterResponse: ...
    async def close(self) -> None: ...
```

All providers consume the same normalized `RouterRequest` and return a
`RouterResponse` (`domain/messages.py`). Modality asymmetries are expressed
through shared shapes: binary payloads live in `Attachment` (bytes/url/ref),
metering in `Usage` (tokens / audio_seconds / images + raw). The identical HTTP
retry loop ({429,500,502,503,504} with backoff; 401/403 → AccessDenied) is lifted
into `RetryClientMixin`. Adapters translate provider failures into the normalized
`domain/errors.py` hierarchy. Adapters never import storage/mcp/web.

## Consequences

- Routing and tools call exactly one method; adding a provider = new adapter file
  + a `service_providers` row + registry rows, with no router changes.
- A single normalized response means the web UI and MCP tools share serialization.
- Cost is overridable per response (`cost_override_usd`) for providers that report
  their own price (e.g. claude CLI `total_cost_usd`).
- Trade-off: a fat `generate()` that dispatches by modality internally, rather than
  separate typed methods — accepted for a uniform call site and simpler routing.

## Alternatives considered

- **One typed method per modality on the interface** — pushes modality branching
  into the router and complicates adding mixed-modality providers.
- **Import sibling service classes directly** — cross-repo imports are brittle;
  we port the proven parsing/retry logic into self-contained adapters instead.

---

## Addendum, 2026-07-26: adapters declare the parameters they read

The interface gains one class attribute:

```python
class ProviderAdapter:
    env_var: str | None = None
    SUPPORTED_PARAMS: dict[Modality, tuple[str, ...]] | None = None
```

`SUPPORTED_PARAMS` maps a modality to the `request.params` keys the adapter
actually reads. It has two consumers and that is the point: the adapter builds
its payload by iterating the same tuple, and `/api/v1/capabilities` serves it to
the console, which draws exactly those fields. A matrix maintained separately in
`web/` diverges from the code on the first new adapter, and both failure modes
are bad — a form offering a field the adapter drops, or hiding one it reads.

Two consequences follow.

- Anything a caller sends outside the declared set comes back on the run as
  `ignored_params`. Adapters used to discard `tools`, `response_format`, `stop`,
  `seed` and `stream` without a word, so a caller could pay for a request that
  quietly ignored half of what they asked for.
- `None` means "not declared yet", which suppresses the warning rather than
  guessing. A new adapter is silent until it says what it reads.

`env_var` names the environment variable that configures the adapter. It is
shown by name only — in the "provider not configured" hint and on the
«Подключения» screen — never by value.

A pair is not always enough for a key: Anthropic makes `max_tokens` mandatory
and defaults it where the OpenAI-shaped providers leave it optional. Those
departures live in `web/capabilities.py` as per-provider model overrides, not in
the adapters.
