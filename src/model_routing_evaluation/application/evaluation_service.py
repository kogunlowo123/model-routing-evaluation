"""Evaluation use cases: offline runs, judge calibration and gated routing-pool promotions."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Mapping, Sequence
from typing import Any

from agentic_core.audit import AuditLog
from agentic_core.domain.classification import DataClassification, Decision
from agentic_core.domain.identifiers import new_id
from agentic_core.models.types import ModelResponse
from agentic_core.observability import correlation_id, get_logger
from agentic_core.policy import PolicyDecision, PolicyEngine, PolicyRequest, Resource

from model_routing_evaluation.application.commands import (
    CalibrationCommand,
    PromotionCommand,
    PromotionDecision,
    RunCommand,
)
from model_routing_evaluation.application.routing_service import RoutingService, subject_of
from model_routing_evaluation.domain.errors import (
    ConflictError,
    ForbiddenError,
    NoEligibleProviderError,
    NotFoundError,
    UnprocessableError,
)
from model_routing_evaluation.domain.evaluation import (
    CalibrationResult,
    CaseResult,
    EvaluationRun,
    GoldenSet,
    Promotion,
    PromotionAction,
    PromotionStatus,
    RunStatus,
)
from model_routing_evaluation.domain.models import LedgerEntry, LedgerKind, RoutingConstraints
from model_routing_evaluation.domain.ports import (
    BanditStore,
    Clock,
    EvaluationRepository,
    Ledger,
    PromotionRepository,
)
from model_routing_evaluation.domain.stats import FAMILY_BUCKET, Observation
from model_routing_evaluation.evaluation.calibration import calibrate
from model_routing_evaluation.evaluation.harness import (
    EvaluationContext,
    OfflineEvaluator,
    pairwise_judges_for,
)
from model_routing_evaluation.evaluation.judge import JUDGE_TASK, LlmJudge
from model_routing_evaluation.observability.metrics import RoutingMetrics
from model_routing_evaluation.security.identity import Caller

log = get_logger("model_routing_evaluation.evaluation")

JUDGE_MAX_TOKENS = 300


class EvaluationService:
    def __init__(
        self,
        *,
        routing: RoutingService,
        evaluator: OfflineEvaluator,
        judge: LlmJudge,
        golden_sets: Mapping[str, GoldenSet],
        runs: EvaluationRepository,
        promotions: PromotionRepository,
        ledger: Ledger,
        bandit: BanditStore,
        governance: PolicyEngine,
        audit: AuditLog,
        metrics: RoutingMetrics,
        clock: Clock,
        min_judge_kappa: float,
        min_calibration_items: int,
    ) -> None:
        self._routing = routing
        self._evaluator = evaluator
        self._judge = judge
        self.golden_sets = dict(golden_sets)
        self._runs = runs
        self._promotions = promotions
        self._ledger = ledger
        self._bandit = bandit
        self._governance = governance
        self._audit = audit
        self._metrics = metrics
        self._clock = clock
        self._min_kappa = min_judge_kappa
        self._min_items = min_calibration_items
        self._tasks: set[asyncio.Task[None]] = set()

    # -------------------------------------------------------------- helpers

    def _provider_names(self) -> set[str]:
        return {p.profile.name for p in self._routing.router.providers}

    def _govern(
        self,
        caller: Caller,
        action: str,
        resource: Resource,
        environment: dict[str, Any],
    ) -> PolicyDecision:
        decision = self._governance.evaluate(
            PolicyRequest(
                subject=subject_of(caller),
                action=action,
                resource=resource,
                environment=environment,
            )
        )
        if decision.decision is Decision.DENY:
            raise ForbiddenError(
                f"{action} denied by governance policy: {decision.reason}",
                details={"matched_rules": list(decision.matched_rules)},
            )
        return decision

    def judges_cleared_for(
        self, classification: DataClassification, constraints: RoutingConstraints
    ) -> frozenset[str]:
        """Judges that may see material of this classification under these constraints."""
        eligible, _ = self._routing.compliance.evaluate(
            self._routing.router.providers,
            pool=sorted(self._provider_names()),
            family=JUDGE_TASK,
            classification=classification,
            constraints=RoutingConstraints(
                residency=constraints.residency, requires_baa=constraints.requires_baa
            ),
            estimate_cost=lambda _: 0.0,
        )
        return frozenset(p.profile.name for p in eligible)

    async def _record_usage(
        self, reference: str, family: str, caller: str, response: ModelResponse, kind: LedgerKind
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

    # ----------------------------------------------------------------- runs

    def _estimate_run(
        self, golden: GoldenSet, providers: Sequence[str], judges: Sequence[str], pairwise: bool
    ) -> float:
        family = self._routing.config.family(golden.task)
        router = self._routing.router
        per_case = 0.0
        for name in providers:
            profile = router.provider(name).profile
            for case in golden.cases:
                per_case += RoutingService.estimate_cost(
                    profile,
                    input_chars=len(case.input) + len(golden.system_prompt or ""),
                    max_tokens=golden.max_tokens,
                    family=family,
                )
        judge_cost = 0.0
        if judges:
            judge_profile = router.provider(judges[0]).profile
            prompt_chars = max(len(c.input) for c in golden.cases) + 2 * golden.max_tokens * 4
            one_call = RoutingService.estimate_cost(
                judge_profile, input_chars=prompt_chars, max_tokens=JUDGE_MAX_TOKENS, family=family
            )
            calls = len(golden.cases) * len(providers) if golden.uses_judge else 0
            if pairwise:
                calls += len(golden.cases) * math.comb(len(providers), 2) * 2
            judge_cost = calls * one_call
        return per_case + judge_cost

    async def start_run(self, cmd: RunCommand, caller: Caller) -> EvaluationRun:
        golden = self.golden_sets.get(cmd.golden_set)
        if golden is None:
            raise NotFoundError(
                f"golden set {cmd.golden_set!r} not found",
                details={"golden_sets": sorted(self.golden_sets)},
            )
        family_name = golden.task
        family = self._routing.config.family(family_name)
        requested = cmd.providers or await self._routing.pool(family_name)
        unknown = sorted(set(requested) - self._provider_names())
        if unknown:
            raise UnprocessableError(f"unknown providers {unknown}")
        eligible, excluded = self._routing.compliance.evaluate(
            self._routing.router.providers,
            pool=list(requested),
            family=family_name,
            classification=golden.classification,
            constraints=golden.constraints,
            estimate_cost=lambda _: 0.0,
        )
        providers = tuple(p.profile.name for p in eligible)
        if not providers:
            raise NoEligibleProviderError(
                "no requested provider may process this golden set",
                details={"excluded": [e.model_dump(mode="json") for e in excluded]},
            )
        allowed_judges = self.judges_cleared_for(golden.classification, golden.constraints)
        pairwise_judges: tuple[str, ...] = ()
        if cmd.pairwise:
            if len(providers) < 2:
                raise UnprocessableError("pairwise ranking needs at least two eligible providers")
            wanted = cmd.pairwise_judges or pairwise_judges_for(
                golden, family.shadow.judge_providers
            )
            pairwise_judges = tuple(j for j in wanted if j in allowed_judges)
            if not pairwise_judges:
                raise UnprocessableError("no judge is cleared for this golden set's data")
        estimate = self._estimate_run(
            golden,
            providers,
            pairwise_judges or tuple(sorted(allowed_judges)),
            cmd.pairwise,
        )
        decision = self._govern(
            caller,
            "evaluation:run",
            Resource(type="golden-set", id=golden.name, classification=golden.classification),
            {"estimated_cost_usd": round(estimate, 6)},
        )
        if decision.decision is Decision.REQUIRE_APPROVAL:
            raise ForbiddenError(
                "this evaluation run needs approval; reduce its scope or ask governance",
                details={"estimated_cost_usd": estimate},
            )
        run = EvaluationRun(
            run_id=new_id(),
            golden_set=golden.name,
            golden_version=golden.version,
            golden_digest=golden.digest,
            family=family_name,
            providers=providers,
            pairwise=cmd.pairwise,
            status=RunStatus.RUNNING,
            requested_by=caller.subject,
            estimated_cost_usd=round(estimate, 6),
            created_at=self._clock.now(),
        )
        await self._runs.add_run(run)
        await self._audit.record(
            actor=caller.subject,
            action="evaluation.started",
            resource=f"evaluation:{run.run_id}",
            outcome="running",
            correlation_id=correlation_id(),
            details={
                "golden_set": golden.name,
                "golden_digest": run.golden_digest,
                "providers": list(providers),
                "excluded": [f"{e.provider}:{e.reason.value}" for e in excluded],
                "estimated_cost_usd": run.estimated_cost_usd,
                "policy_digest": decision.policy_digest,
            },
        )
        context = EvaluationContext(
            golden=golden,
            providers=providers,
            allowed_judges=allowed_judges,
            pairwise_judges=pairwise_judges,
            calibrations=await self._runs.latest_calibrations(),
            record_usage=lambda response, kind: self._record_usage(
                run.run_id, family_name, caller.subject, response, kind
            ),
        )
        excluded_map = {e.provider: e.reason.value for e in excluded}
        task = asyncio.create_task(self._execute(run, context, excluded_map))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return run

    async def _execute(
        self, run: EvaluationRun, context: EvaluationContext, excluded: dict[str, str]
    ) -> None:
        try:
            results, summary = await self._evaluator.run(context)
            summary = summary.model_copy(update={"excluded": excluded})
            await self._runs.add_results(run.run_id, results)
            weight = self._routing.config.offline_prior_weight
            if weight > 0:
                for result in results:
                    if result.error is None:
                        await self._bandit.observe(
                            run.family,
                            FAMILY_BUCKET,
                            result.provider,
                            Observation(
                                quality=result.score,
                                latency_ms=result.latency_ms,
                                cost_usd=result.cost_usd,
                                weight=weight,
                            ),
                        )
            status, error = RunStatus.COMPLETED, None
        except Exception as exc:
            log.exception("evaluation_run_failed", run_id=run.run_id)
            status, summary, error = RunStatus.FAILED, None, f"{type(exc).__name__}: {exc}"[:500]
        await self._runs.finish_run(
            run.run_id, status=status, summary=summary, error=error, at=self._clock.now()
        )
        self._metrics.evaluations.labels(status.value).inc()
        await self._audit.record(
            actor=run.requested_by,
            action="evaluation.finished",
            resource=f"evaluation:{run.run_id}",
            outcome=status.value,
            details={"error": error} if error else {},
        )

    async def get_run(self, run_id: str) -> tuple[EvaluationRun, Sequence[CaseResult]]:
        run = await self._runs.get_run(run_id)
        if run is None:
            raise NotFoundError(f"evaluation run {run_id} not found")
        return run, await self._runs.results(run_id)

    async def recover(self) -> int:
        """Runs cannot survive a restart (they execute in-process); mark them failed."""
        return await self._runs.fail_running(
            reason="interrupted by service restart", at=self._clock.now()
        )

    async def drain(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    # ---------------------------------------------------------- calibration

    async def calibrate(self, cmd: CalibrationCommand, caller: Caller) -> CalibrationResult:
        if cmd.judge_provider not in self._provider_names():
            raise UnprocessableError(f"unknown judge provider {cmd.judge_provider!r}")
        if cmd.judge_provider not in self.judges_cleared_for(
            cmd.classification, RoutingConstraints()
        ):
            raise NoEligibleProviderError(
                f"{cmd.judge_provider} may not process {cmd.classification.name} material"
            )
        self._govern(
            caller,
            "evaluation:calibrate",
            Resource(type="judge", id=cmd.judge_provider, classification=cmd.classification),
            {"items": len(cmd.items)},
        )
        outcome = await calibrate(
            self._judge,
            judge_provider=cmd.judge_provider,
            rubric=cmd.rubric,
            items=cmd.items,
            classification=cmd.classification,
            min_kappa=self._min_kappa,
            min_items=self._min_items,
            created_by=caller.subject,
            now=self._clock.now(),
        )
        for response in outcome.responses:
            await self._record_usage(
                outcome.result.calibration_id,
                JUDGE_TASK,
                caller.subject,
                response,
                LedgerKind.JUDGE,
            )
        await self._runs.add_calibration(outcome.result)
        await self._audit.record(
            actor=caller.subject,
            action="judge.calibrated",
            resource=f"judge:{cmd.judge_provider}",
            outcome="calibrated" if outcome.result.calibrated else "not_calibrated",
            correlation_id=correlation_id(),
            details={
                "rubric_digest": outcome.result.rubric_digest,
                "items": outcome.result.items,
                "weighted_kappa": outcome.result.weighted_kappa,
            },
        )
        return outcome.result

    # ----------------------------------------------------------- promotions

    async def _evidence(self, cmd: PromotionCommand, pool: Sequence[str]) -> dict[str, Any]:
        family = self._routing.config.family(cmd.family)
        if cmd.action is PromotionAction.REMOVE:
            if cmd.provider not in pool:
                raise UnprocessableError(f"{cmd.provider} is not in the {cmd.family} pool")
            if len(pool) == 1:
                raise UnprocessableError("cannot remove the last provider from a routing pool")
            return {"pool_size_after": len(pool) - 1}
        if cmd.provider in pool:
            raise ConflictError(f"{cmd.provider} is already in the {cmd.family} pool")
        if cmd.run_id is None:
            raise UnprocessableError("adding a provider requires an evaluation run as evidence")
        run = await self._runs.get_run(cmd.run_id)
        if run is None or run.status is not RunStatus.COMPLETED or run.summary is None:
            raise UnprocessableError("evidence run is missing or not completed")
        if run.family != cmd.family:
            raise UnprocessableError(
                f"evidence run evaluated family {run.family}, not {cmd.family}"
            )
        summary = next((s for s in run.summary.providers if s.provider == cmd.provider), None)
        if summary is None:
            raise UnprocessableError(f"evidence run did not evaluate {cmd.provider}")
        gate = family.promotion
        failures: list[str] = []
        if summary.cases < gate.min_cases:
            failures.append(f"{summary.cases} cases < required {gate.min_cases}")
        if summary.pass_rate < gate.min_pass_rate:
            failures.append(f"pass rate {summary.pass_rate:.3f} < required {gate.min_pass_rate}")
        if gate.require_calibrated_judge and run.summary.judge_calibrated is False:
            failures.append("judge was not calibrated against human scores")
        if failures:
            raise UnprocessableError(
                "promotion evidence is insufficient", details={"gate": failures}
            )
        return {
            "run_id": run.run_id,
            "golden_set": run.golden_set,
            "golden_digest": run.golden_digest,
            "cases": summary.cases,
            "pass_rate": summary.pass_rate,
            "pass_rate_ci95_low": summary.pass_rate_ci95.low,
            "judge_calibrated": run.summary.judge_calibrated,
        }

    async def request_promotion(self, cmd: PromotionCommand, caller: Caller) -> Promotion:
        if cmd.provider not in self._provider_names():
            raise UnprocessableError(f"unknown provider {cmd.provider!r}")
        pool = await self._routing.pool(cmd.family)
        evidence = await self._evidence(cmd, pool)
        decision = self._govern(
            caller,
            "routing-pool:change",
            Resource(type="task-family", id=cmd.family, attributes={"change": cmd.action.value}),
            {"evidence_ok": True},
        )
        now = self._clock.now()
        auto = decision.decision is Decision.ALLOW
        promotion = Promotion(
            promotion_id=new_id(),
            family=cmd.family,
            provider=cmd.provider,
            action=cmd.action,
            run_id=cmd.run_id,
            justification=cmd.justification,
            status=PromotionStatus.APPROVED if auto else PromotionStatus.PENDING,
            requested_by=caller.subject,
            evidence=evidence,
            policy_version=decision.policy_version,
            policy_digest=decision.policy_digest,
            decided_by=f"policy:{','.join(decision.matched_rules)}" if auto else None,
            decision_reason=decision.reason if auto else None,
            created_at=now,
            decided_at=now if auto else None,
        )
        await self._promotions.add(promotion)
        self._metrics.promotions.labels(promotion.status.value).inc()
        await self._audit.record(
            actor=caller.subject,
            action="promotion.requested",
            resource=f"promotion:{promotion.promotion_id}",
            outcome=promotion.status.value,
            correlation_id=correlation_id(),
            details={
                "family": cmd.family,
                "provider": cmd.provider,
                "change": cmd.action.value,
                "evidence": evidence,
                "policy_digest": decision.policy_digest,
                "matched_rules": list(decision.matched_rules),
            },
        )
        return promotion

    async def decide_promotion(
        self, promotion_id: str, cmd: PromotionDecision, approver: Caller
    ) -> Promotion:
        promotion = await self._promotions.get(promotion_id)
        if promotion is None:
            raise NotFoundError(f"promotion {promotion_id} not found")
        if promotion.status is not PromotionStatus.PENDING:
            raise ConflictError(f"promotion is already {promotion.status.value}")
        if approver.subject == promotion.requested_by:
            raise ForbiddenError(
                "the requester cannot decide their own promotion (two-person rule)"
            )
        status = PromotionStatus.APPROVED if cmd.approve else PromotionStatus.REJECTED
        if cmd.approve:
            # Re-check evidence at decision time: the pool may have changed since the request.
            await self._evidence(
                PromotionCommand(
                    family=promotion.family,
                    provider=promotion.provider,
                    action=promotion.action,
                    run_id=promotion.run_id,
                    justification=promotion.justification,
                ),
                await self._routing.pool(promotion.family),
            )
        now = self._clock.now()
        if not await self._promotions.decide(
            promotion_id, status=status, decided_by=approver.subject, reason=cmd.reason, at=now
        ):
            raise ConflictError("promotion was decided concurrently")
        self._metrics.promotions.labels(status.value).inc()
        await self._audit.record(
            actor=approver.subject,
            action="promotion.decided",
            resource=f"promotion:{promotion_id}",
            outcome=status.value,
            correlation_id=correlation_id(),
            details={
                "family": promotion.family,
                "provider": promotion.provider,
                "change": promotion.action.value,
                "reason": cmd.reason,
                "requested_by": promotion.requested_by,
            },
        )
        decided = await self._promotions.get(promotion_id)
        if decided is None:  # pragma: no cover - row cannot disappear (no deletes)
            raise NotFoundError(f"promotion {promotion_id} not found")
        return decided

    async def promotions(
        self, *, family: str | None, status: PromotionStatus | None
    ) -> Sequence[Promotion]:
        return await self._promotions.list(family=family, status=status)
