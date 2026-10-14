"""agentic-core ModelRouter whose failover order is decided per request.

The router keeps agentic-core's retry policy, circuit breakers, latency
tracking and observers. Only ``plan`` changes: the routing service registers
an ordered provider list for a request id (after compliance filtering and
strategy ordering), and the router tries exactly those providers, in that
order, that the kernel's own eligibility check also accepts.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager

from agentic_core.models.resilience import CircuitBreaker, RetryPolicy
from agentic_core.models.router import ModelRouter, RouterObserver, RoutingStrategy, Sleep
from agentic_core.models.types import ModelProvider, ModelRequest


class PlannedRouter(ModelRouter):
    def __init__(
        self,
        providers: Sequence[ModelProvider],
        *,
        retry: RetryPolicy | None = None,
        breaker_factory: Callable[[], CircuitBreaker] = CircuitBreaker,
        observers: Sequence[RouterObserver] = (),
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        super().__init__(
            providers,
            strategy=RoutingStrategy.PRIORITY,
            retry=retry,
            breaker_factory=breaker_factory,
            observers=observers,
            sleep=sleep,
        )
        self._by_name = {p.profile.name: p for p in providers}
        self._plans: dict[str, tuple[str, ...]] = {}

    @property
    def providers(self) -> tuple[ModelProvider, ...]:
        return tuple(self._by_name.values())

    def provider(self, name: str) -> ModelProvider:
        return self._by_name[name]

    @contextmanager
    def planned(self, request_id: str, order: Sequence[str]) -> Iterator[None]:
        """Pin the failover order for one request for the duration of the block."""
        unknown = [n for n in order if n not in self._by_name]
        if unknown:
            raise ValueError(f"unknown providers in plan: {unknown}")
        self._plans[request_id] = tuple(order)
        try:
            yield
        finally:
            self._plans.pop(request_id, None)

    def plan(self, request: ModelRequest) -> list[ModelProvider]:
        order = self._plans.get(request.request_id)
        if order is None:
            # Unplanned calls are refused: every model call must pass the
            # compliance filter first, so there is no implicit default route.
            return []
        accepted = {p.profile.name for p in super().plan(request)}
        return [self._by_name[name] for name in order if name in accepted]
