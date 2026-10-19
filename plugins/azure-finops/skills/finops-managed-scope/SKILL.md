---
name: finops-managed-scope
description: Read-only scope guard for FinOps skills. Dynamically reads the Azure SRE Agent managedResources configuration, canonicalizes subscription/resource-group/management-group scopes, expands management groups with caller-supplied descendant data, filters UsageDetails rows, reports exact coverage, and applies fail-closed scheduled or explicitly confirmed interactive policy using bundled dependency-free scope.py.
---

## Purpose

Use this foundational skill before a FinOps skill retrieves or reports Azure cost. It
prevents a scheduled task from silently querying beyond the Azure SRE Agent's current
managed resources and makes broader interactive requests visible to the user.
Broad Azure RBAC is not evidence that a scope is managed: managedResources remains the
policy boundary even when the agent identity can read more.

This skill is read-only. It introduces no runtime changes and uses no write tools. The
bundled `scope.py` is pure offline logic and makes **no Azure calls**. Keep the whole
skill directory together: `scope.py` loads its dependency-free `_scope_resolution.py`
sibling relative to `__file__`, so direct dynamic imports do not depend on `sys.path`.

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

## 4. Filter UsageDetails before analysis

Consumption UsageDetails uses a **subscription-scoped transport endpoint**. For a
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
