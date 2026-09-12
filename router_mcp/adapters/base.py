"""ProviderAdapter ABC + a shared HTTP retry mixin.

`RetryClientMixin` factors out the identical retry loop the sibling services use
(retry on {429,500,502,503,504} with 0.4*attempt backoff; 401 -> AccessDenied),
translating httpx failures into the normalized error hierarchy.
"""

from __future__ import annotations

import abc
import asyncio
import logging
import time
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

import httpx

import re

from router_mcp.domain.errors import (
    AccessDenied,
    ContentFiltered,
    InsufficientCredits,
    ModelUnavailable,
    ProviderError,
    ProviderTimeout,
    ProviderUnavailable,
    RateLimited,
    ResponseTruncated,
)
from router_mcp.domain.messages import RouterRequest, RouterResponse
from router_mcp.domain.modality import Modality
from router_mcp.domain.task import Task
from router_mcp.registry.models import RegistryModel

if TYPE_CHECKING:  # the service imports this module; a runtime import would cycle
    from router_mcp.registry.service import RegistryService

logger = logging.getLogger(__name__)

RETRY_STATUS = {429, 500, 502, 503, 504}

# HTTP retries happen inside one provider attempt and today they are invisible:
# an answer that took eight seconds because of two 429s and a sleep reads on
# screen as "a slow model". The retry loop records each try here; the router
# drains the list once the attempt finishes. A ContextVar (not an attribute on
# the adapter) because one adapter instance serves concurrent requests.
_http_trace: ContextVar[list[dict[str, Any]] | None] = ContextVar("http_trace", default=None)


def start_http_trace() -> None:
    """Begin recording HTTP tries for the current provider attempt."""
    _http_trace.set([])


def take_http_trace() -> list[dict[str, Any]]:
    """Return the tries recorded since `start_http_trace` and stop recording."""
    trace = _http_trace.get() or []
    _http_trace.set(None)
    return trace


def _record_try(**fields: Any) -> None:
    trace = _http_trace.get()
    if trace is not None:
        trace.append(fields)


def parse_retry_after(response: httpx.Response) -> float | None:
    """Seconds the provider asked us to wait, when it said so in a header.

    Only the numeric form is honoured: the HTTP-date form is rare here and
    guessing a clock skew is worse than reporting nothing.
    """
    for header in ("retry-after", "x-ratelimit-reset-after"):
        raw = response.headers.get(header)
        if not raw:
            continue
        try:
            value = float(raw.strip())
        except ValueError:
            continue
        if value >= 0:
            return value
    return None

# "This model id is gone" as the OpenAI-compatible providers phrase it. Matching
# on the text is unpleasant but it is the only signal they give: DeepSeek and
# friends answer 400 with a message, not a typed error code.
MODEL_GONE_RE = re.compile(
    r"model.{0,40}(not found|does not exist|no longer|unknown|invalid|deprecated|retired)"
    r"|(not found|unknown|invalid|deprecated).{0,20}model",
    re.IGNORECASE | re.DOTALL,
)


# The safety filter, as each provider words it. Seen live in this log:
# Replicate «The input or output was flagged as sensitive … (E005)» and «All
# generated images contained NSFW content», OpenRouter HTTP 400 «The response
# was filtered due to the prompt triggering our content management policy»,
# plus the OpenAI-shaped `content_filter` / `moderation_blocked` codes.
CONTENT_FILTER_RE = re.compile(
    r"flagged as sensitive|\(E005\)|NSFW|content management policy"
    r"|content_filter|content filter|moderation_blocked|safety (system|filter)"
    r"|flagged by .{0,30}moderation|violat.{0,20}(usage|content) polic",
    re.IGNORECASE | re.DOTALL,
)

# An empty account. OpenRouter «Insufficient credits» (HTTP 402), DeepSeek
# «Insufficient Balance» (402), the OpenAI-shaped `insufficient_quota`.
NO_CREDITS_RE = re.compile(
    r"insufficient[ _](credits?|balance|quota|funds)|exceeded your current quota"
    r"|payment required|billing hard limit",
    re.IGNORECASE,
)


