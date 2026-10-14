"""Offline evaluation harness: run a golden set against providers, grade, rank."""

from __future__ import annotations

import asyncio
import itertools
import random
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass

from agentic_core.domain.classification import TrustLevel
from agentic_core.domain.errors import ProviderUnavailableError
from agentic_core.models.types import Message, ModelRequest, ModelResponse, Role
from agentic_core.security import ContentGuard

from model_routing_evaluation.domain.evaluation import (
    CalibrationResult,
    CaseResult,
    ExactGrader,
    GoldenCase,
    GoldenSet,
    Grade,
    Interval,
    JsonSchemaGrader,
    JudgeGrader,
    ProviderSummary,
    RankedProvider,
    RegexGrader,
    RunSummary,
)
from model_routing_evaluation.domain.models import LedgerKind
from model_routing_evaluation.evaluation import bradley_terry
from model_routing_evaluation.evaluation.graders import (
    grade_exact,
    grade_json_schema,
    grade_regex,
)
from model_routing_evaluation.evaluation.judge import JudgeError, LlmJudge
from model_routing_evaluation.evaluation.statistics import (
    isotonic_predict,
    percentile,
    wilson_interval,
)
from model_routing_evaluation.routing.router import PlannedRouter

UsageSink = Callable[[ModelResponse, LedgerKind], Awaitable[None]]
MAX_STORED_OUTPUT = 8000


@dataclass(frozen=True)
class EvaluationContext:
    """Inputs fixed for one run."""

    golden: GoldenSet
    providers: tuple[str, ...]
    allowed_judges: frozenset[str]  # judges cleared by compliance for this set
    pairwise_judges: tuple[str, ...]
    calibrations: Mapping[tuple[str, str], CalibrationResult]
    record_usage: UsageSink


