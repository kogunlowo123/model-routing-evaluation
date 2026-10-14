"""Per task-family reports: traffic, spend, online/shadow/offline quality, learned posteriors."""

from __future__ import annotations

import math
from datetime import datetime, timedelta

from pydantic import BaseModel, ConfigDict

from model_routing_evaluation.application.routing_service import RoutingService
from model_routing_evaluation.domain.evaluation import Interval, PromotionStatus
from model_routing_evaluation.domain.models import LedgerGroup, LedgerTotal, ShadowOutcome
from model_routing_evaluation.domain.ports import (
    BanditStore,
    Clock,
    DecisionRepository,
    EvaluationRepository,
    Ledger,
    PromotionRepository,
    ShadowRepository,
)
from model_routing_evaluation.domain.stats import FAMILY_BUCKET, ArmStats
from model_routing_evaluation.evaluation.statistics import Z95, percentile
from model_routing_evaluation.routing.bandit import ThompsonSampler, utility


class ProviderReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider: str
    in_pool: bool
    requests: int
    traffic_share: float
    cost_usd: float
    latency_p50_ms: float
    latency_p95_ms: float
    feedback_n: int
    feedback_mean: float | None
    posterior_quality: float
    posterior_quality_ci95: Interval
    posterior_latency_ms: float
    posterior_cost_usd: float
    expected_utility: float
    shadow_comparisons: int
    shadow_win_rate: float | None
    offline_pass_rate: float | None
    offline_pass_rate_ci95: Interval | None
    offline_strength: float | None


class FamilyReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    family: str
    description: str
    generated_at: datetime
    window_days: int
    pool: tuple[str, ...]
    pending_promotions: tuple[str, ...]
    offline_run_id: str | None
    offline_golden_set: str | None
    providers: tuple[ProviderReport, ...]
    spend_by_kind: tuple[LedgerTotal, ...]
    total_cost_usd: float


def _beta_interval(stats: ArmStats) -> Interval:
    a = 1.0 + stats.quality_sum
    b = 1.0 + max(stats.quality_n - stats.quality_sum, 0.0)
    mean = a / (a + b)
    sd = math.sqrt(a * b / ((a + b) ** 2 * (a + b + 1)))
    return Interval(low=max(0.0, mean - Z95 * sd), high=min(1.0, mean + Z95 * sd))


