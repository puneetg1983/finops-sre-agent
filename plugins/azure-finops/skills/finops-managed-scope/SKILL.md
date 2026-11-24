---
name: finops-managed-scope
description: Read-only scope guard and cost-retrieval contract for FinOps skills. Dynamically reads the Azure SRE Agent managedResources configuration, canonicalizes subscription/resource-group/management-group scopes, expands management groups with caller-supplied descendant data, plans and validates ARM MCP query_costs calls per managed scope (cost_query.py), filters cost rows, reports exact coverage, and applies fail-closed scheduled or explicitly confirmed interactive policy. Consumption UsageDetails remains a disclosed fallback.
---

## Purpose

Use this foundational skill before a FinOps skill retrieves or reports Azure cost. It
prevents a scheduled task from silently querying beyond the Azure SRE Agent's current
managed resources and makes broader interactive requests visible to the user.
Broad Azure RBAC is not evidence that a scope is managed: managedResources remains the
policy boundary even when the agent identity can read more.

**Dashboard-only scope contract:** `finops-setup` MUST obtain an explicit **Managed resources**,
**Everything**, or **Selected subscriptions** choice. Selected subscriptions allows one or multiple
user-confirmed subscriptions, with the agent's hosting subscription preselected when selectable.
The consolidated dashboard task persists that choice in its prompt.
Only that task may call `resolve_dashboard_scopes(managed_resources, policy,
management_group_expansions=complete_descendants, tenant_id=current_agent_tenant_id)`.
Managed mode reads live managedResources. Everything requires one explicitly confirmed tenant
root management group matching the agent's tenant, expanded completely on every run. Empty,
missing or incomplete hierarchy discovery fails closed, never falling back to visible subscriptions.
Selected subscriptions uses `{"mode": "subscriptions", "scopes": ["/subscriptions/<guid>", ...]}`.
It covers all resources in exactly those subscriptions, not merely managed resources and not future
tenant subscriptions. On every run additionally pass `subscription_inventory` (fresh Enabled
subscription metadata with `id` or `subscriptionId`, `tenantId`, `state`), `verified_agent_scopes`
and `verified_cost_scopes` to `resolve_dashboard_scopes`. Verify every selected subscription
against the current agent tenant, the exact agent principal's inherited cost-read roles, and a
successful per-subscription cost probe using the active connector identity (or the explicitly
disclosed UsageDetails fallback under the agent identity). Only successful independent checks
populate these scope lists. Caller-visible `az account list` choices do not establish agent or
connector access. Missing, invalid, foreign-tenant, disabled or incompletely authorized selections
fail closed before analysis/report save: never silently remove failed subscriptions.
Use the returned effective scopes as the boundary passed to pure planners/filters and analysis
helpers. It does not update managedResources or change `decide_scope_policy` for standalone
skills, subagents or other scheduled tasks. Those still accept no scheduled override.

This skill is read-only. It introduces no runtime changes and uses no write tools. The
bundled `scope.py`, `cost_query.py`, and `usage.py` are pure offline logic and make **no
Azure calls**. Keep the whole
skill directory together: `scope.py` loads its dependency-free `_scope_resolution.py`
sibling relative to `__file__`, so direct dynamic imports do not depend on `sys.path`.
`cost_query.py` and `usage.py` import `scope` by name, so add this skill directory to
`sys.path` before importing them.

## 1. Read the live managed scope every run

Do not cache, copy from the manifest, or infer scope from prior conversations. Read the
current agent resource dynamically:

```bash
az resource show --ids <AGENT_RESOURCE_ID> \
  --query properties.knowledgeGraphConfiguration.managedResources -o json
```

Pass the returned array to `resolve_managed_scopes`. Entries may be scope strings or
objects with `id`, `resourceId`, or `scope`. Only subscription, resource-group, and
management-group scope IDs are accepted. Duplicate casing is handled
case-insensitively and diagnosed.

If `AGENT_RESOURCE_ID` is unavailable, the read fails, the result is malformed/empty,
or required expansion cannot be completed, a scheduled run must stop **fail closed**.

