---
name: finops-cost-optimization-report
description: Executive FinOps rollup for Azure — one report bundling cost anomalies, rightsizing savings, budget status, and governance (policy) findings. It orchestrates the four existing read-only skills (finops-cost-anomaly-detection, finops-rightsizing-advisor, finops-cost-allocation, finops-budget-governance), then rolls their outputs up with the bundled summarize.py using the runtime's sandbox Python tool into an executive headline, a single dollar-ranked priorities list, and per-section detail. Read-only — no writes, no new RBAC; "policy findings" reuse existing tag-hygiene and budget-gate signals rather than a new data source. Use for a periodic FinOps review, a cost-optimization summary, or an executive "where should we act" briefing.
---

## When to use this skill

Use it when the user wants **one consolidated FinOps review** rather than a single analysis —
"give me the cost-optimization report", "what are our top savings/risk items this week", an executive
"where should we act" briefing, or a recurring FinOps summary. It bundles the pack's four read-only
analyses into one prioritized picture: what we can save, what is spiking, what is about to blow a
budget, and what spend is ungoverned.

For a single dimension use the underlying skill directly: cost spikes →
`finops-cost-anomaly-detection`; idle/oversized waste → `finops-rightsizing-advisor`; who owns the
spend → `finops-cost-allocation`; budget status → `finops-budget-governance`; right-sizing a budget →
`finops-budget-editor`.

## Required access

- **Cost Management Reader** (or **Reader**) on the subscription — the same read-only access the four
  underlying skills use. No write role is needed or used.
- Read-only `az` (`RunAzCliReadCommands`) and sandbox Python: use `ExecutePythonCode` when
  available, otherwise `RunInTerminal` with `python3`. **No POST/PUT or write APIs are called.**
- This report is purely a **rollup**: "policy findings" are the existing tag-hygiene / untagged-spend
  and budget-gate signals — it introduces no new data source and no new permission.

## Scope

Bundles the four existing analyses at the requested scope (subscription or resource group). Each
underlying skill pulls and shapes its own data; this skill only aggregates their outputs, so it
inherits their scope and caveats (e.g. `currentSpend` can lag on a fresh budget; a `run-rate` budget
forecast is an estimate; rightsizing rows marked unvalidated need verification). Any analysis that
can't run or returns nothing becomes an **empty section**, not a failure — a partial report still
ships. Cross-currency portfolios are out of scope until needed.

## Procedure

### Step 0 — Resolve the managed boundary

First load `finops-managed-scope` and follow its `scope.py` procedure to dynamically GET and validate
the current agent `managedResources`; never reuse cached scope. Pass one immutable effective-scope set
for this request to every underlying skill. Scheduled runs are strict/fail-closed with no override.
An interactive named outside-scope target requires disclosure and explicit confirmation in a
subsequent turn before any broader query. Broad RBAC never silently expands scope.

For each effective scope or expanded descendant, every underlying analysis must query and enforce its own
defense-in-depth filtering. Shared UsageDetails may be reused only when it was independently paginated
per effective scope, de-duplicated, boundary-filtered, and annotated with per-scope completeness.
The report must disclose included scopes, excluded/unattributed data, unsupported scopes, and partial
coverage alongside the existing analysis caveats.

### Step 1 — Run the four underlying analyses (read-only)

Run each existing skill and keep its structured output. Follow each skill's own SKILL.md for the data
pull and call — do **not** re-implement their logic here:

1. **finops-cost-anomaly-detection** → `detect_anomalies(line_items)` → a list of anomalies.
2. **finops-rightsizing-advisor** → `recommend_rightsizing(...)` → a list of findings.
3. **finops-cost-allocation** → `allocate_costs(costs, tags, dimension=...)` → an allocation dict.
4. **finops-budget-governance** → `evaluate_budgets(budgets)` → a budget-status dict.

The heavy/`413`-prone step is the shared **cost line-item pull** used by anomaly + allocation — pull it
**once** and feed both cores. If any one analysis fails or is out of scope, keep going with the rest;
`summarize.py` treats a missing input as an empty section. Do **not** use `az rest --method post` or
the Cost Management Query API — POST is blocked as a write.

### Step 2 — Roll up (bundled summarize.py, in-sandbox)

Read the module and run it — do **not** re-implement the rollup in the prompt:

```
read_skill_file(skill_name="finops-cost-optimization-report", file_path="summarize.py")
```

```python
from summarize import summarize_optimization
report = summarize_optimization(
    anomalies=anomalies_list,       # from detect_anomalies (or None)
    rightsizing=rightsizing_list,   # from recommend_rightsizing (or None)
    allocation=allocation_dict,     # from allocate_costs (or None)
    budgets=budgets_dict,           # from evaluate_budgets (or None)
)
```

`summarize_optimization` produces the executive rollup:

- **headline**: `total_monthly_spend`, `potential_monthly_savings`, `anomaly_count`,
  `top_anomaly_impact_usd`, budgets `over` / `forecast_over` / `at_risk` counts, `untagged_usd`,
  `unallocated_pct`.
- **priorities**: ONE dollar-ranked action list across all sources. Each item carries an
  **`impact_type`** — `savings` (rightsizing), `overrun` (budget gate), `spike` (anomaly), or
  `governance` (untagged spend / tag hygiene) — so amounts are never silently conflated. Unknown-dollar
  items are kept and sort last.
- **rightsizing / anomalies / budgets / governance**: the per-section detail for the dashboard
  (top rows, totals, budget buckets, tag-hygiene, budget gates).

### Step 3 — Report

Produce an **executive summary** dashboard: the **headline** metrics up top; the **top priorities**
table next (rank, category, impact, action) — this is the "where to act first" list; then a section
per analysis (rightsizing savings, anomalies, budget status, governance/policy findings). Label each
priority by its `impact_type` and **never sum savings, overruns, spikes, and governance exposure into
one number** — they are different kinds of dollars. Carry through the underlying caveats: mark
`run-rate` budget forecasts and unvalidated rightsizing rows as estimates/"verify first", and note an
unsynced `currentSpend` where relevant. If an analysis returned nothing, show its section as a clean
empty-state. If every section is clean, say so in one line.
