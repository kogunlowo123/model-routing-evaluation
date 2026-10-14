# ADR-0002: Compliance constraints are hard filters applied before utility

- Status: Accepted
- Date: 2026-10-14

## Context

Residency and BAA coverage are legal obligations, not preferences. Folding
them into the utility as penalties would let a large enough quality or cost
advantage outweigh a contract. Contract facts also differ per customer and
change over time, so they cannot be constants in code.

## Decision

- `ComplianceFilter` runs before any ordering and removes, in a fixed order,
  providers that are outside the family pool, above their approved
  classification ceiling, not serving the task, without a compliance profile,
  outside the requested residency jurisdictions, without a BAA when one is
  required, or above the request's cost ceiling. The first failing check is
  the recorded exclusion reason and is returned to the caller.
- A BAA is required whenever the caller asks for one, and always for
  RESTRICTED data (`restricted_requires_baa`).
- A provider without a compliance profile is excluded (fail closed).
- Compliance facts come from `configs/compliance.yaml` through `${NAME}`
  references; start-up fails if any is unset. The Helm chart refuses to render
  until every BAA and retention flag is explicitly true or false.
- The same filter applies to shadow providers (with the primary request's
  constraints) and to judges, because judges see the material they grade.
- Compliance posture (BAA, zero retention) also enters the utility as a soft
  preference, only among providers that already passed the filter.

## Consequences

- No utility setting can cause a contract violation;
  `tests/security/test_routing_attacks.py` asserts this for residency and BAA
  constraints.
- A request can be refused (422) when nothing qualifies; callers see the
  reason for every excluded provider.
- Legal and procurement own the facts; engineering owns the mechanism.
