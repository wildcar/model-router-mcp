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
    provider-native usage block verbatim so nothing is lost."""

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    audio_seconds: float | None = None
    images: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


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
