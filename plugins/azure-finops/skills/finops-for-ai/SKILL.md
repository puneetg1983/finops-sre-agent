---
name: finops-for-ai
description: FinOps for Azure AI spend, read-only. Attributes AI cost per resource, model, Azure billing service, and meter from ARM MCP query_costs (Consumption UsageDetails as a disclosed fallback) — covering Azure OpenAI, Azure AI Foundry models and tools, SRE Agent units, Cognitive Services, and Machine Learning compute. Discovers costs using both ResourceType and ServiceName so Foundry Tools resources outside the Cognitive Services namespace are included. Keeps agent-service charges in the AI total but separate from model-token analysis, ranks top cost drivers, and emits read-only optimization hints using bundled attribute.py. Use for AI spend, agent costs, model breakdowns, and AI cost governance.
---

## When to use this skill

Use it when the user wants to understand **Azure AI spend** — "how much are we spending on Azure
OpenAI / AI Foundry", "break the AI bill down by model", "which AI resource costs the most", or AI
cost governance. It answers *where the AI money goes and which cost driver to look at first*. It is
read-only and recommends actions only; it never changes anything.

For cost *spikes* and their cause use `finops-cost-anomaly-detection`; for owner/team showback use
`finops-cost-allocation`; for VM/idle rightsizing use `finops-rightsizing-advisor`.

## Why this skill exists (AI billing taxonomy)

Azure AI spend is easy to **under-count** if the report selects resources only by provider
namespace. Classic **Azure OpenAI** and **Azure AI Foundry** model costs often bill under
`Microsoft.CognitiveServices`, but Foundry Tools can use other resource namespaces. For example,
Azure SRE Agent resources are `Microsoft.App/agents` and bill as ServiceName/MeterCategory
**Foundry Tools**, with a meter such as **SRE Agent Unit**. Foundry agent tool charges can also
appear under Foundry Tools, while model-token charges remain in their model billing service.

The discovery query uses both **`ResourceType`** and **`ServiceName`**. It includes the
`Microsoft.CognitiveServices` and `Microsoft.MachineLearningServices` namespaces, plus any
resource type associated with recognized AI billing services (including **Foundry Tools**).
The detail query retains ServiceName and MeterCategory, so the classifier can include Foundry and
SRE Agent costs even when `consumedService` is a different provider namespace. It never gates on
resource `kind`.

## Required access

- The **ARM MCP connector** with its read-only `*_query_costs` tool; its signed-in user needs **Cost
  Management Reader** on the managed scopes. The agent identity needs Cost Management Reader only for
  the UsageDetails fallback, and **Reader** for the optional Resource Graph `kind` lookup.
- Read-only `az` (`RunAzCliReadCommands`) and sandbox Python: use `ExecutePythonCode` when
  available, otherwise `RunInTerminal` with `python3`. No ARM MCP write tool or Azure write API is used.

## Scope

v1 attributes AI spend from **cost meter lines only** (30-day totals per resource and meter) — per **resource**, per **model** (parsed from
the token meter), per **service family**, and a **token vs compute** meter split. Per-**deployment**
attribution and $/1K-token efficiency need Azure Monitor token *metrics* (a separate GET) and are
**deferred to a v2**; v1 stops at per-resource + per-model. Optimization hints are cost-only and
advisory — true "idle endpoint" detection needs utilization metrics, so a compute-with-no-tokens
resource is flagged to **verify**, not asserted idle.

## Procedure

### Step 0 — Resolve the managed boundary

First load `finops-managed-scope` and follow its `scope.py` procedure to dynamically GET and validate
the current agent `managedResources`; never use cached scope. Managed scopes and their expanded
descendants are the default boundary. Scheduled runs are strict/fail-closed with no override. An
interactive named resource outside the boundary requires disclosure and explicit confirmation in a
subsequent turn before any broader query. Broad RBAC never expands scope implicitly.

Retrieve cost through the ARM MCP `query_costs` contract in `finops-managed-scope` (section 4):
query each effective scope independently (one planned query per scope, never a wider scope), track
truncation and completeness per scope, and let `prepare_cost_query` de-duplicate and filter resource
ids and attributable scope fields against the expanded boundary before AI classification. Run
Resource Graph once per unique effective subscription with `--subscriptions`; for RG-only managed
scopes, use the exact case-insensitive resource-group predicate and client-side ARM-prefix filter
below. De-duplicate overlapping results and retain only in-bound ids. Report included scopes,
excluded rows, unattributed AI cost, unsupported scopes, and partial/failed scope coverage.

### Step 1 — Pull AI cost line items

Use two small `query_costs` shapes over the trailing 30 days (inclusive UTC dates). Each shape is
planned, dispatched via `batch_cost_queries` from `cost_query` with section 4's bounded parallel
execution and throttling backoff, split on truncation, and prepared separately:

1. **Find the AI resource types.** Totals grouped by `ResourceType,ServiceName`:

   ```python
   from cost_query import plan_cost_queries, prepare_cost_query, split_cost_query
   from attribute import ai_resource_types

   type_queries = plan_cost_queries(managed_resources, management_group_expansions=mg_descendants,
                                    start=START, end=END, as_of=TODAY,
                                    group_by="ResourceType,ServiceName")
   discovery = prepare_cost_query(type_results, managed_resources,
                                  management_group_expansions=mg_descendants, window=(START, END))
   ai_types = ai_resource_types(discovery["included_rows"])
   ```

   `ai_resource_types` keeps every `Microsoft.CognitiveServices/*` and
   `Microsoft.MachineLearningServices/*` type and resource types associated with recognized AI
   ServiceNames, including `Microsoft.App/agents` when billed as Foundry Tools. It returns resource
   type spellings exactly as Cost Management provides them. Do **not** filter on `kind`. If `ai_types`
   is empty, there is no matching AI billing service in the checked scope: skip to Step 4.

