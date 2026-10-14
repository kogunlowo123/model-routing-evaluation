# ADR-0003: Extend agentic-core ModelRouter with a per-request planned order

- Status: Accepted
- Date: 2026-10-14

## Context

agentic-core already provides vendor adapters, retries with jitter, circuit
breakers, latency tracking and telemetry observers in `ModelRouter`. Its
ordering strategies (priority, cost, latency) are fixed per router instance,
while this service decides a different order for every request after
compliance filtering and sampling.

## Decision

- `PlannedRouter` subclasses `ModelRouter` and overrides only `plan()`.
- The routing service registers an ordered provider list for a request id
  with `router.planned(request_id, order)`; `plan()` returns, in that order,
  the providers that the kernel's own eligibility check (classification,
  task) also accepts.
- A request without a registered plan gets an empty plan, so the kernel
  raises `ProviderUnavailableError`. There is no implicit default route that
  could bypass the compliance filter.
- Offline evaluation plans exactly one provider per call (no failover), so a
  case result always belongs to the provider being measured. Judges are
  planned from the judge list minus the providers being judged.

## Consequences

- Retry, breaker, telemetry and adapter behaviour stay identical to every
  other service built on the kernel; kernel fixes apply here unchanged.
- Circuit breaker state is shared by all requests within a replica.
- The subclass relies on `plan()` being the kernel's only ordering hook;
  agentic-core is pinned to v0.1.0 and the behaviour is covered by tests.
