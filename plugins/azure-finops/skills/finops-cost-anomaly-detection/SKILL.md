---
name: finops-cost-anomaly-detection
description: Detect Azure cost spikes and explain why they happened. Pulls the daily cost time-series from Cost Management (Consumption UsageDetails), detects per-service/per-meter anomalies with the bundled detect.py using the runtime's sandbox Python tool, then correlates each spike to recent deployments, activity-log changes, and GitHub merges to surface a probable cause. Use for scheduled cost monitoring, "did anything spike this week and why", and adding a cost lens to incident investigations.
---

## When to use this skill

Use it when the user wants to **find cost spikes and understand their cause** — daily/weekly
automated cost monitoring, an on-demand "why did cost go up?" question, or a cost check during an
incident. This skill does **detection + root-cause correlation**, not just totals. For a plain
periodic cost summary, use `cost-optimization-report` instead.

## Required access

- **Cost Management Reader** on the subscription (or billing scope) for the agent's managed
  identity. Without it, `costInUSD` comes back null. This is a one-time RBAC grant (see the
  plugin README).
- Read-only `az` (`RunAzCliReadCommands`), the GitHub connector (for change correlation), and
  sandbox Python: use `ExecutePythonCode` when available, otherwise `RunInTerminal` with
  `python3`. No POST/write APIs are used.

## Procedure

### Step 0 — Resolve the managed boundary

First load `finops-managed-scope` and follow its `scope.py` procedure to dynamically GET and validate
the current agent `managedResources`; never reuse cached scope. Managed scopes and expanded descendants
are the default boundary. Scheduled runs are strict/fail-closed and accept no override. An interactive
named resource outside scope requires disclosure and explicit confirmation in a subsequent turn before
any broader query. Broad RBAC never silently expands scope.

Pull UsageDetails independently for every effective scope where supported, paginate each scope
independently, and track completeness per scope. De-duplicate overlap, then filter resource ids and
scope fields against the expanded boundary as defense in depth. Scope deployment and Activity Log
correlation to the same effective scope/resource. Report included scopes, excluded rows, unattributed
cost, unsupported scopes, and partial/failed coverage.

### Step 1 — Pull the daily cost time-series

Use the **modern Consumption UsageDetails GET** (the Cost Management Query/POST aggregation API is
blocked by the read-only gate and is **not needed** — detection happens client-side). Request a
trailing window large enough for a baseline plus the current period (default: 35 days).

**Start with a bounded page size, minimal field projection, and full pagination.** Request the
trailing window directly with `\$top=1000`, project only the fields the detector needs with
`--query`, and follow every `nextLink`. Do not lead with `usageStart` slicing: live runs showed that
the service does not reliably apply that filter, so slicing is a fallback rather than the primary
retrieval strategy.

The controls solve different problems:

1. **Bounded `\$top` limits each server page.** A `413 Request Too Large` is a server-side response
   size failure. Start at `\$top=1000`; if the initial request or a `nextLink` returns 413, retry the
   same request with a smaller page size (`100`, then `20`).
2. **`--query` keeps retained data small.** It is applied client-side after download, so it cannot by
   itself prevent a server 413. It does keep the JSON stored, concatenated, and passed to the sandbox
   limited to the fields the analysis uses.
3. **Date slicing is the final fallback.** If bounded pages still fail, split the trailing window into
   short half-open `usageStart` slices, paginate each slice, and de-duplicate the combined rows.

```bash
az rest --method get --url "https://management.azure.com/subscriptions/<SUB_ID>/providers/Microsoft.Consumption/usageDetails?api-version=2023-05-01&metric=ActualCost&\$top=1000" \
  --query "{value: value[].{id: id, date: properties.date, cost: properties.costInUSD, meterCategory: properties.meterCategory, subscriptionId: properties.subscriptionId, resourceGroup: properties.resourceGroup, resourceId: properties.instanceName || properties.resourceId, tags: tags}, nextLink: nextLink}"
```

- **Primary anti-413 sequence:** bounded `\$top=1000`; on 413 retry with `100`, then `20`.
- **`--query` projection (retained-payload hygiene):** keeps only `{date, cost, meterCategory,
  resourceGroup, resourceId, tags}` plus `nextLink`, so the concatenated dataset stays small.
- **Paginate the complete result:** follow `nextLink` until absent. `nextLink` already carries the
  skip token — GET it as-is (don't re-add parameters). Note the
  `nextLink` in the raw body is HTML-escaped (`&amp;`) — decode `&amp;`→`&` before following it.
- **Date-slice fallback:** only if smaller pages still 413, retry with short
  `\$filter=properties/usageStart ge '<SLICE_START>' and properties/usageStart lt '<SLICE_END>'`
  windows. Because the filter is not reliably applied, verify returned dates and de-duplicate by
  `(resourceId, date, meterId)` when concatenating.
- **Never proceed on silently-partial cost.** If the pull cannot complete after all fallbacks,
  keep the rows you have **but explicitly label every downstream total as "partial — cost pull
  truncated, spend understated"** so the numbers aren't trusted as complete.
- The projected rows already match the shape `detect.py` expects:
  `{date, cost, meterCategory, resourceGroup, resourceId, tags}` — `date` from
  `properties.date`, `cost` from `properties.costInUSD`. For `resourceId` use
  **`properties.instanceName`** (in modern billing the full ARM resource id lives there and
  `properties.resourceId` is null); fall back to `properties.resourceId` when `instanceName`
  is absent.
- Pass the persisted page envelopes to `finops-managed-scope`'s
  `prepare_usage_details(...)`; call `detect_anomalies` only with its `included_rows`. Do not
  concatenate, de-duplicate, or scope-filter pages in model reasoning.

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
| Microsoft Fabric cost | Not in the Consumption surface — out of scope; state it explicitly |
| Weak tag coverage | Where tags are missing, correlate on resource/RG identity instead of team/owner |
| Correlation != causation | Present candidate causes with evidence; do not claim certainty |
| Cost Management Reader missing | `costInUSD` is null — stop and report the missing grant, don't guess |

## Testing

`detect.py` is pure and offline-testable. Run `pytest tests/` at the repo root — see
`tests/test_detect.py` for the Layer-1 suite (known spike, tiny-meter floor, new-spend labelling,
flat-history no-false-alarm, week-over-week creep, dollar-impact ranking, partial-last-day
handling).
