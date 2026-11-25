# Consolidated dashboard contract

The setup task saves **FinOps: Dashboard**, one daily-refreshed Live Report with 18 sections. The first, **Decision briefing**, is built by `briefing.py` (see below).
Keep `dashboard.py`, `models.py`, `render.py`, `theme.css`, `dashboard.css`, `briefing.py`, `fluent-icons.svg`, and `microsoft-product-icons.svg`
together. There are no third-party scripts, fonts, network calls or view-time Azure tools in
dashboard HTML. CSP nonces are substituted by SaveReport. Without JavaScript, all sections remain
readable; with it, tabs support keyboard navigation, the mobile section selector, table search and
print/PDF (which prints all sections when opened locally).
Live Reports embeds the report in an `allow-scripts` iframe: file downloads and print
dialogs are blocked by that viewer. CSV export therefore exposes all matching records
in a read-only text area with Select CSV text; copying does not require clipboard permissions.
Opened locally, the same button also downloads the CSV. Print / save PDF opens a complete
print view in the embedded viewer, with instructions to download HTML using the native Live
Reports toolbar and print that file locally. Return to dashboard preserves filters.

The report uses a compact portal-inspired shell, grouped navigation, a command bar, and an
inline scope disclosure. The overview's Top services is a ranked horizontal bar graph with
Azure service symbols and an expandable data table. The heading retains **FinOps: Dashboard**,
with **Powered by Azure SRE Agent** below it. The layout is portal-inspired, not a copy of the
authenticated SRE Agent portal or a claim of Microsoft endorsement.
The header's **Last refreshed** card also shows the installed plugin version and a
7-character build fingerprint. The fingerprint matches the revision `finops-update`
records, so an old build means the user should run **Update the FinOps setup** after
installing a newer plugin.
Overview ranks only the top 15 services. Its Browse every service button opens Cost
explorer's Service dropdown, including zero-cost and smaller categories present in saved
cost rows. A cost report is not resource inventory: web apps often share a plan's bill,
and existing resources without cost rows need not appear. Reported cost is not a SKU-price
estimate; do not invent charges to fill an inventory gap.

The header has explicit **Light** and **Dark** buttons. The initial theme follows the report
URL or the browser's color preference; either button switches the open report without a reload,
Azure call or browser storage. Without JavaScript, the static light stylesheet remains readable.

