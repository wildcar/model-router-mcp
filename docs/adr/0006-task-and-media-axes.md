# ADR-0006: Task and Media as two axes; Modality demoted to a billing group

**Status:** Accepted
**Date:** 2026-07-26
**Deciders:** wildcar, with the agent
**Scope:** `domain/task.py`, `domain/modality.py`, `domain/messages.py`,
`registry/models.py`, `storage/database.py` (`registry_models.tasks`,
`request_logs.task`), `adapters/base.py` and every adapter, `routing/router.py`,
`accounting/cost.py`, `web/{api,capabilities,labels}.py`, `mcp_server/tools.py`

---

## Context

Until now the router had one classification: `Modality`, five values
(`chat`, `image`, `stt`, `tts`, `cli`). That single enum was simultaneously

1. the routing key (`RouterRequest.modality`),
2. a model's capability declaration (`RegistryModel.modalities`),
3. an adapter's capability declaration (`supported_modalities`, and the key of
   `SUPPORTED_PARAMS`),
4. the billing-unit selector (`accounting/cost.py:_terms`,
   `RegistryModel.has_rate_for`),
5. the identity of an MCP tool and an HTTP endpoint (one of each per value),
6. a log column, its index, and a console filter.

Five values for six jobs was too coarse, and the failure was concrete. The
`codex-oauth` provider was declared `{IMAGE}` with one seeded model,
`gpt-image-2` — because it can produce an image. That declaration says nothing
about the same backend also answering text prompts and reading images (vision),
and nothing about it being unable to *edit* an image it was given.

The naive fix is to declare input and output media sets per model, the way
OpenRouter's catalog does (`architecture.input_modalities` /
`output_modalities`). It does not work. `gpt-image-2` accepts an image (for
vision) and produces an image, so "inputs ⊇ {image} and outputs ⊇ {image}" would
claim image editing is available. The operation is not a function of the media.

Two live defects made the same point from the other end. `openrouter.py`
tagged every vision chat model in the catalog as `image` because `image` appeared
in its input modalities, which put those models into the candidate pool for
`generate_image` and priced them at `image_price` — zero for such rows. And the
fallback ladder sorted candidates by `prompt_price + completion_price`, which is
zero for every image/stt/tts row, so for four groups out of five the ladder was
effectively ordered by `model_id`.

## Decision

Two orthogonal axes, with the operation declared rather than inferred.

**`Media`** — what a payload is: `text`, `image`, `audio`, `video`, `file`. Used
to type a task and to validate a request. Never a routing key. `file` is
deliberately barred from any signature's `requires`: a task demanding an
unspecified kind of file is satisfied by every attachment.

**`Task`** — the operation the caller asks for, in HuggingFace `pipeline_tag`
wording, which is the nearest thing to a standard vocabulary and is also how
Replicate names its collections:

| Task | requires | optional | produces | billing group |
|---|---|---|---|---|
| `text-to-text` | text | | text | `chat` |
| `image-to-text` | image | text | text | `chat` |
| `text-to-image` | text | | image | `image` |
| `image-to-image` | image, text | | image | `image` |
| `speech-to-text` | audio | text | text | `stt` |
| `text-to-speech` | text | | audio | `tts` |
| `text-to-audio` | text | | audio | `audio` |
| `text-to-video` | text | | video | `video` |
| `image-to-video` | image | text | video | `video` |
| `cli` | text | | text | `cli` |

A model declares the **set** of tasks it supports (`RegistryModel.tasks`), an
adapter declares `supported_tasks`, and the router checks membership. Requests
name a task. This is what makes "produces images but cannot edit them"
expressible, and it is how every catalog that got this right does it: OpenAI keeps
media sets on the model and the operation in the endpoint name
(`images/generations` vs `images/edits`), Gemini keeps `supportedGenerationMethods`
alongside its media sets.

**`Modality` survives as the second, coarser level: the billing group, which is
also the endpoint a call arrives through.** `text-to-image` and `image-to-image`
bill identically, per image. Two new groups join the original five: `audio`
(generated music and sound, which nobody bills per character the way `tts` is
billed) and `video`. `Task.billing_group` is the one-way map; `tasks_for()`
inverts it.

`accounting/` keeps taking `Modality` and never learns about `Task`; callers pass
`task.billing_group`. `RouterRequest.task` is the stored field and
`RouterRequest.modality` is a property over it, which leaves every logging and
costing call site both compiling and correct, because those sites genuinely want
the group.

Storage adds columns instead of widening vocabularies: `registry_models.tasks`
and `request_logs.task`, backfilled from `modality` by the existing
`_add_missing_columns` pass. `modality` is never rewritten.

`cli` stays in the `Task` enum as an acknowledged outlier — running a local agent
is not a media transform — and carries `is_media_transform=False` so code walking
the media axes skips it rather than pretending.

## Consequences

Easier: a provider whose models differ only by task (Replicate) needs no special
case; a request for a task no model supports fails with a message naming the task
instead of silently landing on a model that bills for something else; the console
generates its form from a task's signature, so text and a file stop being
mutually exclusive and `image-to-image` becomes expressible at all; adding a
billing unit (per second of provider compute) is one group and one column.

Harder: two names for adjacent concepts, and the risk that new code says
"modality" when it means "task". Mitigations — `BillingGroup` is an alias new
code uses when it means the group, `Modality`'s docstring now says what it is,
and `tests/test_taxonomy.py` fails if any group has no task or any task no group.

Migration: log rows written before the change keep their `modality` and get
`task` backfilled from it, which is exact for four of the five legacy values.
`image` is backfilled as `text-to-image` because the old `generate_image` tool had
no way to send an input image, so no historical row can have meant editing.

Accepted risk: `RegistryModel.modalities` and `.tasks` are both present, derived
from each other. They can drift if a future writer sets only one. The
`__post_init__` derivation and one test are the guard; the intent is that
`modalities` becomes read-only once nothing writes it.

## Alternatives considered

- **Infer the task from input and output media sets.** Rejected: it is exactly
  the inference that produces the false claim about `gpt-image-2`, and
  `text+image -> text` is the same signature for chat-with-vision, OCR and
  captioning.
- **Replace `Modality` outright, one level only.** Rejected: `request_logs.modality`
  is the only record of what was spent, and rewriting it to a finer vocabulary
  forces accounting to learn that vocabulary, forces a data migration across the
  whole log, and loses the ability to ask "was this row billed per image".
  Something would still have to map task to billing unit — i.e. `Modality` again,
  under a new name and without the historical continuity.
- **A `registry_model_tasks` child table.** Rejected: every read goes through
  `SELECT *` and `_row_to_model`; a join would rework the whole storage layer for
  a set that never exceeds ten elements. The comma-joined column with token-exact
  `LIKE` matching is the established idiom here and is exact even for hyphenated
  values.
- **A generic `run_task` MCP tool alongside the verb-named ones.** Rejected for
  now: MCP tools are read by a model, and a single tool with a `task` string and a
  union-of-everything body degrades tool selection. `POST /api/v1/runs` already is
  the generic entry point. Worth revisiting past a dozen tasks.
- **A compatibility shim letting an adapter declare only modalities.** Rejected:
  nine adapters in one repository, and two sources of truth for the parameter
  matrix is what the ADR-0001 addendum exists to forbid.
