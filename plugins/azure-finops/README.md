# azure-finops plugin

A read-only Azure SRE Agent plugin with 11 skills and **one consolidated FinOps dashboard**.
Install from the Plugin Marketplace, configure the ARM MCP connector, then say
**"Set up FinOps for me"**.

## Mandatory scope choice and OBO permissions

Setup asks whether the dashboard covers **Managed resources**, **Everything**, or **Selected
subscriptions** and waits for
an explicit answer. Managed mode follows the agent's live managedResources, preserving exact RG
boundaries. Everything covers all resources in the agent's Azure tenant, including unmanaged ones,
using the tenant root management group and fresh recursive subscription discovery.
Selected subscriptions includes all resources in one or multiple explicitly confirmed Enabled
subscriptions in the verified agent tenant. The hosting subscription is preselected when visible,
but still needs confirmation. Caller-visible choices do not establish agent or connector access;
both are independently checked for every selection. Missing or incomplete access leaves setup
pending without dropping subscriptions. This mode requests per-subscription cost grants, not
a tenant-root grant, plus any separate managed-resource anomaly grants needed.

For Everything, setup explicitly explains and requests an **on-behalf-of (OBO)** permission prompt
to assign the **agent's managed identity** the **Cost Management Reader** Azure RBAC role at tenant
root. That role is inherited by existing and future subscriptions. It is a role, not an Entra group.
Reader on the agent is also required. The agent never receives write or role-assignment privileges.
The approving user needs grant rights at those scopes; otherwise setup provides an exact-scope
script for an authorized administrator to review and run. Billing policy may also require EA
AO-view-charges or MCA Azure-charges access.

Only the dashboard uses Everything or Selected subscriptions. Standalone skills, anomaly alerts and the optional subagent
remain managed-resource-only. Setup does not modify managedResources. If the root hierarchy
cannot be completely read, the dashboard fails closed rather than reporting only visible
subscriptions as Everything. Either fixed-scope dashboard can run without managedResources; in that
case setup does not create the managed-only anomaly task.

## ARM MCP connector

`.mcp.json` declares `https://mcp.management.azure.com`, header `x-mcp-toolset: CostManagement`.
Installing the plugin does not create/authenticate the connector.

1. **Plugins > azure-finops > Connectors > Configure** on `azure-resource-manager`.
2. Set **OAuth**, scope `api://22bfbae3-f4e7-485f-be43-8cee15065084/.default`.
3. Sign in with an account that can read costs on the selected dashboard scopes.
4. Select only read tools: `query_costs`, `forecast_costs`, `list_budgets`, `get_budget`,
   `list_dimensions`, `list_alerts`, and supported optional read-only benefit/container tools.
   Never select write tools such as `create_budget`, `create_deployment` or `execute_resource_action`.

Connector calls use that user's sign-in, including scheduled runs. OBO grants to the agent do not
grant the connector user access. Missing/expired connector falls back to Consumption UsageDetails
with a warning. Setup reports the two identities' access separately.

Cost queries are sequential, limited to 5,000 rows and split on truncation. Lookback is 92 days;
longer budget-proposal history uses UsageDetails. Tags are joined by resource ID because query_costs
doesn't group by tags. Root management groups are expanded to subscriptions for cost queries.

## Dashboard sections

Overview; cost explorer; ownership/allocation; billing/adjustments; budgets/forecasts; anomalies;
rightsizing/waste; commitments; containers; AI spend; unit economics; sustainability;
planning/scenarios; governance/scorecards; cost/reliability; prioritized actions; data/scope coverage.

The dashboard is a CSP-safe scheduled snapshot with local keyboard-accessible tabs, search,
inline charts, light/dark themes and print/PDF. It never fetches Azure data in the viewer.
Every source retains its own timestamp, period and ready/partial/unavailable/unsupported status.
Missing optional data is not zero cost or a healthy score.

The core six analyzers are reused; optional benefit, AKS allocation, business volume, carbon/energy,
scenario, billing-export and policy sources populate additional sections only when available.
This is not feature parity with Cloudability or a multi-cloud ingestion product.
See [research and capability gaps](../../docs/CLOUDABILITY-COVERAGE.md) and
[normalized data contracts](skills/finops-report-renderer/DASHBOARD.md).

## Scheduled tasks (UTC)

| Task | Cron | Output |
|---|---|---|
| FinOps: Cost Anomaly Detection (Daily) | `0 14 * * *` | Managed-resource alert in task result, optional email |
| FinOps: Dashboard (Live Report, Daily) | `30 14 * * *` | One **FinOps: Dashboard** report |

Setup creates/updates tasks in review mode and builds the dashboard immediately.
After the replacement report is successfully saved, setup retires the old individual-report
schedules by exact name. It never deletes historical reports or custom schedules.
Repair reuses the dashboard ID. Email/GitHub/schedule-only edits preserve the stored scope policy;
a setup/repair run asks for the scope choice again.

**Access warning:** local search and tabs are presentation, not security filters. Report viewers
can inspect the entire saved snapshot, especially important for Everything. Use platform report
access controls for the intended audience.

## Skills

| Skill | Purpose |
|---|---|
| `finops-setup` | Scope choice, OBO permissions, tasks, immediate dashboard, safe migration |
| `finops-managed-scope` | Managed-resource guard plus isolated explicit dashboard-policy resolver |
| `finops-cost-anomaly-detection` | Spikes with deployment/Activity Log and optional GitHub correlation |
| `finops-rightsizing-advisor` | Advisor/utilization-backed rightsizing and waste |
| `finops-cost-allocation` | Tag showback, unallocated spend and tag coverage |
| `finops-budget-governance` | Native-period budgets, forecasts and gaps |
| `finops-budget-editor` | Human-reviewed budget proposal scripts, never unattended writes |
| `finops-cost-optimization-report` | Prioritized actions with distinct impact types |
| `finops-for-ai` | AI cost/model/token attribution |
| `finops-cost-vs-reliability` | Cost joined to health, alerts and availability recommendations |
| `finops-report-renderer` | Deterministic consolidated dashboard and standalone adapters |
