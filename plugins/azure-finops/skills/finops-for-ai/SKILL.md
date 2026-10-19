---
name: finops-for-ai
description: FinOps for Azure AI spend, read-only. Attributes Azure AI cost per resource, model, and service family from the modern Consumption UsageDetails pull — deliberately covering BOTH classic Azure OpenAI (kind=OpenAI) and Azure AI Foundry (kind=AIServices) accounts (both bill under Microsoft.CognitiveServices) PLUS Microsoft.MachineLearningServices (Foundry hub/project compute, managed online endpoints, fine-tuning), so Foundry-hosted model spend is never dropped. Splits token/model-meter spend from compute-meter spend, ranks the top cost drivers, and emits light read-only optimization hints, using the bundled attribute.py with the runtime's sandbox Python tool. Use for "what is our AI/OpenAI bill", per-model cost breakdown, and AI cost governance.
---

## When to use this skill

Use it when the user wants to understand **Azure AI spend** — "how much are we spending on Azure
OpenAI / AI Foundry", "break the AI bill down by model", "which AI resource costs the most", or AI
cost governance. It answers *where the AI money goes and which cost driver to look at first*. It is
read-only and recommends actions only; it never changes anything.

For cost *spikes* and their cause use `finops-cost-anomaly-detection`; for owner/team showback use
`finops-cost-allocation`; for VM/idle rightsizing use `finops-rightsizing-advisor`.

## Why this skill exists (AI resource taxonomy)

Azure AI spend is easy to **under-count**. Classic **Azure OpenAI** accounts are
`Microsoft.CognitiveServices/accounts` with `kind=OpenAI`; **Azure AI Foundry** (the unified
resource, formerly AI Services) is the **same** resource type with `kind=AIServices`. An OpenAI
model deployed **inside** a Foundry account bills under `Microsoft.CognitiveServices` with the same
token meter names but the account `kind` is `AIServices`. So filtering on `kind==OpenAI` or a meter
category literally named "Azure OpenAI" **drops Foundry-hosted model spend**.

This skill therefore keys off **`consumedService`** (both kinds roll up under
`Microsoft.CognitiveServices`) and never gates on kind, and it also includes
**`Microsoft.MachineLearningServices`** so Foundry hub compute / managed endpoints / fine-tuning
aren't dropped. See the pack README's "AI resource taxonomy" note.

## Required access

- **Cost Management Reader** on the subscription (`costInUSD` is null without it). **Reader** for the
  optional Resource Graph `kind` lookup.
- Read-only `az` (`RunAzCliReadCommands`) and sandbox Python: use `ExecutePythonCode` when
  available, otherwise `RunInTerminal` with `python3`. No POST/write APIs are used.

## Scope

v1 attributes AI spend from **cost line items only** — per **resource**, per **model** (parsed from
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

Pull UsageDetails independently for each effective scope where supported, with separate pagination
state and completeness tracking. De-duplicate overlapping line items, then filter resource ids and
attributable scope fields against the expanded boundary before AI classification. Run Resource Graph
once per unique effective subscription with `--subscriptions`; for RG-only managed scopes, use the
exact case-insensitive resource-group predicate and client-side ARM-prefix filter below. De-duplicate
overlapping results and retain only in-bound ids. Report included scopes, excluded rows,
unattributed AI cost, unsupported scopes, and partial/failed scope coverage.

### Step 1 — Pull AI cost line items

Follow the canonical Consumption UsageDetails transport contract in `finops-managed-scope`. Two
details are specific to this skill:

1. **Project the extra fields** the classifier needs — add `consumedService`, `meterSubCategory`,
   and `meterName` on top of the usual cost fields:

   ```
   --query "{value: value[].{id: id, date: properties.date, cost: properties.costInUSD, consumedService: properties.consumedService, meterCategory: properties.meterCategory, meterSubCategory: properties.meterSubCategory, meterName: properties.meterName, subscriptionId: properties.subscriptionId, resourceGroup: properties.resourceGroup, resourceId: properties.instanceName || properties.resourceId}, nextLink: nextLink}"
   ```

   In modern billing the full ARM resource id is in `properties.instanceName`; the projection falls
   back to `properties.resourceId`.

2. **Keep only the AI service families.** After flattening, retain rows whose `consumedService` is
   `Microsoft.CognitiveServices` or `Microsoft.MachineLearningServices` (case-insensitive). Do **not**
   filter on `kind` or on a meter category — that is the whole point (Foundry `AIServices` accounts
   would be dropped). Aggregate over the trailing ~30 days.

Before AI classification, pass the persisted page envelopes through `finops-managed-scope`'s
`prepare_usage_details(...)` and classify only `included_rows`. Use the returned coverage/partial
fields directly; do not merge, de-duplicate, or scope-filter pages in model reasoning.

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
    line_items=ai_line_items,      # from Step 1 (already filtered to the AI families)
    resource_kinds=resource_kinds, # optional, from Step 2 ({resourceId: kind})
)
```

`attribute_ai_costs` returns:

- **total_ai_usd**, **resource_count**, **model_count**, **as_of** (latest line-item date).
- **by_service_family** — Cognitive Services / OpenAI vs Machine Learning, ranked.
- **by_meter_type** — `model_token` (LLM/embedding tokens), `compute` (managed endpoints / training),
  `other_cognitive` (Speech/Vision/Language transactions) — the key "what kind of AI spend" split.
- **by_resource** — per account/workspace, with `kind` (if provided), `service_family`, and its
  `top_model`.
- **by_model** — per model (model_token lines only), with `resource_count`.
- **top_drivers** — the top blended resource×model×meter-type lines by dollars.
- **hints** — light cost-only advisories: `model_concentration` (one model dominates),
  `commitment_opportunity` (high steady model spend → evaluate PTU), `compute_no_tokens_verify`
  (compute with no tokens → verify it isn't idle), `resource_sprawl` (many tiny AI resources).

### Step 4 — Report

Produce: a **headline** (total AI spend, # resources, # models, the model-token vs compute split); a
**by-model** table and a **by-resource** table (with kind + top model); the **top cost drivers**; and
the **hints** as a "where to look" list. Never sum model-token and compute dollars into a single
"savings" number — they are different cost drivers. Label PTU/commitment and compute-no-tokens hints
as **estimates / verify first**. If cost was partial (Step 1 truncation), say so at the top. If there
is no AI spend, say so in one line.
