"""Routing, evaluation and governance use cases through the composition root."""

from __future__ import annotations

from typing import Any

import pytest
from agentic_core.domain.classification import DataClassification

from model_routing_evaluation.application.commands import (
    CalibrationCommand,
    FeedbackCommand,
    PromotionCommand,
    PromotionDecision,
    RouteCommand,
    RunCommand,
)
from model_routing_evaluation.domain.errors import (
    ApprovalRequiredError,
    ConflictError,
    ForbiddenError,
    NoEligibleProviderError,
    NotFoundError,
    UnprocessableError,
)
from model_routing_evaluation.domain.evaluation import (
    CalibrationItem,
    PromotionAction,
    PromotionStatus,
    RunStatus,
)
from model_routing_evaluation.domain.models import RoutingConstraints, ShadowOutcome, Strategy
from model_routing_evaluation.runtime.container import Container
from model_routing_evaluation.security.identity import Caller
from tests.conftest import AGENT, FakeModelWorld, triage_inputs

AGENT_CALLER = Caller(subject=AGENT, kind="agent")
APPROVER = Caller(subject="dana", kind="human", roles=frozenset({"model-governance-approver"}))
TRIAGE_SYSTEM = (
    "Classify the customer support ticket into exactly one of these labels:\n"
    "billing, technical, account_access, shipping, cancellation, other.\n"
    "Reply with the label only, in lower case, with no punctuation.\n"
)


def command(text: str = "Explain our refund policy.", **kwargs: Any) -> RouteCommand:
    data: dict[str, Any] = {"task": "general", "messages": [{"role": "user", "content": text}]}
    data.update(kwargs)
    return RouteCommand.model_validate(data)


class TestStrategies:
    async def test_priority_follows_pool_order(self, container: Container) -> None:
        planned = await container.routing.plan(command(strategy="priority"), AGENT_CALLER)
        assert planned.plan.order == ("azure-gpt", "claude", "llama")
        assert all(c.utility is None for c in planned.plan.candidates)

    async def test_cost_orders_by_price_sheet(self, container: Container) -> None:
        planned = await container.routing.plan(command(strategy="cost"), AGENT_CALLER)
        assert planned.plan.order == ("llama", "azure-gpt", "claude")
        costs = [c.estimated_cost_usd for c in planned.plan.candidates]
        assert costs == sorted(costs)

    async def test_long_inputs_use_their_own_context(self, container: Container) -> None:
        planned = await container.routing.plan(command("x " * 5000), AGENT_CALLER)
        assert planned.plan.bucket == "long"

    async def test_unknown_family(self, container: Container) -> None:
        with pytest.raises(UnprocessableError, match="unknown task family"):
            await container.routing.plan(command(task="poetry"), AGENT_CALLER)


class TestComplianceRouting:
    async def test_restricted_requires_purpose_then_routes_to_baa_provider(
        self, container: Container
    ) -> None:
        restricted = command(classification=3)
        with pytest.raises(ForbiddenError, match="governance"):
            await container.routing.route(restricted, AGENT_CALLER)
        result = await container.routing.route(
            command(classification=3, purpose_of_use="treatment"), AGENT_CALLER
        )
        assert result.provider == "azure-gpt"
        reasons = {e.provider: e.reason.value for e in result.excluded}
        assert reasons["claude"] == "classification_ceiling"
        assert reasons["llama"] == "classification_ceiling"

    async def test_residency_and_cost_ceiling(self, container: Container) -> None:
        with pytest.raises(NoEligibleProviderError) as info:
            await container.routing.route(command(constraints={"residency": ["EU"]}), AGENT_CALLER)
        assert {e["reason"] for e in info.value.details["excluded"]} >= {"residency"}
        cheap = await container.routing.route(
            command(constraints={"max_cost_usd": 0.001}, max_tokens=200), AGENT_CALLER
        )
        assert cheap.provider == "llama"

    async def test_governance_can_require_approval(self, container: Container) -> None:
        from agentic_core.policy import PolicyEngine, PolicySet

        container.routing._governance = PolicyEngine(  # type: ignore[attr-defined]
            PolicySet.model_validate(
                {
                    "name": "hold",
                    "version": "1",
                    "rules": [
                        {"id": "hold", "effect": "require_approval", "actions": ["*"]},
                    ],
                }
            )
        )
        with pytest.raises(ApprovalRequiredError):
            await container.routing.route(command(), AGENT_CALLER)


