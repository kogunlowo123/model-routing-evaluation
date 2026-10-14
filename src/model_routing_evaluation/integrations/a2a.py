"""A2A protocol surface: Agent Card and JSON-RPC ``message/send``.

Other agents delegate model calls to this service by sending an A2A message
whose text parts are the prompt and whose optional data part carries routing
parameters (task, classification, constraints, purpose_of_use) or selects
the ``family-report`` skill. Authentication is the caller's bearer credential,
authorized exactly like the HTTP API.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Literal

from agentic_core.domain.errors import AgenticCoreError
from agentic_core.domain.identifiers import new_id
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from model_routing_evaluation import __version__
from model_routing_evaluation.application.commands import ChatMessage, RouteCommand
from model_routing_evaluation.application.reports import render_markdown
from model_routing_evaluation.config import Settings
from model_routing_evaluation.domain.errors import (
    ForbiddenError,
    RoutingError,
    UnauthorizedError,
)

if TYPE_CHECKING:
    from model_routing_evaluation.runtime.container import Container

A2A_PROTOCOL_VERSION = "0.3.0"
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
APPLICATION_ERROR = -32000


class TextPart(BaseModel):
    model_config = ConfigDict(extra="allow")

    kind: Literal["text"]
    text: str = Field(max_length=100_000)


class DataPart(BaseModel):
    model_config = ConfigDict(extra="allow")

    kind: Literal["data"]
    data: dict[str, Any]


class A2AMessage(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    kind: Literal["message"] = "message"
    role: Literal["user", "agent"]
    parts: list[TextPart | DataPart] = Field(min_length=1, max_length=50)
    message_id: str = Field(alias="messageId", min_length=1, max_length=128)
    context_id: str | None = Field(default=None, alias="contextId", max_length=128)


class SendParams(BaseModel):
    model_config = ConfigDict(extra="allow")

    message: A2AMessage


def agent_card(settings: Settings) -> dict[str, Any]:
    base = settings.public_url_str
    return {
        "protocolVersion": A2A_PROTOCOL_VERSION,
        "name": "model-routing-evaluation",
        "description": (
            "Routes model calls across GPT, Claude, Gemini, Nova, Llama and Phi under "
            "residency, BAA, classification and cost constraints, learns from outcomes, "
            "and reports quality, latency and spend per task family."
        ),
        "url": f"{base}/a2a",
        "preferredTransport": "JSONRPC",
        "version": __version__,
        "provider": {
            "organization": "kogunlowo123",
            "url": "https://github.com/kogunlowo123/model-routing-evaluation",
        },
        "documentationUrl": "https://github.com/kogunlowo123/model-routing-evaluation",
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "stateTransitionHistory": False,
        },
        "defaultInputModes": ["text/plain", "application/json"],
        "defaultOutputModes": ["text/plain", "application/json"],
        "skills": [
            {
                "id": "route-completion",
                "name": "Route a completion",
                "description": (
                    "Answer the prompt with the best eligible foundation model. Data part "
                    "fields: task, classification (0-3), constraints {residency, "
                    "requires_baa, max_cost_usd}, max_tokens, purpose_of_use."
                ),
                "tags": ["routing", "llm", "compliance"],
                "inputModes": ["text/plain", "application/json"],
                "outputModes": ["text/plain", "application/json"],
            },
            {
                "id": "family-report",
                "name": "Task-family routing report",
                "description": (
                    "Traffic, spend, online/shadow/offline quality and learned routing "
                    'posteriors for a task family. Data part: {"skill": "family-report", '
                    '"family": "<name>", "days": 7}.'
                ),
                "tags": ["evaluation", "reporting", "cost"],
                "inputModes": ["application/json"],
                "outputModes": ["text/markdown", "application/json"],
            },
        ],
        "securitySchemes": {
            "agentCredential": {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "JWT",
                "description": "Agent credential issued by agent-identity-fabric",
            }
        },
        "security": [{"agentCredential": []}],
    }


def _error(request_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def _reply(request_id: Any, message: A2AMessage, parts: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "kind": "message",
            "role": "agent",
            "messageId": new_id(),
            "contextId": message.context_id or new_id(),
            "parts": parts,
        },
    }


def _parse(payload: object) -> tuple[str | int, SendParams] | dict[str, Any]:
    """The request id and params of a ``message/send`` call, or a JSON-RPC error."""
    if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
        return _error(None, INVALID_REQUEST, "Invalid Request")
    request_id = payload.get("id")
    if not isinstance(request_id, str | int) or isinstance(request_id, bool):
        return _error(None, INVALID_REQUEST, "Invalid Request: id must be a string or integer")
    if payload.get("method") != "message/send":
        return _error(request_id, METHOD_NOT_FOUND, "Method not found")
    try:
        return request_id, SendParams.model_validate(payload.get("params"))
    except ValidationError as exc:
        return _error(request_id, INVALID_PARAMS, "Invalid params", exc.errors(include_url=False))


async def handle_jsonrpc(
    c: Container, payload: object, authorization: str | None
) -> dict[str, Any]:
    parsed = _parse(payload)
    if isinstance(parsed, dict):
        return parsed
    request_id, params = parsed
    message = params.message
    data: dict[str, Any] = {}
    for part in message.parts:
        if isinstance(part, DataPart):
            data.update(part.data)
    skill = data.pop("skill", "route-completion")
    handler = SKILLS.get(skill) if isinstance(skill, str) else None
    if handler is None:
        return _error(request_id, INVALID_PARAMS, f"unknown skill {skill!r}")
    try:
        return await handler(c, request_id, message, data, authorization)
    except (UnauthorizedError, ForbiddenError):
        raise  # transport-level refusal (HTTP 401/403), not a JSON-RPC result
    except RoutingError as exc:
        return _error(
            request_id,
            APPLICATION_ERROR,
            exc.message,
            {"error": exc.code, "details": exc.details},
        )
    except AgenticCoreError as exc:
        return _error(request_id, APPLICATION_ERROR, str(exc), {"error": exc.code})


async def _route(
    c: Container,
    request_id: Any,
    message: A2AMessage,
    data: dict[str, Any],
    authorization: str | None,
) -> dict[str, Any]:
    text = "\n\n".join(p.text for p in message.parts if isinstance(p, TextPart)).strip()
    try:
        command = RouteCommand.model_validate(
            {
                "task": data.get("task", "general"),
                "messages": [ChatMessage(role="user", content=text)] if text else [],
                **{
                    k: data[k]
                    for k in (
                        "classification",
                        "constraints",
                        "max_tokens",
                        "purpose_of_use",
                        "strategy",
                    )
                    if k in data
                },
            }
        )
    except ValidationError as exc:
        return _error(request_id, INVALID_PARAMS, "Invalid params", exc.errors(include_url=False))
    caller = await c.access.agent(
        authorization,
        action="model-routing:invoke",
        resource_type="task-family",
        resource_id=command.task,
        classification=command.classification,
        environment={"purpose_of_use": command.purpose_of_use} if command.purpose_of_use else {},
        channel="a2a",
    )
    result = await c.routing.route(command, caller)
    return _reply(
        request_id,
        message,
        [
            {"kind": "text", "text": result.text},
            {
                "kind": "data",
                "data": {
                    "decision_id": result.decision_id,
                    "family": result.family,
                    "provider": result.provider,
                    "model": result.model,
                    "cost_usd": result.cost_usd,
                    "latency_ms": result.latency_ms,
                },
            },
        ],
    )


async def _report(
    c: Container,
    request_id: Any,
    message: A2AMessage,
    data: dict[str, Any],
    authorization: str | None,
) -> dict[str, Any]:
    family = data.get("family")
    days = data.get("days", 7)
    if not isinstance(family, str) or not isinstance(days, int) or not 1 <= days <= 90:
        return _error(request_id, INVALID_PARAMS, "family (string) and days (1-90) are required")
    await c.access.agent(
        authorization,
        action="report:read",
        resource_type="task-family",
        resource_id=family,
        channel="a2a",
    )
    report = await c.reports.family(family, days=days)
    return _reply(
        request_id,
        message,
        [
            {"kind": "text", "text": render_markdown(report)},
            {"kind": "data", "data": report.model_dump(mode="json")},
        ],
    )


Skill = Callable[
    ["Container", Any, A2AMessage, dict[str, Any], str | None], Awaitable[dict[str, Any]]
]
SKILLS: dict[str, Skill] = {"route-completion": _route, "family-report": _report}