## 2. Discover management-group descendants when needed

Management-group membership is not encoded in a resource ID. For each configured
management group, use read-only Azure CLI discovery to collect descendant subscriptions
and child management groups, following child groups until all descendant subscriptions
are known. For example:

```bash
az account management-group subscription show-sub-under-mg \
  --name <MANAGEMENT_GROUP_ID> -o json
```

That command returns direct subscriptions only. Build the complete recursive descendant
set using either:

- Azure Resource Graph subscription-container data and each subscription's
  `properties.managementGroupAncestorsChain`, selecting subscriptions whose ancestry
  contains the configured management group; or
- `az account management-group show --name <MANAGEMENT_GROUP_ID> --expand --recurse
  -o json`, recursively walking its returned child management groups and subscriptions.

The caller must supply **all descendant subscriptions**, not only direct children. Pass
the resulting mapping/records as `management_group_expansions`; `scope.py` handles
recursive expansion, overlap with explicit subscriptions/RGs, duplicate casing, and
cycles. It never performs discovery itself.

## 3. Resolve and enforce

Read and run the helper instead of reimplementing it:

```python
from scope import (
    decide_scope_policy,
    filter_usage_details,
    resolve_managed_scopes,
)

resolved = resolve_managed_scopes(
    managed_resources,
    management_group_expansions=mg_descendants,
)
decision = decide_scope_policy(
    requested_scopes,
    managed_resources,
    mode="scheduled",  # or "interactive"
    management_group_expansions=mg_descendants,
)
```

- **Scheduled:** strict and fail closed. Never continue for an outside scope, malformed
  or empty managed scope, or an unexpanded management group. Scheduled mode accepts no
  override; user confirmation cannot broaden it.
- **Interactive:** continue directly only when all requested scopes/resources are
  contained. A broader/outside request returns
  `outside_scope_confirmation_required`; show the exact `outside_scopes` and obtain an
  explicit user confirmation together with the returned `confirmation_key`. Re-evaluate
  in a subsequent turn with both `outside_scope_confirmed=True` and
  `confirmation_key=<DISPLAYED_KEY>`. Missing or mismatched keys remain confirmation
  required and must show the newly computed request/key. Silence, ambiguity, or a
  declined confirmation is denial.

The confirmation key binds approval to the exact displayed request. A key displayed for
request B cannot authorize request C. Confirmation does not alter managedResources or
become a standing permission.

## 4. Retrieve cost with ARM MCP `query_costs` (primary)

Cost data comes from the **Azure Resource Manager (ARM) MCP server** connector declared in
this plugin's `.mcp.json` (`https://mcp.management.azure.com`). Its tools are registered as
`<connection>_<tool>`, for example `azure-resource-manager_query_costs`. Use the tool whose
name ends in `_query_costs`; do not depend on the connector name.

**Availability check (every run).** If no `*_query_costs` tool is available, or a call
fails with an authentication/consent error, do not silently switch sources:

- Interactive: tell the user ARM MCP is not configured or its sign-in expired, give the
  setup path (**Plugins → azure-finops → Connectors → Configure**, authentication
  **OAuth**, scope `api://22bfbae3-f4e7-485f-be43-8cee15065084/.default`), then continue
  with the section 5 fallback only if they agree.
- Scheduled: use the section 5 fallback and add the warning
  `ARM MCP query_costs unavailable; used Consumption UsageDetails fallback` to every output.

Only ARM MCP **read** tools may be called: `query_costs`, `forecast_costs`,
`list_budgets`, `get_budget`, `list_dimensions`, `list_alerts`, `query_aks_costs`,
`get_retail_prices`, `list_benefit_utilization`, `get_benefit_recommendations`, and
`list_reservation_transactions`. Never call `create_budget`, `create_or_update_resource`,
`create_or_update_resource_group`, `create_deployment`, `cancel_deployment`,
`whatif_deployment`, `execute_resource_action`, or `start_pricesheet_download`.

Plan, validate, and filter with the bundled `cost_query.py` instead of reasoning over raw
rows:

