"""HTTP routes.

Agent endpoints take the agent's credential as a bearer token and are
authorized per action (identity fabric or local access policy) before the
governance policy and compliance filters run. Promotion decisions and audit
verification are human operations authenticated against the enterprise IdP.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Any, Literal

from agentic_core.audit import verify_chain
from agentic_core.domain.errors import AuditIntegrityError
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text

from model_routing_evaluation.api.schemas import (
    AuditVerification,
    ChainVerification,
    EvaluationRunView,
    GoldenSetList,
    GoldenSetView,
    LedgerView,
    ProviderList,
    ProviderView,
)
from model_routing_evaluation.application.commands import (
    CalibrationCommand,
    FeedbackCommand,
    FeedbackResult,
    PromotionCommand,
    PromotionDecision,
    RouteCommand,
    RouteResult,
    RunCommand,
)
from model_routing_evaluation.application.reports import FamilyReport, render_markdown
from model_routing_evaluation.domain.evaluation import (
    CalibrationResult,
    EvaluationRun,
    Promotion,
    PromotionStatus,
)
from model_routing_evaluation.domain.models import LedgerGroup, RoutePlan
from model_routing_evaluation.infrastructure.database import SqlAuditSink, chain_ids
from model_routing_evaluation.integrations.a2a import agent_card, handle_jsonrpc
from model_routing_evaluation.runtime.container import Container

router = APIRouter()


def container(request: Request) -> Container:
    value: Container = request.app.state.container
    return value


Deps = Annotated[Container, Depends(container)]
Authorization = Annotated[str | None, Header()]


def _routing_environment(cmd: RouteCommand) -> dict[str, Any]:
    return {"purpose_of_use": cmd.purpose_of_use} if cmd.purpose_of_use else {}


# ------------------------------------------------------------------ routing


@router.post("/v1/route", tags=["routing"])
async def route(body: RouteCommand, c: Deps, authorization: Authorization = None) -> RouteResult:
    """Route a completion to the best eligible foundation model and return its answer."""
    caller = await c.access.agent(
        authorization,
        action="model-routing:invoke",
        resource_type="task-family",
        resource_id=body.task,
        classification=body.classification,
        environment=_routing_environment(body),
    )
    return await c.routing.route(body, caller)


@router.post("/v1/route/plan", tags=["routing"])
async def plan(body: RouteCommand, c: Deps, authorization: Authorization = None) -> RoutePlan:
    """Dry run: which providers are eligible, why others are excluded, and the order."""
    caller = await c.access.agent(
        authorization,
        action="model-routing:plan",
        resource_type="task-family",
        resource_id=body.task,
        classification=body.classification,
        environment=_routing_environment(body),
    )
    return (await c.routing.plan(body, caller)).plan


@router.post("/v1/feedback", tags=["routing"])
async def feedback(
    body: FeedbackCommand, c: Deps, authorization: Authorization = None
) -> FeedbackResult:
    caller = await c.access.agent(
        authorization,
        action="model-routing:feedback",
        resource_type="decision",
        resource_id=body.decision_id,
    )
    return await c.routing.feedback(body, caller)


@router.get("/v1/providers", tags=["routing"])
async def providers(c: Deps, authorization: Authorization = None) -> ProviderList:
    await c.access.agent(authorization, action="provider:read", resource_type="provider")
    router_ = c.routing.router
    return ProviderList(
        items=[
            ProviderView(
                name=p.profile.name,
                model=p.profile.model,
                tasks=sorted(p.profile.tasks),
                max_classification=p.profile.max_classification,
                region=p.profile.region,
                compliance=c.routing.compliance.profile(p.profile.name),
                circuit=router_.breaker(p.profile.name).state,
            )
            for p in router_.providers
        ]
    )


# --------------------------------------------------------------- evaluation


@router.get("/v1/golden-sets", tags=["evaluation"])
async def golden_sets(c: Deps, authorization: Authorization = None) -> GoldenSetList:
    await c.access.agent(authorization, action="evaluation:read", resource_type="golden-set")
    return GoldenSetList(
        items=[
            GoldenSetView(
                name=g.name,
                version=g.version,
                task=g.task,
                description=g.description,
                classification=g.classification,
                cases=len(g.cases),
                grader=g.grader.kind,
                uses_judge=g.uses_judge,
                digest=g.digest,
            )
            for g in sorted(c.evaluation.golden_sets.values(), key=lambda g: g.name)
        ]
    )


@router.post("/v1/evaluations", status_code=202, tags=["evaluation"])
async def start_evaluation(
    body: RunCommand, c: Deps, authorization: Authorization = None
) -> EvaluationRun:
    """Start an offline run of a golden set; poll GET /v1/evaluations/{run_id}."""
    caller = await c.access.agent(
        authorization,
        action="evaluation:run",
        resource_type="golden-set",
        resource_id=body.golden_set,
    )
    return await c.evaluation.start_run(body, caller)


@router.get("/v1/evaluations/{run_id}", tags=["evaluation"])
async def get_evaluation(
    run_id: str, c: Deps, authorization: Authorization = None
) -> EvaluationRunView:
    await c.access.agent(
        authorization, action="evaluation:read", resource_type="evaluation", resource_id=run_id
    )
    run, results = await c.evaluation.get_run(run_id)
    return EvaluationRunView(run=run, results=list(results))


@router.post("/v1/judges/calibrations", status_code=201, tags=["evaluation"])
async def calibrate_judge(
    body: CalibrationCommand, c: Deps, authorization: Authorization = None
) -> CalibrationResult:
    """Measure a judge against human 1-5 scores for a rubric (weighted kappa, isotonic map)."""
    caller = await c.access.agent(
        authorization,
        action="evaluation:calibrate",
        resource_type="judge",
        resource_id=body.judge_provider,
        classification=body.classification,
    )
    return await c.evaluation.calibrate(body, caller)


# ------------------------------------------------------- reports and ledger


@router.get("/v1/reports/{family}", tags=["reports"], response_model=FamilyReport)
async def family_report(
    family: str,
    c: Deps,
    authorization: Authorization = None,
    days: Annotated[int, Query(ge=1, le=90)] = 7,
    format: Annotated[Literal["json", "markdown"], Query()] = "json",  # noqa: A002
) -> Any:
    await c.access.agent(
        authorization, action="report:read", resource_type="task-family", resource_id=family
    )
    report = await c.reports.family(family, days=days)
    if format == "markdown":
        return PlainTextResponse(render_markdown(report), media_type="text/markdown")
    return report


@router.get("/v1/ledger", tags=["reports"])
async def ledger(
    c: Deps,
    authorization: Authorization = None,
    days: Annotated[int, Query(ge=1, le=366)] = 30,
    group_by: LedgerGroup = LedgerGroup.PROVIDER,
    family: Annotated[str | None, Query(max_length=64)] = None,
) -> LedgerView:
    await c.access.agent(authorization, action="ledger:read", resource_type="ledger")
    until = c.clock.now() + timedelta(seconds=1)
    since = until - timedelta(days=days)
    totals = await c.ledger.totals(since=since, until=until, group_by=group_by, family=family)
    return LedgerView(
        since=since,
        until=until,
        group_by=group_by.value,
        family=family,
        totals=list(totals),
        total_cost_usd=sum(t.cost_usd for t in totals),
    )


# --------------------------------------------------------------- governance


@router.post("/v1/promotions", status_code=201, tags=["governance"])
async def request_promotion(
    body: PromotionCommand, c: Deps, authorization: Authorization = None
) -> Promotion:
    """Ask to add or remove a provider in a family's routing pool; needs human approval."""
    caller = await c.access.agent(
        authorization,
        action="promotion:request",
        resource_type="task-family",
        resource_id=body.family,
    )
    return await c.evaluation.request_promotion(body, caller)


