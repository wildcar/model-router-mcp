"""Pure-function tests for the response and catalog parsers (no network)."""

from __future__ import annotations

import pytest

from router_mcp.adapters.claude_api import ClaudeApiAdapter
from router_mcp.adapters.deepseek import DeepSeekAdapter
from router_mcp.adapters.openrouter import OpenRouterAdapter
from router_mcp.domain.errors import ProviderError, ResponseTruncated
from router_mcp.domain.messages import RouterRequest
from router_mcp.domain.modality import Modality
from router_mcp.domain.task import Task


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
        task=Task.TEXT_TO_TEXT,
        external_user_id="u",
        messages=[
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": "hi"},
        ],
    )
    system, turns = ClaudeApiAdapter._build_messages(req)
    assert system == "be brief"
    assert turns == [{"role": "user", "content": "hi"}]


# --- the catalog tagging that put chat models into image generation --------
def _entry(**architecture) -> dict:
    return {
        "id": "vendor/model",
        "name": "Model",
        "pricing": {"prompt": "0.000001", "completion": "0.000002"},
        "architecture": architecture,
    }


def test_a_vision_chat_model_is_not_offered_for_drawing():
    """The defect: `image` in the *input* modalities used to tag the row as an
    image producer, so every vision chat model in the catalog became a candidate
    for `generate_image` — priced at `image_price`, which for those rows is zero.
    """
    model = OpenRouterAdapter._parse_catalog_model(
        _entry(input_modalities=["text", "image"], output_modalities=["text"])
    )
    assert model.tasks == (Task.TEXT_TO_TEXT, Task.IMAGE_TO_TEXT)
    assert Task.TEXT_TO_IMAGE not in model.tasks
    # Its billing group follows from the tasks, so it is a chat row.
    assert model.modalities == (Modality.CHAT,)
    assert "vision" in model.capabilities


def test_a_model_that_emits_images_gets_the_drawing_task():
    model = OpenRouterAdapter._parse_catalog_model(
        _entry(input_modalities=["text"], output_modalities=["image"])
    )
    assert Task.TEXT_TO_IMAGE in model.tasks


def test_a_plain_text_model_is_text_to_text():
    model = OpenRouterAdapter._parse_catalog_model(
        _entry(input_modalities=["text"], output_modalities=["text"])
    )
    assert model.tasks == (Task.TEXT_TO_TEXT,)


def test_an_empty_architecture_block_is_a_chat_model():
    """Plenty of catalog entries carry no architecture at all, and guessing
    anything but chat there would put them on the wrong screen."""
    model = OpenRouterAdapter._parse_catalog_model({"id": "vendor/model", "pricing": {}})
    assert model.tasks == (Task.TEXT_TO_TEXT,)


def test_the_legacy_modality_string_is_understood():
    """Older entries describe themselves as `text+image->text` in one string."""
    model = OpenRouterAdapter._parse_catalog_model(_entry(modality="text+image->text"))
    assert model.tasks == (Task.TEXT_TO_TEXT, Task.IMAGE_TO_TEXT)


def test_the_deepseek_catalog_row_declares_both_axes():
    """A row that declares neither axis can never be selected, and the failure is
    silent — it just stops appearing as a candidate. This one broke the whole
    DeepSeek catalog sync once."""
    model = DeepSeekAdapter._parse_catalog_model({"id": "deepseek-v4-pro", "owned_by": "deepseek"})
    assert model.tasks == (Task.TEXT_TO_TEXT,)
    assert model.modalities == (Modality.CHAT,)


def test_a_row_declaring_neither_axis_is_refused():
    from router_mcp.registry.models import RegistryModel

    with pytest.raises(ValueError, match="neither tasks nor modalities"):
        RegistryModel(
            service_provider_id="p", model_provider="v", model_id="m", display_name="m"
        )


# --- codex-oauth: the text and vision branch ------------------------------
def _codex():
    from router_mcp.adapters.codex_oauth import CodexOAuthAdapter

    return CodexOAuthAdapter(
        auth_path=None, base_url="https://example.invalid", main_model="gpt-5.4",
        image_model="gpt-image-2", size="1024x1536", quality="medium",
    )


def test_codex_declares_three_tasks_and_not_editing():
    """The provider this whole taxonomy started from: it draws, it reads, it
    talks — and it cannot change a picture it is handed."""
    adapter = _codex()
    assert adapter.supported_tasks == {
        Task.TEXT_TO_TEXT, Task.IMAGE_TO_TEXT, Task.TEXT_TO_IMAGE
    }
    assert Task.IMAGE_TO_IMAGE not in adapter.supported_tasks
    # The billing groups follow, and are not declared twice.
    assert adapter.supported_modalities == {Modality.CHAT, Modality.IMAGE}


def test_codex_text_payload_carries_the_prompt_and_no_tools():
    from router_mcp.registry.models import RegistryModel

    adapter = _codex()
    model = RegistryModel(
        service_provider_id="codex-oauth", model_provider="openai",
        model_id="gpt-5.4", display_name="gpt-5.4", tasks=(Task.TEXT_TO_TEXT,),
    )
    payload = adapter._text_payload(
        RouterRequest(task=Task.TEXT_TO_TEXT, external_user_id="u", text="привет"), model
    )
    assert payload["model"] == "gpt-5.4"
    assert "tools" not in payload
    assert payload["input"][-1]["content"] == [{"type": "input_text", "text": "привет"}]


def test_codex_vision_payload_inlines_the_image():
    from router_mcp.domain.messages import Attachment
    from router_mcp.registry.models import RegistryModel

    adapter = _codex()
    model = RegistryModel(
        service_provider_id="codex-oauth", model_provider="openai",
        model_id="gpt-5.4", display_name="gpt-5.4", tasks=(Task.IMAGE_TO_TEXT,),
    )
    payload = adapter._text_payload(
        RouterRequest(
            task=Task.IMAGE_TO_TEXT,
            external_user_id="u",
            text="что на картинке",
            attachments=[Attachment(kind="image", mime="image/png", data=b"\x89PNG")],
        ),
        model,
    )
    content = payload["input"][-1]["content"]
    assert content[0]["type"] == "input_text"
    assert content[1]["type"] == "input_image"
    assert content[1]["image_url"].startswith("data:image/png;base64,")


def test_codex_usage_keeps_the_cache_count_inside_the_prompt():
    """`/responses` already reports it that way, which is the Usage contract, so
    the prompt total must not be adjusted."""
    usage = _codex()._parse_usage({
        "usage": {
            "input_tokens": 1200, "output_tokens": 300, "total_tokens": 1500,
            "input_tokens_details": {"cached_tokens": 900},
            "output_tokens_details": {"reasoning_tokens": 120},
        }
    })
    assert usage.prompt_tokens == 1200
    assert usage.cache_read_tokens == 900
    assert usage.reasoning_tokens == 120


def test_codex_image_params_are_no_longer_fixed_by_the_environment():
    adapter = _codex()
    assert adapter.SUPPORTED_PARAMS[Task.TEXT_TO_IMAGE] == ("size", "quality")
    payload = adapter._payload("кот", size="1024x1024", quality="high")
    tool = payload["tools"][0]
    assert tool["size"] == "1024x1024" and tool["quality"] == "high"
