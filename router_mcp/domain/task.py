"""Tasks and media — the two axes that replace one coarse `Modality`.

A model's capability cannot be derived from the media it accepts and produces.
`gpt-image-2` behind `codex-oauth` reads images (vision) and writes images, so
"accepts image, produces image" would claim it can edit an existing picture —
which it cannot. The operation has to be declared, not inferred. Every catalog
that got this right does the same thing: OpenAI keeps input/output modality sets
on the model and the *operation* in the endpoint name (`images/generations` vs
`images/edits`), Gemini keeps `supportedGenerationMethods` next to its modality
sets, and HuggingFace names the operation outright in `pipeline_tag`.

So there are two axes here:

* `Media` — what a payload *is*: text, image, audio, video, file.
* `Task` — the operation a caller asks for. The values are HuggingFace's
  `pipeline_tag` vocabulary, which is the nearest thing to a standard and is also
  the wording Replicate uses for its collections. A model declares the *set* of
  tasks it supports; the router checks membership. No set arithmetic on media can
  substitute for that check.

`TaskSignature` types each task in terms of media, so request validation, form
generation and attachment handling read one table instead of branching per task.
The `billing` field points back at `Modality`, which survives as the coarser
billing / endpoint group — see the docstring there for why.

Import direction is one-way (`task` -> `modality`), so the inverse map lives here.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable

from router_mcp.domain.modality import Modality


class Media(str, Enum):
    """What a payload is. Used to type a task's signature, never to route."""

    TEXT = "text"
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    # "Anything else." Deliberately never appears in a signature's `requires`:
    # a task that demands an unspecified kind of file cannot be validated, and
    # `validate_task_inputs` would degenerate into accepting everything.
    FILE = "file"


class Task(str, Enum):
    """The operation a caller asks for, in HuggingFace `pipeline_tag` wording."""

    TEXT_TO_TEXT = "text-to-text"
    IMAGE_TO_TEXT = "image-to-text"  # vision, OCR, captioning
    TEXT_TO_IMAGE = "text-to-image"
    IMAGE_TO_IMAGE = "image-to-image"  # editing, inpainting, upscaling
    SPEECH_TO_TEXT = "speech-to-text"
    TEXT_TO_SPEECH = "text-to-speech"
    TEXT_TO_AUDIO = "text-to-audio"  # music and sound effects, not speech
    TEXT_TO_VIDEO = "text-to-video"
    IMAGE_TO_VIDEO = "image-to-video"
    # The outlier: running a local agent CLI is not a media transform at all.
    # Kept in the same enum because it is a thing a caller asks for and a thing
    # a model row declares; `is_media_transform=False` marks it so anything
    # walking the media axes skips it instead of pretending.
    CLI = "cli"

    @classmethod
    def from_str(cls, value: "str | Task | Modality") -> "Task":
        """Canonical value, a legacy modality name, or a `Modality` member.

        Callers that predate tasks send `modality: "chat"`, and they must keep
        working — the API field is a published contract. Underscores are accepted
        because `text_to_image` is what a caller writes without thinking.
        """
        if isinstance(value, cls):
            return value
        if isinstance(value, Modality):
            return DEFAULT_TASK[value]
        raw = str(value).strip().lower().replace("_", "-")
        try:
            return cls(raw)
        except ValueError:
            pass
        legacy = LEGACY_TASK.get(raw)
        if legacy is not None:
            return legacy
        raise ValueError(f"Unknown task: {value!r}")

    @property
    def signature(self) -> "TaskSignature":
        return TASKS[self]

    @property
    def billing_group(self) -> Modality:
        return TASKS[self].billing


# The name new code uses when it means "the group this is billed in". `Modality`
# stays as the literal name only where a database column or a published API
# field forces it.
BillingGroup = Modality


@dataclass(frozen=True, slots=True)
class TaskSignature:
    """What a task consumes and produces, plus the group it is billed in."""

    requires: frozenset[Media]
    optional: frozenset[Media]
    produces: frozenset[Media]
    billing: Modality
    is_media_transform: bool = True


def _sig(
    requires: Iterable[Media],
    produces: Iterable[Media],
    billing: Modality,
    optional: Iterable[Media] = (),
    *,
    is_media_transform: bool = True,
) -> TaskSignature:
    return TaskSignature(
        requires=frozenset(requires),
        optional=frozenset(optional),
        produces=frozenset(produces),
        billing=billing,
        is_media_transform=is_media_transform,
    )


