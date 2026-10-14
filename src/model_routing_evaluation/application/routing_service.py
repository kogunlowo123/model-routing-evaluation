"""Routing use cases: plan, route, shadow-evaluate and learn from feedback."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time
from typing import Any

from agentic_core.audit import AuditLog
from agentic_core.domain.classification import Decision, TrustLevel
from agentic_core.domain.errors import ProviderUnavailableError, SecurityViolationError
from agentic_core.domain.identifiers import new_id
from agentic_core.domain.provenance import content_digest
from agentic_core.models.router import AttemptOutcome
from agentic_core.models.types import Message, ModelRequest, ModelResponse, ProviderProfile, Role
from agentic_core.observability import correlation_id, get_logger
from agentic_core.policy import PolicyEngine, PolicyRequest, Resource, Subject
from agentic_core.security import ContentGuard

from model_routing_evaluation.application.commands import (
    FeedbackCommand,
    FeedbackResult,
    RouteCommand,
    RouteResult,
    Usage,
)
from model_routing_evaluation.domain.errors import (
    ApprovalRequiredError,
    ConflictError,
    ForbiddenError,
    NoEligibleProviderError,
    NotFoundError,
    OutputBlockedError,
    UpstreamUnavailableError,
)
from model_routing_evaluation.domain.evaluation import PromotionAction, PromotionStatus
from model_routing_evaluation.domain.models import (
    Candidate,
    DecisionRecord,
    LedgerEntry,
    LedgerKind,
    RoutePlan,
    RoutingConstraints,
    ShadowOutcome,
    ShadowResult,
    Strategy,
)
from model_routing_evaluation.domain.ports import (
    BanditStore,
    Clock,
    DecisionRepository,
    Ledger,
    PromotionRepository,
    ShadowRepository,
)
from model_routing_evaluation.domain.stats import FAMILY_BUCKET, ArmStats, Observation
from model_routing_evaluation.evaluation.graders import check_caller_schema, grade_json_schema
from model_routing_evaluation.evaluation.judge import JudgeError, LlmJudge
from model_routing_evaluation.observability.metrics import RoutingMetrics
from model_routing_evaluation.routing.bandit import ThompsonSampler, utility
from model_routing_evaluation.routing.compliance import ComplianceFilter
from model_routing_evaluation.routing.config import FamilyConfig, RoutingConfig
from model_routing_evaluation.routing.router import PlannedRouter
from model_routing_evaluation.security.identity import Caller

log = get_logger("model_routing_evaluation.routing")

ROUTE_ACTION = "model-routing:invoke"
PAIRWISE_SHADOW_WEIGHT = 0.5


@dataclass(frozen=True)
class PlannedRequest:
    plan: RoutePlan
    request: ModelRequest
    family: FamilyConfig


@dataclass(frozen=True)
class ShadowJob:
    """Everything a background shadow comparison needs from the primary request."""

    planned: PlannedRequest
    request: ModelRequest
    primary: ModelResponse
    primary_text: str
    cmd: RouteCommand
    caller: Caller


def _trust(value: str) -> TrustLevel:
    return TrustLevel.EXTERNAL if value == "external" else TrustLevel.USER


def subject_of(caller: Caller) -> Subject:
    return Subject(
        id=caller.subject,
        kind=caller.kind,
        roles=caller.roles,
        attributes={"delegated": bool(caller.delegation_chain)},
    )


class RoutingService:
    def __init__(
        self,
        *,
        router: PlannedRouter,
        config: RoutingConfig,
        compliance: ComplianceFilter,
        sampler: ThompsonSampler,
        bandit: BanditStore,
        decisions: DecisionRepository,
        ledger: Ledger,
        shadows: ShadowRepository,
        promotions: PromotionRepository,
        governance: PolicyEngine,
        guard: ContentGuard,
        judge: LlmJudge,
        audit: AuditLog,
        metrics: RoutingMetrics,
        clock: Clock,
        rng: random.Random,
        shadow_daily_budget_usd: float,
    ) -> None:
        self.router = router
        self.config = config
        self.compliance = compliance
        self._sampler = sampler
        self._bandit = bandit
        self._decisions = decisions
        self._ledger = ledger
        self._shadows = shadows
        self._promotions = promotions
        self._governance = governance
        self._guard = guard
        self._judge = judge
        self._audit = audit
        self._metrics = metrics
        self._clock = clock
        self._rng = rng
        self._shadow_budget = shadow_daily_budget_usd
        self._tasks: set[asyncio.Task[None]] = set()

    # ---------------------------------------------------------------- pools

    async def pool(self, family: str) -> tuple[str, ...]:
        """Configured pool with approved promotions applied in approval order."""
        members = list(self.config.family(family).pool)
        approved = await self._promotions.list(family=family, status=PromotionStatus.APPROVED)
        for promotion in sorted(approved, key=lambda p: p.decided_at or p.created_at):
            if promotion.action is PromotionAction.ADD and promotion.provider not in members:
                members.append(promotion.provider)
            elif promotion.action is PromotionAction.REMOVE and promotion.provider in members:
                members.remove(promotion.provider)
        return tuple(members)

    # ------------------------------------------------------------- planning

    @staticmethod
    def estimate_cost(
        profile: ProviderProfile, *, input_chars: int, max_tokens: int, family: FamilyConfig
    ) -> float:
        """Price-sheet estimate: ~4 characters per input token, expected share of max_tokens."""
        return profile.estimate_cost(
            input_chars // 4 + 1, int(max_tokens * family.expected_output_ratio)
        )

    def _request(self, cmd: RouteCommand, family: str) -> ModelRequest:
        return ModelRequest(
            messages=tuple(
                Message(role=Role(m.role), content=m.content, trust=_trust(m.trust))
                for m in cmd.messages
            ),
            max_tokens=cmd.max_tokens,
            temperature=cmd.temperature,
            classification=cmd.classification,
            task=family,
        )

    def _govern(self, caller: Caller, cmd: RouteCommand) -> tuple[str, str]:
        environment: dict[str, Any] = {}
        if cmd.purpose_of_use:
            environment["purpose_of_use"] = cmd.purpose_of_use
        decision = self._governance.evaluate(
            PolicyRequest(
                subject=subject_of(caller),
                action=ROUTE_ACTION,
                resource=Resource(
                    type="task-family", id=cmd.task, classification=cmd.classification
                ),
                environment=environment,
            )
        )
        if decision.decision is Decision.DENY:
            raise ForbiddenError(
                f"routing denied by governance policy: {decision.reason}",
                details={"matched_rules": list(decision.matched_rules)},
            )
        if decision.decision is Decision.REQUIRE_APPROVAL:
            raise ApprovalRequiredError(
                "routing this request requires human approval",
                details={"matched_rules": list(decision.matched_rules)},
            )
        return decision.policy_version, decision.policy_digest

    async def plan(self, cmd: RouteCommand, caller: Caller) -> PlannedRequest:
        family = self.config.family(cmd.task)
        if cmd.response_schema is not None:
            check_caller_schema(cmd.response_schema)
        policy_version, policy_digest = self._govern(caller, cmd)
        request = self._request(cmd, cmd.task)
        input_chars = sum(len(m.content) for m in cmd.messages)
        strategy = cmd.strategy or self.config.default_strategy
        pool = await self.pool(cmd.task)
        eligible, excluded = self.compliance.evaluate(
            self.router.providers,
            pool=pool,
            family=cmd.task,
            classification=cmd.classification,
            constraints=cmd.constraints,
            estimate_cost=lambda p: self.estimate_cost(
                p, input_chars=input_chars, max_tokens=cmd.max_tokens, family=family
            ),
        )
        for exclusion in excluded:
            self._metrics.exclusions.labels(cmd.task, exclusion.reason.value).inc()
        bucket = self.config.bucket(input_chars)
        candidates = await self._order(
            strategy=strategy,
            family_name=cmd.task,
            family=family,
            bucket=bucket,
            eligible=sorted(eligible, key=lambda p: pool.index(p.profile.name)),
            input_chars=input_chars,
            max_tokens=cmd.max_tokens,
        )
        plan = RoutePlan(
            family=cmd.task,
            bucket=bucket,
            strategy=strategy,
            candidates=tuple(candidates),
            excluded=tuple(excluded),
            policy_version=policy_version,
            policy_digest=policy_digest,
        )
        return PlannedRequest(plan=plan, request=request, family=family)

    async def _order(
        self,
        *,
        strategy: Strategy,
        family_name: str,
        family: FamilyConfig,
        bucket: str,
        eligible: Sequence[Any],
        input_chars: int,
        max_tokens: int,
    ) -> list[Candidate]:
        def estimate(profile: ProviderProfile) -> float:
            return self.estimate_cost(
                profile, input_chars=input_chars, max_tokens=max_tokens, family=family
            )

        if strategy is Strategy.PRIORITY:
            return [
                Candidate(
                    provider=p.profile.name,
                    model=p.profile.model,
                    estimated_cost_usd=estimate(p.profile),
                )
                for p in eligible
            ]
        if strategy is Strategy.COST:
            ranked = sorted(eligible, key=lambda p: (estimate(p.profile), p.profile.name))
            return [
                Candidate(
                    provider=p.profile.name,
                    model=p.profile.model,
                    estimated_cost_usd=estimate(p.profile),
                )
                for p in ranked
            ]
        stats = await self._bandit.load(family_name)
        scored: list[Candidate] = []
        for provider in eligible:
            name = provider.profile.name
            posterior = self._sampler.posterior(
                stats.get((bucket, name), ArmStats()), stats.get((FAMILY_BUCKET, name), ArmStats())
            )
            est = estimate(provider.profile)
            sample = self._sampler.sample(
                posterior, latency_target_ms=family.latency_target_ms, estimated_cost_usd=est
            )
            facts = self.compliance.profile(name)
            value = utility(
                quality=sample.quality,
                latency_ms=sample.latency_ms,
                cost_usd=sample.cost_usd,
                posture=facts.posture_score if facts else 0.0,
                family=family,
            )
            scored.append(
                Candidate(
                    provider=name,
                    model=provider.profile.model,
                    estimated_cost_usd=est,
                    utility=round(value, 6),
                    sampled_quality=round(sample.quality, 6),
                    sampled_latency_ms=round(sample.latency_ms, 3),
                    sampled_cost_usd=sample.cost_usd,
                )
            )
        scored.sort(key=lambda c: (-(c.utility or 0.0), c.provider))
        return scored

    # -------------------------------------------------------------- routing

    async def route(self, cmd: RouteCommand, caller: Caller) -> RouteResult:
        planned = await self.plan(cmd, caller)
        plan = planned.plan
        decision_id = planned.request.request_id
        if not plan.candidates:
            self._metrics.route_failures.labels(plan.family, "no_eligible_provider").inc()
            await self._audit_event(
                caller, "route.rejected", decision_id, "no_eligible_provider", plan=plan
            )
            raise NoEligibleProviderError(
                "no provider satisfies the request's compliance, pool and cost constraints",
                details={"excluded": [e.model_dump(mode="json") for e in plan.excluded]},
            )

        inspected = self._guard.inspect_request(planned.request)
        if inspected.blocked or inspected.request is None:
            self._metrics.guard_blocks.labels("input").inc()
            await self._audit_event(
                caller,
                "route.blocked",
                decision_id,
                "input_blocked",
                plan=plan,
                extra={"findings": sorted({f.kind for f in inspected.findings})},
            )
            raise SecurityViolationError(
                "request blocked by content controls", findings=list(inspected.reasons)
            )
        request = inspected.request
        redactions = sum(1 for f in inspected.findings if f.category == "pii")

        with self.router.planned(request.request_id, plan.order):
            try:
                routed = await self.router.generate(request)
            except ProviderUnavailableError as exc:
                for name in {a.split("#", 1)[0] for a in exc.attempts if a.endswith(":failed")}:
                    await self._bandit.observe(
                        plan.family, plan.bucket, name, Observation(quality=0.0)
                    )
                self._metrics.route_failures.labels(plan.family, "providers_unavailable").inc()
                await self._audit_event(
                    caller,
                    "route.failed",
                    decision_id,
                    "providers_unavailable",
                    plan=plan,
                    extra={"attempts": exc.attempts},
                )
                raise UpstreamUnavailableError(
                    "every eligible provider failed", details={"attempts": exc.attempts}
                ) from exc

        response = routed.response
        for failed in {
            a.provider
            for a in routed.attempts
            if a.outcome is AttemptOutcome.FAILED and a.provider != response.provider
        }:
            await self._bandit.observe(plan.family, plan.bucket, failed, Observation(quality=0.0))
        await self._charge(decision_id, LedgerKind.ROUTE, plan.family, response, caller.subject)

        checked = self._guard.inspect_output(response.text)
        if checked.blocked or checked.text is None:
            self._metrics.guard_blocks.labels("output").inc()
            await self._bandit.observe(
                plan.family,
                plan.bucket,
                response.provider,
                Observation(
                    quality=0.0, latency_ms=response.latency_ms, cost_usd=response.cost_usd
                ),
            )
            await self._audit_event(
                caller, "route.blocked", decision_id, "output_blocked", plan=plan
            )
            raise OutputBlockedError("the model output failed content controls and was withheld")
        text = checked.text
        redactions += sum(1 for f in checked.findings if f.category == "pii")

        schema_valid: bool | None = None
        quality: float | None = None
        if cmd.response_schema is not None:
            grade = grade_json_schema(cmd.response_schema, None, text)
            schema_valid, quality = grade.passed, grade.score
        await self._bandit.observe(
            plan.family,
            plan.bucket,
            response.provider,
            Observation(
                quality=quality, latency_ms=response.latency_ms, cost_usd=response.cost_usd
            ),
        )
        record = DecisionRecord(
            decision_id=decision_id,
            family=plan.family,
            bucket=plan.bucket,
            strategy=plan.strategy,
            caller=caller.subject,
            classification=cmd.classification,
            provider=response.provider,
            model=response.model,
            candidates=plan.order,
            excluded=plan.excluded,
            attempts=tuple(
                f"{a.provider}#{a.try_number}:{a.outcome.value}" for a in routed.attempts
            ),
            input_digest=content_digest("\n".join(m.content for m in request.messages)),
            output_digest=content_digest(text),
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cost_usd=response.cost_usd,
            latency_ms=response.latency_ms,
            quality=quality,
            quality_source="schema" if quality is not None else None,
            created_at=self._clock.now(),
        )
        await self._decisions.add(record)
        self._metrics.routed.labels(plan.family, response.provider, plan.strategy.value).inc()
        await self._audit_event(
            caller,
            "route.completed",
            decision_id,
            "served",
            plan=plan,
            extra={
                "provider": response.provider,
                "model": response.model,
                "cost_usd": response.cost_usd,
                "attempts": list(record.attempts),
                "output_digest": record.output_digest,
            },
        )
        self._maybe_shadow(
            ShadowJob(
                planned=planned,
                request=request,
                primary=response,
                primary_text=text,
                cmd=cmd,
                caller=caller,
            )
        )
        return RouteResult(
            decision_id=decision_id,
            family=plan.family,
            strategy=plan.strategy,
            provider=response.provider,
            model=response.model,
            text=text,
            finish_reason=response.finish_reason,
            usage=Usage(
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            ),
            cost_usd=response.cost_usd,
            latency_ms=response.latency_ms,
            candidates=plan.candidates,
            excluded=plan.excluded,
            attempts=routed.attempts,
            failed_over=routed.failed_over,
            schema_valid=schema_valid,
            redactions=redactions,
            policy_version=plan.policy_version,
            policy_digest=plan.policy_digest,
        )

    async def _charge(
        self, reference: str, kind: LedgerKind, family: str, response: ModelResponse, caller: str
    ) -> None:
        await self._ledger.record(
            LedgerEntry(
                reference=reference,
                kind=kind,
                family=family,
                provider=response.provider,
                model=response.model,
                caller=caller,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                cost_usd=response.cost_usd,
                created_at=self._clock.now(),
            )
        )
        self._metrics.cost.labels(family, response.provider, kind.value).inc(response.cost_usd)
        self._metrics.model_latency.labels(response.provider, kind.value).observe(
            response.latency_ms / 1000
        )
        self._metrics.tokens.labels(response.provider, "input").inc(response.usage.input_tokens)
        self._metrics.tokens.labels(response.provider, "output").inc(response.usage.output_tokens)

    async def _audit_event(
        self,
        caller: Caller,
        action: str,
        resource: str,
        outcome: str,
        *,
        plan: RoutePlan,
        extra: dict[str, Any] | None = None,
    ) -> None:
        await self._audit.record(
            actor=caller.subject,
            action=action,
            resource=f"decision:{resource}",
            outcome=outcome,
            correlation_id=correlation_id(),
            details={
                "family": plan.family,
                "bucket": plan.bucket,
                "strategy": plan.strategy.value,
                "candidates": list(plan.order),
                "excluded": [f"{e.provider}:{e.reason.value}" for e in plan.excluded],
                "delegation_chain": list(caller.delegation_chain),
                "policy_version": plan.policy_version,
                "policy_digest": plan.policy_digest,
                **(extra or {}),
            },
        )

    # ------------------------------------------------------------- feedback

    async def feedback(self, cmd: FeedbackCommand, caller: Caller) -> FeedbackResult:
        record = await self._decisions.get(cmd.decision_id)
        if record is None:
            raise NotFoundError(f"decision {cmd.decision_id} not found")
        if record.caller != caller.subject:
            raise ForbiddenError("only the agent that made the request may rate its outcome")
        if not await self._decisions.set_quality(cmd.decision_id, cmd.score, "feedback"):
            raise ConflictError("feedback for this decision was already recorded")
        await self._bandit.observe(
            record.family, record.bucket, record.provider, Observation(quality=cmd.score)
        )
        self._metrics.feedback.labels(record.family, "caller").inc()
        await self._audit.record(
            actor=caller.subject,
            action="route.feedback",
            resource=f"decision:{record.decision_id}",
            outcome="recorded",
            correlation_id=correlation_id(),
            details={"provider": record.provider, "family": record.family, "score": cmd.score},
        )
        return FeedbackResult(
            decision_id=record.decision_id,
            provider=record.provider,
            family=record.family,
            score=cmd.score,
        )

    # --------------------------------------------------------------- shadow

    def _maybe_shadow(self, job: ShadowJob) -> None:
        planned, cmd = job.planned, job.cmd
        shadow = planned.family.shadow
        if shadow.rate <= 0 or not shadow.providers or self._rng.random() >= shadow.rate:
            return
        input_chars = sum(len(m.content) for m in cmd.messages)
        eligible, _ = self.compliance.evaluate(
            self.router.providers,
            pool=[p for p in shadow.providers if p != job.primary.provider],
            family=planned.plan.family,
            classification=cmd.classification,
            constraints=cmd.constraints,
            estimate_cost=lambda p: self.estimate_cost(
                p, input_chars=input_chars, max_tokens=cmd.max_tokens, family=planned.family
            ),
        )
        if not eligible:
            return
        task = asyncio.create_task(self._shadow(job, [p.profile.name for p in eligible]))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _judges(self, planned: PlannedRequest, cmd: RouteCommand) -> list[str]:
        judges = planned.family.shadow.judge_providers
        if not judges:
            return []
        eligible, _ = self.compliance.evaluate(
            self.router.providers,
            pool=judges,
            family="judge",
            classification=cmd.classification,
            constraints=RoutingConstraints(
                residency=cmd.constraints.residency, requires_baa=cmd.constraints.requires_baa
            ),
            estimate_cost=lambda _: 0.0,
        )
        return [p.profile.name for p in eligible]

    async def _shadow(self, job: ShadowJob, providers: list[str]) -> None:
        planned, caller = job.planned, job.caller
        self._metrics.shadow_in_flight.inc()
        try:
            start_of_day = datetime.combine(self._clock.now().date(), time.min, tzinfo=UTC)
            for provider in providers:
                spent = await self._ledger.spent(kind=LedgerKind.SHADOW.value, since=start_of_day)
                if spent >= self._shadow_budget:
                    self._metrics.shadow.labels(planned.plan.family, "budget_exhausted").inc()
                    return
                result = await self._compare(job, provider)
                await self._shadows.add(result)
                self._metrics.shadow.labels(planned.plan.family, result.outcome.value).inc()
                await self._audit.record(
                    actor=caller.subject,
                    action="shadow.compared",
                    resource=f"decision:{result.decision_id}",
                    outcome=result.outcome.value,
                    details={
                        "primary": result.primary_provider,
                        "shadow": result.shadow_provider,
                        "cost_usd": result.cost_usd,
                        "detail": result.detail,
                    },
                )
        except Exception:
            log.exception("shadow_evaluation_failed", decision_id=job.request.request_id)
        finally:
            self._metrics.shadow_in_flight.dec()

    async def _compare(self, job: ShadowJob, provider: str) -> ShadowResult:
        planned, request, primary, cmd = job.planned, job.request, job.primary, job.cmd
        primary_text = job.primary_text
        plan = planned.plan
        shadow_request = request.model_copy(update={"request_id": new_id()})

        def result(
            outcome: ShadowOutcome, quality: float | None, detail: str, latency: float, cost: float
        ) -> ShadowResult:
            return ShadowResult(
                shadow_id=shadow_request.request_id,
                decision_id=request.request_id,
                family=plan.family,
                primary_provider=primary.provider,
                shadow_provider=provider,
                outcome=outcome,
                shadow_quality=quality,
                latency_ms=latency,
                cost_usd=cost,
                detail=detail[:500],
                created_at=self._clock.now(),
            )

        with self.router.planned(shadow_request.request_id, [provider]):
            try:
                routed = await self.router.generate(shadow_request)
            except ProviderUnavailableError as exc:
                await self._bandit.observe(
                    plan.family, plan.bucket, provider, Observation(quality=0.0)
                )
                return result(ShadowOutcome.ERROR, None, f"unavailable: {exc}", 0.0, 0.0)
        response = routed.response
        await self._charge(
            shadow_request.request_id, LedgerKind.SHADOW, plan.family, response, "shadow"
        )
        checked = self._guard.inspect_output(response.text)
        latency, cost = response.latency_ms, response.cost_usd
        if checked.blocked or checked.text is None:
            await self._bandit.observe(
                plan.family,
                plan.bucket,
                provider,
                Observation(quality=0.0, latency_ms=latency, cost_usd=cost),
            )
            return result(ShadowOutcome.LOSS, 0.0, "shadow output blocked", latency, cost)

        if cmd.response_schema is not None:
            mine = grade_json_schema(cmd.response_schema, None, checked.text).score
            theirs = grade_json_schema(cmd.response_schema, None, primary_text).score
            outcome = (
                ShadowOutcome.WIN
                if mine > theirs
                else ShadowOutcome.LOSS if mine < theirs else ShadowOutcome.TIE
            )
            await self._bandit.observe(
                plan.family,
                plan.bucket,
                provider,
                Observation(quality=mine, latency_ms=latency, cost_usd=cost),
            )
            return result(outcome, mine, "graded by response schema", latency, cost)

        await self._bandit.observe(
            plan.family, plan.bucket, provider, Observation(latency_ms=latency, cost_usd=cost)
        )
        judges = self._judges(planned, cmd)
        if not judges:
            return result(
                ShadowOutcome.TIE, None, "no eligible judge; latency and cost only", latency, cost
            )
        prompt = "\n\n".join(m.content for m in request.messages if m.role is not Role.SYSTEM)
        try:
            verdict = await self._judge.compare(
                criteria=planned.family.description,
                prompt=prompt,
                output_a=checked.text,
                output_b=primary_text,
                judges=judges,
                exclude={provider, primary.provider},
                classification=cmd.classification,
            )
        except JudgeError as exc:
            return result(ShadowOutcome.ERROR, None, f"judge: {exc}", latency, cost)
        for judge_response in verdict.responses:
            await self._charge(
                shadow_request.request_id, LedgerKind.SHADOW, plan.family, judge_response, "shadow"
            )
        relative = {"a": 1.0, "b": 0.0, "tie": 0.5}[verdict.winner]
        weight = PAIRWISE_SHADOW_WEIGHT
        await self._bandit.observe(
            plan.family, plan.bucket, provider, Observation(quality=relative, weight=weight)
        )
        await self._bandit.observe(
            plan.family,
            plan.bucket,
            primary.provider,
            Observation(quality=1.0 - relative, weight=weight),
        )
        outcome = {"a": ShadowOutcome.WIN, "b": ShadowOutcome.LOSS, "tie": ShadowOutcome.TIE}[
            verdict.winner
        ]
        detail = f"judge={verdict.provider} consistent={verdict.consistent}"
        return result(outcome, relative, detail, latency, cost)

    async def drain(self) -> None:
        """Wait for in-flight shadow comparisons (shutdown and tests)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
