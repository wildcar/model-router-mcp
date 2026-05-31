"""The set of request modalities the router understands."""

from __future__ import annotations

from enum import Enum


class Modality(str, Enum):
    CHAT = "chat"
    IMAGE = "image"
    STT = "stt"
    TTS = "tts"
    CLI = "cli"

    @classmethod
    def from_str(cls, value: str) -> "Modality":
        try:
            return cls(value.strip().lower())
        except ValueError as exc:  # pragma: no cover - defensive
            raise ValueError(f"Unknown modality: {value!r}") from exc
