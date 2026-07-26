"""Shared subprocess helpers for the CLI adapters (codex, claude).

A one-shot runner (capture stdout/stderr with a hard timeout), executable
resolution with PATH + common fallbacks (nvm/npm/.local/bin), and cwd-whitelist
validation. Ported/trimmed from telecodex_bot/telecodex/runner.py.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from router_mcp.domain.errors import ProviderError, ProviderUnavailable

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class CliResult:
    return_code: int
    stdout: str
    stderr: str
    timed_out: bool


def resolve_executable(name: str, fallbacks: tuple[str, ...] = ()) -> str | None:
    """Resolve a CLI binary from PATH, then a few well-known locations."""
    if os.sep in name:
        return name if _is_exec(Path(name)) else None
    found = shutil.which(name)
    if found:
        return found
    home = Path.home()
    candidates: list[Path] = [
        home / ".local/bin" / name,
        home / ".npm/bin" / name,
        *[Path(f) for f in fallbacks],
    ]
    nvm_root = home / ".nvm/versions/node"
    try:
        if nvm_root.is_dir():
            candidates += sorted(nvm_root.glob(f"*/bin/{name}"), reverse=True)
    except OSError:
        # Probed paths under $HOME may be unreadable (e.g. systemd ProtectHome=true
        # masks the service user's home). Treat as "not found" rather than crashing.
        pass
    for c in candidates:
        if _is_exec(c):
            return str(c)
    return None


def _is_exec(path: Path) -> bool:
    try:
        return path.is_file() and os.access(path, os.X_OK)
    except OSError:
        return False


def validate_cwd(requested: str | None, allowed_roots: list[str], default: str) -> str:
    """Return a safe cwd. An explicit cwd must sit under an allowed root."""
    if not requested:
        return default
    rp = Path(requested).resolve()
    for root in allowed_roots:
        root_resolved = Path(root).resolve()
        if rp == root_resolved or str(rp).startswith(str(root_resolved) + os.sep):
            return str(rp)
    raise ProviderError(
        f"cwd {requested!r} is not under an allowed root (set CLI_ALLOWED_CWD_ROOTS)"
    )


# Both engines print one JSON object per line, and a single line carrying a
# large tool result can be big. The default 64 KiB stream limit would make
# `readline` throw the line away, so the buffer is generous.
_STREAM_LIMIT = 8 * 1024 * 1024


async def run_subprocess(
    argv: list[str],
    cwd: str,
    timeout_sec: int,
    env: dict[str, str] | None = None,
    on_line: Callable[[str], None] | None = None,
) -> CliResult:
    """Run a CLI to completion, optionally reporting stdout line by line.

    The output is read as it arrives rather than in one block at the end. Both
    engines emit JSON lines while they work, and a five-minute run whose events
    only become visible after it finishes gives the operator a blank screen and
    no way to tell a slow run from a stuck one.

    Cancellation kills the process. Without that the caller's task would return
    while the CLI kept running — spending money and holding the working
    directory — with nothing left holding a handle to it.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env or os.environ.copy(),
            limit=_STREAM_LIMIT,
        )
    except OSError as exc:
        raise ProviderError(f"Failed to start {argv[0]!r}: {exc}") from exc

    try:
        stdout, stderr = await asyncio.wait_for(
            asyncio.gather(
                _drain(proc.stdout, on_line),
                _drain(proc.stderr, None),
            ),
            timeout=timeout_sec,
        )
        await proc.wait()
    except asyncio.TimeoutError:
        await _terminate(proc)
        raise ProviderUnavailable(f"CLI timed out after {timeout_sec}s") from None
    except asyncio.CancelledError:
        await _terminate(proc)
        raise

    return CliResult(
        return_code=proc.returncode if proc.returncode is not None else -1,
        stdout=stdout,
        stderr=stderr,
        timed_out=False,
    )


async def _drain(
    stream: asyncio.StreamReader | None, on_line: Callable[[str], None] | None
) -> str:
    """Collect a stream line by line, handing each line to `on_line`."""
    if stream is None:
        return ""
    parts: list[str] = []
    while True:
        try:
            raw = await stream.readline()
        except ValueError:
            # A single line longer than the 8 MiB buffer. asyncio has already
            # discarded it; say so in the captured output rather than leaving a
            # gap that looks like the engine simply said nothing.
            logger.warning("Dropped an over-long output line from the CLI")
            parts.append("<line dropped: over 8 MiB>\n")
            continue
        if not raw:
            return "".join(parts)
        line = raw.decode("utf-8", errors="replace")
        parts.append(line)
        if on_line is not None:
            on_line(line.rstrip("\r\n"))


async def _terminate(proc: asyncio.subprocess.Process) -> None:
    try:
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
    finally:
        # Close the pipes now rather than leaving them to the garbage collector,
        # which may run after the event loop is gone and complain about it.
        transport = getattr(proc, "_transport", None)
        if transport is not None:
            transport.close()
