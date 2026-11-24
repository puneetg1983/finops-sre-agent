---
name: finops-cost-anomaly-detection
description: Detect Azure cost spikes and explain why they happened. Pulls the daily cost time-series through the ARM MCP query_costs tool (Consumption UsageDetails only as a disclosed fallback), detects per-service/per-meter anomalies with the bundled detect.py using the runtime's sandbox Python tool, then correlates each spike to recent deployments, activity-log changes, and GitHub merges to surface a probable cause. Use for scheduled cost monitoring, "did anything spike this week and why", and adding a cost lens to incident investigations.
---

## When to use this skill

Use it when the user wants to **find cost spikes and understand their cause** — daily/weekly
automated cost monitoring, an on-demand "why did cost go up?" question, or a cost check during an
incident. This skill does **detection + root-cause correlation**, not just totals. For a plain
periodic cost summary, use `cost-optimization-report` instead.

## Required access

- The **ARM MCP connector** from this plugin's `.mcp.json`, with its read-only `*_query_costs`
  tool selected. Its calls run as the user who signed in to the connector, who needs **Cost
  Management Reader** on the managed scopes.
- **Cost Management Reader** for the agent's managed identity, used only by the UsageDetails
  fallback (one-time RBAC grant; see the plugin README).
- Read-only `az` (`RunAzCliReadCommands`) for change correlation, the GitHub connector, and
  sandbox Python: use `ExecutePythonCode` when available, otherwise `RunInTerminal` with
  `python3`. No ARM MCP write tool or Azure write API is used.

## Procedure

### Step 0 — Resolve the managed boundary

First load `finops-managed-scope` and follow its `scope.py` procedure to dynamically GET and validate
the current agent `managedResources`; never reuse cached scope. Managed scopes and expanded descendants
are the default boundary. Scheduled runs are strict/fail-closed and accept no override. An interactive
named resource outside scope requires disclosure and explicit confirmation in a subsequent turn before
any broader query. Broad RBAC never silently expands scope.

Retrieve cost through the ARM MCP `query_costs` contract in `finops-managed-scope` (section 4):
query every effective scope independently (one planned query per scope, never a wider scope),
track truncation and completeness per scope, and let `prepare_cost_query` de-duplicate and
filter rows against the expanded boundary as defense in depth. Scope deployment and Activity Log correlation to the same effective scope/resource.
Report included scopes, excluded rows, unattributed cost, unsupported scopes, and
partial/failed coverage.

### Step 1 — Pull the daily cost time-series

Request a trailing window large enough for a baseline plus the current period (default: 35
days, ending today UTC). Detection happens client-side, so only one query shape is needed:
daily cost by resource and meter category. `resourceGroup` is derived from each resource ID.

```python
from cost_query import plan_cost_queries, prepare_cost_query, split_cost_query

queries = plan_cost_queries(
    managed_resources,
    management_group_expansions=mg_descendants,
    start=START, end=END, as_of=TODAY,
    granularity="Daily",
    group_by="ResourceId,MeterCategory",
)
```

Call `*_query_costs` with each argument object exactly as planned, using
`batch_cost_queries` from `cost_query` and section 4's bounded parallel execution and throttling backoff. When a result
has a non-null `properties.nextLink`, discard it and query both `split_cost_query(arguments)`
halves instead. Then:

```python
coverage = prepare_cost_query(results, managed_resources,
                              management_group_expansions=mg_descendants,
                              window=(START, END))
line_items = coverage["included_rows"]   # {date, cost, meterCategory, resourceGroup, resourceId, ...}
```

Do not concatenate, de-duplicate, or scope-filter rows in model reasoning. `query_costs` has no
tags, so the default dimensions (`meterCategory`, `resourceGroup`, `resourceId`) are used; for a
`tag:<key>` dimension, join resource IDs to Resource Graph tags first. If `prepare_cost_query`
reports `partial`, label every downstream total **"partial — cost pull incomplete, spend
understated"**.

**Fallback (only when `finops-managed-scope` section 4 requires it):** use the Consumption
UsageDetails contract in `finops-managed-scope` section 5 over the same window with this
projection, then call `prepare_usage_details(...)` and use its `included_rows`:

