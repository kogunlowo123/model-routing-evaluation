# Runbook: disaster recovery

| Component | Recovery point | Recovery time | Mechanism |
|---|---|---|---|
| PostgreSQL | Point-in-time, seconds | Under 1 hour | Zone-redundant HA (automatic failover); PITR within the backup retention; geo-redundant backups |
| Service pods | Stateless | Minutes | Deployment across zones; redeploy the chart |
| Secrets | Key Vault soft delete and purge protection | Minutes | Restore deleted secrets; External Secrets resync |
| Configuration | Git | Minutes | Chart values and `configs/` from the release tag |

## Zone failure

No action: the database fails over to its standby zone and the remaining
replicas serve traffic. Confirm with `/readyz` and the `MreNotReady` alert.

## Regional failure

1. Restore the database from geo-redundant backup into the paired region:
   `az postgres flexible-server geo-restore --resource-group <rg> --name <new> --source-server <id> --location <paired-region>`.
2. Apply `deployments/terraform/azure` for the paired region (Key Vault,
   identity, Azure OpenAI) and copy the provider and audit secrets.
3. Update the database URL secret, install the chart with region-specific
   values. Check that compliance values (`jurisdiction`, `region`) still state
   the truth for the new endpoints before routing restricted traffic.
4. Repoint DNS or the ingress for `mre.publicUrl`.

## Data loss of learning state

Bandit statistics rebuild from new traffic, shadow results and offline runs;
rerun the golden sets to warm start each family. The ledger and audit chains
are recovered only from backups; treat any gap as an incident.

## Verify after recovery

```bash
curl -sf $MRE/readyz
curl -s $MRE/v1/audit/verify -H "Authorization: Bearer $AUDITOR_TOKEN" | jq '.chains[] | select(.intact == false)'
curl -s "$MRE/v1/ledger?group_by=kind&days=1" -H "Authorization: Bearer $TOKEN" | jq
```
