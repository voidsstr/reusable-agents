# Decommissioned services — where the data went

Anything retired to reduce Azure spend is recorded here, with the exact
location of its data and enough detail to stand it back up. Nothing below was
deleted without a backup first.

Storage account: **`nscagentstorage`**, container **`agents`**
(`AZURE_STORAGE_CONNECTION_STRING` in `~/.reusable-agents/secrets.env`).

---

## 2026-09-09 — Azure cost reduction

Context: August 2026 spend was $920.23 against a $200/month target.

### Container Apps DELETED

| App | Image (still in ACR) | Had a database? | Custom domain |
|---|---|---|---|
| `affiliateflow` | `nscappsacr.azurecr.io/affiliateflow:1.0.37-20260326-155834-81398d5a` | yes — `affiliateflow` | none |
| `application-research` | `nscappsacr.azurecr.io/application-research:v13-multi-source` | no | none |

Both were already `minReplicas=0` with **zero live replicas**, so the direct
compute saving is close to zero — they were retired to reduce surface area and
clutter, not because they were burning money. Say so plainly rather than
claiming a saving that is not there.

The full `az containerapp show` JSON for each is archived at:

```
agents/decommissioned/2026-09-09/containerapp-affiliateflow.json
agents/decommissioned/2026-09-09/containerapp-application-research.json
```

To recreate one: `az containerapp create -g nsc-apps -n <name> --environment
nsc-apps-env --image <image above>` then reapply ingress/env/scale from the
archived JSON. The container images were **not** deleted from `nscappsacr`.

### Databases — KEPT, NOT DROPPED

`affiliateflow`, `hearthnote` and `dealradar` all still exist on the shared
server **`nscappsdb`** (resource group `nsc-apps`). They were deliberately left
in place: the server is already on the cheapest practical tier
(`Standard_B2ms` / Burstable, downgraded from `Standard_D2ds_v5` /
GeneralPurpose on 2026-09-09), and three databases totalling ~30 MB on an
already-provisioned 32 GB volume cost effectively nothing. Dropping them would
have saved $0 and lost the data.

A point-in-time export was taken anyway, before anything was touched:

```
agents/decommissioned/2026-09-09/hearthnote.json.gz      (69 tables,  27 rows)
agents/decommissioned/2026-09-09/affiliateflow.json.gz   (18 tables,   4 rows)
```

Format: gzipped JSON, `{database, exported_at, tables:{<table>:{columns,
row_count, rows}}}`. Both schemas are essentially **empty** — 69 tables holding
27 rows between them — so there was very little to lose. Written with psycopg2
because this host has no `pg_dump`/`psql`.

Read one back with:

```python
import gzip, json
d = json.load(gzip.open('hearthnote.json.gz', 'rt'))
d['tables']['<table>']['rows']
```

### NOT decommissioned — `hearthnote`

`hearthnote` was on the retire list but **is still running, deliberately**. It
serves the custom domain **`contactfollowup.com`** ("ContactFollowUp — the CRM
built for healthcare practices"), which returns HTTP 200, and public DNS points
at the Container Apps environment IP `51.8.15.167`. Deleting the app would have
taken that site down.

It costs almost nothing as it stands (`minReplicas=0`, zero live replicas, cold
starts on request), so there is no financial reason to remove it. Retire it only
once `contactfollowup.com` is intentionally being shut down or moved — and
repoint DNS first. Its config is archived alongside the others.
