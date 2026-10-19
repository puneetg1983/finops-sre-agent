---
name: finops-rightsizing-advisor
description: Recommend rightsizing and idle-resource cleanup for Azure, read-only. Combines Azure Advisor cost recommendations with live utilization from Azure Monitor and inventory state from Resource Graph, validates each candidate against real CPU utilization and real cost before surfacing it, then ranks recommendations by estimated monthly savings using the bundled rightsize.py with the runtime's sandbox Python tool. Use for "what can we downsize or turn off", periodic savings reviews, and adding a cost-efficiency lens to capacity questions.
---

## When to use this skill

Use it when the user wants to **find underutilized or idle Azure resources and get concrete
resize / deallocate / delete recommendations** — an on-demand "what can we rightsize or shut down?",
a weekly savings review, or a cost-efficiency pass during a capacity discussion. This skill
**validates Advisor's suggestions against real utilization and real cost** before surfacing them and
ranks by dollar savings. It recommends only; it never changes anything (all steps are read-only, and
a human approves any action).

For cost *spikes* and their root cause, use `finops-cost-anomaly-detection` instead.

## Required access

- **Reader** on the subscription/resource groups for Advisor, Resource Graph, and Azure Monitor
  metrics. **Cost Management Reader** to attach per-resource monthly cost (without it, savings are
  still estimated from Advisor where available but resources with no Advisor number show unknown cost).
- Read-only `az` (`RunAzCliReadCommands`) and sandbox Python: use `ExecutePythonCode` when
  available, otherwise `RunInTerminal` with `python3`. No POST/write APIs are used.

## Scope

VM / disk / storage-tier / App Service plan / node-level rightsizing works today, plus **Azure
Container Apps idle detection**: unused Container Apps environments (empty, or every app received
zero traffic), always-on container apps (`minReplicas>=1`) that keep billing with no requests, and
**warm dynamic session pools** (`readySessionInstances>=1`) with no session traffic — a class that is
invisible to `az resource list` yet often tops the bill. A **cost-led coverage sweep** additionally
flags any high-spend resource whose type has no idle rule as `review`, so nothing expensive is
silently dropped. **Pod- and namespace-level AKS rightsizing needs a Log Analytics Reader grant +
`api.loganalytics.io` scope on the agent identity** (Container Insights KQL is otherwise blocked) —
out of scope here until that grant lands.

## Procedure

### Step 0 — Resolve the managed boundary

First load `finops-managed-scope` and follow its `scope.py` procedure to dynamically GET and validate
the current agent `managedResources`. Do this before every FinOps request; do not reuse a prior list.
Use the validated managed scopes, expanded to their descendant subscriptions/resource groups/resources,
as the default and only scheduled boundary. Scheduled runs are fail-closed with no override. For an
interactive resource named outside that boundary, disclose the mismatch and wait for explicit
confirmation in a subsequent user turn before any broader Azure query. Broad RBAC never expands scope.

For each effective scope, run Advisor and Resource Graph with explicit subscription selection, and
retain only returned resource ids inside the expanded boundary. A full-subscription effective scope
is queried once. For an RG-only effective scope, use both the subscription id and exact resource-group
constraint shown below, then apply the normalized ARM-id client-side scope filter as defense in depth.
De-duplicate resources when effective scopes overlap. Pull metrics only for those retained resources.
Pull UsageDetails independently for every effective
scope where the API supports it, paginate each scope independently, de-duplicate overlapping line
items, and filter resource ids against the boundary as defense in depth. Report included scopes,
excluded rows/resources, unattributed cost rows, and any scope/API coverage gaps.

### Step 1 — Pull Azure Advisor cost recommendations

```bash
az advisor recommendation list \
  --subscription <EFFECTIVE_SUBSCRIPTION_ID> \
  --category Cost \
  -o json
```

For an RG-only managed scope, constrain the same command to that exact resource group:

```bash
az advisor recommendation list \
  --subscription <EFFECTIVE_SUBSCRIPTION_ID> \
  --resource-group <EFFECTIVE_RESOURCE_GROUP> \
  --category Cost \
  -o json
```

Run once per unique effective subscription, or once per unique effective RG when the subscription is
not fully managed. After either command, retain only recommendations whose normalized
`resourceMetadata.resourceId`/`impactedValue` is inside the corresponding effective scope.

Flatten each recommendation to the shape `rightsize.py` expects:
`{resourceId, problem, recommendation, targetSku, savingsUsd}` — `resourceId` from
`resourceMetadata.resourceId` (or the `impactedValue`), `problem`/`recommendation` from
`shortDescription`, and `savingsUsd` from `extendedProperties.savingsAmount`/`annualSavingsAmount`
(convert an annual figure to monthly by dividing by 12) when present.

### Step 2 — Pull inventory (Resource Graph) for idle patterns Advisor misses

