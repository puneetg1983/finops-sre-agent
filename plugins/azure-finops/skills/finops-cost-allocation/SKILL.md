---
name: finops-cost-allocation
description: Showback / cost allocation for Azure, read-only. Joins per-resource monthly cost (ARM MCP query_costs; Consumption UsageDetails as a disclosed fallback) with resource tags (Resource Graph) to attribute spend to an owner dimension (team / env / service / costCenter / app / owner), then surfaces the spend that has no owner — an explicit unallocated bucket, a ranked untagged-resource list, and tag-hygiene flags — using the bundled allocate.py with the runtime's sandbox Python tool. Use for "who owns this bill", showback/chargeback prep, and finding untagged spend.
---

## When to use this skill

Use it when the user wants to **attribute Azure spend to an owner** (team, environment, service,
cost center, app, or owner) — a showback breakdown, chargeback preparation, or a hunt for **untagged
spend that has no owner**. It answers "who/what is this bill for?" and "how much spend is
unattributable?" It is read-only and recommends tagging actions only; it never changes tags.

For cost *spikes* and their cause use `finops-cost-anomaly-detection`; for rightsizing / idle waste
use `finops-rightsizing-advisor`.

## Required access

- The **ARM MCP connector** with its read-only `*_query_costs` tool; its signed-in user needs **Cost
  Management Reader** on the managed scopes.
- **Reader** for Resource Graph tags, and **Cost Management Reader** for the agent identity when the
  UsageDetails fallback is used.
- Read-only `az` (`RunAzCliReadCommands`) and sandbox Python: use `ExecutePythonCode` when
  available, otherwise `RunInTerminal` with `python3`. No ARM MCP write tool or Azure write API is used.

## Scope

Attributes any resource that has cost in Cost Management — the allocation is
**service-agnostic** (it works off cost + tags, not a hard-coded resource list). It is also
**tag-generic**: it groups by whatever tag keys exist in your data (a **tag inventory**) and treats
`team / env / service / costCenter / app / owner` as a *recommended* set to report coverage against —
not a hard-coded filter. Shared or untaggable cost (session pools, networking, shared clusters) is
**never force-allocated**: cost with no value for the requested dimension is kept in an explicit
**unallocated** bucket. A resource with **no tags at all** is listed as **untagged** for follow-up
(having *some* tag but not the requested dimension is unallocated, not untagged). Cross-charging
exports and management-group rollups are out of scope here until needed.

## Procedure

### Step 0 — Resolve the managed boundary

First load `finops-managed-scope` and follow its `scope.py` procedure to dynamically GET and validate
the current agent `managedResources`; never reuse cached scope. Managed scopes and expanded descendants
are the default boundary. Scheduled runs are strict/fail-closed with no override. An interactive named
outside-scope target requires disclosure and explicit confirmation in a subsequent turn before any
broader query. Broad RBAC never silently expands scope.

Retrieve cost through the ARM MCP `query_costs` contract in `finops-managed-scope` (section 4):
query each effective scope independently (one planned query per scope, never a wider scope), track
truncation and completeness per scope, and let `prepare_cost_query` de-duplicate and filter
attributable resource ids against the boundary. Query Resource Graph once per unique effective
subscription, always using
`--subscriptions`; if only specific RGs are managed in that subscription, query each unique RG with
the exact case-insensitive predicate and client-side ARM-prefix filter below. De-duplicate overlapping
results and retain only in-bound resources. Preserve cost rows with no usable resource/scope identity as `unattributed` rather than
assigning them. Report included scopes, excluded rows/resources, unattributed cost, unsupported scopes,
and partial/failed coverage.

### Step 1 — Pull per-resource monthly cost

Request the trailing 30 days (inclusive UTC dates) as **totals per resource**. `query_costs` cannot
group by tag; tags come from Resource Graph in Step 2.

```python
from cost_query import plan_cost_queries, prepare_cost_query, split_cost_query, sum_cost_by
from cost_query import batch_cost_queries

queries = plan_cost_queries(managed_resources, management_group_expansions=mg_descendants,
                            start=START, end=END, as_of=TODAY, group_by="ResourceId")
# dispatch each batch_cost_queries(queries) batch concurrently; split any result whose
# properties.nextLink is not null with split_cost_query(arguments) and query both halves
prepared = prepare_cost_query(results, managed_resources,
                              management_group_expansions=mg_descendants, window=(START, END))
costs, shared_usd = sum_cost_by(prepared["included_rows"], "resourceId")   # {resourceId: monthly_usd}
```

