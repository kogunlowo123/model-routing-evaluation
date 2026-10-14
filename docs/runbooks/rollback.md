# Runbook: rollback

The schema is additive within a minor version (`schema_version` table), so an
application rollback within 0.x.y needs no database change.

```bash
helm -n model-routing history routing
helm -n model-routing rollback routing <revision> --wait
kubectl -n model-routing rollout status deployment/routing-model-routing-evaluation
curl -sf "$MRE/readyz"
```

## Configuration-only regressions

- Routing weights, targets, pools or shadow settings: revert the commit that
  changed `configs/routing.yaml`, release, and confirm with
  `GET /v1/reports/<family>`.
- Governance policy: revert `policies/model-governance.yaml` and confirm the
  `policy_digest` returned in route responses matches the reverted file.
- A wrong promotion: request the opposite action (`remove` or `add`) and have
  it approved; promotions are never deleted.

## Learning state

Bandit statistics are additive and cannot be partially rolled back. If a
defect wrote wrong statistics for a family, restore the `bandit_stats` rows
for that family from a point-in-time copy of the database, or delete them so
the family relearns from priors and offline evidence (run the golden sets
again to warm start).

Every release keeps its image tag and chart version in GHCR, and CI renders
the chart and starts the container from a clean state on every change.
