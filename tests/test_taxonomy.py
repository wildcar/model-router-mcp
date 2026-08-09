"""The task table is load-bearing, so it is checked rather than trusted.

Every branch downstream — billing units, form generation, attachment validation,
candidate filtering — reads `TASKS`. A task missing from that table, or a billing
group with no task pointing at it, would surface as a request priced at zero or a
screen with no controls. Cheaper to fail here.
"""

from __future__ import annotations

import pytest

from router_mcp.domain.messages import Attachment
from router_mcp.domain.modality import Modality
from router_mcp.domain.task import (
    DEFAULT_TASK,
    TASKS,
    Media,
    Task,
    media_of,
    resolve_task,
    tasks_for,
    validate_task_inputs,
)


def test_every_task_has_a_signature():
    assert set(TASKS) == set(Task)


@pytest.mark.parametrize("task", list(Task))
def test_signatures_are_well_formed(task: Task):
    sig = TASKS[task]
    assert sig.produces, f"{task.value} produces nothing"
    assert sig.requires, f"{task.value} requires nothing, so anything matches it"
    assert not (sig.requires & sig.optional), f"{task.value} lists a medium twice"
    for member in sig.requires | sig.optional | sig.produces:
        assert isinstance(member, Media)
    # A required "file" cannot be validated: every attachment satisfies it.
    assert Media.FILE not in sig.requires


@pytest.mark.parametrize("task", list(Task))
def test_every_task_bills_in_exactly_one_group(task: Task):
    group = task.billing_group
    assert isinstance(group, Modality)
    assert [t for t in tasks_for(group) if t is task] == [task]


def test_every_billing_group_has_at_least_one_task():
    """A group with no task is a group `accounting/cost.py` can never be asked
    about — and the reverse, a task with no group, cannot be priced at all."""
    empty = [group.value for group in Modality if not tasks_for(group)]
    assert not empty, f"billing groups nothing routes to: {empty}"


def test_every_group_has_a_default_task():
    """`DEFAULT_CHAT_MODEL` and the five pinned endpoints know only the group."""
    assert set(DEFAULT_TASK) == set(Modality)


@pytest.mark.parametrize(
    "legacy,expected",
    [
        ("chat", Task.TEXT_TO_TEXT),
        ("image", Task.TEXT_TO_IMAGE),
        ("stt", Task.SPEECH_TO_TEXT),
        ("tts", Task.TEXT_TO_SPEECH),
        ("cli", Task.CLI),
    ],
)
def test_legacy_modality_names_still_parse(legacy: str, expected: Task):
    """Callers written before tasks existed send `modality: "chat"` and keep working."""
    assert Task.from_str(legacy) is expected
    assert Modality.from_str(legacy) is expected.billing_group


def test_canonical_and_underscored_values_parse():
    assert Task.from_str("text-to-image") is Task.TEXT_TO_IMAGE
    assert Task.from_str("TEXT_TO_IMAGE") is Task.TEXT_TO_IMAGE
    assert Task.from_str(Task.CLI) is Task.CLI
    assert Task.from_str(Modality.IMAGE) is Task.TEXT_TO_IMAGE


def test_unknown_task_is_rejected_by_name():
    with pytest.raises(ValueError, match="Unknown task"):
        Task.from_str("text-to-hologram")


def test_resolve_task_prefers_the_explicit_field():
    assert resolve_task("image-to-image", fallback="image") is Task.IMAGE_TO_IMAGE
    assert resolve_task(None, fallback="stt") is Task.SPEECH_TO_TEXT
    assert resolve_task("", fallback=None) is Task.TEXT_TO_TEXT


def test_media_of_reads_kind_and_mime():
    assert media_of([Attachment(kind="image", mime="image/png")]) == {Media.IMAGE}
    assert media_of([Attachment(kind="audio", mime="audio/ogg")]) == {Media.AUDIO}
    # Video arrives as kind="file" because Attachment.kind predates it; the mime
    # is what says otherwise.
    assert media_of([Attachment(kind="file", mime="video/mp4")]) == {Media.VIDEO}
    assert media_of([Attachment(kind="file", mime="application/pdf")]) == {Media.FILE}
    assert media_of(None) == set()


def test_validation_names_the_missing_medium():
    with pytest.raises(ValueError, match="requires image"):
        validate_task_inputs(Task.IMAGE_TO_IMAGE, has_text=True, media=set())
    with pytest.raises(ValueError, match="requires audio"):
        validate_task_inputs(Task.SPEECH_TO_TEXT, has_text=False, media=set())
    # Text alone is not enough for editing. An image alone is: a background
    # remover and an upscaler take a picture and nothing else, and demanding an
    # instruction they have nowhere to put made them unreachable.
    validate_task_inputs(Task.IMAGE_TO_IMAGE, has_text=False, media={Media.IMAGE})
    validate_task_inputs(Task.IMAGE_TO_IMAGE, has_text=True, media={Media.IMAGE})
    validate_task_inputs(Task.IMAGE_TO_TEXT, has_text=False, media={Media.IMAGE})


def test_cli_is_marked_as_not_a_media_transform():
    assert Task.CLI.signature.is_media_transform is False
    assert all(
        task.signature.is_media_transform for task in Task if task is not Task.CLI
    )
