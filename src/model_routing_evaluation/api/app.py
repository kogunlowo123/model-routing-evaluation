"""FastAPI application factory."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager

from agentic_core.domain.errors import PolicyDeniedError, SecurityViolationError
from agentic_core.observability import bind_correlation_id, get_logger
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from model_routing_evaluation import __version__
from model_routing_evaluation.api.routes import router
from model_routing_evaluation.domain.errors import RoutingError
from model_routing_evaluation.integrations.mcp_server import build_mcp
from model_routing_evaluation.runtime.container import Container

ContainerFactory = Callable[[], Awaitable[Container]]

log = get_logger("model_routing_evaluation.api")


def create_app(factory: ContainerFactory, *, mcp_allowed_hosts: Sequence[str] = ()) -> FastAPI:
    holder: dict[str, Container] = {}

    def current() -> Container:
        return holder["container"]

    mcp_server, mcp_app = build_mcp(current, allowed_hosts=list(mcp_allowed_hosts))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        container = await factory()
        holder["container"] = container
        app.state.container = container
        log.info("startup", version=__version__)
        try:
            async with mcp_server.session_manager.run():
                yield
        finally:
            await container.aclose()

    app = FastAPI(
        title="Model Routing and Evaluation Platform",
        version=__version__,
        description=(
            "Compliance-filtered, learned routing across GPT, Claude, Gemini, Nova, Llama and "
            "Phi; offline and shadow evaluation with calibrated judges; cost ledger; "
            "per-family reports. MCP tools at /mcp, A2A at /a2a."
        ),
        lifespan=lifespan,
    )
    app.include_router(router)
    app.mount("/mcp", mcp_app)

    @app.middleware("http")
    async def correlation_and_metrics(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        incoming = request.headers.get("x-correlation-id")
        cid = incoming if incoming and len(incoming) <= 64 and incoming.isprintable() else None
        started = time.perf_counter()
        with bind_correlation_id(cid) as bound:
            response = await call_next(request)
        response.headers["X-Correlation-ID"] = bound
        response.headers["X-Content-Type-Options"] = "nosniff"
        route = request.scope.get("route")
        path = getattr(
            route, "path", "/mcp" if request.url.path.startswith("/mcp") else "unmatched"
        )
        container: Container | None = getattr(request.app.state, "container", None)
        if container is not None:
            container.metrics.requests.labels(
                request.method, path, str(response.status_code)
            ).observe(time.perf_counter() - started)
        return response

    @app.exception_handler(RoutingError)
    async def routing_error(_: Request, exc: RoutingError) -> JSONResponse:
        headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else None
        content: dict[str, object] = {"error": exc.code, "error_description": exc.message}
        if exc.details:
            content["details"] = exc.details
        return JSONResponse(status_code=exc.status, content=content, headers=headers)

    @app.exception_handler(SecurityViolationError)
    async def security_violation(_: Request, exc: SecurityViolationError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={
                "error": "security_violation",
                "error_description": str(exc),
                "details": {"findings": exc.findings},
            },
        )

    @app.exception_handler(PolicyDeniedError)
    async def policy_denied(_: Request, exc: PolicyDeniedError) -> JSONResponse:
        return JSONResponse(
            status_code=403,
            content={"error": "forbidden", "error_description": str(exc)},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": "invalid_request",
                "error_description": "request failed validation",
                "details": [
                    {"loc": list(e.get("loc", ())), "msg": str(e.get("msg", ""))}
                    for e in exc.errors()
                ],
            },
        )

    return app
