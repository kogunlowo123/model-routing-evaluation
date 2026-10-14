"""Process entry point: ``python -m model_routing_evaluation``."""

from __future__ import annotations

import os

import uvicorn
from agentic_core.observability import configure_logging, configure_telemetry
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from model_routing_evaluation import __version__
from model_routing_evaluation.api.app import create_app
from model_routing_evaluation.config import Settings
from model_routing_evaluation.runtime.container import build_container


def main() -> None:
    settings = Settings()  # populated from MRE_* environment variables
    configure_logging(
        service="model-routing-evaluation",
        environment=settings.environment,
        level=settings.log_level,
    )
    exporter = None
    if settings.otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (  # noqa: PLC0415
            OTLPSpanExporter,
        )

        exporter = OTLPSpanExporter(endpoint=settings.otlp_endpoint)
    configure_telemetry(
        service_name="model-routing-evaluation",
        service_version=__version__,
        environment=settings.environment,
        span_exporter=exporter,
    )
    app = create_app(
        lambda: build_container(settings), mcp_allowed_hosts=settings.mcp_allowed_hosts
    )
    FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz,readyz,metrics")
    uvicorn.run(
        app,
        host=os.environ.get("MRE_HOST", "127.0.0.1"),
        port=int(os.environ.get("MRE_PORT", "8080")),
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("MRE_FORWARDED_ALLOW_IPS", "127.0.0.1"),
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":
    main()
