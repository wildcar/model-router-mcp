# ADR-0009: A provider whose models are computed in this process

**Status:** Accepted
**Date:** 2026-09-02
**Deciders:** wildcar, with the agent
**Scope:** `adapters/local.py`, `adapters/local_uvdoc.py`, `domain/errors.py`
(`ModelNotLoaded`, `ComputeTimeout`, `ImageTooLarge`), `registry/models.py`
(`description`), `storage/database.py` (`description` column, seed writes the
human-only columns on insert), `bootstrap.py`, `config.py` (`LOCAL_*`),
`deploy/fetch-models.sh`, `pyproject.toml` (`local` extra)

---

## Context

Every adapter so far is a client: HTTP to a vendor, or a subprocess around a
CLI that is itself a client. The first model we wanted that no vendor hosts —
UVDoc, an 8-million-parameter network that straightens a photographed page and
runs on a CPU in under half a second — had nowhere to plug in. Two forces
decided where it should go:

* The applications on the other side of `router-client` are not allowed to
  reach models on their own. That rule is what makes the log the one record of
  what was asked and what it cost; a model loaded inside an application would
  be the first thing outside it.
* The application wants the model the way it wants every other one: a row in
  the registry, an assignment in its admin, a run through `/api/v1/runs` whose
  result is a blob. It should not know, and must not care, that this one
  happens to be computed on the same host.

What is different about computing here rather than there: the CPU is the
router's own, a model file is code the process runs on every request, and a
free model must read as free — the accounting already has a word for "nobody
priced this" and it is the wrong word.

## Decision

A provider `local` with one adapter and a dictionary of models. Each
`LocalModel` declares its id, task, input fields, weight files and two
functions: `load(models_dir, threads)` and `infer(handle, files, params)`. From
the router's side nothing is new: `generate(request, model)`, a
`RouterResponse` with an attachment, the usual `raw_meta` keys
(`applied_input`, `dropped_params`), the usual errors.

Three constraints particular to in-process compute are enforced in the adapter:

| Concern | Mechanism |
|---|---|
| CPU shared with the event loop | a `ThreadPoolExecutor` of `LOCAL_MAX_CONCURRENCY` workers behind an `asyncio.Semaphore` of the same size; a queued run says so on its timeline; each session gets `cores // concurrency` ONNX threads |
| a run that overruns | `asyncio.wait_for` over a shielded future → `ComputeTimeout` (`timeout`, 504, retriable); the thread finishes on its own and releases the slot only then, so the ceiling holds |
| weights not on disk | checked per model on first use → `ModelNotLoaded` (`model_not_loaded`, 503, not retriable), a `NotConfigured` subclass so a fallback policy may still pick a hosted model |

The registry row is a startup seed (`tier='free'`, `image_price=0`,
`price_source_kind='manual'`): a zero with confirmed provenance is `is_free`,
and `compute_cost` yields `$0 computed`. Seeds now also write the human-only
columns (`input_map`, `extra_params`, `description`) — on insert only, so a sync
still cannot erase an operator's edits and the seed cannot overwrite anyone.

A `description` column joins the registry: the caveats of a model ("one sheet
in frame; a two-page spread is unpredictable") are not in any catalog, and the
applications that let a user choose a model need to show them.

Weights are fetched by `deploy/fetch-models.sh` with sha256 hashes pinned in the
script, called from both `install.sh` and `deploy.sh`. A file that changed
upstream fails loudly instead of loading. The runtime libraries are an extra
(`.[local]`: CPU `onnxruntime`, `opencv-python-headless`, `numpy`) because they
are ~100 MB of wheels; `is_configured()` is "those import", checked with
`find_spec` so the router process does not load OpenCV until a request needs it.

UVDoc itself (`local_uvdoc.py`): the grid-output ONNX export, because the
alternative export returns a finished 288×288 picture and a recipe's text does
not survive that. The 45×31 inverse map is stretched with the grid corners
pinned to the image corners (the authors' `align_corners=True`) and applied to
the full-resolution original with `cv2.remap`, in horizontal bands so the two
float32 coordinate maps are never allocated at full size — on a 4 GB host with
two concurrent runs that is the difference between 80 MB and 400 MB per
12-megapixel photo. Above 50 megapixels the run is refused from the file header
(`image_too_large`, 413), before a pixel is decoded.

## Consequences

* An application gets a local model exactly as it gets a hosted one; nothing in
  `router-client` changes.
* The router process grows by the runtime libraries once the first local run
  arrives (roughly 150 MB resident with the model loaded), and two concurrent
  runs take two cores for about half a second each. `LOCAL_MAX_CONCURRENCY=1`
  is the knob if `chat` latency suffers.
* `local/uvdoc` is the only free `image-to-image` row, so an *unhinted*
  `image-to-image` request now scans to it. Applications pin `model_id`; the
  console's picker does too. Noted rather than fixed: an unhinted edit request
  was already "the cheapest whatever" before this row existed.
* `MAX_CONCURRENT_RUNS` (3) and `LOCAL_MAX_CONCURRENCY` (2) are independent
  ceilings; a third local run holds a run slot while it waits for a compute
  slot. Acceptable at these numbers; revisit if either grows.
* Adding a model is a `LocalModel` in `MODELS` plus a line in
  `fetch-models.sh`; the seed row, the deploy and the tests follow from that.

## Alternatives considered

- **Load the model inside the application (agentish).** Breaks the one rule the
  log depends on, and puts a second inference stack next to a web app.
- **A separate microservice behind the Replicate-shaped adapter.** Another
  process, another port, another unit to keep alive, to gain nothing the pool
  and semaphore do not already give for a sub-second CPU model.
- **Core dependencies instead of an extra.** 100 MB in every install, including
  the test environments that never run a model.
- **`is_configured()` false without weights.** Would make the router answer
  `not_configured` (503, "set the variable") for what is a missing file, and
  would never reach the `model_not_loaded` code the application switches on.
- **`cv2.resize` for the grid (the model card's recipe).** Half-pixel centres
  put the grid's corners half a cell inside the image; the authors pin them to
  the corners. The difference is a sliver at the border, but "as in the
  authors' code" is the cheaper thing to explain.