def provider_error_for(
    message: str, *, provider_status: int | None = None, **fields: Any
) -> ProviderError:
    """The right `ProviderError` subclass for an application-level refusal.

    Every adapter used to raise the base class with the provider's sentence
    inside, and the two failures an operator most needs told apart — «the
    account is empty» and «this picture was refused» — were both a
    `provider_error` with a different message. HTTP 402 is unambiguous; the
    rest is matched on the text, because the text is all the providers give.
    """
    if provider_status == 402 or NO_CREDITS_RE.search(message):
        return InsufficientCredits(message, provider_status=provider_status, **fields)
    if CONTENT_FILTER_RE.search(message):
        return ContentFiltered(message, provider_status=provider_status, **fields)
    return ProviderError(message, provider_status=provider_status, **fields)


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def openai_shaped_usage(raw: Any) -> dict[str, int | None]:
    """Cache and reasoning counts out of an OpenAI-shaped usage block.

    OpenAI, OpenRouter and DeepSeek all put the same facts in different places —
    `prompt_tokens_details.cached_tokens` for the first two,
    `prompt_cache_hit_tokens` for the third. Reading whichever arrived beats a
    branch per provider at each call site.

    Note that in all three the cached count is *inside* `prompt_tokens`, which
    is the contract `Usage` normalizes to (see domain/messages.py).
    """
    if not isinstance(raw, dict):
        return {"cache_read": None, "cache_write": None, "reasoning": None}
    prompt_details = raw.get("prompt_tokens_details")
    completion_details = raw.get("completion_tokens_details")
    cache_read = raw.get("prompt_cache_hit_tokens")
    if cache_read is None and isinstance(prompt_details, dict):
        cache_read = prompt_details.get("cached_tokens")
    reasoning = None
    if isinstance(completion_details, dict):
        reasoning = completion_details.get("reasoning_tokens")
    return {
        "cache_read": _int_or_none(cache_read),
        "cache_write": _int_or_none(raw.get("prompt_cache_write_tokens")),
        "reasoning": _int_or_none(reasoning),
    }


def truncated_error(
    provider: str,
    *,
    max_tokens: Any = None,
    completion_tokens: Any = None,
) -> ResponseTruncated:
    """Build the "hit the output ceiling" error with the numbers that explain it.

    Reasoning models spend their first hundreds of tokens on thinking, so a low
    `max_tokens` yields an empty content block plus finish_reason=length. The
    message must say that outright.
    """
    limit = f"max_tokens={max_tokens}" if max_tokens else "the model's output limit"
    produced = f", produced {completion_tokens} completion tokens" if completion_tokens else ""
    return ResponseTruncated(
        f"{provider} stopped at {limit} before returning any content{produced}. "
        "Raise max_tokens (reasoning models spend the first ~1k tokens thinking)."
    )


def merge_params(
    declared: tuple[str, ...], model: RegistryModel | None
) -> tuple[str, ...]:
    """The adapter's declared parameter names plus this row's own allowlist.

    Order matters and duplicates do not: the adapter's names come first because
    that is the order the console draws them in, and a row repeating one of them
    must not make the field appear twice.
    """
    if model is None or not model.extra_params:
        return tuple(declared)
    out = list(declared)
    for name in model.extra_params:
        if name not in out:
            out.append(name)
    return tuple(out)


def ignored_params(
    adapter: "ProviderAdapter",
    task: Task,
    params: dict[str, Any] | None,
    model: RegistryModel | None = None,
) -> list[str]:
    """Params the caller sent that this adapter will not read.

    Adapters drop `tools`, `response_format`, `seed` and friends without a word,
    so a caller can spend money on a request that quietly ignored half of it.
    Naming the difference is the whole point; when an adapter has not declared
    its parameters we stay silent rather than invent a warning.

    The row's `extra_params` counts as declared here — otherwise a knob an
    operator deliberately allowed through would be reported as ignored on the
    very run that honoured it.
    """
    if not params or adapter.SUPPORTED_PARAMS is None:
        return []
    supported = adapter.SUPPORTED_PARAMS.get(task)
    if supported is None:
        return []
    allowed = merge_params(supported, model)
    return sorted(key for key in params if key not in allowed)


class ProviderAdapter(abc.ABC):
    """Common interface every provider adapter implements."""

    name: str  # service-provider id, e.g. "openrouter"
    # What this provider can be asked to do (ADR-0006). This is the declaration
    # routing checks; it is not derivable from the media the provider handles.
    supported_tasks: set[Task]
    # Environment variable that configures this adapter, if any. Shown by name
    # (never by value) when a request fails with "provider not configured": the
    # operator's next action is to set it.
    env_var: str | None = None

    # Which `request.params` keys this adapter actually reads, per task.
    # One tuple, two consumers: the adapter builds its payload from it and the
    # console renders exactly these fields — a hand-written copy in web/ drifts
    # from the code on the first new adapter. `None` means "not declared yet",
    # which suppresses the "ignored parameter" warning rather than guessing.
    #
    # Keyed by task rather than by group because the knobs genuinely differ:
    # `size` and `quality` belong to generating an image, `strength` and `mask`
    # to editing one, and both are billed the same way.
    SUPPORTED_PARAMS: dict[Task, tuple[str, ...]] | None = None

    # The largest file this provider accepts inline; anything above it has to be
    # handed over as a URL it can fetch, which needs `PUBLIC_BLOB_BASE_URL` and a
    # signing key. `None` means the provider does not take files by link at all,
    # and so has nothing to warn about. Declared here rather than checked in
    # `web/` so the warning names the real number instead of a copy of it.
    inline_file_limit_bytes: int | None = None

    @property
    def supported_modalities(self) -> set[Modality]:
        """The billing groups this provider can be charged in.

        Derived, never declared: a second declaration would drift from the first
        one, and the parameter matrix in `web/` exists precisely because that kind
        of copy always does (see the addendum to ADR-0001).
        """
        return {task.billing_group for task in self.supported_tasks}

    @abc.abstractmethod
    def is_configured(self) -> bool:
        """True if the adapter has the credentials / binary it needs."""

    @abc.abstractmethod
    async def generate(self, request: RouterRequest, model: RegistryModel) -> RouterResponse:
        """Execute the request against the provider and return a normalized response."""

    # Whether the catalog is small enough to keep in the registry wholesale.
    #
    # "full"   — the provider offers a handful of models (Anthropic, DeepSeek,
    #            Codex, the Replicate allowlist) and the sync stores all of them.
    # "browse" — the catalog is a market, not an inventory: OpenRouter lists
    #            hundreds of ids of which five were ever addressed here. The sync
    #            refreshes the rows we already have and never inserts new ones;
    #            the list is fetched on demand instead, and a row appears when an
    #            operator adopts a model or a request comes back with an answer
    #            (ADR-0008).
    catalog_scope: str = "full"

    async def list_catalog(self) -> list[RegistryModel] | None:
        """The provider's current model list, or None if it has no catalog API.

        None and an empty list mean different things: None is "cannot ask"
        (CLI engines, fixed single-model services) and leaves the registry
        untouched; an empty list would mean "the provider offers nothing", which
        the sync treats as "deprecate everything" — so return None when in doubt.
        """
        return None

    def bind_registry(self, registry: RegistryService) -> None:  # noqa: B027
        """Hand the adapter the registry, once it exists.

        Adapters are built before the database; the registry is built after
        both. The one adapter that needs it today is codex-oauth, whose image
        request is led by a text model it must pick from the live rows and
        retire when the backend refuses it (ADR-0010). Everything else ignores
        the call.
        """
        return None

    async def close(self) -> None:  # pragma: no cover - default no-op
        return None


