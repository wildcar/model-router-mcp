"""ModelRouter: resolve a request to (adapter, model), execute, log, return.

Routing is deliberately simple in v1 but structured for extension:
  - explicit model_id wins;
  - else filter the registry by modality (+ provider/tier hints), order by tier
    preference then cheapest, pick the first whose adapter is configured+enabled;
  - else fall back to a per-modality default model id from settings.

Adding a provider later requires no change here: register an adapter + a
service_providers row + registry rows.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass

from router_mcp.accounting.cost import CostResult, compute_cost
from router_mcp.adapters.base import ProviderAdapter
from router_mcp.blobs.store import BlobStore
from router_mcp.domain.errors import NotConfigured, RouterError
from router_mcp.domain.messages import Attachment, RouterRequest, RouterResponse
from router_mcp.domain.modality import Modality
from router_mcp.registry.models import RegistryModel
from router_mcp.registry.service import RegistryService
from router_mcp.storage.database import Database, RequestLogRecord

logger = logging.getLogger(__name__)

_TIER_ORDER = {"free": 0, "cheap": 1, "expensive": 2}
_TEXT_TRUNCATE = 4000


@dataclass(slots=True)
class DispatchResult:
    response: RouterResponse
    cost: CostResult
    log_id: int


class ModelRouter:
    def __init__(
        self,
        db: Database,
        registry: RegistryService,
        adapters: dict[str, ProviderAdapter],
        blobs: BlobStore,
        defaults: dict[Modality, str | None] | None = None,
    ) -> None:
        self._db = db
        self._registry = registry
        self._adapters = adapters
        self._blobs = blobs
        self._defaults = defaults or {}

    async def resolve(self, request: RouterRequest) -> tuple[ProviderAdapter, RegistryModel]:
        enabled = await self._db.get_enabled_provider_ids()

        # 1. Explicit model id.
        if request.model_id:
            model = await self._db.get_registry_model_by_id(request.model_id)
            if model is None:
                raise RouterError(f"Unknown model_id: {request.model_id!r}")
            if not model.supports(request.modality):
                raise RouterError(
                    f"Model {model.model_id!r} does not support modality {request.modality.value}"
                )
            adapter = self._adapter_for(model, enabled)
            return adapter, model

        # 2/3. Candidate selection by modality (+ hints), else default.
        candidates = await self._registry.get_models(
            modality=request.modality.value,
            tier=request.tier,
            provider=request.provider_hint,
        )
        candidates = [m for m in candidates if m.service_provider_id in enabled]
        candidates.sort(key=self._candidate_sort_key(request))
        for model in candidates:
            adapter = self._adapters.get(model.service_provider_id)
            if adapter is not None and adapter.is_configured():
                return adapter, model

        # Per-modality default fallback.
        default_id = self._defaults.get(request.modality)
        if default_id:
            model = await self._db.get_registry_model_by_id(default_id)
            if model is not None:
                adapter = self._adapter_for(model, enabled)
                return adapter, model

        raise RouterError(
            f"No configured provider available for modality {request.modality.value}"
        )

    def _adapter_for(self, model: RegistryModel, enabled: set[str]) -> ProviderAdapter:
        adapter = self._adapters.get(model.service_provider_id)
        if adapter is None:
            raise RouterError(f"No adapter for provider {model.service_provider_id!r}")
        if model.service_provider_id not in enabled:
            raise NotConfigured(f"Provider {model.service_provider_id!r} is disabled")
        if not adapter.is_configured():
            raise NotConfigured(f"Provider {model.service_provider_id!r} is not configured")
        return adapter

    @staticmethod
    def _candidate_sort_key(request: RouterRequest):
        requested_tier = request.tier

        def key(m: RegistryModel) -> tuple:
            # When a tier is requested, exact matches first; otherwise prefer
            # cheaper tiers, then cheaper absolute price.
            tier_rank = 0 if (requested_tier and m.tier == requested_tier) else 1
            return (
                tier_rank,
                _TIER_ORDER.get(m.tier, 99),
                m.prompt_price + m.completion_price,
                m.model_id,
            )

        return key

    async def dispatch(self, request: RouterRequest) -> DispatchResult:
        t0 = time.monotonic()
        # Persist large request payloads up-front so they survive failures.
        request_ref = self._persist_request_attachment(request)
        request_text = self._truncate_request_text(request)

        try:
            adapter, model = await self.resolve(request)
        except RouterError as exc:
            log_id = await self._log_error(request, None, request_text, request_ref, exc, t0)
            raise

        try:
            response = await adapter.generate(request, model)
        except RouterError as exc:
            log_id = await self._log_error(request, model, request_text, request_ref, exc, t0)
            raise
        except Exception as exc:  # normalize unexpected failures
            log_id = await self._log_error(request, model, request_text, request_ref, exc, t0)
            raise RouterError(str(exc)) from exc

        latency_ms = int((time.monotonic() - t0) * 1000)
        response_ref = self._persist_response_attachment(response)

        subscription = None
        if model.pricing_mode == "amortized_subscription" and model.subscription_id:
            subscription = await self._db.get_subscription(model.subscription_id)
        cost = compute_cost(
            model,
            request.modality,
            response.usage,
            override_usd=response.cost_override_usd,
            subscription=subscription,
        )

        rec = RequestLogRecord(
            external_user_id=request.external_user_id,
            modality=request.modality.value,
            service_provider_id=model.service_provider_id,
            model_provider=model.model_provider,
            model_id=model.model_id,
            status="ok",
            request_text=request_text,
            request_ref=request_ref,
            response_text=(response.text or "")[:_TEXT_TRUNCATE] or None,
            response_ref=response_ref,
            prompt_tokens=response.usage.prompt_tokens,
            completion_tokens=response.usage.completion_tokens,
            total_tokens=response.usage.total_tokens,
            audio_seconds=response.usage.audio_seconds,
            images=response.usage.images,
            usage_raw=response.usage.raw,
            cost_usd=cost.cost_usd,
            cost_source=cost.cost_source,
            pricing_mode=cost.pricing_mode,
            latency_ms=latency_ms,
        )
        log_id = await self._db.log_request(rec)
        return DispatchResult(response=response, cost=cost, log_id=log_id)

    # --- blob / text helpers -------------------------------------------
    def _persist_request_attachment(self, request: RouterRequest) -> str | None:
        for att in request.attachments:
            if att.data:
                att.ref = self._blobs.save(att.data, att.mime)
                return att.ref
        return None

    def _persist_response_attachment(self, response: RouterResponse) -> str | None:
        first_ref: str | None = None
        for att in response.attachments:
            if att.data and not att.ref:
                att.ref = self._blobs.save(att.data, att.mime)
            if first_ref is None and att.ref:
                first_ref = att.ref
        return first_ref

    @staticmethod
    def _truncate_request_text(request: RouterRequest) -> str | None:
        if request.text is not None:
            return request.text[:_TEXT_TRUNCATE]
        if request.messages:
            return json.dumps(request.messages, ensure_ascii=False)[:_TEXT_TRUNCATE]
        return None

    async def _log_error(
        self,
        request: RouterRequest,
        model: RegistryModel | None,
        request_text: str | None,
        request_ref: str | None,
        exc: Exception,
        t0: float,
    ) -> int:
        rec = RequestLogRecord(
            external_user_id=request.external_user_id,
            modality=request.modality.value,
            service_provider_id=model.service_provider_id if model else request.provider_hint,
            model_provider=model.model_provider if model else None,
            model_id=model.model_id if model else request.model_id,
            status="error",
            error_message=f"{type(exc).__name__}: {exc}"[:1000],
            request_text=request_text,
            request_ref=request_ref,
            latency_ms=int((time.monotonic() - t0) * 1000),
        )
        return await self._db.log_request(rec)
