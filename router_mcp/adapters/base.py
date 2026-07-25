"""ProviderAdapter ABC + a shared HTTP retry mixin.

`RetryClientMixin` factors out the identical retry loop the sibling services use
(retry on {429,500,502,503,504} with 0.4*attempt backoff; 401 -> AccessDenied),
translating httpx failures into the normalized error hierarchy.
"""

from __future__ import annotations

import abc
import asyncio
import logging
from typing import Any

import httpx

import re

from router_mcp.domain.errors import (
    AccessDenied,
    ModelUnavailable,
    ProviderError,
    ProviderUnavailable,
    ResponseTruncated,
)
from router_mcp.domain.messages import RouterRequest, RouterResponse
from router_mcp.domain.modality import Modality
from router_mcp.registry.models import RegistryModel

logger = logging.getLogger(__name__)

RETRY_STATUS = {429, 500, 502, 503, 504}

# "This model id is gone" as the OpenAI-compatible providers phrase it. Matching
# on the text is unpleasant but it is the only signal they give: DeepSeek and
# friends answer 400 with a message, not a typed error code.
MODEL_GONE_RE = re.compile(
    r"model.{0,40}(not found|does not exist|no longer|unknown|invalid|deprecated|retired)"
    r"|(not found|unknown|invalid|deprecated).{0,20}model",
    re.IGNORECASE | re.DOTALL,
)


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


class ProviderAdapter(abc.ABC):
    """Common interface every provider adapter implements."""

    name: str  # service-provider id, e.g. "openrouter"
    supported_modalities: set[Modality]

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
        for attempt in range(1, attempts + 1):
            try:
                response = await self._client.request(method, url, **kwargs)
                if response.status_code in (401, 403):
                    raise AccessDenied(f"Authorization failed ({response.status_code})")
                if response.status_code in RETRY_STATUS:
                    if attempt < attempts:
                        await asyncio.sleep(0.4 * attempt)
                        continue
                    raise ProviderUnavailable(
                        f"Upstream temporarily unavailable (HTTP {response.status_code})"
                    )
                if response.status_code >= 400:
                    body = response.text[:500]
                    # A retired model id is the one 4xx a fallback policy may
                    # answer by choosing a different model.
                    if response.status_code == 404 or (
                        response.status_code == 400 and MODEL_GONE_RE.search(body)
                    ):
                        raise ModelUnavailable(f"HTTP {response.status_code}: {body}")
                    raise ProviderError(f"HTTP {response.status_code}: {body}")
                return response
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                logger.warning("HTTP network error attempt=%s url=%s: %s", attempt, url, exc)
                if attempt >= attempts:
                    raise ProviderUnavailable("Could not reach upstream provider") from exc
                await asyncio.sleep(0.4 * attempt)
        raise ProviderUnavailable("Upstream unavailable")

    async def close(self) -> None:
        await self._client.aclose()
