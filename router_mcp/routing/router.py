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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from router_mcp.accounting.cost import CostResult, compute_cost
from router_mcp.adapters.base import (
    ProviderAdapter,
    ignored_params,
    start_http_trace,
    take_http_trace,
)
from router_mcp.blobs.store import BlobStore
from router_mcp.domain.errors import (
    ModelRetired,
    ModelUnavailable,
    NotConfigured,
    ProviderDisabled,
    ProviderTimeout,
    ProviderUnavailable,
    RouterError,
    UnknownModel,
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
# What a cost estimate assumes about the answer, since its length is the one
# number nobody can know before the call. Stated on screen next to the figure.
_ASSUMED_COMPLETION_TOKENS = 500


def _pair(model: RegistryModel) -> dict[str, Any]:
    """A model is addressed by the pair, never by the id alone: two providers can
    serve the same id at different prices."""
    return {
        "provider": model.service_provider_id,
        "model_id": model.model_id,
        "display_name": model.display_name,
        "is_active": model.is_active,
        "deprecated_at": model.deprecated_at,
    }


def _last_status(trace: list[dict[str, Any]]) -> int | None:
    """The upstream HTTP status of the last try, when there was one."""
    for entry in reversed(trace):
        status = entry.get("status")
        if status is not None:
            return int(status)
    return None


def _provider_request_id(meta: dict[str, Any]) -> str | None:
    """Providers name their request id half a dozen ways; keep whichever came.

    It is the one string a support ticket is answered by, so it must reach the
    screen verbatim.
    """
    for key in ("provider_request_id", "request_id", "id", "x-request-id"):
        value = meta.get(key)
        if value:
            return str(value)[:200]
    return None


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
class Candidate:
    """One model the fallback ladder looked at, and what became of it."""

    model: RegistryModel
    verdict: str  # "eligible" | "filtered"
    why: str
    price: float  # prompt + completion, USD per token
    rank: int | None = None  # position on the ladder, for eligible ones


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
    # Execution detail. It all exists inside dispatch already; before this it
    # died here and the console showed a result with no path behind it.
    latency_ms: int = 0
    attempts: int = 1  # HTTP tries inside the provider attempt that succeeded
    provider_status: int | None = None
    provider_request_id: str | None = None
    finish_reason: str | None = None
    steps: list[dict[str, Any]] = field(default_factory=list)
    ignored_params: list[str] = field(default_factory=list)
    finished_at: str | None = None


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

    async def plan(
        self, request: RouterRequest, *, allow_sync: bool = True
    ) -> list[Attempt]:
        """Ordered attempts: the resolved model first, then policy substitutes.

        Planning up-front keeps dispatch honest: the substitutes are chosen from
        the registry state at the time of the request, not re-derived after each
        failure, so a retry storm cannot walk the whole catalog.

        `allow_sync=False` keeps the dry run off the network — see `explain`.
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
            model = await self._pick_by_hints(request, enabled, allow_sync=allow_sync)
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
            for sub in await self._substitutes(
                request, reference, policy, exclude, allow_sync=allow_sync
            ):
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
        self, request: RouterRequest, enabled: set[str], *, allow_sync: bool = True
    ) -> RegistryModel | None:
        """Modality (+ tier/provider hints) selection, then the per-modality default."""
        candidates = await self._registry.get_models(
            modality=request.modality.value,
            tier=request.tier,
            provider=request.provider_hint,
            allow_sync=allow_sync,
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
                UnknownModel(f"Unknown model_id: {model_id!r}", model_id=model_id),
            )
        if not model.is_active:
            when = f" on {model.deprecated_at}" if model.deprecated_at else ""
            return _Problem(
                f"model {model_id!r} was retired by its provider{when}",
                ModelRetired(
                    f"Model {model_id!r} is retired (deprecated{when})",
                    provider=model.service_provider_id,
                    model_id=model_id,
                    hint={"kind": "pick_model", "action": "choose an active model"},
                ),
            )
        provider = model.service_provider_id
        adapter = self._adapters.get(provider)
        if adapter is None:
            return _Problem(
                f"no adapter for provider {provider!r}",
                RouterError(f"No adapter for provider {provider!r}", provider=provider),
            )
        if provider not in enabled:
            return _Problem(
                f"provider {provider!r} is disabled",
                ProviderDisabled(
                    f"Provider {provider!r} is disabled",
                    provider=provider,
                    model_id=model_id,
                    hint={"kind": "enable_provider", "target": provider},
                ),
            )
        if not adapter.is_configured():
            return _Problem(
                f"provider {provider!r} is not configured",
                NotConfigured(
                    f"Provider {provider!r} is not configured",
                    provider=provider,
                    model_id=model_id,
                    # Name the variable, never its value: the operator's next
                    # step is to set it, and the console must not leak secrets.
                    hint={
                        "kind": "env_var",
                        "env_var": adapter.env_var,
                        "target": provider,
                    },
                ),
            )
        return None

    async def _ladder(
        self,
        request: RouterRequest,
        reference: RegistryModel | None,
        policy: FallbackPolicy,
        exclude: set[str],
        *,
        allow_sync: bool = True,
    ) -> list[Candidate]:
        """Every model the policy considered, in ladder order, with a verdict.

        Dispatch only needs the eligible head of this list, but the console needs
        the whole thing: "почему запрос ушёл не на ту модель" is answered by the
        rows that were dropped, not by the one that won.
        """
        enabled = await self._db.get_enabled_provider_ids()
        pool = await self._registry.get_models(
            modality=request.modality.value, allow_sync=allow_sync
        )
        ref = (
            reference.prompt_price + reference.completion_price
            if reference is not None
            else None
        )

        considered: list[Candidate] = []
        eligible: list[Candidate] = []
        for model in pool:
            if model.model_id in exclude:
                continue
            price = model.prompt_price + model.completion_price
            adapter = self._adapters.get(model.service_provider_id)
            if model.price_is_unknown:
                # A price ladder cannot place a model whose price nobody knows,
                # and stepping "one cheaper" onto it would be a guess.
                verdict, why = "filtered", "цены нет, на лестницу не ставится"
            elif not self._in_scope(model, reference, request, policy.scope):
                verdict, why = "filtered", "вне области подстановки"
            elif model.service_provider_id not in enabled:
                verdict, why = "filtered", "поставщик выключен"
            elif adapter is None or not adapter.is_configured():
                verdict, why = "filtered", "поставщик не настроен"
            elif ref is not None and policy.policy == FallbackPolicy.CHEAPER and price >= ref:
                verdict, why = "filtered", "не дешевле опорной"
            elif ref is not None and policy.policy == FallbackPolicy.PRICIER and price <= ref:
                verdict, why = "filtered", "не дороже опорной"
            else:
                verdict, why = "eligible", ""
            candidate = Candidate(model=model, verdict=verdict, why=why, price=price)
            considered.append(candidate)
            if verdict == "eligible":
                eligible.append(candidate)

        eligible.sort(key=lambda c: (c.price, c.model.model_id))
        if ref is None:
            # Nothing to step from (the id was never in the registry): the
            # cheapest working model is the least surprising answer.
            ordered = eligible
        elif policy.policy == FallbackPolicy.CHEAPER:
            ordered = list(reversed(eligible))  # closest step down first
        elif policy.policy == FallbackPolicy.PRICIER:
            ordered = eligible
        else:  # SAME: nearest price in either direction, cheaper on a tie
            ordered = sorted(eligible, key=lambda c: (abs(c.price - ref), c.price))
        for rank, candidate in enumerate(ordered, start=1):
            candidate.rank = rank
            candidate.why = (
                "ближайшая ступень" if rank <= max(policy.max_steps, 0) else "дальше по лестнице"
            )
        rejected = [c for c in considered if c.verdict == "filtered"]
        return ordered + sorted(rejected, key=lambda c: (c.price, c.model.model_id))

    async def _substitutes(
        self,
        request: RouterRequest,
        reference: RegistryModel | None,
        policy: FallbackPolicy,
        exclude: set[str],
        *,
        allow_sync: bool = True,
    ) -> list[RegistryModel]:
        """Walk the price ladder in the direction the policy asked for."""
        if policy.max_steps <= 0:
            return []
        ladder = await self._ladder(
            request, reference, policy, exclude, allow_sync=allow_sync
        )
        chosen = [c.model for c in ladder if c.verdict == "eligible"]
        return chosen[: policy.max_steps]

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

    async def explain(self, request: RouterRequest) -> dict[str, Any]:
        """The dry run: what would happen, without anything happening.

        Two properties this owes the caller. It never touches the network — the
        button says "ничего не отправляем и не тратим", and a plan that quietly
        polls eight catalogs breaks that promise. And when there is nothing to
        run it still answers successfully, describing the problem: "почему
        запрос не ушёл" is exactly the case the screen exists for.
        """
        policy = request.fallback or FallbackPolicy()
        enabled = await self._db.get_enabled_provider_ids()

        requested: RegistryModel | None = None
        problem: _Problem | None = None
        if request.model_id:
            requested = await self._db.get_registry_model_by_id(
                request.model_id, active_only=False
            )
            if requested is not None and not requested.supports(request.modality):
                problem = _Problem(
                    f"модель {request.model_id!r} не поддерживает модальность "
                    f"{request.modality.value}",
                    RouterError("modality mismatch"),
                )
            else:
                problem = self._problem_with(requested, request.model_id, enabled)

        attempts: list[Attempt] = []
        blocked: str | None = None
        try:
            attempts = await self.plan(request, allow_sync=False)
        except RouterError as exc:
            blocked = str(exc)

        exclude = {request.model_id} if request.model_id else set()
        ladder = await self._ladder(
            request, requested, policy, exclude, allow_sync=False
        )

        will_call = attempts[0].model if attempts else None
        pool_total = len(
            await self._registry.get_models(
                modality=request.modality.value, allow_sync=False
            )
        )
        return {
            "modality": request.modality.value,
            "requested": _pair(requested) if requested else (
                {"provider": request.provider_hint, "model_id": request.model_id}
                if request.model_id
                else None
            ),
            "problem": problem.reason if problem else None,
            "blocked": blocked,
            "policy": {
                "policy": policy.policy,
                "scope": policy.scope,
                "max_steps": policy.max_steps,
            },
            "considered": pool_total,
            "eligible": sum(1 for c in ladder if c.verdict == "eligible"),
            "candidates": [
                {
                    "rank": c.rank,
                    "provider": c.model.service_provider_id,
                    "model_id": c.model.model_id,
                    "price_per_1m": round(c.price * 1e6, 6),
                    "price_source_kind": c.model.price_source_kind,
                    "verdict": c.verdict,
                    "why": c.why,
                }
                for c in ladder[:20]
            ],
            "will_call": _pair(will_call) if will_call else None,
            "estimate": self._estimate(request, will_call),
            "plan": [
                {
                    "provider": a.model.service_provider_id,
                    "model_id": a.model.model_id,
                    "reason": a.reason,
                }
                for a in attempts
            ],
        }

    def _estimate(
        self, request: RouterRequest, model: RegistryModel | None
    ) -> dict[str, Any]:
        """A price before the money is spent.

        The prompt is measured; the answer cannot be, so a fixed assumption is
        used and stated. For the modalities with no pricing method at all the
        answer is "неизвестна" — never a confident zero (§7.5).
        """
        if model is None:
            return {"value": None, "source": "unpriced", "assumption": None}
        if model.price_is_unknown:
            return {
                "value": None,
                "source": "unpriced",
                "assumption": "у модели нет цены в реестре",
            }
        if request.modality in (Modality.IMAGE, Modality.STT, Modality.TTS, Modality.CLI):
            per_unit = {
                Modality.IMAGE: model.image_price,
                Modality.STT: model.audio_price_per_second,
                Modality.TTS: model.audio_price_per_second,
                Modality.CLI: 0.0,
            }[request.modality]
            if not per_unit:
                return {
                    "value": None,
                    "source": "unpriced",
                    "assumption": "для этой модальности ставки в реестре нет",
                }
            unit = "изображение" if request.modality is Modality.IMAGE else "секунду"
            return {
                "value": per_unit,
                "source": "computed" if model.price_is_verified else "estimated",
                "assumption": f"за одну {unit}",
            }
        text = request.text or json.dumps(request.messages or [], ensure_ascii=False)
        # Four characters to a token is the usual rule of thumb; it is a rough
        # figure and the screen says so rather than implying precision.
        prompt_tokens = max(1, len(text) // 4)
        completion_tokens = _ASSUMED_COMPLETION_TOKENS
        value = (
            prompt_tokens * model.prompt_price + completion_tokens * model.completion_price
        )
        return {
            "value": value,
            "source": "computed" if model.price_is_verified else "estimated",
            "assumption": (
                f"промпт ≈{prompt_tokens} токенов (по длине текста), "
                f"ответ {completion_tokens} токенов"
            ),
        }

    async def dispatch(self, request: RouterRequest) -> DispatchResult:
        t0 = time.monotonic()
        # Persist large request payloads up-front so they survive failures.
        request_ref = self._persist_request_attachment(request)
        request_text = self._truncate_request_text(request)
        steps: list[dict[str, Any]] = []

        def step(kind: str, title: str, **detail: Any) -> None:
            steps.append(
                {
                    "at_ms": int((time.monotonic() - t0) * 1000),
                    "kind": kind,
                    "title": title,
                    **detail,
                }
            )

        try:
            attempts = await self.plan(request)
        except RouterError as exc:
            step("plan", "plan failed", error=str(exc))
            await self._log_error(
                request, None, request_text, request_ref, exc, t0, steps=steps
            )
            raise
        step(
            "plan",
            "plan built",
            planned=[
                {"provider": a.model.service_provider_id, "model_id": a.model.model_id}
                for a in attempts
            ],
        )

        response: RouterResponse | None = None
        model: RegistryModel | None = None
        used_adapter: ProviderAdapter | None = None
        reason: str | None = None  # why we are not on the originally chosen model
        requested_model_id: str | None = None
        http_trace: list[dict[str, Any]] = []
        for index, attempt in enumerate(attempts):
            model = attempt.model
            if index or attempt.reason:
                requested_model_id = request.model_id or attempts[0].model.model_id
                reason = attempt.reason or reason
                logger.info(
                    "Fallback: %s -> %s (%s)", requested_model_id, model.model_id, reason
                )
            last = index == len(attempts) - 1
            attempt_step_index = len(steps)
            step(
                "attempt",
                f"attempt {index + 1}",
                provider=model.service_provider_id,
                model_id=model.model_id,
                reason=attempt.reason or (reason if index else None),
                status="running",
            )
            start_http_trace()
            try:
                response = await attempt.adapter.generate(request, model)
                http_trace = take_http_trace()
                used_adapter = attempt.adapter
                steps[attempt_step_index].update(status="ok", retries=http_trace)
                break
            except FALLBACK_TRIGGERS as exc:
                http_trace = take_http_trace()
                steps[attempt_step_index].update(
                    status="error", retries=http_trace, error=f"{type(exc).__name__}: {exc}"
                )
                await self._log_error(
                    request, model, request_text, request_ref, exc, t0,
                    requested_model_id=requested_model_id, fallback_reason=reason,
                    steps=steps, http_trace=http_trace,
                )
                if last:
                    raise
                # The next attempt carries the failure that caused it.
                reason = f"{model.model_id} failed: {type(exc).__name__}: {exc}"
            except RouterError as exc:
                http_trace = take_http_trace()
                steps[attempt_step_index].update(
                    status="error", retries=http_trace, error=f"{type(exc).__name__}: {exc}"
                )
                await self._log_error(
                    request, model, request_text, request_ref, exc, t0,
                    requested_model_id=requested_model_id, fallback_reason=reason,
                    steps=steps, http_trace=http_trace,
                )
                raise
            except Exception as exc:  # normalize unexpected failures
                http_trace = take_http_trace()
                steps[attempt_step_index].update(
                    status="error", retries=http_trace, error=f"{type(exc).__name__}: {exc}"
                )
                normalized = RouterError(str(exc))
                await self._log_error(
                    request, model, request_text, request_ref, normalized, t0,
                    requested_model_id=requested_model_id, fallback_reason=reason,
                    steps=steps, http_trace=http_trace,
                )
                raise normalized from exc

        assert response is not None and model is not None and used_adapter is not None
        latency_ms = int((time.monotonic() - t0) * 1000)
        response_ref = self._persist_response_attachment(response)
        step("response", "response parsed", finish_reason=_finish_reason(response))
        if response_ref:
            step("blob", "response file stored", ref=response_ref)

        subscription = None
        if model.pricing_mode == "amortized_subscription" and model.subscription_id:
            subscription = await self._db.get_subscription(model.subscription_id)
        cost = compute_cost(
            model,
            request.modality,
            response.usage,
            override_usd=response.cost_override_usd,
            subscription=subscription,
            notional=response.cost_is_notional,
        )
        step("cost", "cost computed", cost_usd=cost.cost_usd, cost_source=cost.cost_source)

        meta = response.raw_meta or {}
        finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
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
            cache_read_tokens=response.usage.cache_read_tokens,
            cache_write_tokens=response.usage.cache_write_tokens,
            reasoning_tokens=response.usage.reasoning_tokens,
            audio_seconds=response.usage.audio_seconds,
            images=response.usage.images,
            characters=response.usage.characters,
            usage_raw=response.usage.raw,
            cost_usd=cost.cost_usd,
            cost_source=cost.cost_source,
            pricing_mode=cost.pricing_mode,
            latency_ms=latency_ms,
            finish_reason=_finish_reason(response),
            requested_model_id=requested_model_id,
            fallback_reason=reason,
            channel=request.channel,
            tool=request.tool,
            client_id=request.client_id,
            client_request_id=request.client_request_id,
            attempts=max(len(http_trace), 1),
            provider_status=_last_status(http_trace),
            provider_request_id=_provider_request_id(meta),
            session_id=meta.get("session_id"),
            return_code=meta.get("return_code"),
            finished_at=finished_at,
        )
        log_id = await self._db.log_request(rec)
        step("log", "logged", log_id=log_id, channel=request.channel)
        return DispatchResult(
            response=response,
            cost=cost,
            log_id=log_id,
            requested_model_id=requested_model_id,
            fallback_reason=reason,
            latency_ms=latency_ms,
            attempts=rec.attempts or 1,
            provider_status=rec.provider_status,
            provider_request_id=rec.provider_request_id,
            finish_reason=rec.finish_reason,
            steps=steps,
            ignored_params=ignored_params(used_adapter, request.modality, request.params),
            finished_at=finished_at,
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
        steps: list[dict[str, Any]] | None = None,
        http_trace: list[dict[str, Any]] | None = None,
    ) -> int:
        trace = http_trace or []
        # A read timeout means the provider most likely ran — and billed — the
        # request; recording zero would understate the bill with confidence.
        cost_source = "cost_unknown" if isinstance(exc, ProviderTimeout) else "computed"
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
            cost_source=cost_source,
            latency_ms=int((time.monotonic() - t0) * 1000),
            requested_model_id=requested_model_id,
            fallback_reason=fallback_reason,
            channel=request.channel,
            tool=request.tool,
            client_id=request.client_id,
            client_request_id=request.client_request_id,
            attempts=len(trace) or None,
            provider_status=_last_status(trace)
            or getattr(exc, "provider_status", None),
            plan_json=json.dumps(steps, ensure_ascii=False) if steps else None,
            finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        log_id = await self._db.log_request(rec)
        # "Open this error in the log" is offered on three screens; without the
        # id travelling with the exception the link has nowhere to point.
        if isinstance(exc, RouterError):
            exc.log_id = log_id
            if exc.provider is None:
                exc.provider = rec.service_provider_id
            if exc.model_id is None:
                exc.model_id = rec.model_id
        return log_id
