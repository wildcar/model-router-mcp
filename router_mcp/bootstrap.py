"""Assemble the application core: DB, adapters, registry, router.

This is the single place that knows how to wire concrete adapters from settings.
Both the FastAPI app (production) and tests build their core here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from router_mcp.adapters.base import ProviderAdapter
from router_mcp.adapters.openrouter import OpenRouterAdapter
from router_mcp.blobs.store import BlobStore
from router_mcp.config import Settings
from router_mcp.domain.modality import Modality
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


def _build_adapters(settings: Settings) -> list[ProviderSpec]:
    """Construct every adapter we ship. Phase 1 ships OpenRouter; later phases
    append Deepgram/ElevenLabs/Codex/DeepSeek/Claude here."""
    specs: list[ProviderSpec] = [
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
    ]
    return specs


async def build_core(settings: Settings) -> AppCore:
    db = Database(settings.db_path)
    await db.init()

    blobs = BlobStore(settings.blob_dir)

    specs = _build_adapters(settings)
    adapters: dict[str, ProviderAdapter] = {spec.id: spec.adapter for spec in specs}

    # Seed service_providers; a provider is enabled iff its adapter is configured.
    for spec in specs:
        await db.upsert_service_provider(
            provider_id=spec.id,
            display_name=spec.display_name,
            adapter=spec.adapter_key,
            base_url=spec.base_url,
            enabled=spec.adapter.is_configured(),
        )

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
