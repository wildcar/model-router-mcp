"""Normalized error hierarchy.

Adapters translate provider-native failures into these so the routing layer,
MCP tools and the web API can map them to clean responses uniformly. This
mirrors the *Error / *ServiceUnavailable / *AccessDenied / *ProviderError shape
the sibling services use, collapsed into one shared hierarchy.
"""

from __future__ import annotations


class RouterError(Exception):
    """Base class for all router-level failures."""


class NotConfigured(RouterError):
    """The selected provider/adapter has no credentials or binary available."""


class ProviderUnavailable(RouterError):
    """Transient upstream failure (timeout, 5xx, rate limit exhausted)."""


class AccessDenied(RouterError):
    """Authentication/authorization failure against the upstream provider."""


class ProviderError(RouterError):
    """Application-level error reported by the provider (bad request, refusal)."""


class ResponseTruncated(ProviderError):
    """Generation hit the output-token ceiling before producing usable content.

    Providers signal this as `finish_reason="length"` / `stop_reason="max_tokens"`
    and often return an empty content block, which reads like "the model said
    nothing". Raising a distinct error (and naming the ceiling in the message)
    points the caller at `max_tokens` instead of at the prompt.
    """


class ModelUnavailable(ProviderError):
    """The requested model cannot serve the request (unknown/retired upstream).

    Distinct from ProviderError because it is the one provider-side failure a
    fallback policy may retry on a different model.
    """
