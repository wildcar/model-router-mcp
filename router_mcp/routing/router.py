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
from router_mcp.domain.errors import (
    ModelUnavailable,
    NotConfigured,
    ProviderUnavailable,
    RouterError,
)
from router_mcp.domain.messages import (
    Attachment,
    FallbackPolicy,
    RouterRequest,
    RouterResponse,
)
from router_mcp.domain.modality import Modality
from router_mcp.registry.models import RegistryModel
from router_mcp.registry.service import RegistryService
from router_mcp.storage.database import Database, RequestLogRecord

logger = logging.getLogger(__name__)

_TIER_ORDER = {"free": 0, "cheap": 1, "expensive": 2}
_TEXT_TRUNCATE = 4000


def _finish_reason(response: RouterResponse) -> str | None:
    """Providers name the stop reason differently; log whichever one arrived."""
    meta = response.raw_meta or {}
    for key in ("finish_reason", "stop_reason"):
        value = meta.get(key)
        if value:
            return str(value)[:100]
    return None


# Failures that mean "this model cannot serve the request" — the only ones a
# fallback policy retries. A refusal, a malformed prompt or a truncated answer
# would fail the same way on any model, so those are left to the caller.
FALLBACK_TRIGGERS = (ModelUnavailable, NotConfigured, ProviderUnavailable)


@dataclass(slots=True)
class Attempt:
    """One planned (adapter, model) try. `reason` is set when this attempt is a
    substitute chosen before the first call was even made."""

    adapter: ProviderAdapter
    model: RegistryModel
    reason: str | None = None


@dataclass(slots=True)
class _Problem:
    """A pre-flight reason a model cannot run, plus the error to raise when no
    fallback policy is in play (kept identical to the pre-fallback messages)."""

    reason: str
    error: RouterError


