"""Billing / endpoint groups.

This enum used to be the router's only classification: one value described what
a request was, which model could serve it, how it was billed, which MCP tool and
HTTP endpoint carried it, and how the log was filtered. Six jobs for five values
was too coarse — `codex-oauth` was declared `{IMAGE}` because it can produce an
image, which said nothing about it also reading images (vision) and nothing about
it *not* being able to edit one.

The operation a caller asks for is now `domain.task.Task`, which is what routing
and capability declarations use. What is left here is the coarser second level:
the group a task is **billed** in, which is also the endpoint it arrives through.
`text-to-image` and `image-to-image` both bill per image, so both live in
`IMAGE`; `speech-to-text` and `text-to-speech` count different things and keep
their own groups. Accounting deliberately knows only about this enum
(see `accounting/cost.py`), which is why historical log rows keep pricing exactly
as they did before tasks existed.

`request_logs.modality` and `registry_models.modality` still store these values.
`Task.billing_group` is the one-way mapping; `domain.task.tasks_for()` inverts it.
"""

from __future__ import annotations

from enum import Enum


class Modality(str, Enum):
    CHAT = "chat"  # token-billed text (and vision, which is billed the same way)
    IMAGE = "image"  # billed per generated image
    STT = "stt"  # billed per second of input audio
    TTS = "tts"  # billed per character of synthesized text
    CLI = "cli"  # token-billed local engine run
    # Generated sound and music. Not TTS: nobody bills a music model per
    # character, and the providers that host these models bill per second of
    # their own compute (see `RegistryModel.compute_price_per_second`).
    AUDIO = "audio"
    VIDEO = "video"  # generated video, billed per second of provider compute

    @classmethod
    def from_str(cls, value: str) -> "Modality":
        try:
            return cls(value.strip().lower())
        except ValueError as exc:  # pragma: no cover - defensive
            raise ValueError(f"Unknown modality: {value!r}") from exc
