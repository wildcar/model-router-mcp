"""Assemble the application core: DB, adapters, registry, router.

This is the single place that knows how to wire concrete adapters from settings.
Both the FastAPI app (production) and tests build their core here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from router_mcp.adapters.base import ProviderAdapter
from router_mcp.adapters.codex_image import CodexImageAdapter
from router_mcp.adapters.deepgram import DeepgramAdapter
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
    # Fixed-catalog seed models for providers without a live catalog (everything
    # except OpenRouter, whose catalog is synced by RegistryService).
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
    sp_id: str, provider: str, model_id: str, name: str, modality: Modality, **prices
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
        **prices,
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
    ]


async def build_core(settings: Settings) -> AppCore:
    db = Database(settings.db_path)
    await db.init()

    blobs = BlobStore(settings.blob_dir)

    specs = _build_adapters(settings)
    adapters: dict[str, ProviderAdapter] = {spec.id: spec.adapter for spec in specs}

    # Seed service_providers; a provider is enabled iff its adapter is configured.
    # Then seed each provider's fixed-catalog models (idempotent upsert).
    for spec in specs:
        await db.upsert_service_provider(
            provider_id=spec.id,
            display_name=spec.display_name,
            adapter=spec.adapter_key,
            base_url=spec.base_url,
            enabled=spec.adapter.is_configured(),
        )
        for seed in spec.seed_models:
            await db.upsert_registry_model(seed)

    registry = RegistryService(
        db=db,
        openrouter_api_key=settings.openrouter_api_key,
        openrouter_base_url=settings.openrouter_base_url,
        ttl_seconds=settings.model_cache_ttl_seconds,
    )
    # Best-effort initial catalog load (no-op without an API key).
    try:
        await registry.refresh_catalog(force=True)
    except Exception:  # pragma: no cover - never block startup on catalog
        logger.warning("Initial catalog refresh failed", exc_info=True)

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