class TestRoutingOutcomes:
    async def test_response_schema_grades_immediately_and_feedback_overrides(
        self, container: Container
    ) -> None:
        schema = {"type": "object", "required": ["label"]}
        result = await container.routing.route(command(response_schema=schema), AGENT_CALLER)
        assert result.schema_valid is False
        record = await container.routing._decisions.get(result.decision_id)  # type: ignore[attr-defined]
        assert record is not None
        assert record.quality == 0.0
        assert record.quality_source == "schema"
        feedback = await container.routing.feedback(
            FeedbackCommand(decision_id=result.decision_id, score=0.8), AGENT_CALLER
        )
        assert feedback.score == 0.8
        with pytest.raises(ConflictError):
            await container.routing.feedback(
                FeedbackCommand(decision_id=result.decision_id, score=0.1), AGENT_CALLER
            )
        with pytest.raises(NotFoundError):
            await container.routing.feedback(
                FeedbackCommand(decision_id="missing", score=1), AGENT_CALLER
            )

    async def test_pii_is_redacted_before_the_model_sees_it(
        self, container: Container, world: FakeModelWorld
    ) -> None:
        result = await container.routing.route(
            command("Contact jane.doe@corp.test about the renewal.", strategy="priority"),
            AGENT_CALLER,
        )
        assert "jane.doe@corp.test" not in result.text
        assert result.redactions >= 1

    async def test_failover_records_failure_for_learning(
        self, container: Container, world: FakeModelWorld
    ) -> None:
        world.down.add("azure-gpt")
        result = await container.routing.route(command(strategy="priority"), AGENT_CALLER)
        assert result.provider == "claude"
        assert result.failed_over
        stats = await container.routing._bandit.load("general")  # type: ignore[attr-defined]
        assert stats[("*", "azure-gpt")].quality_n == 1
        assert stats[("*", "azure-gpt")].quality_sum == 0


class TestShadowEvaluation:
    async def test_shadow_pairwise_comparison(
        self, container: Container, world: FakeModelWorld
    ) -> None:
        family = container.routing.config.families["summarize"]
        shadow = family.shadow.model_copy(update={"rate": 1.0, "providers": ("nova",)})
        container.routing.config.families["summarize"] = family.model_copy(
            update={"shadow": shadow}
        )
        world.accuracy.update({"nova": 1.0, "phi": 0.0, "claude": 0.0, "azure-gpt": 0.0})
        summary_input = (
            "At 09:12 UTC the payments API began returning HTTP 503 for 18 minutes. About 4% of "
            "card authorisations failed and were retried by clients. Cause: a configuration push "
            "lowered the connection-pool limit on the database proxy from 400 to 40. The change "
            "was rolled back at 09:30 and a pre-deploy check for pool limits was added."
        )
        result = await container.routing.route(
            command(summary_input, task="summarize", strategy="priority"), AGENT_CALLER
        )
        await container.routing.drain()
        shadows = await container.routing._shadows.list_since(  # type: ignore[attr-defined]
            "summarize", container.clock.now().replace(year=2000)
        )
        assert len(shadows) == 1
        assert shadows[0].primary_provider == result.provider
        assert shadows[0].shadow_provider == "nova"
        assert shadows[0].outcome is ShadowOutcome.WIN
        assert world.judge_calls  # an independent judge decided

    async def test_shadow_respects_budget_and_compliance(
        self, container: Container, world: FakeModelWorld
    ) -> None:
        family = container.routing.config.families["general"]
        container.routing.config.families["general"] = family.model_copy(
            update={"shadow": family.shadow.model_copy(update={"rate": 1.0})}
        )
        # Gemini is the general shadow and is EU-resident: a US-only request never reaches it.
        await container.routing.route(command(constraints={"residency": ["US"]}), AGENT_CALLER)
        await container.routing.drain()
        assert world.calls["gemini"] == 0
        container.routing._shadow_budget = 0.0  # type: ignore[attr-defined]
        await container.routing.route(command(), AGENT_CALLER)
        await container.routing.drain()
        assert world.calls["gemini"] == 0

    async def test_shadow_with_schema_grading(
        self, container: Container, world: FakeModelWorld
    ) -> None:
        family = container.routing.config.families["general"]
        container.routing.config.families["general"] = family.model_copy(
            update={"shadow": family.shadow.model_copy(update={"rate": 1.0})}
        )
        await container.routing.route(
            command(response_schema={"type": "string"}, strategy="priority"), AGENT_CALLER
        )
        await container.routing.drain()
        assert world.calls["gemini"] == 1


