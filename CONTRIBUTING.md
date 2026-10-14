# Contributing

## Setup

```bash
uv sync
make check
```

## Requirements for every change

- `make check` passes: ruff, black, mypy `--strict`, bandit, contract drift,
  tests with at least 90% branch coverage.
- New behaviour has tests. Security controls need adversarial cases in
  `tests/security`; dependency failure handling belongs in `tests/chaos`.
- Contract changes regenerate `schemas/` and `docs/api/openapi.json` with
  `uv run python -m model_routing_evaluation.contracts` and are recorded in
  `CHANGELOG.md`.
- Golden-set changes bump the set's `version`; its digest is recorded with
  every evaluation run and promotion.
- Architecturally significant decisions get an ADR in `docs/architecture/adr/`.
- No credentials, customer data or PHI in code, tests, fixtures or golden
  sets. Use synthetic data only. No model prices or benchmark numbers in code
  or docs: prices are configuration and results come from running the harness.

## Commit messages

Conventional Commits (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`,
`chore:`). Breaking changes use `!` and a `BREAKING CHANGE:` footer.

## Review

Changes to `src/model_routing_evaluation/security`, `policies/`, `configs/`
or `deployments/` need approval from a code owner (see `CODEOWNERS`).