`shared_usd` is in-scope cost with no resource id (purchases, support, shared charges): report it
next to the unallocated bucket; never assign it to an owner. Do not concatenate, de-duplicate, or
scope-filter rows in model reasoning.

**Fallback (only when `finops-managed-scope` section 4 requires it):** follow the canonical
Consumption UsageDetails transport contract in `finops-managed-scope` section 5 over the same window.
That projection must explicitly retain `tags`:

```
--query "{value: value[].{id: id, date: properties.date, cost: properties.costInUSD, subscriptionId: properties.subscriptionId, resourceGroup: properties.resourceGroup, resourceId: properties.instanceName || properties.resourceId, tags: tags}, nextLink: nextLink}"
```

Perform the page merge and managed-scope filtering with `finops-managed-scope`'s
`prepare_usage_details(...)`, then build `costs` with `sum_cost_by(included_rows, "resourceId")`.
Resource Graph in Step 2 remains the source for the current resource tag map used by
`allocate_costs`.

### Step 2 — Pull resource tags (Resource Graph)

```bash
az graph query \
  --subscriptions <EFFECTIVE_SUBSCRIPTION_ID> \
  -q "Resources | project id, tags" \
  --first 1000 \
  -o json
```

For an RG-only managed scope, use:

```bash
az graph query \
  --subscriptions <EFFECTIVE_SUBSCRIPTION_ID> \
  -q "Resources | where resourceGroup =~ '<EFFECTIVE_RESOURCE_GROUP>' | project id, tags" \
  --first 1000 \
  -o json
```

Then keep only ids whose normalized ARM prefix is exactly
`/subscriptions/<effective_subscription_id>/resourcegroups/<effective_resource_group>/`.
Paginate with `--skip-token` until empty (Resource Graph caps at 1000 rows/page), retaining the same
`--subscriptions`, RG predicate, and client-side filter on every page. Flatten to
`{resourceId: {tagKey: tagVal}}`. Keep the raw tag values as-is — the ranker normalizes case and
flags variant spellings itself.

### Step 3 — Allocate (bundled allocate.py, in-sandbox)

Read the module and run it — do **not** re-implement the logic in the prompt:

```
read_skill_file(skill_name="finops-cost-allocation", file_path="allocate.py")
```

```python
from allocate import allocate_costs
result = allocate_costs(
    costs=costs,               # from Step 1  {resourceId: monthly_usd}
    tags=tags,                 # from Step 2  {resourceId: {tagKey: tagVal}}
    dimension="team",          # or env / service / costCenter / app / owner
)
```

`allocate_costs` handles all grouping and governance:

- **groups**: spend per owner value for the requested `dimension`, ranked by dollars, with `pct` of
  total and `resource_count`. Values are grouped case-insensitively; the most costly raw spelling is
  shown.
- **unallocated**: cost of resources with **no value for the requested dimension** — kept as its own
  bucket (never spread onto tagged owners) with `monthly_usd`, `pct`, and the top resources.
- **untagged_resources** / **untagged_usd**: resources with **no tags at all** — the tagging backlog,
  ranked by cost.
- **tag_inventory**: every tag key present in the data with the `resource_count` and `cost_usd` it
  covers, ranked by cost — the "group by the ones we have" view (includes non-recommended keys).
- **recommended_coverage** / **missing_recommended**: for each recommended ownership key
  (`team / env / service / costCenter / app / owner`, overridable via `recommended_keys`), whether it
  is `present` and the cost/resource share it covers; `missing_recommended` lists the ones with no
  usage — an adopt-these-tags recommendation.
- **tag_hygiene**: owner values that collapse to the same group under normalization (e.g. `Prod` vs
  `production`) with the cost each variant carries — these otherwise split one owner's spend.

Run it once per dimension the user cares about (e.g. `team` and `env`).

### Step 4 — Report

Produce, for the chosen dimension: a **showback table** (owner, monthly $, % of total, resource
count) with the **total** and the **unallocated** row called out explicitly; a **tag inventory**
(every key in use, with cost + resource coverage); a **recommended-coverage** section (which of
`team / env / service / costCenter / app / owner` are present vs missing, with an adopt-the-missing
recommendation); an **untagged-spend** section (total $ + top resources to tag — resources with no
tags at all); and **tag-hygiene** warnings. Recommend tagging actions only — never modify tags. If
cost was partial (Step 1 truncation), say so at the top. If everything is allocated and there is no
untagged spend, say so in one line.
