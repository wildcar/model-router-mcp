"""How long the log and the response files are kept.

Until now the answer was "forever": rows accumulated and blobs were never
deleted, which is why «Обзор» shows the space they take (§21). A policy has two
halves that must be settable apart, because they cost different things:

  - the log rows are small and are the only record of what was spent, so they
    are worth keeping long;
  - the response files are almost all of the bytes on disk, and a row whose file
    has gone still tells the whole accounting story.

Nothing is deleted implicitly. `preview` says what a purge would remove, the
button on «Обзор» runs it, and the timer only does what the settings ask for —
with both retentions unset (the default) it does nothing at all, which is
exactly how the service behaved before this module existed.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)


def _cutoff(days: int) -> str | None:
    if days <= 0:
        return None
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(
        timespec="seconds"
    )


async def preview(core) -> dict[str, Any]:
    """What a purge would delete right now, without deleting anything."""
    log_days = core.settings.log_retention_days
    blob_days = core.settings.blob_retention_days
    log_cutoff = _cutoff(log_days)
    blob_cutoff = _cutoff(blob_days)

    logs = await core.db.count_logs_before(log_cutoff) if log_cutoff else 0
    blobs: list[str] = []
    if blob_cutoff:
        blobs = await core.db.list_refs_before(blob_cutoff)
    orphans = await _orphans(core)
    return {
        "log_retention_days": log_days,
        "blob_retention_days": blob_days,
        "log_cutoff": log_cutoff,
        "blob_cutoff": blob_cutoff,
        "logs_to_delete": logs,
        "blobs_to_delete": len(blobs),
        "orphan_blobs": len(orphans),
        "total_logs": await core.db.count_request_logs(),
    }


async def purge(core, *, orphans_too: bool = True) -> dict[str, Any]:
    """Apply the policy. Files first, then the rows that named them.

    That order is deliberate: a crash between the two leaves rows pointing at
    files that are gone, which the console renders as «файл не сохранён». The
    other order would leave files nothing points at — invisible bytes that only
    a full sweep would ever find again.
    """
    result = await preview(core)
    blob_cutoff = result["blob_cutoff"]
    log_cutoff = result["log_cutoff"]

    removed_files = 0
    if blob_cutoff:
        for ref in await core.db.list_refs_before(blob_cutoff):
            removed_files += 1 if core.blobs.delete(ref) else 0
    if orphans_too:
        for ref in await _orphans(core):
            removed_files += 1 if core.blobs.delete(ref) else 0

    removed_rows = 0
    if log_cutoff:
        removed_rows = await core.db.delete_logs_before(log_cutoff)

    logger.info("Retention purge: %d rows, %d files", removed_rows, removed_files)
    return {**result, "deleted_logs": removed_rows, "deleted_blobs": removed_files}


async def _orphans(core) -> list[str]:
    """Files on disk that no log row points at.

    They exist because a run can store a file and then fail before its row is
    written, and because rows were deletable long before files were.
    """
    referenced = await core.db.all_referenced_refs()
    return [ref for ref in core.blobs.list_refs() if ref not in referenced]