```python
from cost_query import plan_cost_queries, prepare_cost_query, split_cost_query

queries = plan_cost_queries(
    managed_resources,
    management_group_expansions=mg_descendants,
    start="2026-10-12", end="2026-11-10",          # inclusive UTC dates
    granularity="Daily",                            # omit for totals over the window
    group_by="ResourceGroupName,ServiceName",       # at most 5 dimensions; tags unsupported
)
# Combine pending queries from independent shapes before scheduling.
from cost_query import batch_cost_queries
batches = batch_cost_queries(queries)            # <=4 calls total, <=2 per subscription
```

- Call `query_costs` once per planned argument object, **exactly as returned** (it pins
  `metric=PreTaxCostUSD`, explicit `from`/`to`, `top=5000`, and one effective scope).
  Never widen `scope`; management groups are already expanded to their subscriptions, and
  resource-group scopes are queried directly.
- Persist each call as `{"arguments": <the arguments>, "result": <raw tool output>}`. When
  the runtime saves a large result to a file, load that file in Python; do not copy rows
  through the conversation.
- **Parallel execution:** use `batch_cost_queries` on the entire pending queue, including
  independent shapes and scopes. Dispatch ALL calls in one batch concurrently in the
  same tool-call turn; wait for those calls to finish before dispatching the next batch.
  Do not run separate queues per shape or spawn agents to fetch slices. The default
  limits are four in-flight calls overall and two per subscription, shared by its
  resource groups. Never exceed these bounds. If the tool host cannot execute concurrent
  calls, disclose that limitation; do not claim the sequential fallback is parallel.
- **Truncation:** a result whose `properties.nextLink` is not null was cut off at `top` rows
  and `query_costs` cannot page. Discard it, call `split_cost_query(arguments)`, and query
  both halves in later parallel batches with the other pending work. Keep completed
  leaves; never re-query them or combine a truncated parent with its children.
  If a single day still truncates, reduce `groupBy` or add a filter only when the
  analysis permits a new shape; otherwise report incomplete coverage.
- **Lookback:** `query_costs` only reaches back 92 days (pass `as_of=<today UTC>` to
  `plan_cost_queries` to enforce it). Longer histories use the section 5 fallback.
- **Request only the shape the analysis needs.** Daily × `ResourceId` × meter queries over
  30 days can need several split calls even for one subscription; prefer totals
  (no granularity) or coarser dimensions where the skill allows it.
- **Avoid oversized probes:** for Daily histories use `plan_cost_queries(...,
  slice_days=<positive integer>)` to start with bounded contiguous slices. The dashboard
  uses 21-day overview and 7-day resource slices. These are initial spans, not guarantees
  that 5,000 rows will fit; split any truncated leaf again.
- **Throttling:** on HTTP 429 or a throttling message, stop launching new batches.
  Let already dispatched reads settle and persist their successful results. Wait for the
  indicated retry time (otherwise 60 s, doubling, at most 3 retries per failed call).
  Re-batch only failed and not-yet-started calls with `max_per_subscription=1` for the
  rest of the run. Do not immediately fan out retries or restart successful queries.
  Cost data refreshes about every 4 hours, so reuse completed reads within one run.

```python
from cost_query import prepare_cost_query

coverage = prepare_cost_query(
    results,                                   # the persisted {"arguments", "result"} list
    managed_resources,
    management_group_expansions=mg_descendants,
    window=("2026-10-12", "2026-11-10"),
)
```

`prepare_cost_query` fails closed on a queried scope outside the managed boundary, a
non-USD metric, a tool error, mixed query shapes for one scope, overlapping slices,
truncated results, date gaps, and unqueried scopes. Set `require_complete=False` only after
retries and splitting fail; then disclose `partial`, `warnings`, `truncated_slices`,
`date_gaps`, and `missing_scopes` on every downstream total. For an interactive outside
scope confirmed under section 3, pass it in both `expected_scopes` and `confirmed_scopes`.

