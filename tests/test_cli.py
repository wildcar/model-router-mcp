"""Pure-function tests for the CLI adapters (no subprocess execution)."""

from __future__ import annotations

import pytest

from router_mcp.adapters._subprocess import validate_cwd
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