2. **Pull AI meter lines.** Totals for only those types, with the fields the classifier needs:

   ```python
   ai_queries = plan_cost_queries(managed_resources, management_group_expansions=mg_descendants,
                                  start=START, end=END, as_of=TODAY,
                                  group_by="ResourceId,ServiceName,MeterCategory,MeterSubCategory,Meter",
                                  filter_dimension="ResourceType", filter_values=ai_types)
   prepared = prepare_cost_query(ai_results, managed_resources,
                                 management_group_expansions=mg_descendants, window=(START, END))
   ai_line_items = prepared["included_rows"]
   ```

   The connector permits at most five grouping dimensions. Each row carries `cost`, `resourceId`,
   `serviceName`, `meterCategory`, `meterSubCategory` and `meterName`. The classifier derives the
   provider namespace from the resource ID when `consumedService` is absent, preserving the
   Cognitive Services and Machine Learning fallback. Classify only `included_rows`; use the
   returned coverage/partial fields directly; do not merge, de-duplicate, or scope-filter rows in
   model reasoning.

**Fallback (only when `finops-managed-scope` section 4 requires it):** follow the canonical
Consumption UsageDetails transport contract in `finops-managed-scope` section 5 with this projection.
Pass the page envelopes through `prepare_usage_details(...)` first to enforce scope, completeness
and de-duplication, then retain only its included rows for which `is_ai_cost_item(row)` is true.
This keeps `Microsoft.CognitiveServices` and `Microsoft.MachineLearningServices` rows, plus rows
whose `serviceName` or `meterCategory` is a recognized AI billing label (case-insensitive),
including Foundry Tools:

```
--query "{value: value[].{id: id, date: properties.date, cost: properties.costInUSD, consumedService: properties.consumedService, serviceName: properties.serviceName, meterCategory: properties.meterCategory, meterSubCategory: properties.meterSubCategory, meterName: properties.meterName, subscriptionId: properties.subscriptionId, resourceGroup: properties.resourceGroup, resourceId: properties.instanceName || properties.resourceId}, nextLink: nextLink}"
```

For the fallback, use the bundled helper after scope preparation rather than reimplementing the
service-name and meter-category checks:

```python
from attribute import is_ai_cost_item
ai_line_items = [
    row for row in usage_prepared["included_rows"] if is_ai_cost_item(row)
]
```

### Step 2 (optional) — Pull resource `kind` (Resource Graph)

Only to label each resource OpenAI vs AIServices vs the ML kind in the report:

```bash
az graph query \
  --subscriptions <EFFECTIVE_SUBSCRIPTION_ID> \
  -q "Resources | where type =~ 'microsoft.cognitiveservices/accounts' or type startswith 'microsoft.machinelearningservices/' | project id, kind" \
  --first 1000 \
  -o json
```

For an RG-only managed scope, add
`| where resourceGroup =~ '<EFFECTIVE_RESOURCE_GROUP>'` before `project`, retaining the explicit
`--subscriptions` value. Also keep only ids whose normalized ARM prefix is exactly
`/subscriptions/<effective_subscription_id>/resourcegroups/<effective_resource_group>/`.
Retain the same subscription constraint, RG predicate, and client-side filter on every paginated
Resource Graph request.

Flatten to `{resourceId: kind}`. Omit this step and the report just leaves the kind label blank —
the attribution is identical either way.

### Step 3 — Attribute (bundled attribute.py, in-sandbox)

Read the module and run it — do **not** re-implement the logic in the prompt:

```
read_skill_file(skill_name="finops-for-ai", file_path="attribute.py")
```

```python
from attribute import attribute_ai_costs
result = attribute_ai_costs(
    line_items=ai_line_items,      # from Step 1 (already filtered to AI billing families)
    resource_kinds=resource_kinds, # optional, from Step 2 ({resourceId: kind})
)
```

`attribute_ai_costs` returns:

- **total_ai_usd**, **resource_count**, **model_count**, **as_of** (latest line-item date).
- **by_service_family** — Cognitive Services / OpenAI, Foundry Models, Foundry Tools, Machine
  Learning, and other supported AI billing families, ranked.
- **by_meter_type** — `model_token` (LLM/embedding tokens), `compute` (managed endpoints / training),
  `agent_service` (Foundry Tools and agent units), and `other_cognitive` (other AI transactions).
  Agent-service costs contribute to total AI spend but never to model-token rankings or token
  optimization hints.
- **by_resource** — per account/workspace, with `kind` (if provided), `service_family`, and its
  `top_model`.
- **by_model** — per model (model_token lines only), with `resource_count`.
- **top_drivers** — the top blended resource×model×meter-type lines by dollars.
- **hints** — light cost-only advisories: `model_concentration` (one model dominates),
  `commitment_opportunity` (high steady model spend → evaluate PTU), `compute_no_tokens_verify`
  (compute with no tokens → verify it isn't idle), `resource_sprawl` (many tiny AI resources).

### Step 4 — Report

Produce: a **headline** (total AI spend, # resources, # models, the model-token vs compute vs
agent-service split); a
**by-model** table and a **by-resource** table (with kind + top model); the **top cost drivers**; and
the **hints** as a "where to look" list. Never sum model-token and compute dollars into a single
"savings" number — they are different cost drivers. Label PTU/commitment and compute-no-tokens hints
as **estimates / verify first**. If cost was partial (Step 1 truncation), say so at the top. If there
is no AI spend, say so in one line.