class TestEvaluationService:
    async def test_run_validation(self, container: Container) -> None:
        evaluation = container.evaluation
        with pytest.raises(NotFoundError, match="golden set"):
            await evaluation.start_run(RunCommand(golden_set="nope"), AGENT_CALLER)
        with pytest.raises(UnprocessableError, match="unknown providers"):
            await evaluation.start_run(
                RunCommand(golden_set="support-ticket-triage", providers=("mystery",)),
                AGENT_CALLER,
            )
        with pytest.raises(NoEligibleProviderError):
            # Gemini is approved only up to INTERNAL; invoices are CONFIDENTIAL.
            await evaluation.start_run(
                RunCommand(golden_set="invoice-field-extraction", providers=("gemini",)),
                AGENT_CALLER,
            )
        with pytest.raises(UnprocessableError, match="at least two"):
            await evaluation.start_run(
                RunCommand(golden_set="support-ticket-triage", providers=("phi",), pairwise=True),
                AGENT_CALLER,
            )
        with pytest.raises(UnprocessableError, match="no judge is cleared"):
            await evaluation.start_run(
                RunCommand(
                    golden_set="support-ticket-triage",
                    providers=("phi", "nova"),
                    pairwise=True,
                    pairwise_judges=("gemini-unknown",),
                ),
                AGENT_CALLER,
            )

    async def test_over_budget_run_is_denied(self, container: Container) -> None:
        from model_routing_evaluation.routing import router as router_module

        claude = container.routing.router.provider("claude")
        expensive = claude.profile.model_copy(update={"input_cost_per_mtok": 1_000_000.0})
        claude._profile = expensive  # type: ignore[attr-defined]
        assert router_module  # module imported for clarity of intent
        with pytest.raises(ForbiddenError, match="governance"):
            await container.evaluation.start_run(
                RunCommand(golden_set="incident-summary", providers=("claude",)), AGENT_CALLER
            )

    async def test_extraction_run_and_offline_prior(
        self, container: Container, world: FakeModelWorld
    ) -> None:
        run = await container.evaluation.start_run(
            RunCommand(golden_set="invoice-field-extraction", providers=("azure-gpt", "nova")),
            AGENT_CALLER,
        )
        await container.evaluation.drain()
        finished, results = await container.evaluation.get_run(run.run_id)
        assert finished.status is RunStatus.COMPLETED
        assert finished.summary is not None
        rates = {p.provider: p.pass_rate for p in finished.summary.providers}
        assert rates["azure-gpt"] == 1.0
        assert rates["nova"] < 1.0
        assert len(results) == 20
        stats = await container.routing._bandit.load("extract")  # type: ignore[attr-defined]
        assert stats[("*", "azure-gpt")].quality_n == pytest.approx(10 * 0.25)
        with pytest.raises(NotFoundError):
            await container.evaluation.get_run("missing")

    async def test_failed_run_is_recorded(self, container: Container) -> None:
        async def explode(_: Any) -> Any:
            raise RuntimeError("harness crashed")

        container.evaluation._evaluator.run = explode  # type: ignore[method-assign]
        run = await container.evaluation.start_run(
            RunCommand(golden_set="support-ticket-triage", providers=("phi",)), AGENT_CALLER
        )
        await container.evaluation.drain()
        finished, _ = await container.evaluation.get_run(run.run_id)
        assert finished.status is RunStatus.FAILED
        assert finished.error is not None
        assert "harness crashed" in finished.error

    async def test_calibration_validation(self, container: Container) -> None:
        items = tuple(
            CalibrationItem(
                prompt="p", output="ok" if i % 2 else "WRONG", human_score=1 + 4 * (i % 2)
            )
            for i in range(4)
        )
        rubric = "Faithful and complete summary of the incident."
        with pytest.raises(UnprocessableError, match="unknown judge"):
            await container.evaluation.calibrate(
                CalibrationCommand(judge_provider="mystery", rubric=rubric, items=items),
                AGENT_CALLER,
            )
        with pytest.raises(NoEligibleProviderError):
            await container.evaluation.calibrate(
                CalibrationCommand(
                    judge_provider="claude",
                    rubric=rubric,
                    items=items,
                    classification=DataClassification.RESTRICTED,
                ),
                AGENT_CALLER,
            )
        too_few = await container.evaluation.calibrate(
            CalibrationCommand(judge_provider="claude", rubric=rubric, items=items), AGENT_CALLER
        )
        assert too_few.weighted_kappa == 1.0
        assert too_few.calibrated is False  # fewer than min_calibration_items