@dataclass(slots=True)
class DispatchResult:
    response: RouterResponse
    cost: CostResult
    log_id: int
    # Set only when a fallback policy substituted a model: what the caller asked
    # for, and why it did not get it.
    requested_model_id: str | None = None
    fallback_reason: str | None = None


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
        """The model this request would run on right now (first planned attempt)."""
        attempt = (await self.plan(request))[0]
        return attempt.adapter, attempt.model

    async def plan(self, request: RouterRequest) -> list[Attempt]:
        """Ordered attempts: the resolved model first, then policy substitutes.

        Planning up-front keeps dispatch honest: the substitutes are chosen from
        the registry state at the time of the request, not re-derived after each
        failure, so a retry storm cannot walk the whole catalog.
        """
        policy = request.fallback or FallbackPolicy()
        enabled = await self._db.get_enabled_provider_ids()
        attempts: list[Attempt] = []
        reference: RegistryModel | None = None
        first_reason: str | None = None

        if request.model_id:
            model = await self._db.get_registry_model_by_id(
                request.model_id, active_only=False
            )
            reference = model
            if model is not None and not model.supports(request.modality):
                # A modality mismatch is a caller mistake, not a dead model:
                # substituting would silently answer a different question.
                raise RouterError(
                    f"Model {model.model_id!r} does not support modality {request.modality.value}"
                )
            problem = self._problem_with(model, request.model_id, enabled)
            if problem is None:
                assert model is not None
                attempts.append(Attempt(self._adapters[model.service_provider_id], model))
            elif not policy.enabled:
                raise problem.error
            else:
                first_reason = problem.reason
        else:
            model = await self._pick_by_hints(request, enabled)
            if model is None:
                raise RouterError(
                    f"No configured provider available for modality {request.modality.value}"
                )
            reference = model
            attempts.append(Attempt(self._adapters[model.service_provider_id], model))

        if policy.enabled:
            exclude = {a.model.model_id for a in attempts}
            if request.model_id:
                exclude.add(request.model_id)
            for sub in await self._substitutes(request, reference, policy, exclude):
                attempts.append(
                    Attempt(
                        self._adapters[sub.service_provider_id],
                        sub,
                        reason=first_reason if not attempts else None,
                    )
                )
                first_reason = None

        if not attempts:
            # Fallback was on but the ladder is empty — report the original
            # problem rather than a vague "nothing available".
            raise RouterError(
                f"{first_reason} and no substitute matched policy "
                f"{(request.fallback or FallbackPolicy()).policy!r}"
            )
        return attempts

    async def _pick_by_hints(
        self, request: RouterRequest, enabled: set[str]
    ) -> RegistryModel | None:
        """Modality (+ tier/provider hints) selection, then the per-modality default."""
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
                return model

        default_id = self._defaults.get(request.modality)
        if default_id:
            model = await self._db.get_registry_model_by_id(default_id)
            if model is not None and self._problem_with(model, default_id, enabled) is None:
                return model
        return None

    def _problem_with(
        self, model: RegistryModel | None, model_id: str, enabled: set[str]
    ) -> _Problem | None:
        """Why this model cannot run, phrased for both the error and the log."""
        if model is None:
            return _Problem(
                f"model_id {model_id!r} is not in the registry",
                RouterError(f"Unknown model_id: {model_id!r}"),
            )
        if not model.is_active:
            when = f" on {model.deprecated_at}" if model.deprecated_at else ""
            return _Problem(
                f"model {model_id!r} was retired by its provider{when}",
                RouterError(f"Model {model_id!r} is retired (deprecated{when})"),
            )
        provider = model.service_provider_id
        adapter = self._adapters.get(provider)
        if adapter is None:
            return _Problem(
                f"no adapter for provider {provider!r}",
                RouterError(f"No adapter for provider {provider!r}"),
            )
        if provider not in enabled:
            return _Problem(
                f"provider {provider!r} is disabled",
                NotConfigured(f"Provider {provider!r} is disabled"),
            )
        if not adapter.is_configured():
            return _Problem(
                f"provider {provider!r} is not configured",
                NotConfigured(f"Provider {provider!r} is not configured"),
            )
        return None

    async def _substitutes(
        self,
        request: RouterRequest,
        reference: RegistryModel | None,
        policy: FallbackPolicy,
        exclude: set[str],
    ) -> list[RegistryModel]:
        """Walk the price ladder in the direction the policy asked for."""
        if policy.max_steps <= 0:
            return []
        enabled = await self._db.get_enabled_provider_ids()
        candidates = [
            m
            for m in await self._registry.get_models(modality=request.modality.value)
            if m.model_id not in exclude
            # A price ladder cannot place a model whose price nobody knows, and
            # stepping "one cheaper" onto an unpriced model would be a guess.
            and not m.price_is_unknown
            and self._in_scope(m, reference, request, policy.scope)
            and m.service_provider_id in enabled
            and (m.service_provider_id in self._adapters)
            and self._adapters[m.service_provider_id].is_configured()
        ]
        if not candidates:
            return []
        candidates.sort(key=lambda m: (m.prompt_price + m.completion_price, m.model_id))

        if reference is None:
            # Nothing to step from (the id was never in the registry): the
            # cheapest working model is the least surprising answer.
            ordered = candidates
        else:
            ref = reference.prompt_price + reference.completion_price
            if policy.policy == FallbackPolicy.CHEAPER:
                ordered = [m for m in candidates if m.prompt_price + m.completion_price < ref]
                ordered.reverse()  # closest step down first
            elif policy.policy == FallbackPolicy.PRICIER:
                ordered = [m for m in candidates if m.prompt_price + m.completion_price > ref]
            else:  # SAME: nearest price in either direction, cheaper on a tie
                ordered = sorted(
                    candidates,
                    key=lambda m: (
                        abs(m.prompt_price + m.completion_price - ref),
                        m.prompt_price + m.completion_price,
                    ),
                )
        return ordered[: policy.max_steps]

    @staticmethod
    def _in_scope(
        model: RegistryModel,
        reference: RegistryModel | None,
        request: RouterRequest,
        scope: str,
    ) -> bool:
        if scope == "any":
            return True
        if scope == "vendor":
            if reference is not None:
                return model.model_provider == reference.model_provider
            return True
        # "provider": stay with the service provider the caller was using.
        pinned = reference.service_provider_id if reference else request.provider_hint
        return pinned is None or model.service_provider_id == pinned

    @staticmethod
    def _candidate_sort_key(request: RouterRequest):
        requested_tier = request.tier

        def key(m: RegistryModel) -> tuple:
            # When a tier is requested, exact matches first; otherwise prefer
            # cheaper tiers, then cheaper absolute price. Models whose price is
            # simply unknown go last: their zeros are missing data, not a bargain.
            tier_rank = 0 if (requested_tier and m.tier == requested_tier) else 1
            return (
                1 if m.price_is_unknown else 0,
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
            attempts = await self.plan(request)
        except RouterError as exc:
            await self._log_error(request, None, request_text, request_ref, exc, t0)
            raise

        response: RouterResponse | None = None
        model: RegistryModel | None = None
        reason: str | None = None  # why we are not on the originally chosen model
        requested_model_id: str | None = None
        for index, attempt in enumerate(attempts):
            model = attempt.model
            if index or attempt.reason:
                requested_model_id = request.model_id or attempts[0].model.model_id
                reason = attempt.reason or reason
                logger.info(
                    "Fallback: %s -> %s (%s)", requested_model_id, model.model_id, reason
                )
            last = index == len(attempts) - 1
            try:
                response = await attempt.adapter.generate(request, model)
                break
            except FALLBACK_TRIGGERS as exc:
                await self._log_error(
                    request, model, request_text, request_ref, exc, t0,
                    requested_model_id=requested_model_id, fallback_reason=reason,
                )
                if last:
                    raise
                # The next attempt carries the failure that caused it.
                reason = f"{model.model_id} failed: {type(exc).__name__}: {exc}"
            except RouterError as exc:
                await self._log_error(
                    request, model, request_text, request_ref, exc, t0,
                    requested_model_id=requested_model_id, fallback_reason=reason,
                )
                raise
            except Exception as exc:  # normalize unexpected failures
                await self._log_error(
                    request, model, request_text, request_ref, exc, t0,
                    requested_model_id=requested_model_id, fallback_reason=reason,
                )
                raise RouterError(str(exc)) from exc

        assert response is not None and model is not None
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
            finish_reason=_finish_reason(response),
            requested_model_id=requested_model_id,
            fallback_reason=reason,
        )
        log_id = await self._db.log_request(rec)
        return DispatchResult(
            response=response,
            cost=cost,
            log_id=log_id,
            requested_model_id=requested_model_id,
            fallback_reason=reason,
        )

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
        *,
        requested_model_id: str | None = None,
        fallback_reason: str | None = None,
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
            requested_model_id=requested_model_id,
            fallback_reason=fallback_reason,
        )
        return await self._db.log_request(rec)
