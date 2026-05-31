"""RegistryService — the model catalog.

Generalizes ort_bot/services/model_cache.py: it fetches the OpenRouter live
catalog (TTL-cached), parses each entry into a RegistryModel with a computed
tier, and upserts it into `registry_models` with source='catalog'. Manually
registered rows (source='manual') live in the same table and take precedence on
conflict. All reads go through the DB so the routing layer and the admin UI see
one merged view.
"""

from __future__ import annotations

import asyncio
import logging
import time
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from router_mcp.domain.modality import Modality
from router_mcp.registry.models import RegistryModel
from router_mcp.storage.database import Database

logger = logging.getLogger(__name__)

OPENROUTER_PROVIDER_ID = "openrouter"


class RegistryService:
    def __init__(
        self,
        db: Database,
        openrouter_api_key: str | None,
        openrouter_base_url: str,
        ttl_seconds: int = 1800,
        timeout_seconds: float = 30.0,
        retries: int = 2,
    ) -> None:
        self._db = db
        self._api_key = openrouter_api_key
        self._ttl_seconds = ttl_seconds
        self._retries = retries
        self._expires_at = 0.0
        self._lock = asyncio.Lock()
        self._client = httpx.AsyncClient(
            base_url=openrouter_base_url,
            timeout=timeout_seconds,
            headers={
                "Authorization": f"Bearer {openrouter_api_key or ''}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://localhost",
                "X-Title": "model-router-mcp",
            },
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def refresh_catalog(self, force: bool = False) -> int:
        """Fetch the OpenRouter catalog and upsert it into registry_models.

        Returns the number of catalog models written. No-op (returns 0) when the
        TTL has not expired or no API key is configured.
        """
        if not self._api_key:
            return 0
        now = time.monotonic()
        if not force and now < self._expires_at:
            return 0
        async with self._lock:
            now = time.monotonic()
            if not force and now < self._expires_at:
                return 0
            try:
                response = await self._client.get("/models")
                response.raise_for_status()
            except (httpx.HTTPError,) as exc:
                logger.warning("OpenRouter catalog fetch failed: %s", exc)
                return 0
            payload = response.json()
            raw_models = payload.get("data", []) if isinstance(payload, dict) else []
            written = 0
            for item in raw_models:
                if not isinstance(item, dict):
                    continue
                model = self._parse_catalog_model(item)
                if model is None:
                    continue
                await self._db.upsert_registry_model(model)
                written += 1
            self._expires_at = time.monotonic() + self._ttl_seconds
            logger.info("OpenRouter catalog refreshed: %s models", written)
            return written

    async def get_models(
        self,
        *,
        modality: str | None = None,
        tier: str | None = None,
        provider: str | None = None,
        force_refresh: bool = False,
    ) -> list[RegistryModel]:
        await self.refresh_catalog(force=force_refresh)
        return await self._db.list_registry_models(
            modality=modality, tier=tier, service_provider_id=provider
        )

    # --- catalog parsing (ported from ModelCacheService) ----------------
    @staticmethod
    def _to_float(value: Any) -> float:
        if value is None:
            return 0.0
        try:
            return float(Decimal(str(value)))
        except (InvalidOperation, ValueError, TypeError):
            return 0.0

    @staticmethod
    def _extract_provider(model_id: str) -> str:
        if "/" in model_id:
            return model_id.split("/", 1)[0]
        return "other"

    @classmethod
    def _extract_modalities(cls, raw: dict[str, Any]) -> tuple[Modality, ...]:
        mods: set[Modality] = set()
        architecture = raw.get("architecture")
        if isinstance(architecture, dict):
            modality = architecture.get("modality")
            if isinstance(modality, str) and "image" in modality.lower():
                mods.add(Modality.IMAGE)
            for item in architecture.get("input_modalities", []) or []:
                value = str(item).lower()
                if "image" in value:
                    mods.add(Modality.IMAGE)
                if "text" in value:
                    mods.add(Modality.CHAT)
            for item in architecture.get("output_modalities", []) or []:
                value = str(item).lower()
                if "image" in value:
                    mods.add(Modality.IMAGE)
        if not mods:
            mods.add(Modality.CHAT)
        return tuple(sorted(mods, key=lambda m: m.value))

    @classmethod
    def _parse_catalog_model(cls, raw: dict[str, Any]) -> RegistryModel | None:
        model_id = str(raw.get("id") or "").strip()
        if not model_id:
            return None
        pricing = raw.get("pricing") if isinstance(raw.get("pricing"), dict) else {}
        prompt_price = cls._to_float(pricing.get("prompt"))
        completion_price = cls._to_float(pricing.get("completion"))
        image_price = cls._to_float(pricing.get("image"))
        try:
            context_length = int(raw.get("context_length") or 0)
        except (ValueError, TypeError):
            context_length = 0
        modalities = cls._extract_modalities(raw)
        caps: list[str] = []
        if Modality.IMAGE in modalities:
            caps.append("vision")
        return RegistryModel(
            service_provider_id=OPENROUTER_PROVIDER_ID,
            model_provider=cls._extract_provider(model_id),
            model_id=model_id,
            display_name=str(raw.get("name") or model_id),
            modalities=modalities,
            capabilities=tuple(caps),
            tier=RegistryModel.compute_tier(prompt_price, completion_price),
            prompt_price=prompt_price,
            completion_price=completion_price,
            image_price=image_price,
            context_length=context_length,
            source="catalog",
            is_active=True,
        )
