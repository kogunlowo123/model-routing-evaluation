"""MCP tools (official ``mcp`` SDK, streamable HTTP) mounted at /mcp.

Every tool has typed input and output schemas, authorizes the caller's
bearer credential for the matching action exactly like the HTTP API, and
writes an audit event for the call.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from agentic_core.domain.classification import DataClassification
from agentic_core.observability import correlation_id
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field
from starlette.applications import Starlette

from model_routing_evaluation import __version__
from model_routing_evaluation.api.schemas import ProviderList, ProviderView
from model_routing_evaluation.application.commands import ChatMessage, RouteCommand, RouteResult
from model_routing_evaluation.application.reports import FamilyReport
from model_routing_evaluation.domain.models import RoutePlan, RoutingConstraints, Strategy
from model_routing_evaluation.security.identity import Caller

if TYPE_CHECKING:
    from model_routing_evaluation.runtime.container import Container

ContainerGetter = Callable[[], "Container"]


class RouteInput(BaseModel):
    task: str = Field(min_length=1, max_length=64, description="Task family")
    prompt: str = Field(min_length=1, max_length=100_000)
    system: str | None = Field(default=None, max_length=20_000)
    classification: DataClassification = DataClassification.INTERNAL
    residency: list[str] = Field(default_factory=list, max_length=10)
    requires_baa: bool = False
    max_cost_usd: float | None = Field(default=None, gt=0, le=100)
    max_tokens: int = Field(default=1024, ge=1, le=8192)
    strategy: Strategy | None = None
    purpose_of_use: str | None = Field(default=None, min_length=2, max_length=64)

    def command(self) -> RouteCommand:
        messages = [ChatMessage(role="user", content=self.prompt)]
        if self.system:
            messages.insert(0, ChatMessage(role="system", content=self.system))
        return RouteCommand(
            task=self.task,
            messages=tuple(messages),
            classification=self.classification,
            max_tokens=self.max_tokens,
            constraints=RoutingConstraints(
                residency=frozenset(self.residency),
                requires_baa=self.requires_baa,
                max_cost_usd=self.max_cost_usd,
            ),
            strategy=self.strategy,
            purpose_of_use=self.purpose_of_use,
        )


def _authorization(ctx: Context[Any, Any]) -> str | None:
    headers = ctx.headers or {}
    return headers.get("authorization")


async def _audit_tool(c: Container, caller: Caller, tool: str, resource: str) -> None:
    await c.audit.record(
        actor=caller.subject,
        action="mcp.tool_call",
        resource=resource,
        outcome="allowed",
        correlation_id=correlation_id(),
        details={"tool": tool, "delegation_chain": list(caller.delegation_chain)},
    )


def build_mcp(
    get_container: ContainerGetter, *, allowed_hosts: list[str]
) -> tuple[MCPServer[Any], Starlette]:
    """The MCP server and its ASGI app (stateless streamable HTTP, JSON responses)."""
    server: MCPServer[Any] = MCPServer(
        "model-routing-evaluation",
        version=__version__,
        instructions=(
            "Route prompts to the best eligible foundation model under residency, BAA, "
            "classification and cost constraints; inspect routing plans, providers and "
            "per-family evaluation reports."
        ),
    )

    @server.tool(
        name="route_completion",
        description="Answer a prompt with the best eligible model for the task family.",
    )
    async def route_completion(request: RouteInput, ctx: Context[Any, Any]) -> RouteResult:
        c = get_container()
        command = request.command()
        caller = await c.access.agent(
            _authorization(ctx),
            action="model-routing:invoke",
            resource_type="task-family",
            resource_id=command.task,
            classification=command.classification,
            environment=(
                {"purpose_of_use": command.purpose_of_use} if command.purpose_of_use else {}
            ),
            channel="mcp",
        )
        await _audit_tool(c, caller, "route_completion", f"task-family:{command.task}")
        return await c.routing.route(command, caller)

    @server.tool(
        name="plan_route",
        description=(
            "Show eligible providers, exclusion reasons and routing order "
            "without calling a model."
        ),
    )
    async def plan_route(request: RouteInput, ctx: Context[Any, Any]) -> RoutePlan:
        c = get_container()
        command = request.command()
        caller = await c.access.agent(
            _authorization(ctx),
            action="model-routing:plan",
            resource_type="task-family",
            resource_id=command.task,
            classification=command.classification,
            environment=(
                {"purpose_of_use": command.purpose_of_use} if command.purpose_of_use else {}
            ),
            channel="mcp",
        )
        await _audit_tool(c, caller, "plan_route", f"task-family:{command.task}")
        return (await c.routing.plan(command, caller)).plan

    @server.tool(
        name="list_providers",
        description="Configured providers with compliance facts and circuit-breaker state.",
    )
    async def list_providers(ctx: Context[Any, Any]) -> ProviderList:
        c = get_container()
        caller = await c.access.agent(
            _authorization(ctx), action="provider:read", resource_type="provider", channel="mcp"
        )
        await _audit_tool(c, caller, "list_providers", "provider:*")
        router = c.routing.router
        return ProviderList(
            items=[
                ProviderView(
                    name=p.profile.name,
                    model=p.profile.model,
                    tasks=sorted(p.profile.tasks),
                    max_classification=p.profile.max_classification,
                    region=p.profile.region,
                    compliance=c.routing.compliance.profile(p.profile.name),
                    circuit=router.breaker(p.profile.name).state,
                )
                for p in router.providers
            ]
        )

    @server.tool(
        name="family_report",
        description="Traffic, spend, quality and learned routing posteriors for a task family.",
    )
    async def family_report(family: str, ctx: Context[Any, Any], days: int = 7) -> FamilyReport:
        c = get_container()
        caller = await c.access.agent(
            _authorization(ctx),
            action="report:read",
            resource_type="task-family",
            resource_id=family,
            channel="mcp",
        )
        await _audit_tool(c, caller, "family_report", f"task-family:{family}")
        return await c.reports.family(family, days=max(1, min(days, 90)))

    app = server.streamable_http_app(
        streamable_http_path="/",
        json_response=True,
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=bool(allowed_hosts),
            allowed_hosts=allowed_hosts,
            allowed_origins=[],
        ),
    )
    return server, app