class ReportBuilder:
    def __init__(
        self,
        *,
        routing: RoutingService,
        sampler: ThompsonSampler,
        bandit: BanditStore,
        decisions: DecisionRepository,
        ledger: Ledger,
        shadows: ShadowRepository,
        runs: EvaluationRepository,
        promotions: PromotionRepository,
        clock: Clock,
    ) -> None:
        self._routing = routing
        self._sampler = sampler
        self._bandit = bandit
        self._decisions = decisions
        self._ledger = ledger
        self._shadows = shadows
        self._runs = runs
        self._promotions = promotions
        self._clock = clock

    async def family(self, name: str, *, days: int) -> FamilyReport:
        family = self._routing.config.family(name)
        now = self._clock.now()
        since = now - timedelta(days=days)
        pool = await self._routing.pool(name)
        traffic = await self._decisions.traffic(name, since)
        shadows = await self._shadows.list_since(name, since)
        stats = await self._bandit.load(name)
        run = await self._runs.latest_completed(name)
        pending = await self._promotions.list(family=name, status=PromotionStatus.PENDING)
        spend = await self._ledger.totals(
            since=since, until=now + timedelta(seconds=1), group_by=LedgerGroup.KIND, family=name
        )
        offline = {s.provider: s for s in run.summary.providers} if run and run.summary else {}
        strengths = (
            {r.provider: r.strength for r in run.summary.ranking} if run and run.summary else {}
        )

        known = {p.profile.name for p in self._routing.router.providers}
        names = sorted(
            (
                set(pool)
                | {t.provider for t in traffic}
                | {s.shadow_provider for s in shadows}
                | set(offline)
            )
            & known
        )
        total = len(traffic)
        reports = []
        for provider in names:
            mine = [t for t in traffic if t.provider == provider]
            rated = [t.quality for t in mine if t.quality is not None]
            arm = stats.get((FAMILY_BUCKET, provider), ArmStats())
            profile = self._routing.router.provider(provider).profile
            facts = self._routing.compliance.profile(provider)
            point = self._sampler.expected(
                arm,
                latency_target_ms=family.latency_target_ms,
                estimated_cost_usd=profile.estimate_cost(1000, 500),
            )
            compared = [s for s in shadows if s.shadow_provider == provider]
            decided = [s for s in compared if s.outcome is not ShadowOutcome.ERROR]
            wins = sum(
                (
                    1.0
                    if s.outcome is ShadowOutcome.WIN
                    else 0.5 if s.outcome is ShadowOutcome.TIE else 0.0
                )
                for s in decided
            )
            summary = offline.get(provider)
            reports.append(
                ProviderReport(
                    provider=provider,
                    in_pool=provider in pool,
                    requests=len(mine),
                    traffic_share=len(mine) / total if total else 0.0,
                    cost_usd=sum(t.cost_usd for t in mine),
                    latency_p50_ms=percentile([t.latency_ms for t in mine], 50),
                    latency_p95_ms=percentile([t.latency_ms for t in mine], 95),
                    feedback_n=len(rated),
                    feedback_mean=sum(rated) / len(rated) if rated else None,
                    posterior_quality=point.quality,
                    posterior_quality_ci95=_beta_interval(arm),
                    posterior_latency_ms=point.latency_ms,
                    posterior_cost_usd=point.cost_usd,
                    expected_utility=utility(
                        quality=point.quality,
                        latency_ms=point.latency_ms,
                        cost_usd=point.cost_usd,
                        posture=facts.posture_score if facts else 0.0,
                        family=family,
                    ),
                    shadow_comparisons=len(compared),
                    shadow_win_rate=wins / len(decided) if decided else None,
                    offline_pass_rate=summary.pass_rate if summary else None,
                    offline_pass_rate_ci95=summary.pass_rate_ci95 if summary else None,
                    offline_strength=strengths.get(provider),
                )
            )
        return FamilyReport(
            family=name,
            description=family.description,
            generated_at=now,
            window_days=days,
            pool=pool,
            pending_promotions=tuple(p.promotion_id for p in pending),
            offline_run_id=run.run_id if run else None,
            offline_golden_set=run.golden_set if run else None,
            providers=tuple(reports),
            spend_by_kind=tuple(spend),
            total_cost_usd=sum(s.cost_usd for s in spend),
        )


def _fmt(value: float | None, pattern: str = "{:.3f}") -> str:
    return "n/a" if value is None else pattern.format(value)


def render_markdown(report: FamilyReport) -> str:
    lines = [
        f"# Routing report: {report.family}",
        "",
        f"{report.description}",
        "",
        f"- Generated: {report.generated_at.isoformat()}",
        f"- Window: last {report.window_days} days",
        f"- Routing pool: {', '.join(report.pool)}",
        f"- Pending promotions: {len(report.pending_promotions)}",
        f"- Offline evidence: {report.offline_golden_set or 'none'} "
        f"(run {report.offline_run_id or 'n/a'})",
        f"- Total spend: {report.total_cost_usd:.4f} USD",
        "",
        "| Provider | Pool | Requests | Share | Cost USD | p50 ms | p95 ms | Feedback | "
        "Posterior quality | Utility | Shadow win | Offline pass | BT strength |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for p in report.providers:
        lines.append(
            f"| {p.provider} | {'yes' if p.in_pool else 'no'} | {p.requests} | "
            f"{p.traffic_share:.1%} | {p.cost_usd:.4f} | {p.latency_p50_ms:.0f} | "
            f"{p.latency_p95_ms:.0f} | {_fmt(p.feedback_mean)} (n={p.feedback_n}) | "
            f"{p.posterior_quality:.3f} [{p.posterior_quality_ci95.low:.3f}, "
            f"{p.posterior_quality_ci95.high:.3f}] | {p.expected_utility:.3f} | "
            f"{_fmt(p.shadow_win_rate)} (n={p.shadow_comparisons}) | "
            f"{_fmt(p.offline_pass_rate)} | {_fmt(p.offline_strength)} |"
        )
    lines += [
        "",
        "| Spend kind | Entries | Input tokens | Output tokens | Cost USD |",
        "|---|---|---|---|---|",
    ]
    lines += [
        f"| {s.key} | {s.entries} | {s.input_tokens} | {s.output_tokens} | {s.cost_usd:.4f} |"
        for s in report.spend_by_kind
    ]
    return "\n".join(lines) + "\n"