@router.get("/v1/promotions", tags=["governance"])
async def list_promotions(
    c: Deps,
    authorization: Authorization = None,
    family: Annotated[str | None, Query(max_length=64)] = None,
    status: PromotionStatus | None = None,
) -> list[Promotion]:
    await c.access.agent(authorization, action="report:read", resource_type="promotion")
    return list(await c.evaluation.promotions(family=family, status=status))


@router.post("/v1/promotions/{promotion_id}/decision", tags=["governance"])
async def decide_promotion(
    promotion_id: str, body: PromotionDecision, c: Deps, authorization: Authorization = None
) -> Promotion:
    """Human approver decision (enterprise IdP role); the requester cannot approve."""
    approver = await c.access.human(authorization, roles=[c.settings.approver_role])
    return await c.evaluation.decide_promotion(promotion_id, body, approver)


@router.get("/v1/audit/verify", tags=["governance"])
async def verify_audit(c: Deps, authorization: Authorization = None) -> AuditVerification:
    """Verify hash links and signatures of every replica's audit chain."""
    await c.access.human(authorization, roles=[c.settings.auditor_role, c.settings.approver_role])
    key = Ed25519PublicKey.from_public_bytes(c.audit_public_key)
    chains = []
    for chain_id in await chain_ids(c.engine):
        events = [e async for e in SqlAuditSink(c.engine, chain_id).read()]
        try:
            count = verify_chain(events, {c.audit_key_id: key})
            chains.append(ChainVerification(chain_id=chain_id, events=count, intact=True))
        except AuditIntegrityError as exc:
            chains.append(
                ChainVerification(
                    chain_id=chain_id,
                    events=len(events),
                    intact=False,
                    error=f"{exc} at {exc.sequence}",
                )
            )
    return AuditVerification(key_id=c.audit_key_id, chains=chains)


# ---------------------------------------------------------------------- A2A


@router.get("/.well-known/agent-card.json", tags=["a2a"])
async def a2a_card(c: Deps) -> dict[str, Any]:
    return agent_card(c.settings)


@router.post("/a2a", tags=["a2a"])
async def a2a(request: Request, c: Deps, authorization: Authorization = None) -> dict[str, Any]:
    """A2A JSON-RPC 2.0 endpoint (``message/send``)."""
    try:
        payload = await request.json()
    except ValueError:
        payload = None
    return await handle_jsonrpc(c, payload, authorization)


# --------------------------------------------------------------- operations


@router.get("/healthz", tags=["operations"], include_in_schema=False)
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz", tags=["operations"], include_in_schema=False)
async def readyz(c: Deps, response: Response) -> dict[str, str]:
    try:
        async with c.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        response.status_code = 503
        return {"status": "database unavailable"}
    return {"status": "ready"}


@router.get("/metrics", tags=["operations"], include_in_schema=False)
async def metrics(c: Deps) -> Response:
    return Response(generate_latest(c.metrics.registry), media_type=CONTENT_TYPE_LATEST)
