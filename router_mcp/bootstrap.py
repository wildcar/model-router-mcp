"""Assemble the application core: DB, adapters, registry, router.

This is the single place that knows how to wire concrete adapters from settings.
Both the FastAPI app (production) and tests build their core here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from router_mcp.adapters.base import ProviderAdapter
from router_mcp.adapters.claude_api import PRICING_PAGE as anthropic_pricing_page
from router_mcp.adapters.claude_api import ClaudeApiAdapter
from router_mcp.adapters.claude_cli import ClaudeCliAdapter
from router_mcp.adapters.codex_cli import CodexCliAdapter
from router_mcp.adapters.codex_image import CodexImageAdapter
from router_mcp.adapters.deepgram import DeepgramAdapter
from router_mcp.adapters.deepseek import PRICING_PAGE as deepseek_pricing_page
from router_mcp.adapters.deepseek import DeepSeekAdapter
from router_mcp.adapters.elevenlabs import ElevenLabsAdapter
from router_mcp.adapters.openrouter import OpenRouterAdapter
from router_mcp.blobs.store import BlobStore
from router_mcp.config import Settings
from router_mcp.domain.modality import Modality
from router_mcp.registry.models import RegistryModel
from router_mcp.registry.service import RegistryService
from router_mcp.routing.router import ModelRouter
from router_mcp.storage.database import Database

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ProviderSpec:
    id: str
    display_name: str
    adapter_key: str
    base_url: str | None
    adapter: ProviderAdapter
    # Where this provider publishes its prices. Shown in the registry console so
    # a number can always be traced back to a source.
    pricing_url: str | None = None
    # Seed models for providers without a live catalog (CLI engines, single-model
    # services). Inserted only when the row does not exist yet: a restart must
    # never resurrect a model the provider has retired.
    seed_models: list[RegistryModel] = field(default_factory=list)


@dataclass(slots=True)
class AppCore:
    settings: Settings
    db: Database
    blobs: BlobStore
    registry: RegistryService
    router: ModelRouter
    adapters: dict[str, ProviderAdapter]

    async def aclose(self) -> None:
        for adapter in self.adapters.values():
            try:
                await adapter.close()
            except Exception:  # pragma: no cover - best effort cleanup
                logger.warning("Error closing adapter %s", adapter.name, exc_info=True)
        try:
            await self.registry.close()
        finally:
            await self.db.close()


def _seed_model(
    sp_id: str,
    provider: str,
    model_id: str,
    name: str,
    modality: Modality,
    price_source: str | None = None,
    **prices,
) -> RegistryModel:
    """A fixed-catalog registry row for a provider without a live catalog."""
    return RegistryModel(
        service_provider_id=sp_id,
        model_provider=provider,
        model_id=model_id,
        display_name=name,
        modalities=(modality,),
        tier=prices.pop("tier", "expensive"),
        source="seed",
        price_source=price_source,
        price_source_kind="estimate",
        **prices,
    )


def _chat_seed(
    sp_id: str, provider: str, model_id: str, name: str,
    prompt_price: float, completion_price: float, price_source: str | None = None,
) -> RegistryModel:
    """A fixed chat registry row with a tier computed from approximate prices.

    These numbers are transcribed by hand and go stale, so they stay marked as
    estimates: costs computed from them are logged with `cost_source='estimated'`
    until someone confirms the price in the registry admin (/registry).
    """
    return RegistryModel(
        service_provider_id=sp_id,
        model_provider=provider,
        model_id=model_id,
        display_name=name,
        modalities=(Modality.CHAT,),
        tier=RegistryModel.compute_tier(prompt_price, completion_price),
        prompt_price=prompt_price,
        completion_price=completion_price,
        source="seed",
        price_source=price_source,
        price_source_kind="estimate",
    )


def _build_adapters(settings: Settings) -> list[ProviderSpec]:
    """Construct every adapter we ship. Phase 1 = OpenRouter; Phase 2 adds
    Codex-image / Deepgram / ElevenLabs; later phases append DeepSeek/Claude/CLI."""
    return [
        ProviderSpec(
            id="openrouter",
            display_name="OpenRouter",
            adapter_key="openrouter",
            base_url=settings.openrouter_base_url,
            pricing_url="https://openrouter.ai/models",
            adapter=OpenRouterAdapter(
                api_key=settings.openrouter_api_key,
                base_url=settings.openrouter_base_url,
                timeout_seconds=settings.http_timeout_seconds,
                retries=settings.http_retries,
            ),
        ),
        ProviderSpec(
            id="codex-oauth",
            display_name="Codex (OAuth image)",
            adapter_key="codex_image",
            base_url=settings.codex_base_url,
            pricing_url="https://platform.openai.com/docs/pricing",
            adapter=CodexImageAdapter(
                auth_path=settings.codex_auth_path,
                base_url=settings.codex_base_url,
                main_model=settings.codex_image_main_model,
                image_model=settings.codex_image_tool_model,
                size=settings.codex_image_size,
                quality=settings.codex_image_quality,
            ),
            seed_models=[
                _seed_model(
                    "codex-oauth", "openai", settings.codex_image_tool_model,
                    "Codex image generation", Modality.IMAGE, tier="expensive",
                )
            ],
        ),
        ProviderSpec(
            id="deepgram",
            display_name="Deepgram (STT)",
            adapter_key="deepgram",
            base_url=settings.deepgram_base_url,
            pricing_url="https://deepgram.com/pricing",
            adapter=DeepgramAdapter(
                api_key=settings.deepgram_api_key,
                base_url=settings.deepgram_base_url,
                model=settings.deepgram_model,
                timeout_seconds=settings.http_timeout_seconds,
                retries=settings.http_retries,
            ),
            seed_models=[
                _seed_model(
                    "deepgram", "deepgram", settings.deepgram_model,
                    "Deepgram speech-to-text", Modality.STT, tier="cheap",
                )
            ],
        ),
        ProviderSpec(
            id="elevenlabs",
            display_name="ElevenLabs (TTS)",
            adapter_key="elevenlabs",
            base_url=settings.elevenlabs_base_url,
            pricing_url="https://elevenlabs.io/pricing",
            adapter=ElevenLabsAdapter(
                api_key=settings.elevenlabs_api_key,
                base_url=settings.elevenlabs_base_url,
                model_id=settings.elevenlabs_tts_model,
                voice_name=settings.elevenlabs_tts_voice_name,
                voice_id=settings.elevenlabs_tts_voice_id,
                timeout_seconds=settings.http_timeout_seconds,
                retries=settings.http_retries,
            ),
            seed_models=[
                _seed_model(
                    "elevenlabs", "elevenlabs", settings.elevenlabs_tts_model,
                    "ElevenLabs text-to-speech", Modality.TTS, tier="expensive",
                )
            ],
        ),
        ProviderSpec(
            id="deepseek",
            display_name="DeepSeek",
            adapter_key="deepseek",
            base_url=settings.deepseek_base_url,
            pricing_url=deepseek_pricing_page,
            adapter=DeepSeekAdapter(
                api_key=settings.deepseek_api_key,
                base_url=settings.deepseek_base_url,
                timeout_seconds=settings.http_timeout_seconds,
                retries=settings.http_retries,
            ),
            # No seeds: DeepSeek renames its models (v4-pro/v4-flash replaced
            # deepseek-chat/deepseek-reasoner) and hardcoded ids kept coming back
            # from restarts long after the provider had retired them. The catalog
            # sync supplies the live list; prices are filled in /registry.
        ),
        ProviderSpec(
            id="anthropic-api",
            display_name="Claude (Anthropic API)",
            adapter_key="claude_api",
            base_url=settings.anthropic_base_url,
            pricing_url=anthropic_pricing_page,
            adapter=ClaudeApiAdapter(
                api_key=settings.anthropic_api_key,
                base_url=settings.anthropic_base_url,
                timeout_seconds=settings.http_timeout_seconds,
                retries=settings.http_retries,
            ),
            seed_models=[
                # Approximate USD/token (Anthropic list pricing) — adjust in /registry.
                # The model list itself is refreshed by the catalog sync; these
                # rows exist so prices survive before anyone edits them.
                _chat_seed("anthropic-api", "anthropic", "claude-haiku-4-5-20251001", "Claude Haiku 4.5", 8e-7, 4e-6, anthropic_pricing_page),
                _chat_seed("anthropic-api", "anthropic", "claude-sonnet-4-6", "Claude Sonnet 4.6", 3e-6, 1.5e-5, anthropic_pricing_page),
                _chat_seed("anthropic-api", "anthropic", "claude-opus-4-8", "Claude Opus 4.8", 1.5e-5, 7.5e-5, anthropic_pricing_page),
            ],
        ),
        ProviderSpec(
            id="codex-cli",
            display_name="Codex CLI",
            adapter_key="codex_cli",
            base_url=None,
            adapter=CodexCliAdapter(
                command=settings.codex_command,
                timeout_sec=settings.cli_run_timeout_sec,
                allowed_cwd_roots=settings.allowed_cwd_roots(),
                default_cwd=settings.cli_default_cwd,
            ),
            seed_models=[
                _seed_model("codex-cli", "openai", "codex", "Codex CLI", Modality.CLI, tier="expensive")
            ],
        ),
        ProviderSpec(
            id="claude-cli",
            display_name="Claude CLI",
            adapter_key="claude_cli",
            base_url=None,
            adapter=ClaudeCliAdapter(
                command=settings.claude_command,
                timeout_sec=settings.cli_run_timeout_sec,
                subscription=settings.claude_cli_subscription,
                allowed_cwd_roots=settings.allowed_cwd_roots(),
                default_cwd=settings.cli_default_cwd,
            ),
            seed_models=[
                _seed_model("claude-cli", "anthropic", "claude", "Claude CLI", Modality.CLI, tier="expensive")
            ],
        ),
    ]


async def build_core(settings: Settings) -> AppCore:
    db = Database(settings.db_path)
    await db.init()

    blobs = BlobStore(settings.blob_dir)

    specs = _build_adapters(settings)
    adapters: dict[str, ProviderAdapter] = {spec.id: spec.adapter for spec in specs}

    # Seed service_providers. `enabled` is an admin toggle: it takes the adapter's
    # configured state on first insert only — later restarts leave it alone
    # (upsert_service_provider skips it on conflict). Dispatch re-checks
    # is_configured() live; /api/registry/providers exposes both flags.
    # Then seed each provider's fixed-catalog models (idempotent upsert).
    for spec in specs:
        await db.upsert_service_provider(
            provider_id=spec.id,
            display_name=spec.display_name,
            adapter=spec.adapter_key,
            base_url=spec.base_url,
            enabled=spec.adapter.is_configured(),
            pricing_url=spec.pricing_url,
        )
        for seed in spec.seed_models:
            await db.seed_registry_model(seed)

    registry = RegistryService(
        db=db,
        adapters=adapters,
        ttl_seconds=settings.model_cache_ttl_seconds,
    )
    # Best-effort initial catalog sync (skips providers without keys/catalogs).
    try:
        await registry.sync_all(force=True)
    except Exception:  # pragma: no cover - never block startup on catalog
        logger.warning("Initial catalog sync failed", exc_info=True)

    defaults = {
        Modality.CHAT: settings.default_chat_model,
        Modality.IMAGE: settings.default_image_model,
        Modality.STT: settings.default_stt_model,
        Modality.TTS: settings.default_tts_model,
    }
    router = ModelRouter(
        db=db, registry=registry, adapters=adapters, blobs=blobs, defaults=defaults
    )

    return AppCore(
        settings=settings,
        db=db,
        blobs=blobs,
        registry=registry,
        router=router,
        adapters=adapters,
    )
