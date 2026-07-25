"""Pure-function tests for the DeepSeek and Claude response parsers (no network)."""

from __future__ import annotations

import pytest

from router_mcp.adapters.claude_api import ClaudeApiAdapter
from router_mcp.adapters.deepseek import DeepSeekAdapter
from router_mcp.domain.errors import ProviderError, ResponseTruncated
from router_mcp.domain.messages import RouterRequest
from router_mcp.domain.modality import Modality


def test_deepseek_parse_text_and_usage():
    data = {
        "choices": [{"message": {"content": "  hi there  "}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
    }
    text, usage = DeepSeekAdapter._parse(data)
    assert text == "hi there"
    assert (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens) == (7, 3, 10)


def test_deepseek_parse_empty_raises():
    with pytest.raises(ProviderError):
        DeepSeekAdapter._parse({"choices": [{"message": {"content": ""}}]})


def test_deepseek_truncated_reports_the_ceiling():
    """finish_reason=length with empty content is a max_tokens problem, and the
    message must say so — the alternative is another prompt-debugging session."""
    data = {
        "choices": [{"message": {"content": ""}, "finish_reason": "length"}],
        "usage": {"prompt_tokens": 900, "completion_tokens": 1000},
    }
    with pytest.raises(ResponseTruncated) as exc:
        DeepSeekAdapter._parse(data, max_tokens=1000)
    assert "max_tokens=1000" in str(exc.value)
    assert "1000 completion tokens" in str(exc.value)


def test_claude_truncated_on_max_tokens_stop_reason():
    data = {"content": [], "stop_reason": "max_tokens", "usage": {"output_tokens": 512}}
    with pytest.raises(ResponseTruncated):
        ClaudeApiAdapter._parse(data, max_tokens=512)


def test_claude_parse_blocks_and_usage_mapping():
    data = {
        "content": [{"type": "text", "text": "Hello"}, {"type": "text", "text": " world"}],
        "usage": {"input_tokens": 11, "output_tokens": 4},
        "stop_reason": "end_turn",
    }
    text, usage = ClaudeApiAdapter._parse(data)
    assert text == "Hello world"
    assert usage.prompt_tokens == 11 and usage.completion_tokens == 4
    assert usage.total_tokens == 15


def test_claude_error_payload_raises():
    with pytest.raises(ProviderError):
        ClaudeApiAdapter._parse({"type": "error", "error": {"message": "bad request"}})


def test_claude_build_messages_splits_system():
    req = RouterRequest(
        modality=Modality.CHAT,
        external_user_id="u",
        messages=[
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": "hi"},
        ],
    )
    system, turns = ClaudeApiAdapter._build_messages(req)
    assert system == "be brief"
    assert turns == [{"role": "user", "content": "hi"}]
