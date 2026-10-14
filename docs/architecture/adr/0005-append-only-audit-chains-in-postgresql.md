# ADR-0005: Per-replica audit chains in an append-only PostgreSQL table

- Status: Accepted
- Date: 2026-10-14

## Context

agentic-core's `AuditLog` serializes appends within a process and links each
event to its predecessor. Several replicas writing one chain would need a
distributed lock. agent-identity-fabric solved this with a StatefulSet and a
volume per replica, which ties pods to disks and complicates autoscaling.

## Decision

- `SqlAuditSink` implements the agentic-core `AuditSink` protocol over an
  `audit_events` table keyed by (`chain_id`, `sequence`).
- Each replica writes its own chain; `chain_id` is the pod name, set as
  `MRE_AUDIT_CHAIN_ID` from the downward API. A replacement pod starts a new
  chain and old chains remain verifiable.
- Database triggers reject UPDATE and DELETE on `audit_events` in PostgreSQL
  and SQLite. Hashes and Ed25519 signatures (key from the secret store) expose
  any change made by someone who bypasses the triggers.
- `GET /v1/audit/verify` verifies every chain for auditors and approvers.
- The service runs as a Deployment with a Horizontal Pod Autoscaler.

## Consequences

- No per-replica volumes; scale-out and node drains are routine.
- Audit durability equals database durability (zone-redundant HA,
  point-in-time restore, geo-redundant backups).
- A database administrator can disable the triggers; signatures still expose
  alteration, and administrative sessions are logged by the database.