class TestPromotions:
    async def completed_run(self, container: Container, providers: tuple[str, ...]) -> str:
        run = await container.evaluation.start_run(
            RunCommand(golden_set="support-ticket-triage", providers=providers), AGENT_CALLER
        )
        await container.evaluation.drain()
        return run.run_id

    async def test_add_validation(self, container: Container, world: FakeModelWorld) -> None:
        evaluation = container.evaluation
        base = {"family": "classify", "justification": "Evidence from the triage golden set."}
        with pytest.raises(UnprocessableError, match="unknown provider"):
            await evaluation.request_promotion(
                PromotionCommand(provider="mystery", **base), AGENT_CALLER
            )
        with pytest.raises(ConflictError, match="already"):
            await evaluation.request_promotion(
                PromotionCommand(provider="phi", **base), AGENT_CALLER
            )
        with pytest.raises(UnprocessableError, match="requires an evaluation run"):
            await evaluation.request_promotion(
                PromotionCommand(provider="llama", **base), AGENT_CALLER
            )
        with pytest.raises(UnprocessableError, match="missing or not completed"):
            await evaluation.request_promotion(
                PromotionCommand(provider="llama", run_id="nope", **base), AGENT_CALLER
            )
        run_id = await self.completed_run(container, ("azure-gpt",))
        with pytest.raises(UnprocessableError, match="did not evaluate"):
            await evaluation.request_promotion(
                PromotionCommand(provider="llama", run_id=run_id, **base), AGENT_CALLER
            )
        with pytest.raises(UnprocessableError, match="evaluated family classify"):
            await evaluation.request_promotion(
                PromotionCommand(
                    family="extract",
                    provider="nova",
                    run_id=run_id,
                    justification="wrong family evidence",
                ),
                AGENT_CALLER,
            )

    async def test_remove_reject_and_two_person_rule(
        self, container: Container, world: FakeModelWorld
    ) -> None:
        evaluation = container.evaluation
        removal = await evaluation.request_promotion(
            PromotionCommand(
                family="classify",
                provider="nova",
                action=PromotionAction.REMOVE,
                justification="Nova misses the accuracy gate in production feedback.",
            ),
            AGENT_CALLER,
        )
        assert removal.status is PromotionStatus.PENDING
        with pytest.raises(ForbiddenError, match="two-person"):
            await evaluation.decide_promotion(
                removal.promotion_id,
                PromotionDecision(approve=True, reason="self approval"),
                Caller(subject=AGENT, kind="human"),
            )
        decided = await evaluation.decide_promotion(
            removal.promotion_id, PromotionDecision(approve=True, reason="agreed"), APPROVER
        )
        assert decided.status is PromotionStatus.APPROVED
        assert "nova" not in await container.routing.pool("classify")
        with pytest.raises(ConflictError, match="already approved"):
            await evaluation.decide_promotion(
                removal.promotion_id, PromotionDecision(approve=False, reason="too late"), APPROVER
            )
        with pytest.raises(NotFoundError):
            await evaluation.decide_promotion(
                "missing", PromotionDecision(approve=True, reason="nothing"), APPROVER
            )
        with pytest.raises(UnprocessableError, match="not in the classify pool"):
            await evaluation.request_promotion(
                PromotionCommand(
                    family="classify",
                    provider="nova",
                    action=PromotionAction.REMOVE,
                    justification="Remove it again from the pool.",
                ),
                AGENT_CALLER,
            )
        for name in ("phi",):
            pending = await evaluation.request_promotion(
                PromotionCommand(
                    family="classify",
                    provider=name,
                    action=PromotionAction.REMOVE,
                    justification="Consolidate the classify pool.",
                ),
                AGENT_CALLER,
            )
            rejected = await evaluation.decide_promotion(
                pending.promotion_id, PromotionDecision(approve=False, reason="keep it"), APPROVER
            )
            assert rejected.status is PromotionStatus.REJECTED
        listed = await evaluation.promotions(family="classify", status=PromotionStatus.REJECTED)
        assert [p.provider for p in listed] == ["phi"]

    async def test_cannot_remove_last_provider(self, container: Container) -> None:
        family = container.routing.config.families["classify"]
        container.routing.config.families["classify"] = family.model_copy(update={"pool": ("phi",)})
        with pytest.raises(UnprocessableError, match="last provider"):
            await container.evaluation.request_promotion(
                PromotionCommand(
                    family="classify",
                    provider="phi",
                    action=PromotionAction.REMOVE,
                    justification="Remove the only provider.",
                ),
                AGENT_CALLER,
            )

    async def test_policy_may_auto_approve(self, container: Container) -> None:
        from agentic_core.policy import PolicyEngine, PolicySet

        container.evaluation._governance = PolicyEngine(  # type: ignore[attr-defined]
            PolicySet.model_validate(
                {
                    "name": "auto",
                    "version": "1",
                    "rules": [{"id": "auto-removal", "effect": "allow", "actions": ["*"]}],
                }
            )
        )
        promotion = await container.evaluation.request_promotion(
            PromotionCommand(
                family="classify",
                provider="nova",
                action=PromotionAction.REMOVE,
                justification="Automatic removal permitted by policy.",
            ),
            AGENT_CALLER,
        )
        assert promotion.status is PromotionStatus.APPROVED
        assert promotion.decided_by == "policy:auto-removal"

    async def test_report_lists_configured_providers_only(self, container: Container) -> None:
        await container.routing.route(command(constraints=RoutingConstraints()), AGENT_CALLER)
        report = await container.reports.family("general", days=7)
        assert {p.provider for p in report.providers} >= {"azure-gpt", "claude", "llama"}
        assert report.total_cost_usd > 0
        assert report.offline_run_id is None
        assert triage_inputs()[0]
        assert Strategy.LEARNED.value == "learned"
