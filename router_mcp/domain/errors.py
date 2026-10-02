"""Normalized error hierarchy.

Adapters translate provider-native failures into these so the routing layer,
MCP tools and the web API can map them to clean responses uniformly. This
mirrors the *Error / *ServiceUnavailable / *AccessDenied / *ProviderError shape
the sibling services use, collapsed into one shared hierarchy.

Every class carries the wire contract for the failure it represents: a stable
`code`, the HTTP status the web layer answers with, and whether retrying the
same call could succeed. Callers switch on `code`; `message` is for humans and
may change. See `docs/UI_CONCEPT_v2.md` §17.3 for the envelope built from these.
"""

from __future__ import annotations

from typing import Any


class RouterError(Exception):
    """Base class for all router-level failures.

    Instances collect the fields the error envelope needs. `log_id` is filled in
    by the router after it writes the failure to `request_logs`: without it the
    console cannot offer "open this error in the log", which three screens of
    the concept rely on.
    """

    code = "internal_error"
    http_status = 500
    retriable = False

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        model_id: str | None = None,
        provider_status: int | None = None,
        retry_after_s: float | None = None,
        hint: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.model_id = model_id
        self.provider_status = provider_status
        self.retry_after_s = retry_after_s
        self.hint = hint
        self.log_id: int | None = None


class BadRequest(RouterError):
    """The caller asked for something malformed (bad enum, missing field)."""

    code = "bad_request"
    http_status = 400


class NotFound(RouterError):
    """The addressed resource (log entry, blob, provider row) does not exist."""

    code = "not_found"
    http_status = 404


class ImageTooLarge(BadRequest):
    """An input image above the pixel cap a local model will decode.

    Raised from the file header, before a single pixel is decoded: the point is
    to refuse a 200-megapixel scan with a message rather than have the process
    killed for memory while three full-size buffers are being allocated.
    """

    code = "image_too_large"
    http_status = 413


class UnknownModel(RouterError):
    """No registry row matches the requested model id."""

    code = "unknown_model"
    http_status = 404


class Conflict(RouterError):
    """The resource changed under the caller (optimistic-lock failure)."""

    code = "conflict"
    http_status = 409


class ModelRetired(RouterError):
    """The registry row exists but the provider has retired the model."""

    code = "model_retired"
    http_status = 409


class NotConfigured(RouterError):
    """The selected provider/adapter has no credentials or binary available."""

    code = "not_configured"
    http_status = 503


class ProviderDisabled(NotConfigured):
    """An administrator switched this provider off in the registry.

    Subclasses NotConfigured so a fallback policy treats it the same way (the
    provider cannot serve the request either way), but answers 409: it is a
    local decision, not a missing key.
    """

    code = "provider_disabled"
    http_status = 409


class ModelNotLoaded(NotConfigured):
    """A model computed on this host has no weights on disk.

    An installation fault, not a request fault: `deploy/fetch-models.sh` was
    not run, or the file it fetched is gone. Subclasses NotConfigured so a
    fallback policy may still pick a hosted model, and so «Обзор» reads it as
    "set up", never as "the request was wrong". Retrying the same call cannot
    help, which is what `retriable=False` (inherited) says.
    """

    code = "model_not_loaded"
    http_status = 503


class ProviderUnavailable(RouterError):
    """Transient upstream failure (timeout, 5xx, rate limit exhausted)."""

    code = "provider_unavailable"
    http_status = 502
    retriable = True


class RateLimited(ProviderUnavailable):
    """The provider refused because the caller is over its rate limit.

    Carries `retry_after_s` when the provider named a wait: the console shows it
    on the timeline, and an operator staring at an 8-second answer needs to see
    that most of it was sleep.
    """

    code = "rate_limited"
    http_status = 429


class ProviderTimeout(ProviderUnavailable):
    """The provider did not answer inside the configured read timeout.

    The request was very likely executed and billed upstream, so the log keeps
    `cost_source='cost_unknown'` rather than pretending the run was free.
    """

    code = "provider_timeout"
    http_status = 504


class ComputeTimeout(ProviderUnavailable):
    """A run computed on this host did not finish inside its deadline.

    Not `ProviderTimeout`: that one marks the cost unknown because a remote
    provider very likely ran and billed the request. Local compute costs
    nothing whether it finished or not, and the thread that overran is left
    to finish on its own — it still holds its concurrency slot until it does.
    """

    code = "timeout"
    http_status = 504


class AccessDenied(RouterError):
    """Authentication/authorization failure against the upstream provider."""

    code = "access_denied"
    http_status = 502


class ProviderError(RouterError):
    """Application-level error reported by the provider (bad request, refusal)."""

    code = "provider_error"
    http_status = 502


class ResponseTruncated(ProviderError):
    """Generation hit the output-token ceiling before producing usable content.

    Providers signal this as `finish_reason="length"` / `stop_reason="max_tokens"`
    and often return an empty content block, which reads like "the model said
    nothing". Raising a distinct error (and naming the ceiling in the message)
    points the caller at `max_tokens` instead of at the prompt.
    """

    code = "response_truncated"
    http_status = 422


class EmptyResponse(ProviderError):
    """The provider answered with nothing usable: no text, no picture.

    Not truncation (no `finish_reason="length"`) and not a refusal: the model
    simply said nothing, which OpenRouter hosts do now and then. A property of
    the moment, not of the service, so the operator is not paged about it by
    default (it is in `NOTIFY_MUTE_CODES`); the row stays in the log under its
    own code. Same HTTP status as its parent, so callers see no change but the
    code.
    """

    code = "empty_response"


class ModelUnavailable(ProviderError):
    """The requested model cannot serve the request (unknown/retired upstream).

    Distinct from ProviderError because it is the one provider-side failure a
    fallback policy may retry on a different model.
    """

    code = "model_unavailable"
    http_status = 409


class ContentFiltered(ProviderError):
    """The provider's safety filter refused the input or withheld the output.

    A property of the request, not of the service: the same picture gets the
    same refusal from the same model, so `retriable` stays False, and the
    operator is not paged about it by default (it is in `NOTIFY_MUTE_CODES`).
    Replicate says «flagged as sensitive» / E005, OpenRouter «filtered due to
    the prompt triggering our content management policy», Anthropic ends with
    `stop_reason="refusal"`.
    """

    code = "content_filter"
    http_status = 422


class InsufficientCredits(ProviderError):
    """The provider account has no balance left. An operator has to top it up.

    The one provider error that no retry, fallback or prompt change fixes and
    that every following request of the provider will repeat — which is why it
    is the textbook case for a notification. OpenRouter answers HTTP 402
    «Insufficient credits», DeepSeek 402 «Insufficient Balance».
    """

    code = "insufficient_credits"
    http_status = 402