Rows are flattened to the same keys the FinOps helpers already consume: `date` (daily
granularity), `cost` (USD), `resourceId`, `resourceGroup` (derived from the resource ID or
RG scope when not grouped), `subscriptionId`, `serviceName`, `meterCategory`,
`meterSubCategory`, `meterName`, `resourceType`, `resourceLocation`, and `chargeType`.
`query_costs` cannot group by tag; join resource IDs to Resource Graph tags when needed.
Pass `coverage["included_rows"]` to analysis helpers and `coverage["scope_coverage"]` to
report models. ARM MCP reads run as the user who signed in to the connector; that user's
Azure RBAC and Cost Management access still bound the result.

## 5. Fallback: Consumption UsageDetails

Use this section only when section 4 says to fall back. Consumption UsageDetails uses a
**subscription-scoped transport endpoint**. For a
subscription effective scope, query that subscription directly. For one or more RG-only
effective scopes, query each distinct containing subscription once, paginate the complete
result, and then apply `filter_usage_details` to retain only rows inside the exact managed
resource groups. Never construct
`/resourceGroups/<rg>/providers/Microsoft.Consumption/usageDetails`; that endpoint is not
supported. Querying the containing subscription is transport only and does not broaden the
logical analysis boundary.

Use this canonical UsageDetails transport contract in every consuming FinOps skill:

- Use GET on `/subscriptions/<SUB_ID>/providers/Microsoft.Consumption/usageDetails` with
  `metric=ActualCost`, starting at `\$top=1000`; never use the POST Cost Management Query API.
- Project the response envelope as `{value: value[].{...}, nextLink: nextLink}`. Keep only the
  fields needed by scope enforcement and the analysis, alias `cost` once from
  `properties.costInUSD`, and resolve the analysis `resourceId` from
  `properties.instanceName || properties.resourceId`. Include `subscriptionId` and
  `resourceGroup` whenever rows may need metadata-based attribution.
- If any request returns 413, retry that request with `\$top=100`, then `20`. Use verified,
  half-open `usageStart` date slices only if bounded pages still fail, and de-duplicate the
  combined rows.
- If retrieval remains incomplete, retain the available rows only with an explicit partial-cost
  warning on every downstream total.

Do not add a `properties/resourceGroup eq ...` filter to the subscription UsageDetails request.
The service may silently ignore that filter. Fetch the initial page once, persist it, and follow
only that response's `nextLink` chain to completion; do not mix pages from independently restarted
requests because ordering and newly settled usage can differ between calls. Decode `&amp;` in a
returned `nextLink`, then GET it as-is without re-adding initial query parameters. Apply the exact
case-insensitive RG boundary with `filter_usage_details` only after the complete chain is assembled.

Persist every projected response page with the exact URL used for that request:
`{"request_url": "...", "value": [...], "nextLink": "..."}`. Then use `usage.py` so code,
not the model, validates chain continuity and combines the pages:

```python
from usage import prepare_usage_details

coverage = prepare_usage_details(
    usage_pages,
    managed_resources,
    management_group_expansions=mg_descendants,
)
```

`prepare_usage_details` rejects restarted/mixed chains, requires the final `nextLink` to be
retrieved, merges all rows, and calls `filter_usage_details`. Set `require_complete=False` only
after all bounded-page and date-slice fallbacks fail; the returned `partial` flag and
`remaining_next_link` must then be disclosed on every downstream total.

The filtering core prefers UsageDetails `instanceName` (the modern full ARM ID), falls back to
`resourceId`, then to resource-group/subscription metadata. It removes duplicate rows,
keeps subscription-level shared charges only when the subscription itself is managed,
and separates included, excluded, and unattributed rows. Costs and totals are
`Decimal`; do not convert them to binary floats before reconciliation.

Report `included_count`, `excluded_count`, `unattributed_count`, `duplicate_count`,
their exact cost totals, `attribution_coverage_pct`, and deterministic diagnostics.
Never hide excluded or unattributed spend, and never assign an unattributed/shared
charge to an RG by guesswork.

Retrieve analysis data for each effective scope independently, de-duplicate the merged
rows, then apply this filter as a defensive check. Include excluded and unattributed
coverage in the final report; do not broaden a query merely because RBAC permits it.
