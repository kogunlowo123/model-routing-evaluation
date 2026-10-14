# ADR-0006: Data minimisation for routed traffic

- Status: Accepted
- Date: 2026-10-14

## Context

Routed prompts and answers can contain personal data, PHI or trade secrets.
Reports, learning and audit need to know what happened, not what was said.
Evaluation is different: golden sets are reviewed, approved data, and
reviewers need to see failing outputs.

## Decision

- Routing decisions store family, provider, model, candidates, exclusions,
  attempts, token counts, cost, latency, quality and SHA-256 digests of the
  sanitized input and the returned output. Prompt and answer text is never
  persisted or logged.
- PII is redacted from inputs before any vendor sees them and from outputs
  before the caller sees them (agentic-core `ContentGuard`).
- Shadow comparisons keep the primary answer in memory only for the duration
  of the comparison and store the outcome.
- Evaluation results store golden-set outputs truncated to 8,000 characters,
  under the golden set's classification.
- Judge calibration stores metrics and the isotonic map, not the items.

## Consequences

- A caller can prove which answer was served by recomputing the digest; the
  platform cannot reproduce content it never kept.
- Investigating production quality relies on shadow and offline evaluation,
  and on callers keeping examples in their own systems.
