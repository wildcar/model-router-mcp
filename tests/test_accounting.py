"""Cost computation: the three defects §7 says to fix before drawing «Расход».

The screen would otherwise show confidently wrong numbers:

1. seed rows for image, stt and tts carry zero rates, so three of the five
   modalities billed at exactly nothing and looked like the cheapest models in
   the registry;
2. ElevenLabs is billed per character, and pricing a synthesis by the duration
   of its audio prices the speaking rate;
3. cached prompt tokens cost about a tenth of fresh ones, and reporting them at
   the prompt rate — or, at Anthropic, not counting them at all — is wrong by
   multiples rather than percents.
"""

from __future__ import annotations

import pytest

from router_mcp.accounting.cost import compute_cost
from router_mcp.adapters.claude_api import ClaudeApiAdapter
from router_mcp.adapters.deepseek import DeepSeekAdapter
from router_mcp.domain.messages import Usage
from router_mcp.domain.modality import Modality
from router_mcp.registry.models import RegistryModel


def _model(**kw) -> RegistryModel:
    defaults = dict(
        service_provider_id="p", model_provider="v", model_id="m", display_name="m",
        modalities=(Modality.CHAT,), price_source_kind="api",
    )
    defaults.update(kw)
    return RegistryModel(**defaults)


# --- defect 1: a gap is not a zero --------------------------------------
def test_seed_row_with_no_rates_is_unpriced_not_free():
    seed = _model(modalities=(Modality.TTS,), price_source_kind="estimate")
    result = compute_cost(seed, Modality.TTS, Usage(characters=1200))
    assert result.cost_source == "unpriced"
    assert seed.price_is_unknown is True
    # ...and it must not read as the cheapest thing in the catalog.
    assert seed.is_free is False


def test_a_confirmed_zero_is_still_free():
    """`:free` models exist. A catalog that publishes prices saying zero is a
    fact, unlike a seed row nobody filled in."""
    free = _model(price_source_kind="api")
    assert free.is_free is True
    assert free.price_is_unknown is False
    result = compute_cost(free, Modality.CHAT, Usage(prompt_tokens=10, completion_tokens=5))
    assert result.cost_usd == 0.0
    assert result.cost_source == "computed"


def test_a_request_that_consumed_nothing_is_not_unpriced():
    seed = _model(modalities=(Modality.STT,), price_source_kind="estimate")
    result = compute_cost(seed, Modality.STT, Usage())
    assert result.cost_source != "unpriced"


# --- defect 2: TTS is billed per character ------------------------------
def test_tts_prices_characters_when_the_rate_is_known():
    model = _model(modalities=(Modality.TTS,), character_price=0.00003)
    result = compute_cost(model, Modality.TTS, Usage(characters=1000, audio_seconds=42.0))
    assert result.cost_usd == pytest.approx(0.03)
    assert result.breakdown[0]["kind"] == "characters"


def test_tts_falls_back_to_seconds_when_that_is_all_there_is():
    model = _model(modalities=(Modality.TTS,), audio_price_per_second=0.001)
    result = compute_cost(model, Modality.TTS, Usage(characters=1000, audio_seconds=10.0))
    assert result.cost_usd == pytest.approx(0.01)
    assert result.breakdown[0]["kind"] == "audio"


# --- defect 3: cache and reasoning tokens -------------------------------
def test_cache_reads_are_priced_at_their_own_rate():
    model = _model(prompt_price=0.27e-6, completion_price=1.10e-6, cache_read_price=0.027e-6)
    usage = Usage(prompt_tokens=1042, completion_tokens=318, cache_read_tokens=890)
    result = compute_cost(model, Modality.CHAT, usage)
    # 152 fresh prompt tokens, 890 from cache, 318 of answer.
    expected = 152 * 0.27e-6 + 890 * 0.027e-6 + 318 * 1.10e-6
    assert result.cost_usd == pytest.approx(expected)
    assert result.cost_source == "computed"
    kinds = [t["kind"] for t in result.breakdown]
    assert kinds == ["prompt", "cache_read", "completion"]


def test_an_assumed_cache_rate_marks_the_whole_number_estimated():
    """Charging a cache hit at the prompt rate overstates it several times over,
    so the figure must not look exact."""
    model = _model(prompt_price=0.27e-6, completion_price=1.10e-6)  # no cache rate
    usage = Usage(prompt_tokens=1000, completion_tokens=100, cache_read_tokens=900)
    result = compute_cost(model, Modality.CHAT, usage)
    assert result.cost_source == "estimated"
    assert any(t.get("assumed") for t in result.breakdown)


