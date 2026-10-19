---
name: finops-cost-vs-reliability
description: FinOps cost-vs-reliability analysis for Azure, read-only. Joins monthly UsageDetails cost to Azure Monitor alerts (primary pain signal), Resource Health unavailable/degraded events, and Advisor HighAvailability recommendations (secondary signals), then ranks resources and services by transparent weighted-count reliability pain using the bundled reliability.py with the runtime's sandbox Python tool. Use for "where are we spending money but still hurting", "which low-cost resources need HA investment", and "which high-cost resources have no reliability pain and should be verified before rightsizing".
---

## When to use this skill

Use it when the user wants to compare **Azure cost and reliability pain** — for example "which
resources cost the most and alert the most", "where should we invest in HA", or "which high-spend
resources have no incidents before a cost-cutting review". It is read-only and advisory; it never
mutates resources, never creates alerts, and never runs write/POST Azure operations.

For cost spikes use `finops-cost-anomaly-detection`; for concrete savings recommendations use
`finops-rightsizing-advisor`; for showback by tags use `finops-cost-allocation`.

## Required access

- **Cost Management Reader** on the subscription (`costInUSD` is null without it). **Reader** for
  Azure Monitor Alerts, Resource Health, Advisor, and optional Activity Log pulls.
- Read-only `az` (`RunAzCliReadCommands`) and sandbox Python: use `ExecutePythonCode` when
  available, otherwise `RunInTerminal` with `python3`. No POST/write APIs are used.

## Scope

v1 is **per-resource first**, with a **per-service rollup**. It joins monthly UsageDetails cost to:

1. Azure Monitor / Alerts Management alerts — the primary reliability pain signal.
2. Resource Health `Unavailable` / `Degraded` availability statuses.
3. Advisor `HighAvailability` recommendations.
4. Optional Activity Log `ResourceHealth` events when available.

Scoring is deliberately simple and explainable: weighted counts only. Alert severity dominates by
construction; health and Advisor add smaller secondary weights. Duration math, incident-system joins,
KQL/SLO/error-budget math, metric rates, and causal analysis are deferred to v2.

## Procedure

### Step 0 — Resolve the managed boundary

First load `finops-managed-scope` and follow its `scope.py` procedure to dynamically GET and validate
the current agent `managedResources`; never reuse cached scope. Managed scopes and expanded descendants
are the default boundary. Scheduled runs are strict/fail-closed with no override. An interactive named
outside-scope target requires disclosure and explicit confirmation in a subsequent turn before any
broader query. Broad RBAC never silently expands scope.

Pull UsageDetails independently for every effective scope where supported, paginate each independently,
de-duplicate overlaps, and boundary-filter resource ids while preserving unattributed cost separately.
Pull Alerts Management, Resource Health, Advisor, and optional Activity Log once per effective
scope/subscription; retain only in-bound resource signals, while separately counting legitimate
scope-level signals. Report included scopes, excluded/unmatched/unattributed data, unsupported scopes,
and partial/failed coverage.

Advisor must be run once per unique effective subscription with `--subscription`. If the subscription
is represented only by RG-level managed scopes, run once per unique effective RG with
`--resource-group`, then client-side filter normalized resource ids to that exact RG ARM prefix.
De-duplicate recommendations from overlapping scopes.

### Step 1 — Pull monthly cost line items (UsageDetails GET only)

Follow the canonical Consumption UsageDetails transport contract in `finops-managed-scope` for the
complete trailing ~30-day result, using this skill's needed fields:

```bash
az rest --method get \
  --url "https://management.azure.com/subscriptions/<SUB_ID>/providers/Microsoft.Consumption/usageDetails?api-version=2023-05-01&metric=ActualCost&\$top=1000" \
  --query "{value: value[].{id: id, date: properties.date, cost: properties.costInUSD, pretaxCost: properties.pretaxCost, consumedService: properties.consumedService, meterCategory: properties.meterCategory, subscriptionId: properties.subscriptionId, resourceGroup: properties.resourceGroup, resourceId: properties.instanceName || properties.resourceId}, nextLink: nextLink}"
```

In modern billing the full ARM resource id is usually in `properties.instanceName`; the projection
falls back to `properties.resourceId`. If `costInUSD` is null, the bundled core uses the same fallback
order as the other skills: `cost` / `costInUSD` / `pretaxCost`. If the pull cannot complete after all
fallbacks, keep the partial rows but label the report totals **partial — cost pull truncated**.

Pass persisted page envelopes through `finops-managed-scope`'s
`prepare_usage_details(...)` and give `analyze_cost_vs_reliability` only `included_rows`. Preserve
the returned coverage/partial metadata instead of merging or filtering pages in model reasoning.

### Step 2 — Pull Azure Monitor alerts (primary pain signal)

Pull recent alerts through Alerts Management. Keep at least severity, target resource ids, alert rule,
and start time:

```bash
az rest --method get \
  --url "https://management.azure.com/subscriptions/<SUB_ID>/providers/Microsoft.AlertsManagement/alerts?api-version=2019-05-05-preview&timeRange=30d" \
  --query "{value: value[].{name: name, severity: properties.essentials.severity, alertTargetIDs: properties.essentials.alertTargetIDs, startDateTime: properties.essentials.startDateTime, alertRule: properties.essentials.alertRule}, nextLink: nextLink}"
```