TASKS: dict[Task, TaskSignature] = {
    Task.TEXT_TO_TEXT: _sig([Media.TEXT], [Media.TEXT], Modality.CHAT),
    # Text is optional: "what is in this picture" has a default question.
    Task.IMAGE_TO_TEXT: _sig([Media.IMAGE], [Media.TEXT], Modality.CHAT, [Media.TEXT]),
    Task.TEXT_TO_IMAGE: _sig([Media.TEXT], [Media.IMAGE], Modality.IMAGE),
    # Both an image and an instruction are required. A mask is a second image,
    # which a *set* of media cannot express — so it stays a parameter (`mask` in
    # the adapter's SUPPORTED_PARAMS), where its per-provider support belongs
    # anyway. A signature answers "what must the caller send", not "how many".
    Task.IMAGE_TO_IMAGE: _sig([Media.IMAGE, Media.TEXT], [Media.IMAGE], Modality.IMAGE),
    # Optional text is the prompt/vocabulary hint every STT provider accepts.
    Task.SPEECH_TO_TEXT: _sig([Media.AUDIO], [Media.TEXT], Modality.STT, [Media.TEXT]),
    Task.TEXT_TO_SPEECH: _sig([Media.TEXT], [Media.AUDIO], Modality.TTS),
    Task.TEXT_TO_AUDIO: _sig([Media.TEXT], [Media.AUDIO], Modality.AUDIO),
    Task.TEXT_TO_VIDEO: _sig([Media.TEXT], [Media.VIDEO], Modality.VIDEO),
    Task.IMAGE_TO_VIDEO: _sig([Media.IMAGE], [Media.VIDEO], Modality.VIDEO, [Media.TEXT]),
    Task.CLI: _sig([Media.TEXT], [Media.TEXT], Modality.CLI, is_media_transform=False),
}

# The task a legacy modality name means. `image` meant generation, never editing:
# the old `generate_image` tool had no way to send an input image.
LEGACY_TASK: dict[str, Task] = {
    Modality.CHAT.value: Task.TEXT_TO_TEXT,
    Modality.IMAGE.value: Task.TEXT_TO_IMAGE,
    Modality.STT.value: Task.SPEECH_TO_TEXT,
    Modality.TTS.value: Task.TEXT_TO_SPEECH,
    Modality.CLI.value: Task.CLI,
    Modality.AUDIO.value: Task.TEXT_TO_AUDIO,
    Modality.VIDEO.value: Task.TEXT_TO_VIDEO,
}

# The task a billing group resolves to when only the group is known — a per-group
# default model id from settings, or an endpoint that pins its group in code.
DEFAULT_TASK: dict[Modality, Task] = {
    Modality.from_str(name): task for name, task in LEGACY_TASK.items()
}


def tasks_for(group: Modality) -> tuple[Task, ...]:
    """Every task billed in this group, in declaration order."""
    return tuple(task for task, sig in TASKS.items() if sig.billing is group)


# Attachment.kind is a three-value contract ("image" | "audio" | "file"), which
# predates video ever arriving. Sniffing the mime lets a video output be recorded
# as video without widening that contract in every adapter at once.
_KIND_MEDIA: dict[str, Media] = {
    "image": Media.IMAGE,
    "audio": Media.AUDIO,
    "video": Media.VIDEO,
    "text": Media.TEXT,
}


def media_of(attachments: Iterable[Any] | None) -> set[Media]:
    """The media present in a list of `Attachment`s.

    Takes anything with `.kind` and `.mime` rather than importing `Attachment`:
    `messages.py` already imports this module's neighbour, and a cycle here would
    cost more than the duck typing does.
    """
    found: set[Media] = set()
    for att in attachments or ():
        mime = str(getattr(att, "mime", "") or "").lower()
        prefix = mime.split("/", 1)[0]
        media = _KIND_MEDIA.get(prefix) or _KIND_MEDIA.get(
            str(getattr(att, "kind", "") or "").lower()
        )
        found.add(media or Media.FILE)
    return found


def validate_task_inputs(task: Task, *, has_text: bool, media: set[Media]) -> None:
    """Raise `ValueError` naming the medium a request is missing.

    One function instead of a per-endpoint check, because the old hard-coded
    "распознавание requires a file" left `image-to-image` with no message at all
    and every future task with the same gap.
    """
    present = set(media)
    if has_text:
        present.add(Media.TEXT)
    missing = task.signature.requires - present
    if not missing:
        return
    names = ", ".join(sorted(m.value for m in missing))
    raise ValueError(
        f"Task {task.value} requires {names}; the request carries "
        f"{', '.join(sorted(m.value for m in present)) or 'nothing'}"
    )


def resolve_task(
    task: "str | Task | Modality | None",
    *,
    fallback: "str | Task | Modality | None" = None,
) -> Task:
    """The task a request means, preferring an explicit `task` over a `modality`."""
    for candidate in (task, fallback):
        if candidate is not None and candidate != "":
            return Task.from_str(candidate)
    return Task.TEXT_TO_TEXT
