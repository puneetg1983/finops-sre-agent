---
name: finops-report-renderer
description: Renders the consolidated FinOps dashboard with 17 sections, keyboard-accessible tabs, local search, inline charts, source coverage and print/PDF. Also supports individual analysis adapters. Deterministic CSP-compliant static HTML; no Azure or connector calls.
---

## Purpose

**Scheduled default:** build the single **FinOps: Dashboard**, not six independent reports.
Follow [DASHBOARD.md](DASHBOARD.md), call `build_dashboard_model` in `dashboard.py`, then
`write_report` in `render.py`. Keep `dashboard.py`, `models.py`, `render.py`, `theme.css`,
`dashboard.css`, `fluent-icons.svg`, and `microsoft-product-icons.svg` together. Dashboard charts,
Microsoft Fluent system icons, and authorized Microsoft product artwork are inline SVG, with
no external scripts or network dependencies. Product artwork has separate terms; see DASHBOARD.md.
Source gaps retain their own visible tab and reason. Never replace a failed analysis with a
healthy zero. All report types use the shared light/dark theme.

Overview and Cost explorer default to 30 inclusive UTC days and can use a preloaded 90-day
history or custom dates when both daily, scope-filtered cost queries completed. Other sections
retain their native periods. Follow DASHBOARD.md for the history envelopes and fail explicitly
if history is incomplete or exceeds the Live Report 10 MB limit; never silently truncate it.
Opening the saved report makes no Azure calls.

The dashboard includes Light/Dark buttons, full-width signed column charts with drillable Other
and switchable groupings (no pie or expandable category lists),
top 10/25/50 record previews and local CSV export of every matching record. Preview limits
must never change financial totals, full-dataset search or exports. The report host must
allow downloads; complete print/PDF and no-JavaScript tables remain available.

Use this utility after a FinOps analysis helper has produced structured results. Build a report
model with the matching adapter in `models.py`, call `render_report` or `write_report` from
`render.py`, then pass the output path to `SaveReport` with `allowedTools: []`.

The renderer owns HTML escaping, CSP, nonce placement, Chart.js SRI, empty states, partial-data
warnings, and stable layout. Do not ask the model to regenerate or rewrite the returned HTML.
Adapters do no I/O, make no clock or network calls, and do not mutate their input. Supply
`refreshed_at` explicitly.

## Adapter API

Load the exact `models.py`, `render.py`, and `theme.css` files into the same sandbox. Each adapter consumes an
existing deterministic analysis result and returns the generic model accepted by `render_report`
and `write_report`:

```python
from models import (
    build_ai_spend_model,
    build_budget_status_model,
    build_cost_optimization_model,
    build_cost_overview_model,
    build_cost_vs_reliability_model,
    build_rightsizing_savings_model,
)
from render import write_report

model = build_budget_status_model(
    evaluate_budgets_result,
    refreshed_at="2026-08-07T01:30:00Z",
    scope_summary=["/subscriptions/.../resourceGroups/managed"],
    partial=cost_or_scope_pull_was_partial,
    warnings=["One managed scope failed after retries."],
    scope_coverage=[{
        "scope": "/subscriptions/.../resourceGroups/managed",
        "status": "partial",
        "included": 42,
        "excluded": 3,
        "unattributed": 1,
        "cost_usd": 123.45,
        "partial": True,
        "detail": "query_costs result truncated; split before reporting.",
    }],
)
path = write_report(model, "finops-budget-status.html")
```

All six functions have the same keyword-only context arguments:

```text
(*, refreshed_at, scope_summary=None, partial=False,
    warnings=None, scope_coverage=None)
```

- `refreshed_at` is required and must be supplied by the caller; adapters never read the clock.
- `scope_summary`, `warnings`, and `scope_coverage` are sorted and de-duplicated deterministically.
- Input-level `partial`, `warnings`, `scope_summary`, and `scope_coverage` fields are also preserved
  when present. Any partial coverage row makes the report partial.
- Scope coverage rows accept the fields shown above; `prepare_cost_query(...)["scope_coverage"]` already
  has this shape. Counts must be integers and monetary values
  must be finite numbers, not numeric strings.
- Text is retained verbatim in the model. Escaping happens only in `render_report`.
- Cost formatting and cost summation use decimal arithmetic at currency boundaries. Invalid,
  non-finite, boolean, or string-encoded numbers are rejected instead of broadly coerced.

### Report-specific inputs

| Adapter | Deterministic helper output |
|---|---|
| `build_cost_overview_model(overview, ...)` | The object returned by `finops-managed-scope`'s `summarize_cost_overview(prepare_cost_query(...))`, or any cost aggregation with `total_usd`, `daily` (`date`, `cost_usd`), `by_service` (`service`, `cost_usd`), and `by_resource_group` (`resource_group`, `cost_usd`). The aliases `daily_totals`, `top_services`, and `top_resource_groups` are accepted. |
| `build_rightsizing_savings_model(result, ...)` | The list returned by `recommend_rightsizing`, or an object containing it as `findings`/`recommendations`. |
| `build_budget_status_model(result, ...)` | The object returned by `evaluate_budgets`. |
| `build_cost_optimization_model(result, ...)` | The object returned by `summarize_optimization`. |
| `build_ai_spend_model(result, ...)` | The object returned by `attribute_ai_costs`. |
| `build_cost_vs_reliability_model(result, ...)` | The object returned by `analyze_cost_vs_reliability`. |

The adapters deterministically compute display metrics, charts, and tables from those structured
outputs. They apply explicit stable tie-breakers, retain unknown/empty values, label unvalidated
recommendations and estimates, and never use an LLM to aggregate or construct HTML.

## Model

```python
model = {
    "title": "FinOps: Cost Overview",
    "description": "Daily Azure cost snapshot.",
    "refreshedAt": "2026-08-07T01:30:00Z",
    "scopeSummary": ["/subscriptions/.../resourceGroups/managed"],
    "partial": False,
    "warnings": [],
    "metrics": [
        {"label": "30-day spend", "value": "$123.45", "detail": "ActualCost"},
    ],
    "chart": {
        "type": "line",
        "label": "Daily cost",
        "labels": ["Aug 1", "Aug 2"],
        "values": [12.3, 14.8],
    },
    "sections": [
        {
            "title": "Top services",
            "description": "Ranked by cost.",
            "columns": ["Service", "Cost"],
            "rows": [["Storage", "$80.00"]],
            "emptyMessage": "No service cost rows.",
        }
    ],
}
```

All displayed values must already be computed by deterministic analysis code. The renderer does not
derive FinOps totals or recommendations.

## Rendering API

- `render_report(model) -> str`: validate the generic model and return deterministic static HTML.
- `write_report(model, output_path) -> str`: render UTF-8 HTML, create parent directories, and
  return the written path.

Keep `allowedTools: []` when saving the generated static report. Do not add connector calls or
view-time JavaScript data fetching.
