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


async def run_subprocess(
    argv: list[str], cwd: str, timeout_sec: int, env: dict[str, str] | None = None
) -> CliResult:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env or os.environ.copy(),
        )
    except OSError as exc:
        raise ProviderError(f"Failed to start {argv[0]!r}: {exc}") from exc

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
    except asyncio.TimeoutError:
        await _terminate(proc)
        raise ProviderUnavailable(f"CLI timed out after {timeout_sec}s") from None

    return CliResult(
        return_code=proc.returncode if proc.returncode is not None else -1,
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
        timed_out=False,
    )


async def _terminate(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
