"""Service metrics exposed on /metrics in Prometheus text format (own registry)."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram


class RoutingMetrics:
    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or CollectorRegistry()
        self.requests = Histogram(
            "mre_http_request_duration_seconds",
            "HTTP request latency",
            ["method", "route", "status"],
            registry=self.registry,
            buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
        )
        self.routed = Counter(
            "mre_route_decisions_total",
            "Requests routed, by family, provider and strategy",
            ["family", "provider", "strategy"],
            registry=self.registry,
        )
        self.route_failures = Counter(
            "mre_route_failures_total",
            "Requests that could not be served, by reason",
            ["family", "reason"],
            registry=self.registry,
        )
        self.exclusions = Counter(
            "mre_compliance_exclusions_total",
            "Providers removed by hard filters, by reason",
            ["family", "reason"],
            registry=self.registry,
        )
        self.model_latency = Histogram(
            "mre_model_latency_seconds",
            "Provider latency of successful model calls",
            ["provider", "kind"],
            registry=self.registry,
            buckets=(0.1, 0.25, 0.5, 1, 2, 4, 8, 16, 32, 64),
        )
        self.cost = Counter(
            "mre_model_cost_usd_total",
            "Estimated model spend from configured prices",
            ["family", "provider", "kind"],
            registry=self.registry,
        )
        self.tokens = Counter(
            "mre_model_tokens_total",
            "Tokens consumed",
            ["provider", "direction"],
            registry=self.registry,
        )
        self.guard_blocks = Counter(
            "mre_guard_blocks_total",
            "Requests or outputs blocked by content controls",
            ["stage"],
            registry=self.registry,
        )
        self.feedback = Counter(
            "mre_feedback_total",
            "Quality feedback recorded",
            ["family", "source"],
            registry=self.registry,
        )
        self.shadow = Counter(
            "mre_shadow_comparisons_total",
            "Shadow comparisons by outcome",
            ["family", "outcome"],
            registry=self.registry,
        )
        self.evaluations = Counter(
            "mre_evaluation_runs_total",
            "Offline evaluation runs by final status",
            ["status"],
            registry=self.registry,
        )
        self.authz = Counter(
            "mre_authorization_decisions_total",
            "Access decisions on inbound calls",
            ["source", "decision"],
            registry=self.registry,
        )
        self.promotions = Counter(
            "mre_promotions_total",
            "Routing-pool promotion requests and decisions",
            ["status"],
            registry=self.registry,
        )
        self.shadow_in_flight = Gauge(
            "mre_shadow_in_flight",
            "Shadow comparisons currently running",
            registry=self.registry,
        )