class OfflineEvaluator:
    def __init__(
        self,
        router: PlannedRouter,
        judge: LlmJudge,
        guard: ContentGuard,
        *,
        rng: random.Random,
        concurrency: int = 4,
        bootstrap_rounds: int = 200,
    ) -> None:
        self._router = router
        self._judge = judge
        self._guard = guard
        self._rng = rng
        self._concurrency = concurrency
        self._bootstrap_rounds = bootstrap_rounds

    def _request(self, golden: GoldenSet, case: GoldenCase) -> ModelRequest:
        messages = []
        if golden.system_prompt:
            messages.append(Message(role=Role.SYSTEM, content=golden.system_prompt))
        # Golden inputs are reviewed, version-controlled content.
        messages.append(Message(role=Role.USER, content=case.input, trust=TrustLevel.VERIFIED))
        return ModelRequest(
            messages=tuple(messages),
            max_tokens=golden.max_tokens,
            temperature=0.0,
            classification=golden.classification,
            task=golden.task,
        )

    async def _grade(
        self, ctx: EvaluationContext, case: GoldenCase, provider: str, output: str
    ) -> Grade:
        spec = ctx.golden.grader_for(case)
        if isinstance(spec, ExactGrader):
            return grade_exact(spec, case.expected, output)
        if isinstance(spec, RegexGrader):
            return grade_regex(spec, output)
        if isinstance(spec, JsonSchemaGrader):
            return grade_json_schema(spec.json_schema, case.expected, output)
        return await self._judge_grade(ctx, spec, case, provider, output)

    async def _judge_grade(
        self,
        ctx: EvaluationContext,
        spec: JudgeGrader,
        case: GoldenCase,
        provider: str,
        output: str,
    ) -> Grade:
        judges = [j for j in spec.judge_providers if j in ctx.allowed_judges]
        reference = case.expected if isinstance(case.expected, str) else None
        verdict = await self._judge.score(
            rubric=spec.rubric,
            prompt=case.input,
            output=output,
            reference=reference,
            judges=judges,
            exclude={provider},
            classification=ctx.golden.classification,
        )
        for response in verdict.responses:
            await ctx.record_usage(response, LedgerKind.JUDGE)
        if verdict.manipulation:
            return Grade(score=0.0, passed=False, detail=verdict.rationale, judge_calibrated=True)
        calibration = ctx.calibrations.get((verdict.provider, spec.rubric_digest))
        calibrated = calibration is not None and calibration.calibrated
        value = (
            isotonic_predict(calibration.mapping, float(verdict.score))
            if calibration is not None and calibrated
            else float(verdict.score)
        )
        return Grade(
            score=min(1.0, max(0.0, (value - 1.0) / 4.0)),
            passed=value >= spec.pass_threshold,
            detail=f"judge={verdict.provider} raw={verdict.score} {verdict.rationale}".strip(),
            judge_calibrated=calibrated,
        )

    async def _case(
        self, ctx: EvaluationContext, case: GoldenCase, provider: str
    ) -> tuple[CaseResult, bool | None]:
        def failed(error: str, *, latency: float = 0.0, cost: float = 0.0) -> CaseResult:
            return CaseResult(
                case_id=case.id,
                provider=provider,
                score=0.0,
                passed=False,
                latency_ms=latency,
                cost_usd=cost,
                error=error,
            )

        inspected = self._guard.inspect_request(self._request(ctx.golden, case))
        if inspected.blocked or inspected.request is None:
            return failed("input blocked: " + "; ".join(inspected.reasons)), None
        request = inspected.request
        with self._router.planned(request.request_id, [provider]):
            try:
                routed = await self._router.generate(request)
            except ProviderUnavailableError as exc:
                return failed(f"provider unavailable: {exc}"), None
        response = routed.response
        await ctx.record_usage(response, LedgerKind.EVALUATION)
        checked = self._guard.inspect_output(response.text)
        if checked.blocked or checked.text is None:
            return (
                failed("output blocked", latency=response.latency_ms, cost=response.cost_usd),
                None,
            )
        try:
            grade = await self._grade(ctx, case, provider, checked.text)
        except JudgeError as exc:
            return (
                failed(f"judge: {exc}", latency=response.latency_ms, cost=response.cost_usd),
                None,
            )
        return (
            CaseResult(
                case_id=case.id,
                provider=provider,
                score=grade.score,
                passed=grade.passed,
                latency_ms=response.latency_ms,
                cost_usd=response.cost_usd,
                output=checked.text[:MAX_STORED_OUTPUT],
                detail=grade.detail[:500],
            ),
            grade.judge_calibrated,
        )

    async def run(self, ctx: EvaluationContext) -> tuple[list[CaseResult], RunSummary]:
        gate = asyncio.Semaphore(self._concurrency)

        async def bounded(case: GoldenCase, provider: str) -> tuple[CaseResult, bool | None]:
            async with gate:
                return await self._case(ctx, case, provider)

        outcomes = await asyncio.gather(
            *(bounded(case, p) for case in ctx.golden.cases for p in ctx.providers)
        )
        results = [r for r, _ in outcomes]
        judged = [c for _, c in outcomes if c is not None]
        judge_calibrated = (all(judged) if judged else False) if ctx.golden.uses_judge else None
        ranking = await self._pairwise(ctx, results) if ctx.pairwise_judges else ()
        summary = RunSummary(
            providers=tuple(self._summarize(p, results) for p in ctx.providers),
            ranking=ranking,
            judge_calibrated=judge_calibrated,
        )
        return results, summary

    @staticmethod
    def _summarize(provider: str, results: Sequence[CaseResult]) -> ProviderSummary:
        mine = [r for r in results if r.provider == provider]
        passes = sum(r.passed for r in mine)
        low, high = wilson_interval(passes, len(mine))
        latencies = [r.latency_ms for r in mine if r.error is None]
        cost = sum(r.cost_usd for r in mine)
        return ProviderSummary(
            provider=provider,
            cases=len(mine),
            errors=sum(r.error is not None for r in mine),
            pass_rate=passes / len(mine) if mine else 0.0,
            pass_rate_ci95=Interval(low=low, high=high),
            mean_score=sum(r.score for r in mine) / len(mine) if mine else 0.0,
            latency_p50_ms=percentile(latencies, 50),
            latency_p95_ms=percentile(latencies, 95),
            total_cost_usd=cost,
            cost_per_pass_usd=cost / passes if passes else None,
        )

    async def _pairwise(
        self, ctx: EvaluationContext, results: Sequence[CaseResult]
    ) -> tuple[RankedProvider, ...]:
        judges = [j for j in ctx.pairwise_judges if j in ctx.allowed_judges]
        outputs: dict[str, dict[str, str]] = {}
        for r in results:
            if r.error is None:
                outputs.setdefault(r.case_id, {})[r.provider] = r.output
        criteria = f"Correctly and completely performs the task: {ctx.golden.description}"
        cases = {c.id: c for c in ctx.golden.cases}
        comparisons: list[bradley_terry.Comparison] = []
        for case_id, by_provider in sorted(outputs.items()):
            for a, b in itertools.combinations(sorted(by_provider), 2):
                try:
                    verdict = await self._judge.compare(
                        criteria=criteria,
                        prompt=cases[case_id].input,
                        output_a=by_provider[a],
                        output_b=by_provider[b],
                        judges=judges,
                        exclude={a, b},
                        classification=ctx.golden.classification,
                    )
                except JudgeError:
                    continue
                for response in verdict.responses:
                    await ctx.record_usage(response, LedgerKind.JUDGE)
                result = {"a": 1.0, "b": 0.0, "tie": 0.5}[verdict.winner]
                comparisons.append(bradley_terry.Comparison(case_id, a, b, result))
        if not comparisons:
            return ()
        ranking = bradley_terry.rank(
            ctx.providers, comparisons, rng=self._rng, bootstrap_rounds=self._bootstrap_rounds
        )
        return tuple(
            RankedProvider(
                provider=p,
                strength=round(ranking.strengths[p], 6),
                strength_ci95=Interval(
                    low=round(ranking.intervals[p][0], 6), high=round(ranking.intervals[p][1], 6)
                ),
                wins=ranking.wins[p],
                comparisons=ranking.comparisons[p],
            )
            for p in ranking.ordered()
        )


def pairwise_judges_for(golden: GoldenSet, fallback: Collection[str]) -> tuple[str, ...]:
    """Judges for pairwise ranking: the set's judge grader's judges, else the given fallback."""
    for case in golden.cases:
        spec = golden.grader_for(case)
        if isinstance(spec, JudgeGrader):
            return spec.judge_providers
    return tuple(fallback)
