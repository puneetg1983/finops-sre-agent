# Consolidated dashboard contract

The setup task saves **FinOps: Dashboard**, one daily-refreshed Live Report with 17 sections.
Keep `dashboard.py`, `models.py`, `render.py`, `theme.css`, `dashboard.css`, `fluent-icons.svg`, and `microsoft-product-icons.svg`
together. There are no third-party scripts, fonts, network calls or view-time Azure tools in
dashboard HTML. CSP nonces are substituted by SaveReport. Without JavaScript, all sections remain
readable; with it, tabs support keyboard navigation, the mobile section selector, table search and
print/PDF (which prints all sections).

The report uses a compact portal-inspired shell, grouped navigation, a command bar, and an
inline scope disclosure. The overview's Top services is a ranked horizontal bar graph with
Azure service symbols and an expandable data table. The heading retains **FinOps: Dashboard**,
with **Powered by Azure SRE Agent** below it. The layout is portal-inspired, not a copy of the
authenticated SRE Agent portal or a claim of Microsoft endorsement.

Dashboard charts use an Azure-style blue-led categorical palette and
[Microsoft Fluent charting categorical/semantic colors](https://github.com/microsoft/fluentui/blob/master/packages/charts/react-charting/src/utilities/colors.ts).
Palette tokens are scoped to the dashboard, including light/dark metric status colors;
common services retain the same color across Overview, Cost explorer and resource-type
breakdowns. Official product artwork keeps its original colors.

Charts draw in once when first visible during each dashboard opening: bars grow, trend lines
trace, and regional pie slices fade into place. Hidden tabs and below-the-fold charts wait until
viewed. Filtering and revisiting a chart do not replay the entrance animation. Motion is skipped
for reduced-motion preferences, and printing cancels animation to show complete plots.
This is presentation only: it does not fetch fresh costs or change the saved snapshot.
Browsers without animation support, and no-JavaScript views, retain complete static charts.

Cost explorer uses linked service bars and a region pie chart above a compact resource table.
Pie slices and region legend buttons filter the resource list; the legend shows amounts and
percentages. Chart panels grow with their contents rather than using internal scrollbars.
Regional totals containing negative credits use signed bars instead of misleading pie shares;
zero or empty totals show an explicit no-cost message. Region colors stay stable while filtering.
At the top of Cost explorer, compact Subscription, Resource type and Region dropdowns default to
**All** and show `(1 selected)`, `(2 selected)`, etc. for explicit choices. They support multiple
checkbox selections: choices within a dropdown are combined with OR; different dropdowns combine
with AND. Each dropdown's All button clears just that dimension; removing its last explicit
selection also restores All. Escape or clicking outside closes the dropdown. Pie/legend clicks
narrow Region to one choice, and selecting it again restores All.
Resource type is derived from the ARM resource ID (including nested and extension resources);
unidentifiable types remain `(unknown)`, not guessed from a service name.
Ranked savings uses resource-type and opportunity-kind breakdowns. Selecting bars combines
filters; search also filters the breakdowns and totals. Clear filters resets the bar selections,
not the search field. Resource names expand to show full IDs and every original field.
Unknown savings remain unquantified, not zero-valued recommendations. Bar lengths show absolute
magnitude (including credits); signed USD labels remain authoritative. Counts refer to records,
not unique resources. These views total the displayed, cent-rounded USD records; they do not
deduplicate recommendations or claim that overlapping savings can all be realized.
Without JavaScript, the complete table and initial breakdowns remain available. Print includes
all resource rows regardless of interactive filters.

The inline [Microsoft Fluent System Icons](https://github.com/microsoft/fluentui-system-icons)
are MIT-licensed; their notice and source revision are included in `fluent-icons.svg` and the
generated report. `microsoft-product-icons.svg` separately contains official Azure, Cost
Management, subscriptions, Virtual Machine, Storage Accounts, SQL Database, App Services, and
SRE Agent artwork, with the original colors and proportions. Sources and SHA-256 fingerprints
are recorded in that asset. Product artwork is **not** MIT-licensed: it is included under the
repository owner's confirmed authorization, which does not grant downstream users a license.
Review the [Azure icon terms](https://learn.microsoft.com/en-us/azure/architecture/icons/) and
[Microsoft brand guidelines](https://www.microsoft.com/legal/intellectualproperty/trademarks)
before redistributing or reusing the artwork.

```python
from dashboard import build_dashboard_model, build_explorer_rows
from render import write_report

sources = {
    "overview": {
        "status": "ready",
        "source": "ARM MCP query_costs / PreTaxCostUSD",
        "refreshed_at": "2026-11-16T22:30:00Z",
        "window": ["2026-10-18", "2026-11-16"],
        "scope_summary": ["/subscriptions/example/resourceGroups/managed"],
        "data": overview_result,  # summarize_cost_overview output, not raw tool JSON
    },
    "containers": {
        "status": "unavailable",
        "source": "AKS cost allocation",
        "reason": "AKS allocation data is not enabled. Configure it separately with your cluster owner.",
    },
}
model = build_dashboard_model(
    sources,
    refreshed_at="2026-11-16T22:30:00Z",
    scope_summary=["/subscriptions/example/resourceGroups/managed"],
    scope_mode="managed",  # or setup-confirmed "everything" / "subscriptions"
    window=["2026-10-18", "2026-11-16"],
    scope_coverage=prepared["scope_coverage"],
    warnings=["Today's charges are still settling."],
)
write_report(model, "finops-dashboard.html")
```

`ready` means the source completed, not that there are no findings. `partial`, `unavailable` and
`unsupported` require a reason. Missing sources get explicit unavailable states, never healthy
zero totals. All ready/partial sources require a named source, timezone-aware timestamp,
inclusive source window, explicit scope summary and data. Scope summaries must match the dashboard's
effective scopes; missing pulls retain those scopes and use partial status plus coverage detail.
Source periods can differ (budgets follow their own native billing period); each appears in the
tab and coverage inventory. A reused source retains its old timestamp. Never wrap stale or
failed data with the current time to make it look fresh.

The dashboard builder does not discover or authorize Azure scope. Call `resolve_dashboard_scopes`
first, then the existing query planners/filters with that boundary. These are pure functions;
all envelopes must come from already validated, scope-filtered source data.

The setup-confirmed `subscriptions` mode is labeled **Selected subscriptions**. Its scope
summary contains the exact selected subscription ARM IDs, and covers all resources inside
those subscriptions, not just managed resources. Setup preselects the agent's hosting
subscription for confirmation and supports multiple subscriptions in the same tenant.
Runtime must verify the agent/connector's access to every selected subscription before
collecting data; the caller's subscription picker is not proof of agent access.

## Core inputs (unchanged helper outputs)

| Source key | Data |
|---|---|
| `overview` | `summarize_cost_overview` output |
| `budgets` | `evaluate_budgets` output |
| `rightsizing` | `recommend_rightsizing` output |
| `ai` | `attribute_ai_costs` output |
| `reliability` | `analyze_cost_vs_reliability` output |
| `optimization` | `summarize_optimization` output |
| `allocation` | `allocate_costs` output; includes ownership totals, unallocated, tag inventory, recommended coverage and untagged resources |
| `anomalies` | `detect_anomalies` list; dimension, value, current_usd, baseline_mean_usd, kind |
| `explorer` | `build_explorer_rows(prepared_resource_costs["included_rows"])`; Decimal aggregation from `cost` into `cost_usd`, retaining unknown resource charges and all six drilldown dimensions |

Use the existing pure analysis modules; do not invoke their separate SaveReport workflows or
override their standalone managed-scope rules. Aggregate resource IDs case-insensitively before
allocation/AI joins. Financial basis and all amounts in these cost sources are USD, except the
budget adapter, which preserves budget currency.

## Additional normalized row inputs

Every additional source's `data` is a list of objects with the fields below. All numeric fields
must be finite numbers (not bool, null, NaN, infinity or numeric strings). Unknown text is an
explicit `"(unknown)"`, not a guessed value. Missing required measurements must result in a
partial/unavailable source rather than zero. Raw provider normalization belongs in sandbox Python.

| Source key | Required fields per row |
|---|---|
| `billing` | `business_unit`, `basis`, `amount` (number), `currency`, `billing_period`, `methodology`. Keep actual/amortized/credits/tax as distinct bases. No implicit FX conversion or cross-basis total. |
| `commitments` | `benefit`, `kind`, `quantity_unit`, `used_quantity`, `purchased_quantity`, `covered_cost_usd`, `eligible_cost_usd`, `realized_savings_usd`, `unused_cost_usd`, `expires_on`, `recommendation`. Ratios are calculated per row using matching units/periods, never averaged. Zero denominators show unknown. Usage must be between zero and purchased; covered cost between zero and eligible. |
| `containers` | `cluster`, `namespace`, `workload`, `cost_usd`, `idle_usd`, `shared_usd`, `allocation_method`. Supply disjoint provider allocation buckets; do not add idle/shared cost twice. |
| `unit-economics` | `business_unit`, `unit`, `units`, `cost_usd`, `allocation_method`. Cloud cost per outcome is computed with decimal arithmetic; zero units shows unavailable, negative units is an error. Volume and allocated cost MUST have the same scope and time window. |
| `sustainability` | `scope`, `mtco2e`, `kwh`, `methodology`. Preserve source methodology and estimation caveats; do not invent energy for a carbon-only source. |
| `planning` | `scenario`, `baseline_usd`, `projected_usd`, `assumptions`, `term`, `breakeven`. Dollar delta is calculated; inputs must be actual supplied scenario estimates, not model-invented quotes. |
| `governance` | `control`, `owner`, `observed`, `target`, `status`, `action`. Explicit supplied policy findings / measured scorecards; not an invented proprietary efficiency score or industry percentile. |

`coverage` is generated automatically: all source states, timestamps, periods, scope counts and
capability boundaries. A partially collected dataset or partial underlying adapter remains
partial even if the envelope mistakenly says ready.

This is Azure reporting coverage, not Cloudability feature parity. There is no AWS/GCP/OCI/SaaS
ingestion, historical business-mapping rule engine, shared-cost redistribution engine, custom
commercial billing engine, external benchmark feed, drag/drop widget builder, or automated
optimization executor. Existing approved billing/allocation outputs can be reported, not invented.
See [the research and capability matrix](../../../../docs/CLOUDABILITY-COVERAGE.md).