def test_breakdown_sums_to_the_total():
    model = _model(prompt_price=1e-6, completion_price=2e-6, cache_read_price=1e-7)
    usage = Usage(prompt_tokens=500, completion_tokens=200, cache_read_tokens=300)
    result = compute_cost(model, Modality.CHAT, usage)
    assert sum(t["amount"] for t in result.breakdown) == pytest.approx(result.cost_usd)


# --- notional money stays apart -----------------------------------------
def test_subscription_cost_is_reported_but_marked_notional():
    model = _model(modalities=(Modality.CLI,))
    charged = compute_cost(model, Modality.CLI, Usage(), override_usd=0.041)
    notional = compute_cost(model, Modality.CLI, Usage(), override_usd=0.041, notional=True)
    assert charged.cost_source == "provider"
    # Same number, different truth: one was billed, one was not.
    assert notional.cost_source == "provider_notional"
    assert notional.cost_usd == 0.041


# --- the adapters normalize to one contract -----------------------------
def test_anthropic_adds_cache_back_into_the_prompt_total():
    """Anthropic reports cache reads outside input_tokens; Usage's contract is
    that prompt_tokens is the whole prompt."""
    data = {
        "content": [{"type": "text", "text": "hi"}],
        "usage": {
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_input_tokens": 900,
            "cache_creation_input_tokens": 50,
        },
    }
    _text, usage = ClaudeApiAdapter._parse(data)
    assert usage.prompt_tokens == 1050
    assert usage.cache_read_tokens == 900
    assert usage.cache_write_tokens == 50


def test_deepseek_keeps_its_prompt_total_and_adds_the_split():
    """DeepSeek already counts hits inside prompt_tokens — adding them again
    would double-count the cheapest part of the request."""
    data = {
        "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 1000,
            "prompt_cache_hit_tokens": 900,
            "prompt_cache_miss_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 1020,
        },
    }
    _text, usage = DeepSeekAdapter._parse(data)
    assert usage.prompt_tokens == 1000
    assert usage.cache_read_tokens == 900


def test_openai_shaped_reasoning_tokens_are_picked_up():
    from router_mcp.adapters.base import openai_shaped_usage

    details = openai_shaped_usage({
        "prompt_tokens_details": {"cached_tokens": 64},
        "completion_tokens_details": {"reasoning_tokens": 512},
    })
    assert details["cache_read"] == 64
    assert details["reasoning"] == 512


# --- the compute-second unit (Replicate and anything hosted like it) ------
def test_compute_seconds_are_their_own_term():
    """A hosted model bills for how long its hardware ran. That is a different
    quantity from the length of the file it produced, and a different rate."""
    model = _model(
        modalities=(Modality.VIDEO,),
        compute_price_per_second=0.002,
        price_source_kind="manual",
    )
    result = compute_cost(model, Modality.VIDEO, Usage(compute_seconds=42.5))
    assert result.cost_usd == pytest.approx(42.5 * 0.002)
    assert result.cost_source == "computed"
    assert [t["kind"] for t in result.breakdown] == ["compute"]
    assert result.breakdown[0]["label"] == "секунды расчёта"


def test_seconds_of_audio_do_not_pay_for_seconds_of_compute():
    """The reason the rate needed its own column: a row priced per second of
    *audio* must not silently price a video by its GPU time."""
    model = _model(
        modalities=(Modality.VIDEO,),
        audio_price_per_second=0.001,
        price_source_kind="manual",
    )
    result = compute_cost(model, Modality.VIDEO, Usage(compute_seconds=30.0))
    assert result.cost_source == "unpriced"
    assert result.cost_usd == 0.0


def test_a_per_unit_rate_wins_over_a_compute_rate():
    """A provider that names a price per image has already said how it bills."""
    model = _model(
        modalities=(Modality.IMAGE,),
        image_price=0.04,
        compute_price_per_second=0.002,
        price_source_kind="manual",
    )
    result = compute_cost(model, Modality.IMAGE, Usage(images=2, compute_seconds=99.0))
    assert result.cost_usd == pytest.approx(0.08)
    assert [t["kind"] for t in result.breakdown] == ["image"]


def test_generated_music_with_no_rate_is_unpriced_not_free():
    model = _model(modalities=(Modality.AUDIO,), price_source_kind="estimate")
    result = compute_cost(model, Modality.AUDIO, Usage(compute_seconds=12.0))
    assert result.cost_source == "unpriced"


def test_only_a_compute_rate_still_counts_as_a_priced_row():
    """Without this the row would read as «цены нет» forever and stay last in
    every automatic selection, even after an operator had priced it."""
    model = _model(
        modalities=(Modality.VIDEO,),
        compute_price_per_second=0.002,
        price_source_kind="manual",
    )
    assert model.price_is_unknown is False
    assert model.has_rate_for(Modality.VIDEO) is True
    assert model.is_free is False