Paginate `nextLink` when present. Alerts with multiple `alertTargetIDs` count once per target resource
so resource-level pain is explicit.

### Step 3 — Pull Resource Health availability statuses

Pull current Resource Health availability statuses and keep only `Unavailable` / `Degraded` rows for
scoring:

```bash
az rest --method get \
  --url "https://management.azure.com/subscriptions/<SUB_ID>/providers/Microsoft.ResourceHealth/availabilityStatuses?api-version=2023-07-01" \
  --query "{value: value[].{id: id, resourceId: properties.targetResourceId, availabilityState: properties.availabilityState, occurredTime: properties.occurredTime, summary: properties.summary}, nextLink: nextLink}"
```

If `targetResourceId` is absent, the bundled core can strip the
`/providers/Microsoft.ResourceHealth/availabilityStatuses/...` suffix from `id`.

### Step 4 — Pull Advisor HighAvailability recommendations

Advisor is a secondary reliability signal. Pull only HighAvailability recommendations:

```bash
az advisor recommendation list \
  --subscription <EFFECTIVE_SUBSCRIPTION_ID> \
  --category HighAvailability \
  -o json
```

For an RG-only managed scope:

```bash
az advisor recommendation list \
  --subscription <EFFECTIVE_SUBSCRIPTION_ID> \
  --resource-group <EFFECTIVE_RESOURCE_GROUP> \
  --category HighAvailability \
  -o json
```

Flatten each recommendation to `{resourceId, category, problem, recommendation, lastUpdated}` where
`resourceId` comes from `resourceMetadata.resourceId` (or an ARM-shaped `impactedValue`). Ignore
non-HighAvailability Advisor categories.

### Step 5 (optional) — Pull Activity Log ResourceHealth events

Some reliability events are subscription/region-scoped and will not join to a resource. Pulling them is
optional; keep them for coverage/data-quality disclosure rather than forcing allocation:

```bash
az monitor activity-log list \
  --subscription <SUB_ID> \
  --start-time <UTC-START> \
  --offset 30d \
  --namespace Microsoft.ResourceHealth \
  --query "[].{resourceId: resourceId, eventTimestamp: eventTimestamp, status: status.value, category: category.value, operationName: operationName.value}"
```

Include Activity Log rows whose status/state maps to `Unavailable` or `Degraded` in the
`health_events` input. Subscription-level rows are counted separately and not joined to cost.

### Step 6 — Normalize ids and run the bundled core

Read the module and run it — do **not** re-implement the scoring in the prompt:

```
read_skill_file(skill_name="finops-cost-vs-reliability", file_path="reliability.py")
```

```python
from reliability import analyze_cost_vs_reliability

result = analyze_cost_vs_reliability(
    line_items=cost_line_items,
    alerts=alerts,
    health_events=health_events,
    advisor_recommendations=advisor_recommendations,
    top_n_resources=25,
    top_n_services=15,
)
```

`analyze_cost_vs_reliability` returns headline totals, coverage counts, per-resource rankings,
per-service rollups, high-spend/high-pain drivers, hints, unmatched reliability signals, and
data-quality limitations. Resource ids are joined case-insensitively.

### Step 7 — Report

Produce:

- **Headline totals + coverage**: total monthly spend, resource count, reliability signal count,
  joined resource count, unmatched reliability count, subscription-level event count, and whether cost
  was partial.
- **Spend + pain table**: resource, service, monthly $, share of spend, alert count/severity counts,
  health events, Advisor HA count, reliability score, pain per $1K, risk band, and primary signal.
- **Service rollup**: service, monthly $, resources, alerts, health events, Advisor HA count,
  reliability score, and risk band.
- **High pain / low spend** investment candidates: resources with high pain but low spend — recommend
  HA/resilience investigation before cost cutting.
- **High spend / no pain** verify-before-cutting candidates: resources with high spend but no pain in
  these signals — verify utilization, business criticality, and telemetry coverage before rightsizing.
- **Data quality**: unmatched reliability signals, subscription-level signals, partial-cost warning,
  sources used, and disclosed limitations.

If there are no reliability signals, say so clearly and use the high-spend/no-pain rows only as a
**verify first** list. Do not claim zero incidents or safe-to-delete; the input is not a complete
incident system.

## Disclosed Limitations

- Cost comes only from Consumption **UsageDetails GET**. Cost Management Query uses POST and is
  unavailable to `RunAzCliReadCommands`.
- Alerts are the primary pain signal but are not a full incident-management system; missing alert
  coverage can make pain look lower than reality.
- Service Health and some Resource Health / Activity Log events are subscription- or region-scoped, so
  they may be counted but not joined to a resource cost line.
- Advisor HighAvailability is current-state guidance, not historical reliability pain.
- v1 uses weighted counts only. Metrics, KQL, SLO/error-budget math, incident duration, customer impact,
  and causal cost-vs-reliability modeling are deferred to v2.