```bash
az graph query \
  --subscriptions <EFFECTIVE_SUBSCRIPTION_ID> \
  -q "Resources | where type in~ ('microsoft.compute/virtualmachines','microsoft.compute/disks','microsoft.web/serverfarms','microsoft.app/managedenvironments','microsoft.app/containerapps','microsoft.app/sessionpools') | project id, type, sku=tostring(sku.name), properties, tags" \
  --first 1000 \
  -o json
```

For an RG-only managed scope, add the exact case-insensitive Kusto predicate before `project`:
`| where resourceGroup =~ '<EFFECTIVE_RESOURCE_GROUP>'`, while retaining
`--subscriptions <EFFECTIVE_SUBSCRIPTION_ID>`. After retrieval, also keep only ids whose normalized
ARM prefix is exactly
`/subscriptions/<effective_subscription_id>/resourcegroups/<effective_resource_group>/`.

On large subscriptions Resource Graph caps at 1000 rows per page — **paginate with `--skip-token`**
(from the response) until it's empty so you don't miss resources. Every page request must retain the
same `--subscriptions` value and, for RG-only scope, the same `resourceGroup =~` predicate and
client-side ARM-prefix filter.

Flatten each row to `{resourceId, type, sku, powerState, diskState, numberOfSites, environmentId, minReplicas, readySessionInstances, tags}`:

- **VM** `powerState` from `properties.extended.instanceView.powerState.code` (e.g.
  `PowerState/stopped` vs `PowerState/deallocated`). A **Stopped (not Deallocated)** VM still bills
  for compute.
- **Disk** `diskState` from `properties.diskState` (`Unattached` disks are pure waste).
- **App Service plan** `numberOfSites` from `properties.numberOfSites` (0 = empty plan).
- **Container Apps environment** (`microsoft.app/managedenvironments`) — no extra field; the ranker
  counts child apps from the inventory to spot **empty environments**.
- **Container App** (`microsoft.app/containerapps`) `environmentId` from
  `properties.environmentId` (fall back to `properties.managedEnvironmentId`) and `minReplicas` from
  `properties.template.scale.minReplicas`. An app with `minReplicas>=1` is **always-on** (bills even
  with zero traffic).
- **Dynamic session pool** (`microsoft.app/sessionpools`) `readySessionInstances` from
  `properties.scaleConfiguration.readySessionInstances`. Pre-warmed sessions bill continuously, so a
  pool with `readySessionInstances>=1` and no session traffic is pure waste. **Important:** these do
  **not** appear in `az resource list -g <rg>` (only Resource Graph returns them), yet they are
  frequently the single largest line items on the bill.

### Step 3 — Pull utilization (Azure Monitor) for VM candidates

For each VM candidate (from Advisor or inventory), pull trailing CPU so the recommendation can be
**validated against reality**:

```bash
az monitor metrics list --resource <vm-resource-id> --metric "Percentage CPU" --interval PT1H --start-time <UTC-14d> --aggregation Average Maximum -o json
```

Reduce each series to `{cpu_p95, cpu_avg, mem_p95, sample_days}` (percent, 0–100). `cpu_p95` is the
95th percentile of the hourly Average series; `sample_days` is the number of distinct days covered.
Skip resources with fewer than `min_sample_days` (default 7) of data — the skill treats them as
unvalidated rather than guessing.

### Step 3b — Pull request activity (Azure Monitor) for Container Apps and session pools

For each container app, pull the trailing `Requests` metric so idle apps/environments are validated
against **real traffic** (not guessed). Without this signal an app is left unvalidated, never flagged.

```bash
az monitor metrics list --resource <containerapp-resource-id> --metric "Requests" --interval P1D --start-time <UTC-14d> --aggregation Total -o json
```

For each **session pool**, pull `SessionApiRequestCount` (Total) — and optionally
`PoolExecutingPodCount` (Maximum, should be 0 when idle) — as its activity signal:

```bash
az monitor metrics list --resource <sessionpool-resource-id> --metric "SessionApiRequestCount" --interval P1D --start-time <UTC-14d> --aggregation Total -o json
```

Note: the session-pool metrics namespace is flaky — a call may return
`Microsoft.App/sessionPools is not a supported platform metric namespace`. **Retry once or twice.**
If it never succeeds, omit the pool from `activity`; the ranker still surfaces a warm pool as an
*unvalidated* candidate ("verify usage before reducing") rather than dropping the largest line items.

Sum the Total series into `{resourceId: {"requests_total": <sum>, "sample_days": <distinct days>}}`.
`requests_total == 0` over enough days means no traffic (for a session pool, no sessions invoked).
This map is passed as `activity=` below.

### Step 4 — Pull per-resource monthly cost (optional but recommended)