```bash
az rest --method get --url "https://management.azure.com/subscriptions/<SUB_ID>/providers/Microsoft.Consumption/usageDetails?api-version=2023-05-01&metric=ActualCost&\$top=1000" \
  --query "{value: value[].{id: id, date: properties.date, cost: properties.costInUSD, meterCategory: properties.meterCategory, subscriptionId: properties.subscriptionId, resourceGroup: properties.resourceGroup, resourceId: properties.instanceName || properties.resourceId, tags: tags}, nextLink: nextLink}"
```

Follow every `nextLink` (decode `&amp;` first); on 413 retry with `\$top=100`, then `20`.

### Step 2 — Detect (bundled detect.py, in-sandbox)

Read the detector and run it with the available sandbox Python tool:
`ExecutePythonCode`, or `RunInTerminal` with `python3` when that is the runtime-provided tool.

```
read_skill_file(skill_name="finops-cost-anomaly-detection", file_path="detect.py")
```

```python
from detect import detect_anomalies
anomalies = detect_anomalies(line_items)   # returns list ranked by impact_usd desc
```

`detect_anomalies` handles all detection rules — do **not** re-implement them in the prompt:

- Per-dimension trailing baseline (`meterCategory`, `resourceGroup`, `resourceId`); a spike fires
  when today exceeds `mean + k*std` **or** is ≥ `wow_ratio`× the same day last week, and only when
  the absolute change clears `min_delta_usd` (defaults `k=3`, `wow_ratio=1.5`, `min_delta_usd=$5`).
- Dimensions with no prior history are labelled `new_spend`, not `spike`.
- Billing lag: pass `assume_last_partial=True` (default) so the partial newest day is excluded and
  the last **complete** day is analysed. Never flag the partial trailing day.
- Results are ranked by **absolute dollar impact**, not percentage.

Tune via keyword args (`baseline_days`, `k`, `min_delta_usd`, `wow_ratio`) — daily watchers use
tighter settings than weekly ones.

### Step 3 — Correlate each spike to a probable cause

For every returned anomaly, gather evidence in the same date window (spike day ± 1) scoped to the
spiking `value` (resource / RG / meter):

- **Deployments:** `az deployment group list -g <RG>` and `az deployment sub list` — match
  `properties.timestamp` near the spike.
- **Activity log:** `az monitor activity-log list --start-time <T-1d> --end-time <T+1d>` — write
  and action operations on the resource/RG (scale, SKU change, create).
- **GitHub changes:** via the connector, `List Commits` / `List Pull Requests` (merged) near the
  spike date for the connected repo(s).

Join on **resource/RG identity + timestamp proximity**. Present matches as **candidate causes with
evidence links** — correlation, not proof. If nothing correlates, say so ("no deployment/change
found near this spike") rather than inventing a cause.

### Step 4 — Report

Emit a ranked list; each row: `{value, current_usd, dod_delta_usd, dod_delta_pct, wow_delta_usd,
date, kind, candidate cause(s) with links}`, plus a one-line executive summary. In scheduled mode,
**report only when `detect_anomalies` returns a non-empty list**; otherwise emit a short "no
anomalies" line.

## Delivery modes

- **Scheduled task (primary):** run daily/weekly; post the report only on a threshold crossing.
- **Interactive:** the main agent (or the optional `finops-investigator` subagent created by
  `finops-setup`) calls this skill for "did anything spike and why?" drill-downs.
- **Incident-time:** invoked during a reliability investigation to check whether an incident had a
  cost signature.

## Known limits

| Limit | Behavior |
|-------|----------|
| Billing lag (1-2 days) | `assume_last_partial=True` excludes the partial newest day |
| Microsoft Fabric cost | Not in the Cost Management surface — out of scope; state it explicitly |
| `query_costs` row cap (5,000, no paging) | Split truncated date ranges with `split_cost_query`; never accept a result with `nextLink` |
| ARM MCP sign-in expired | Interactive: ask the user to reconnect; scheduled: disclosed UsageDetails fallback |
| Weak tag coverage | Where tags are missing, correlate on resource/RG identity instead of team/owner |
| Correlation != causation | Present candidate causes with evidence; do not claim certainty |
| Cost Management Reader missing | `query_costs` returns an authorization error (or `costInUSD` is null in the fallback) — stop and report the missing grant, don't guess |

## Testing

`detect.py` is pure and offline-testable. Run `pytest tests/` at the repo root — see
`tests/test_detect.py` for the Layer-1 suite (known spike, tiny-meter floor, new-spend labelling,
flat-history no-false-alarm, week-over-week creep, dollar-impact ranking, partial-last-day
handling).
