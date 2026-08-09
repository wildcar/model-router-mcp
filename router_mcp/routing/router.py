"""ModelRouter: resolve a request to (adapter, model), execute, log, return.

Routing is deliberately simple in v1 but structured for extension:
  - explicit model_id wins;
  - else filter the registry by task (+ provider/tier hints), order by tier
    preference then cheapest, pick the first whose adapter is configured+enabled;
  - else fall back to a per-billing-group default model id from settings.

Adding a provider later requires no change here: register an adapter + a
service_providers row + registry rows.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from router_mcp.accounting.cost import (
    PER_UNIT_GROUPS,
    CostResult,
    billing_unit,
    compute_cost,
)
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
from router_mcp.domain import progress
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
# How many timeline steps one run may store. A CLI engine emits an event per
# tool call and can produce thousands; the whole list is read back on every
# inspector open.
_MAX_STEPS = 500
# How many messages of a chat history the request snapshot keeps, for the same
# reason: a long conversation is sent in full and stored capped.
_MAX_MESSAGES = 50


def _steps_json(steps: list[dict[str, Any]], dropped: int = 0) -> str | None:
    """The timeline as it goes into the row, with the truncation stated."""
    if not steps:
        return None
    payload = list(steps)
    if dropped:
        payload.append(
            {
                "at_ms": payload[-1].get("at_ms"),
                "kind": "truncated",
                "title": f"ещё {dropped} шаг(ов) не сохранено",
                "dropped": dropped,
            }
        )
    return json.dumps(payload, ensure_ascii=False)


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


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
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
    price: float  # the billing group's rate, USD per its unit
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
    # The stored request payload (uploaded audio, mostly). Carried out so the
    # console can repeat a transcription without asking for the file again.
    request_ref: str | None = None
    # The request as it was received, exactly as it went into the log row, so the
    # envelope built from a live run and the one read back agree.
    request_snapshot: dict[str, Any] = field(default_factory=dict)


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
            if model is not None and not model.supports(request.task):
                # A task mismatch is a caller mistake, not a dead model:
                # substituting would silently answer a different question. This is
                # the check that refuses to edit an image with a model that can
                # only draw one (ADR-0006).
                raise RouterError(
                    f"Model {model.model_id!r} does not support task {request.task.value}"
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
                    f"No configured provider available for task {request.task.value}"
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
        """Task (+ tier/provider hints) selection, then the per-group default."""
        candidates = await self._registry.get_models(
            task=request.task.value,
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
            task=request.task.value, allow_sync=allow_sync
        )
        # The ladder is ordered by the rate of *this task's* billing group. A sum
        # of the two token rates is zero for every image, audio and video row, so
        # ordering by it meant «дешевле» ranked by model_id.
        group = request.task.billing_group
        ref = reference.ladder_price(group) if reference is not None else None

        considered: list[Candidate] = []
        eligible: list[Candidate] = []
        for model in pool:
            if model.model_id in exclude:
                continue
            price = model.ladder_price(group)
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
            if requested is not None and not requested.supports(request.task):
                problem = _Problem(
                    # The task, not the group: «не поддерживает модальность image»
                    # was both the wrong word and the wrong fact — the model does
                    # bill in that group, it just cannot do this with it.
                    f"модель {request.model_id!r} не умеет "
                    f"{request.task.value}",
                    RouterError("task mismatch"),
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
                task=request.task.value, allow_sync=False
            )
        )
        return {
            "task": request.task.value,
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
        group = request.modality
        if group in PER_UNIT_GROUPS:
            spec, per_unit = billing_unit(model, group)
            if not per_unit:
                return {
                    "value": None,
                    "source": "unpriced",
                    "assumption": "для этой группы тарификации ставки в реестре нет",
                }
            return {
                "value": per_unit,
                "source": "computed" if model.price_is_verified else "estimated",
                "assumption": spec.per_unit,
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

    async def dispatch(
        self, request: RouterRequest, *, steps_out: list[dict[str, Any]] | None = None
    ) -> DispatchResult:
        """Execute one request, from planning to the logged row.

        `steps_out` is the live timeline: pass a list and it fills up as the run
        proceeds, which is how the run registry answers "что сейчас происходит"
        without waiting for the result.
        """
        t0 = time.monotonic()
        # Persist large request payloads up-front so they survive failures.
        request_ref = self._persist_request_attachment(request)
        request_text = self._truncate_request_text(request)
        # Built once and written by every path out of here, including the ones
        # that fail: an entry that says nothing about the request is exactly the
        # entry an operator opens after a failure.
        request_json = self._request_snapshot(request, request_ref)
        steps = steps_out if steps_out is not None else []
        dropped = 0

        def step(kind: str, title: str, **detail: Any) -> dict[str, Any]:
            nonlocal dropped
            entry = {
                "at_ms": int((time.monotonic() - t0) * 1000),
                "kind": kind,
                "title": title,
                **detail,
            }
            # A CLI engine can print thousands of events. The timeline is stored
            # in the log row and re-read on every inspector open, so it is
            # capped — with the count of what was left out, because a silently
            # shortened timeline reads like a run that stopped early.
            if len(steps) < _MAX_STEPS:
                steps.append(entry)
            else:
                dropped += 1
            return entry

        # The row exists before the provider is called: a run in flight has to be
        # visible in the log (and to a second browser tab), and a service that
        # dies mid-run must leave a row that says so rather than nothing at all.
        log_id = await self._db.log_request(
            RequestLogRecord(
                external_user_id=request.external_user_id,
                modality=request.modality.value,
                task=request.task.value,
                service_provider_id=request.provider_hint,
                model_id=request.model_id,
                status="running",
                request_text=request_text,
                request_ref=request_ref,
                request_json=request_json,
                cost_source="cost_unknown",
                channel=request.channel,
                tool=request.tool,
                client_id=request.client_id,
                client_request_id=request.client_request_id,
                batch_id=request.batch_id,
            )
        )

        step("log", "запись журнала открыта", log_id=log_id)

        def _sink(event: dict[str, Any]) -> None:
            detail = dict(event)
            step(detail.pop("kind", "progress"), detail.pop("title", ""), **detail)

        sink_token = progress.set_sink(_sink)
        try:
            return await self._dispatch_inner(
                request, log_id, steps, step, t0, request_text, request_ref, request_json,
                lambda: dropped,
            )
        except asyncio.CancelledError:
            # Cancellation is an outcome, not an absence of one: the provider may
            # have run the request already, so the row says "прервано" and marks
            # the cost unknown instead of recording a confident zero.
            await self._db.finish_request_log(
                log_id,
                RequestLogRecord(
                    external_user_id=request.external_user_id,
                    modality=request.modality.value,
                    task=request.task.value,
                    service_provider_id=request.provider_hint,
                    model_id=request.model_id,
                    status="cancelled",
                    error_message="Прогон отменён оператором",
                    request_text=request_text,
                    request_ref=request_ref,
                    request_json=request_json,
                    cost_source="cost_unknown",
                    latency_ms=int((time.monotonic() - t0) * 1000),
                    channel=request.channel,
                    tool=request.tool,
                    client_id=request.client_id,
                    client_request_id=request.client_request_id,
                    batch_id=request.batch_id,
                    plan_json=_steps_json(steps, dropped),
                    finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ),
            )
            raise
        finally:
            progress.reset_sink(sink_token)

    async def _dispatch_inner(
        self,
        request: RouterRequest,
        log_id: int,
        steps: list[dict[str, Any]],
        step,
        t0: float,
        request_text: str | None,
        request_ref: str | None,
        request_json: dict[str, Any] | None,
        dropped,
    ) -> DispatchResult:
        try:
            attempts = await self.plan(request)
        except RouterError as exc:
            step("plan", "plan failed", error=str(exc))
            await self._log_error(
                request, None, request_text, request_ref, request_json, exc, t0, steps=steps,
                row_id=log_id, dropped=dropped(),
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
                    request, model, request_text, request_ref, request_json, exc, t0,
                    requested_model_id=requested_model_id, fallback_reason=reason,
                    steps=steps, http_trace=http_trace,
                    # Only the last attempt closes the run's own row. An earlier
                    # failure that a fallback recovered from is its own log
                    # entry, as it has been since the fallback policy landed.
                    row_id=log_id if last else None, dropped=dropped(),
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
                    request, model, request_text, request_ref, request_json, exc, t0,
                    requested_model_id=requested_model_id, fallback_reason=reason,
                    steps=steps, http_trace=http_trace,
                    row_id=log_id, dropped=dropped(),
                )
                raise
            except Exception as exc:  # normalize unexpected failures
                http_trace = take_http_trace()
                steps[attempt_step_index].update(
                    status="error", retries=http_trace, error=f"{type(exc).__name__}: {exc}"
                )
                normalized = RouterError(str(exc))
                await self._log_error(
                    request, model, request_text, request_ref, request_json, normalized, t0,
                    requested_model_id=requested_model_id, fallback_reason=reason,
                    steps=steps, http_trace=http_trace,
                    row_id=log_id, dropped=dropped(),
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
            task=request.task.value,
            service_provider_id=model.service_provider_id,
            model_provider=model.model_provider,
            model_id=model.model_id,
            status="ok",
            request_text=request_text,
            request_ref=request_ref,
            request_json=request_json,
            response_text=(response.text or "")[:_TEXT_TRUNCATE] or None,
            response_ref=response_ref,
            prompt_tokens=response.usage.prompt_tokens,
            completion_tokens=response.usage.completion_tokens,
            total_tokens=response.usage.total_tokens,
            cache_read_tokens=response.usage.cache_read_tokens,
            cache_write_tokens=response.usage.cache_write_tokens,
            reasoning_tokens=response.usage.reasoning_tokens,
            audio_seconds=response.usage.audio_seconds,
            compute_seconds=response.usage.compute_seconds,
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
            batch_id=request.batch_id,
            attempts=max(len(http_trace), 1),
            provider_status=_last_status(http_trace),
            provider_request_id=_provider_request_id(meta),
            provider_cost_usd=_float_or_none(meta.get("provider_cost_usd")),
            session_id=meta.get("session_id"),
            return_code=meta.get("return_code"),
            finished_at=finished_at,
        )
        step("log", "logged", log_id=log_id, channel=request.channel)
        # The timeline is stored with the row, so opening the entry a week later
        # replays the same steps the operator watched go by.
        rec.plan_json = _steps_json(steps, dropped())
        await self._db.finish_request_log(log_id, rec)
        # An answer is the only proof the model id is real (ADR-0008). Written
        # after the row is closed, and only for a row that has no date yet, so a
        # provider in daily use pays for this once.
        if model.confirmed_at is None:
            await self._db.confirm_registry_model(
                model.service_provider_id, model.model_id, finished_at
            )
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
            # The row, not just the adapter: a knob an operator allowed through
            # `extra_params` must not be reported as ignored on the run that
            # honoured it.
            ignored_params=ignored_params(
                used_adapter, request.task, request.params, model
            ),
            finished_at=finished_at,
            request_ref=request_ref,
            request_snapshot=request_json or {},
        )

    # --- blob / text helpers -------------------------------------------
    def _persist_request_attachment(self, request: RouterRequest) -> str | None:
        """Store every uploaded payload; answer with the first ref for the column.

        Two things it must not do, both of which it used to. Stop at the first
        attachment — editing an image with a mask sends two, and the second would
        never reach the store, so the entry could not be replayed. And re-save a
        payload that is already stored: the console uploads once and then passes
        a ref, so a run against three models wrote three more copies of the same
        file and pointed the log at the last of them.
        """
        first_ref: str | None = None
        for att in request.attachments:
            if att.data and not att.ref:
                att.ref = self._blobs.save(att.data, att.mime)
            if first_ref is None and att.ref:
                first_ref = att.ref
        return first_ref

    def _persist_response_attachment(self, response: RouterResponse) -> str | None:
        first_ref: str | None = None
        for att in response.attachments:
            if att.data and not att.ref:
                att.ref = self._blobs.save(att.data, att.mime)
            if first_ref is None and att.ref:
                first_ref = att.ref
        return first_ref

    @staticmethod
    def _request_snapshot(
        request: RouterRequest, request_ref: str | None
    ) -> dict[str, Any]:
        """The request as it arrived, in the shape the console's «Запрос» tab shows.

        Stored with the row instead of being reconstructed later: the registry,
        the defaults and the fallback policy all move, so a request re-derived
        against today's configuration is not the request that ran.

        Long payloads are cut here and the cut is stated. A chat history is
        unbounded, and this column is read back on every inspector open.
        """
        text = request.text
        messages: list[dict[str, Any]] | None = None
        dropped_messages = 0
        if request.messages:
            messages = []
            for message in request.messages[:_MAX_MESSAGES]:
                item = dict(message)
                content = item.get("content")
                if isinstance(content, str) and len(content) > _TEXT_TRUNCATE:
                    item["content"] = content[:_TEXT_TRUNCATE]
                    item["content_truncated"] = True
                messages.append(item)
            dropped_messages = len(request.messages) - len(messages)

        fallback = request.fallback
        return {
            "task": request.task.value,
            "modality": request.modality.value,
            "external_user_id": request.external_user_id,
            # What the caller asked for, before routing had its say. The model
            # that actually ran is on the row itself.
            "model_id": request.model_id,
            "tier": request.tier,
            "provider_hint": request.provider_hint,
            "text": (text[:_TEXT_TRUNCATE] if text is not None else None),
            "text_truncated": bool(text and len(text) > _TEXT_TRUNCATE),
            "messages": messages,
            "dropped_messages": dropped_messages,
            "params": dict(request.params or {}),
            "fallback": (
                {
                    "policy": fallback.policy,
                    "scope": fallback.scope,
                    "max_steps": fallback.max_steps,
                }
                if fallback is not None
                else None
            ),
            # The bytes live in the blob store; the row keeps what they were.
            "attachments": [
                {
                    "kind": att.kind,
                    # What it was sent *as*. Without it a two-image run reads as
                    # two identical rows and the log cannot answer which photo
                    # the model was told was the garment.
                    "role": att.role,
                    "mime": att.mime,
                    "bytes": len(att.data) if att.data else None,
                    # A data: URL is megabytes of base64 — name it, don't store it.
                    "url": (att.url[:200] if att.url else None),
                    "ref": att.ref,
                }
                for att in request.attachments
            ],
            "request_ref": request_ref,
            "channel": request.channel,
            "tool": request.tool,
            "client_id": request.client_id,
            "client_request_id": request.client_request_id,
            "batch_id": request.batch_id,
        }

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
        request_json: dict[str, Any] | None,
        exc: Exception,
        t0: float,
        *,
        requested_model_id: str | None = None,
        fallback_reason: str | None = None,
        steps: list[dict[str, Any]] | None = None,
        http_trace: list[dict[str, Any]] | None = None,
        row_id: int | None = None,
        dropped: int = 0,
    ) -> int:
        trace = http_trace or []
        # A read timeout means the provider most likely ran — and billed — the
        # request; recording zero would understate the bill with confidence.
        cost_source = "cost_unknown" if isinstance(exc, ProviderTimeout) else "computed"
        rec = RequestLogRecord(
            external_user_id=request.external_user_id,
            modality=request.modality.value,
            task=request.task.value,
            service_provider_id=model.service_provider_id if model else request.provider_hint,
            model_provider=model.model_provider if model else None,
            model_id=model.model_id if model else request.model_id,
            status="error",
            error_message=f"{type(exc).__name__}: {exc}"[:1000],
            request_text=request_text,
            request_ref=request_ref,
            request_json=request_json,
            cost_source=cost_source,
            latency_ms=int((time.monotonic() - t0) * 1000),
            requested_model_id=requested_model_id,
            fallback_reason=fallback_reason,
            channel=request.channel,
            tool=request.tool,
            client_id=request.client_id,
            client_request_id=request.client_request_id,
            batch_id=request.batch_id,
            attempts=len(trace) or None,
            provider_status=_last_status(trace)
            or getattr(exc, "provider_status", None),
            plan_json=_steps_json(steps or [], dropped),
            finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        if row_id is None:
            log_id = await self._db.log_request(rec)
        else:
            # This failure is the outcome of the run, so it closes the row that
            # was opened when the run started rather than adding a second one.
            await self._db.finish_request_log(row_id, rec)
            log_id = row_id
        # "Open this error in the log" is offered on three screens; without the
        # id travelling with the exception the link has nowhere to point.
        if isinstance(exc, RouterError):
            exc.log_id = log_id
            if exc.provider is None:
                exc.provider = rec.service_provider_id
            if exc.model_id is None:
                exc.model_id = rec.model_id
        return log_id