class RetryClientMixin:
    """Mixin providing a shared httpx client with retry/error normalization.

    Subclasses must set `self._client` (an httpx.AsyncClient) and `self._retries`.
    """

    _client: httpx.AsyncClient
    _retries: int

    async def _request_with_retry(
        self, method: str, url: str, *, retries: int | None = None, **kwargs: Any
    ) -> httpx.Response:
        """`retries=0` opts out of the 5xx retry for a non-idempotent call.

        Retrying a POST that *creates* something bills for a second one nobody
        will collect. `retries` is not an httpx keyword, so `**kwargs` stays clean.
        """
        attempts = (self._retries if retries is None else retries) + 1
        started = time.monotonic()
        for attempt in range(1, attempts + 1):
            at_ms = int((time.monotonic() - started) * 1000)
            try:
                response = await self._client.request(method, url, **kwargs)
                status = response.status_code
                if status in (401, 403):
                    _record_try(at_ms=at_ms, status=status)
                    # OpenRouter answers a moderation refusal with 403 too; the
                    # body tells it from a bad key, and the two are worlds apart
                    # for whoever reads the log.
                    if status == 403 and CONTENT_FILTER_RE.search(response.text[:2000]):
                        raise ContentFiltered(
                            f"HTTP 403: {response.text[:500]}", provider_status=status
                        )
                    raise AccessDenied(
                        f"Authorization failed ({status})", provider_status=status
                    )
                if status in RETRY_STATUS:
                    asked = parse_retry_after(response)
                    wait = asked if asked is not None else 0.4 * attempt
                    last = attempt >= attempts
                    _record_try(
                        at_ms=at_ms,
                        status=status,
                        retry_after_s=asked,
                        wait_s=None if last else wait,
                    )
                    if not last:
                        await asyncio.sleep(wait)
                        continue
                    if status == 429:
                        raise RateLimited(
                            f"Provider rate limit reached (HTTP 429) after {attempts} tries",
                            provider_status=status,
                            retry_after_s=asked,
                        )
                    raise ProviderUnavailable(
                        f"Upstream temporarily unavailable (HTTP {status})",
                        provider_status=status,
                    )
                _record_try(at_ms=at_ms, status=status)
                if status >= 400:
                    body = response.text[:500]
                    # A retired model id is the one 4xx a fallback policy may
                    # answer by choosing a different model.
                    if status == 404 or (status == 400 and MODEL_GONE_RE.search(body)):
                        raise ModelUnavailable(
                            f"HTTP {status}: {body}", provider_status=status
                        )
                    raise provider_error_for(f"HTTP {status}: {body}", provider_status=status)
                return response
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                logger.warning("HTTP network error attempt=%s url=%s: %s", attempt, url, exc)
                timed_out = isinstance(exc, httpx.TimeoutException)
                last = attempt >= attempts
                _record_try(
                    at_ms=at_ms,
                    error=f"{type(exc).__name__}: {exc}"[:200],
                    wait_s=None if last else 0.4 * attempt,
                )
                if last:
                    if timed_out:
                        # The provider very likely ran (and billed) the request;
                        # the caller just never saw the answer.
                        raise ProviderTimeout(
                            "Provider did not answer within the read timeout"
                        ) from exc
                    raise ProviderUnavailable("Could not reach upstream provider") from exc
                await asyncio.sleep(0.4 * attempt)
        raise ProviderUnavailable("Upstream unavailable")

    async def close(self) -> None:
        await self._client.aclose()
