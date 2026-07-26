"""Normalized request/response/usage types that cross the adapter boundary.

Every adapter consumes a RouterRequest and produces a RouterResponse, regardless
of provider or modality. Modality asymmetries (audio in for STT, audio out for
TTS, images for IMAGE, token usage for CHAT) are all expressed through the same
shapes: bytes/urls live in Attachment, metering lives in Usage.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from router_mcp.domain.modality import Modality


@dataclass(slots=True)
class Attachment:
    """A binary or referenced payload attached to a request or response.

    Exactly one of `data`, `url`, `ref` is the source of truth for a given
    attachment; the others may be populated as the payload is persisted.
    """

    kind: str  # "image" | "audio" | "file"
    mime: str
    data: bytes | None = None  # inline bytes (STT input, TTS output)
    url: str | None = None  # remote or data: URL (image outputs)
    ref: str | None = None  # blob-store id once persisted to disk


@dataclass(slots=True)
class Usage:
    """Normalized metering. Fields are populated per modality; `raw` keeps the
    provider-native usage block verbatim so nothing is lost.

    The cache/reasoning breakdown is not cosmetic: Anthropic reports cache reads
    outside `input_tokens` and DeepSeek bills a cache hit about an order of
    magnitude cheaper, so a single prompt-token number makes the cost wrong by
    multiples rather than percents. Adapters fill what their provider reports;
    `None` means "not reported", which is not the same as zero.
    """

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None
    audio_seconds: float | None = None
    images: int | None = None
    characters: int | None = None  # TTS is billed per character, not per token
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class FallbackPolicy:
    """What to do when the requested model is retired, unknown or unavailable.

    Callers pin a model id and then find out weeks later that the provider
    renamed it. This lets them say up-front which neighbour is acceptable
    instead of failing the batch.
    """

    OFF = "off"
    SAME = "same"  # nearest price to the requested model
    CHEAPER = "cheaper"  # one step down the price ladder
    PRICIER = "pricier"  # one step up

    policy: str = OFF
    max_steps: int = 1  # how many substitutes to try before giving up
    scope: str = "provider"  # "provider" | "vendor" | "any"

    @property
    def enabled(self) -> bool:
        return self.policy != self.OFF

    @classmethod
    def parse(cls, value: Any) -> "FallbackPolicy | None":
        """Accept `{"policy": "cheaper"}` or the shorthand `"cheaper"`."""
        if value is None:
            return None
        if isinstance(value, FallbackPolicy):
            return value
        if isinstance(value, str):
            value = {"policy": value}
        if not isinstance(value, dict):
            raise ValueError("fallback must be an object or a policy name")
        policy = str(value.get("policy", cls.OFF)).strip().lower()
        if policy not in (cls.OFF, cls.SAME, cls.CHEAPER, cls.PRICIER):
            raise ValueError(
                f"Unknown fallback policy {policy!r} "
                f"(expected {cls.OFF}/{cls.SAME}/{cls.CHEAPER}/{cls.PRICIER})"
            )
        scope = str(value.get("scope", "provider")).strip().lower()
        if scope not in ("provider", "vendor", "any"):
            raise ValueError(f"Unknown fallback scope {scope!r}")
        try:
            max_steps = int(value.get("max_steps", 1))
        except (TypeError, ValueError) as exc:
            raise ValueError("fallback.max_steps must be an integer") from exc
        return cls(policy=policy, max_steps=max(0, min(max_steps, 5)), scope=scope)


@dataclass(slots=True)
class RouterRequest:
    modality: Modality
    external_user_id: str  # opaque id supplied by the caller; stats key only
    model_id: str | None = None  # explicit model override
    tier: str | None = None  # "free" | "cheap" | "expensive" preference
    provider_hint: str | None = None  # pin a service-provider id
    text: str | None = None  # prompt / single message text
    messages: list[dict[str, Any]] | None = None  # full chat history (OpenAI shape)
    attachments: list[Attachment] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)  # passthrough knobs
    fallback: FallbackPolicy | None = None  # None == off
    # Call provenance, logged but never used for routing or access decisions.
    # Without it the log cannot answer "who is calling this service", which is
    # the question every MCP deployment asks first.
    channel: str = "api"  # "mcp" | "api" | "ui"
    tool: str | None = None  # MCP tool name, when the call came through /mcp
    client_id: str | None = None  # caller-supplied label; untrusted, like the user id
    client_request_id: str | None = None  # caller's idempotency key


@dataclass(slots=True)
class RouterResponse:
    modality: Modality
    provider: str  # service-provider id actually used
    model_id: str  # model actually used
    usage: Usage
    text: str | None = None
    attachments: list[Attachment] = field(default_factory=list)
    raw_meta: dict[str, Any] = field(default_factory=dict)
    # Optional provider-reported cost (e.g. claude CLI total_cost_usd); when set
    # it overrides registry-based cost computation.
    cost_override_usd: float | None = None