Reuse the `finops-cost-anomaly-detection` cost pull (modern Consumption UsageDetails GET, paginated) over
the trailing ~30 days and aggregate `costInUSD` by resource id into `{resourceId: monthly_usd}`.
In **modern billing the full ARM resource id is in `properties.instanceName`** (the `resourceId`
field is null) — key on `instanceName` and fall back to `resourceId`. Ids are matched
case-insensitively. This is what lets the skill **rank by dollars** and size idle waste. The Cost
Management Query/POST API is not needed.

Start with the complete trailing window using `\$top=1000`, project only the needed fields with
`--query "{value: value[].{id: id, date: properties.date, cost: properties.costInUSD,
subscriptionId: properties.subscriptionId, resourceGroup: properties.resourceGroup,
resourceId: properties.instanceName || properties.resourceId, tags: tags}, nextLink: nextLink}"`,
and paginate every `nextLink`. If a request returns `413 Request Too Large`, retry with `\$top=100`,
then `20`. Only if bounded pages still fail, use short half-open `usageStart` date slices as a
fallback; live runs showed that filter is not reliably applied, so verify returned dates and
de-duplicate the combined rows. `--query` is client-side and does not itself prevent a server 413,
but it keeps the retained sandbox payload small. If the pull still cannot complete, keep partial rows
but **label the savings totals "partial — cost pull truncated"** so the biggest line items aren't
undercounted silently.

Persist the page envelopes and run `finops-managed-scope`'s `prepare_usage_details(...)`. Aggregate
monthly cost only from `included_rows`; use its exact coverage and `partial` fields in the final
result. Do not merge or scope-filter UsageDetails in model reasoning.

### Step 5 — Rank (bundled rightsize.py, in-sandbox)

Read the module and run it — do **not** re-implement the logic in the prompt:

```
read_skill_file(skill_name="finops-rightsizing-advisor", file_path="rightsize.py")
```

```python
from rightsize import recommend_rightsizing
recs = recommend_rightsizing(
    resources=resources,       # from Step 2
    utilization=utilization,   # from Step 3  {resourceId: {...}}
    activity=activity,         # from Step 3b {resourceId: {"requests_total", "sample_days"}}
    costs=costs,               # from Step 4  {resourceId: monthly_usd}
    advisor=advisor,           # from Step 1
)   # returns findings ranked by estMonthlySavingsUsd desc
```

`recommend_rightsizing` handles all classification and validation:

- **kind** is `idle` (unattached disk, stopped-not-deallocated VM, empty App Service plan, empty or
  no-traffic Container Apps environment, always-on container app with no requests, warm dynamic
  session pool with no session traffic, or p95 CPU below `cpu_idle_pct`=5%), `oversized` (p95 CPU
  below `cpu_underutil_pct`=20%), `advisor` (an Advisor rec with no independent signal), or `review`
  (see cost-led sweep below).
- **validated** is `True` when utilization/inventory backs the call, `False` when utilization
  **contradicts** an Advisor rec (high CPU — flagged "verify before acting"), and `None`/unvalidated
  when no metrics were available (e.g. a warm session pool whose metrics namespace was unavailable —
  surfaced as "verify usage before reducing" rather than dropped).
- **estMonthlySavingsUsd**: full monthly cost for idle resources; Advisor's own number when present;
  otherwise a conservative 50% estimate for a one-tier downsize. Findings below
  `min_monthly_savings_usd` (default $5) are dropped; findings with unknown cost are kept and sort last.
- **Cost-led coverage sweep** — after the rules run, any resource costing at least
  `review_min_monthly_usd` (default $20/mo) whose **type has no idle rule** and that **no other signal
  flagged** is surfaced as `kind="review"` (no savings claimed, `validated=None`). This guarantees no
  expensive line item is silently dropped just because we lack a heuristic for its type. Covered types
  that a rule evaluated but did not flag are **not** re-listed as review.
- Signals for the same `resourceId` (Advisor + inventory + utilization) merge into one finding, joined
  case-insensitively.

### Step 6 — Report

Produce a ranked table: resource (id + type), kind, current SKU, recommended action, current monthly
$, estimated monthly savings $, validated, and the evidence/sources. Call out the **total estimated
monthly savings** at the top. Clearly mark `validated=False` and unvalidated rows as "verify first".
List `kind="review"` rows in a separate **"High spend, no idle rule yet — review"** section so the
report's coverage is complete by spend even where no automated recommendation exists.
Recommend only — do not perform any resize/deallocate/delete. If nothing clears the threshold, say so
in one line.

Read-only only. Do not use any write/POST Azure operations.

## Notes

- Storage hot→cold tiering and reservation/savings-plan suggestions come through Advisor (Step 1) and
  flow through as `advisor` findings; utilization validation applies only to VM CPU today.
- The 50% one-tier savings estimate and the CPU thresholds are tunable via `recommend_rightsizing`
  keyword args if a subscription needs a different risk posture.