Dashboard charts use an Azure-style blue-led categorical palette and
[Microsoft Fluent charting categorical/semantic colors](https://github.com/microsoft/fluentui/blob/master/packages/charts/react-charting/src/utilities/colors.ts).
Palette tokens are scoped to the dashboard, including light/dark metric status colors;
common services retain the same color across Overview, Cost explorer and resource-type
breakdowns. Official product artwork keeps its original colors.

Charts draw in once when first visible during each dashboard opening: bars grow and trend lines
trace. Hidden tabs, inactive chart groupings and below-the-fold charts wait until
viewed. Filtering and revisiting a chart do not replay the entrance animation. Motion is skipped
for reduced-motion preferences, and printing cancels animation to show complete plots.
This is presentation only: it does not fetch fresh costs or change the saved snapshot.
Browsers without animation support, and no-JavaScript views, retain complete static charts.

The default is the saved snapshot's most recent **30 inclusive UTC days**. When the scheduled
90-day history is complete, the date control offers **Last 90 days** and a custom inclusive
date range within that saved window. It updates **Overview and Cost explorer only**; budgets,
recommendations and other sections keep their own source periods. There is no hourly view,
browser-side Azure query or live refresh. Today's costs can still be incomplete. Without
JavaScript, the original 30-day snapshot remains readable. Scope-coverage counts and costs
refer to the default 30-day window, even while another range is selected.

The scheduled task retrieves two independent Daily query shapes over the same 90-day boundary:
`ResourceGroupName,ServiceName` for Overview and
`ResourceId,ServiceName,ResourceLocation,ChargeType` for Cost explorer. It filters each to
the confirmed effective scope before inclusion. These overlapping shapes are never added together.
Retrieval starts with 21-day overview and 7-day resource slices; these initial spans can still
truncate and require splitting. `batch_cost_queries` interleaves shapes and schedules at most
four calls per batch, with two per subscription across all its resource groups. Dispatch each
batch concurrently, persist its results and then dispatch the next. Truncated leaves enqueue
two children; successful leaves are retained. Throttling pauses dispatch and reduces the
per-subscription bound to one for the rest of the run. These are tool-call instructions and
a deterministic planner, not a new Azure client or a guarantee of faster server responses.
`slice_daily_cost_query(prepared, [start, end])` creates the 30-day rows and default-period
coverage for the existing analyses. Where the source cannot attribute coverage counts to a
specific query scope, the counts are shown as unavailable rather than copied from 90 days.
The history is disabled with an explicit reason if either retrieval is incomplete or cannot
fit in the Live Report's 10 MB limit; it is never silently truncated.

```python
history_window = ["2026-08-19", "2026-11-16"]
cost_history = {
    "window": history_window,
    "overview": {**sources["overview"], "window": history_window,
                 "data": prepared_daily_overview["included_rows"]},
    "explorer": {**sources["explorer"], "window": history_window,
                 "data": prepared_daily_resource["included_rows"]},
}
# The two source envelopes above must describe the 30-day slice of these same rows.
model = build_dashboard_model(
    sources, refreshed_at="2026-11-16T22:30:00Z",
    scope_summary=["/subscriptions/example/resourceGroups/managed"],
    window=["2026-10-18", "2026-11-16"], cost_history=cost_history,
)
# Or use cost_history={"status": "unavailable", "reason": "History query was incomplete."}.
```

History envelopes must have the same source, status, timestamp and scope summary as their
corresponding 30-day source. Each daily row requires a valid UTC `date` and finite `cost`;
the explorer also retains resource, subscription, resource group, service, region and charge
type. The builder checks that the default history slice matches the supplied totals, daily
points, breakdowns and resource rows. Financial aggregation preserves decimal precision and
signed credits; displayed resource breakdowns sum cent-rounded records.

Cost explorer uses one full-width column chart above a compact resource table. **Service**
and **Region** controls switch the grouping without changing filters or totals. Labels sit
directly below the columns; full names, amounts and record counts are available on hover and
in accessible button labels. There is no pie, separate legend or expandable category list.
Each breakdown shows ten leading categories plus **Other** when there are more than eleven.
Other retains the exact net amount and record count. Selecting it filters to its actual
members and replaces the same chart with their breakdown; repeat to reach the smaller tail.
**Back to broader view** reverses one level, and Clear filters resets all drill levels.
Negative amounts plot below the shared zero baseline; Other combines signed amounts, so a
net positive Other may contain individual credits visible when drilled into. Empty, zero and
unquantified amounts do not invent axis values or bar heights. Smaller chart containers use
horizontal signed bars with the same data, rather than squeezed columns or internal scrollbars.
Compact horizontal rows align the name, bar and amount on one line; only narrow mobile
containers move the bar below the label. Desktop plots use a shorter shared axis and tighter padding.
Colors stay stable while filtering.
At the top of Cost explorer, compact Service, Subscription, Resource type and Region dropdowns default to
**All** and show `(1 selected)`, `(2 selected)`, etc. for explicit choices. They support multiple
checkbox selections: choices within a dropdown are combined with OR; different dropdowns combine
with AND. Each dropdown's All button clears just that dimension; removing its last explicit
selection also restores All. Escape or clicking outside closes the dropdown. Column clicks
narrow the plotted dimension to one choice; selecting it again restores its broader drill scope.
Resource type is derived from the ARM resource ID (including nested and extension resources);
unidentifiable types remain `(unknown)`, not guessed from a service name.
Ranked savings uses resource-type and opportunity-kind breakdowns. Selecting bars combines
filters; search also filters the breakdowns and totals. Clear filters resets the bar selections,
dropdowns, drill levels and search field. The toolbar's Clear filters resets all sections.
Changing filters or dates closes any CSV preview so stale exports cannot appear current.
Resource names expand a full-width details panel with the full ID and
every original field. Filtering hides the corresponding details with each resource row.
Unknown savings remain unquantified, not zero-valued recommendations. Bars share a signed
USD scale; labels remain authoritative. Counts refer to records,
not unique resources. These views total the displayed, cent-rounded USD records; they do not
deduplicate recommendations or claim that overlapping savings can all be realized.
Without JavaScript, the complete table and initial breakdowns remain available. Print includes
all resource rows regardless of interactive filters.

AI spend's resource table separates subscription ID, resource group, resource type and name.
Expand a name for the full resource ID; it remains searchable. Missing identity components
are shown as `(unknown)` rather than inferred from the billed service.

Resource lists default to the **top 10 matching records**, with 25 and 50 available.
Cost and savings ranking uses the displayed signed amount, with unquantified savings last.
Search, filters, charts and totals always use all matching records, including those outside
the preview. **Download CSV · all matching** exports that entire set, with full resource IDs
and original detail fields, for the selected cost dates. It never exports only the top preview.
Amounts use the displayed cent precision, not raw sub-cent billing precision.
The UTF-8 CSV quotes cells and neutralizes spreadsheet formulas in untrusted text while
preserving numeric credits and unknown values. Downloads are browser-local and require the
report host to permit downloads; full print/PDF remains available. No-JavaScript views and
printing retain the full saved table. The existing 10 MB Live Report limit still applies to
the complete embedded data: a short preview is not a license to truncate the source dataset.

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
        "availability": "setup_required",
        "source": "AKS cost allocation",
        "reason": "AKS clusters were found, but allocation data is not enabled.",
        "next_step": "Configure cost allocation separately with your cluster owner, then refresh.",
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

## Decision briefing

`build_dashboard_model(..., briefing=inputs)` calls `briefing.build_briefing` and inserts the
**Decision briefing** tab first. Without `briefing`, the tab shows **Not collected**. Accepted keys
(any other key is rejected); each may be a list/dict when its check ran, `{"status": "unavailable",
"reason": ...}` when it failed, or omitted when it was not attempted:

- `anomalies`: `detect_anomalies` output (`dimension`, `value`, `date`, `kind`, `current_usd`,
  `baseline_mean_usd`, `impact_usd`). Drops are skipped.
- `change_events`: `{time, resourceId, kind, summary, actor?}`; `kind` is one of scale, sku, create,
  deployment, config, commit, incident. Events from 2 days before a spike through the spike day are
  matched by resource ID, resource group, or service. A matched scale/SKU/create change is High
  confidence, any other precise match Medium, otherwise Low ("likely usage growth").
- `owners`: `{scope, owner, source}`; the longest matching scope prefix wins, else **Unassigned**.
- `rightsizing`: `recommend_rightsizing` recommendations, de-duplicated by resource ID. `idle`
  items are cleanup decisions; `advisor` items are tier/size decisions. `validated: false` savings
  are shown but excluded from totals.
- `tag_gaps`: `{resourceId, monthly_usd, missing, proposed?, basis?}`.
- `agent_cost`: `{period_usd, window, source, runs?}`, normalized to a 30-day month. Per-run rows
  are shown only when supplied.
- `repo`: `owner/name`; tier actions then open a pull request there.

Findings have stable `F-xxxxxxxx` IDs, are ranked by impact and capped at 25 (the rest are counted).
Actions are copy-to-chat prompts; any prompt that would change something ends with an explicit
request to show the change and wait for approval. The renderer never runs actions itself.
## Core inputs (unchanged helper outputs)

### Explain missing and empty sources

Keep the internal `status` contract (`ready`, `partial`, `unavailable`, `unsupported`).
For non-data sources, supply an optional `availability` classification plus a precise `reason`:

| `availability` | Report label | Evidence and action |
|---|---|---|
| `access_required` | Access needed | A permission denial for the agent's actual read identity; `next_step` names the least-privilege read access and scope. |
| `setup_required` | Setup needed | A verified disabled feature, missing business inputs or unconnected read source; `next_step` explains how to configure it. A missing tool is not a permission denial. |
| `not_applicable` | Not in scope | A complete successful scoped inventory found no relevant resources. Include its `source`, `refreshed_at`, `window`, matching `scope_summary`, and a reason stating what was checked. Never infer absence from no cost records or failed/incomplete reads. |
| `read_failed` | Read failed | Retrieval failed or could not be validated; `next_step` explains the retry or repair needed. |

Non-data states must omit `data`. `next_step` is required except for `not_applicable`.
Unclassified failures display "Data not available"; omitted sources display "Not collected".
All labels, reasons, next steps and provenance appear in the section and Data & scope.
Do not claim subscription-wide absence when the report covers only selected resource groups.

Complete successful empty row sources remain `ready` and display "No matching records",
not "resources do not exist". Complete budget retrieval with `evaluate_budgets`' `no_budgets=true`
displays "No budgets configured", without healthy-zero metrics, and explains how to create a budget.
Failed or partially collected budget data must never use that state.

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
| `sustainability` | `scope`, `mtco2e`, `methodology`; optional `kwh` may be omitted or null. Carbon-only sources are supported as ready: missing energy displays "Not supplied by source", not zero. Normalize documented provider emissions units to metric tonnes CO2 equivalent. Preserve the actual source period, methodology and estimation caveats; do not prorate subscription-only emissions to managed resource groups. |
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
