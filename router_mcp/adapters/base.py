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
from typing import Any

import httpx

import re

from router_mcp.domain.errors import (
    AccessDenied,
    ModelUnavailable,
    ProviderError,
    ProviderTimeout,
    ProviderUnavailable,
    RateLimited,
    ResponseTruncated,
)
from router_mcp.domain.messages import RouterRequest, RouterResponse
from router_mcp.domain.modality import Modality
from router_mcp.registry.models import RegistryModel

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


def ignored_params(
    adapter: "ProviderAdapter", modality: Modality, params: dict[str, Any] | None
) -> list[str]:
    """Params the caller sent that this adapter will not read.

    Adapters drop `tools`, `response_format`, `seed` and friends without a word,
    so a caller can spend money on a request that quietly ignored half of it.
    Naming the difference is the whole point; when an adapter has not declared
    its parameters we stay silent rather than invent a warning.
    """
    if not params or adapter.SUPPORTED_PARAMS is None:
        return []
    supported = adapter.SUPPORTED_PARAMS.get(modality)
    if supported is None:
        return []
    return sorted(key for key in params if key not in supported)


class ProviderAdapter(abc.ABC):
    """Common interface every provider adapter implements."""

    name: str  # service-provider id, e.g. "openrouter"
    supported_modalities: set[Modality]
    # Environment variable that configures this adapter, if any. Shown by name
    # (never by value) when a request fails with "provider not configured": the
    # operator's next action is to set it.
    env_var: str | None = None

    # Which `request.params` keys this adapter actually reads, per modality.
    # One tuple, two consumers: the adapter builds its payload from it and the
    # console renders exactly these fields — a hand-written copy in web/ drifts
    # from the code on the first new adapter. `None` means "not declared yet",
    # which suppresses the "ignored parameter" warning rather than guessing.
    SUPPORTED_PARAMS: dict[Modality, tuple[str, ...]] | None = None

    @abc.abstractmethod
    def is_configured(self) -> bool:
        """True if the adapter has the credentials / binary it needs."""

    @abc.abstractmethod
    async def generate(self, request: RouterRequest, model: RegistryModel) -> RouterResponse:
        """Execute the request against the provider and return a normalized response."""

    async def list_catalog(self) -> list[RegistryModel] | None:
        """The provider's current model list, or None if it has no catalog API.

        None and an empty list mean different things: None is "cannot ask"
        (CLI engines, fixed single-model services) and leaves the registry
        untouched; an empty list would mean "the provider offers nothing", which
        the sync treats as "deprecate everything" — so return None when in doubt.
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

    async def _request_with_retry(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        attempts = self._retries + 1
        started = time.monotonic()
        for attempt in range(1, attempts + 1):
            at_ms = int((time.monotonic() - started) * 1000)
            try:
                response = await self._client.request(method, url, **kwargs)
                status = response.status_code
                if status in (401, 403):
                    _record_try(at_ms=at_ms, status=status)
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
                    raise ProviderError(f"HTTP {status}: {body}", provider_status=status)
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
