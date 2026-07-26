"""Pure-function tests for the CLI adapters (no subprocess execution)."""

from __future__ import annotations

from pathlib import Path

import pytest

from router_mcp.adapters import _subprocess
from router_mcp.adapters._subprocess import resolve_executable, validate_cwd
from router_mcp.adapters.claude_cli import ClaudeCliAdapter
from router_mcp.adapters.codex_cli import CodexCliAdapter
from router_mcp.domain.errors import ProviderError


def test_validate_cwd_default_when_none():
    assert validate_cwd(None, [], "/srv/app") == "/srv/app"


def test_validate_cwd_accepts_allowed_root(tmp_path):
    sub = tmp_path / "proj"
    sub.mkdir()
    assert validate_cwd(str(sub), [str(tmp_path)], "/default") == str(sub.resolve())


def test_validate_cwd_rejects_outside(tmp_path):
    with pytest.raises(ProviderError):
        validate_cwd("/etc", [str(tmp_path)], "/default")


def test_resolve_executable_survives_unreadable_home(monkeypatch):
    # systemd ProtectHome=true masks the service user's home, so probing
    # ~/.nvm raises PermissionError. resolve_executable must not propagate it.
    real_is_dir = Path.is_dir

    def boom_is_dir(self):
        if ".nvm" in str(self):
            raise PermissionError(13, "Permission denied")
        return real_is_dir(self)

    monkeypatch.setattr(_subprocess.shutil, "which", lambda _name: None)
    monkeypatch.setattr(Path, "is_dir", boom_is_dir)
    assert resolve_executable("definitely-not-a-real-binary-xyz") is None


def test_codex_parse_deltas_and_usage():
    stdout = "\n".join([
        '{"type":"item.message.delta","delta":"Hel"}',
        '{"type":"item.message.delta","delta":"lo"}',
        '{"type":"turn.completed","usage":{"input_tokens":5,"output_tokens":2,"total_tokens":7}}',
        "garbage-not-json",
    ])
    text, token_info = CodexCliAdapter._parse_stdout(stdout)
    assert text == "Hello"
    usage = CodexCliAdapter._usage_from_token_info(token_info)
    assert (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens) == (5, 2, 7)


def test_codex_parse_completed_snapshot():
    stdout = '{"type":"item.completed","item":{"type":"agent_message","text":"done"}}'
    text, _ = CodexCliAdapter._parse_stdout(stdout)
    assert text == "done"


def test_claude_parse_result_and_usage():
    out = (
        '{"type":"result","result":"hi there","session_id":"abc",'
        '"total_cost_usd":0.012,"usage":{"input_tokens":10,"output_tokens":3}}'
    )
    data = ClaudeCliAdapter._parse_stdout(out, 0, "")
    assert data["result"] == "hi there"
    assert data["total_cost_usd"] == 0.012
    usage = ClaudeCliAdapter._usage(data["usage"])
    assert usage.total_tokens == 13


def test_claude_error_payload_raises():
    with pytest.raises(ProviderError):
        ClaudeCliAdapter._parse_stdout('{"is_error":true,"result":"boom"}', 1, "")


def test_claude_non_json_raises():
    with pytest.raises(ProviderError):
        ClaudeCliAdapter._parse_stdout("not json", 0, "")


def test_claude_stream_json_takes_the_result_line():
    """`--output-format stream-json` prints a line per turn and ends with the
    same result object the single-object format used to be."""
    out = "\n".join([
        '{"type":"system","subtype":"init","session_id":"s-1"}',
        '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Read"}]}}',
        '{"type":"result","subtype":"success","result":"готово","session_id":"s-1",'
        '"total_cost_usd":0.02,"usage":{"input_tokens":4,"output_tokens":2,'
        '"cache_read_input_tokens":900,"cache_creation_input_tokens":100}}',
    ])
    data = ClaudeCliAdapter._parse_stdout(out, 0, "")
    assert data["result"] == "готово"
    usage = ClaudeCliAdapter._usage(data["usage"])
    # Cache tokens sit outside input_tokens in Anthropic's accounting, and
    # Usage's contract is that prompt_tokens is the whole prompt.
    assert usage.prompt_tokens == 1004
    assert usage.cache_read_tokens == 900
    assert usage.total_tokens == 1006


def test_claude_still_reads_the_single_object_format():
    """An older CLI, or `--output-format json` left in CLAUDE_COMMAND."""
    data = ClaudeCliAdapter._parse_stdout('{"result":"ok","usage":{}}', 0, "")
    assert data["result"] == "ok"


@pytest.mark.asyncio
async def test_stdout_arrives_line_by_line(tmp_path):
    """The whole point of wave 3's CLI change: events are readable while the
    process is still running, not once it has exited."""
    script = tmp_path / "chatty.sh"
    script.write_text('#!/bin/sh\necho one\necho two\necho three\n')
    script.chmod(0o755)

    seen: list[str] = []
    result = await _subprocess.run_subprocess(
        [str(script)], cwd=str(tmp_path), timeout_sec=10, on_line=seen.append
    )
    assert seen == ["one", "two", "three"]
    assert result.stdout == "one\ntwo\nthree\n"
    assert result.return_code == 0


@pytest.mark.asyncio
async def test_cancelling_kills_the_process(tmp_path):
    """Otherwise the CLI keeps running — and keeps spending — with nothing left
    in the service holding a handle to it."""
    import asyncio
    import os
    import signal

    script = tmp_path / "slow.sh"
    script.write_text('#!/bin/sh\necho "pid $$"\nsleep 30\n')
    script.chmod(0o755)

    pids: list[int] = []
    task = asyncio.create_task(
        _subprocess.run_subprocess(
            [str(script)], cwd=str(tmp_path), timeout_sec=30,
            on_line=lambda line: pids.append(int(line.split()[1])),
        )
    )
    for _ in range(100):
        if pids:
            break
        await asyncio.sleep(0.05)
    assert pids, "the script never reported its pid"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.2)
    with pytest.raises(OSError):
        os.kill(pids[0], signal.SIGKILL)   # already gone
