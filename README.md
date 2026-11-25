# Azure SRE Agent FinOps plugin

A standard [Azure SRE Agent](https://learn.microsoft.com/azure/sre-agent/) plugin marketplace with one
plugin, the read-only **azure-finops** plugin. It contains 12 skills and an `.mcp.json` that declares
the Azure Resource Manager (ARM) MCP server used for cost data: no installer script, no hooks, no
out-of-band API calls.

Converted from [nirmash/finops-sre-agent-pack](https://github.com/nirmash/finops-sre-agent-pack) (MIT).
The analysis skills now retrieve cost and budgets through ARM MCP (`query_costs`, `list_budgets`) to
avoid Consumption API throttling, with UsageDetails as a disclosed fallback. The pack's installer is
replaced by the in-chat `finops-setup` skill.

## Install (end user)

1. **Builder → Plugins → Add marketplace** → `https://github.com/puneetg1983/finops-sre-agent`,
   or install from the official marketplace once it is published there.
2. Open **azure-finops** → **Install**.
3. On the plugin page, under **Connectors**, select **Configure** for `azure-resource-manager`; choose
   **OAuth** with scope `api://22bfbae3-f4e7-485f-be43-8cee15065084/.default`, sign in, and select only
   its read tools. See [plugins/azure-finops/README.md](plugins/azure-finops/README.md#arm-mcp-connector).
4. In chat: **"Set up FinOps for me"**. Choose **Managed resources**, **Everything**, or
   **Selected subscriptions** (one or multiple; the agent's hosting subscription is preselected,
   but requires confirmation), then approve any required OBO permission prompt.

With your approval, setup grants missing read-only cost roles on your behalf and builds **one
consolidated FinOps dashboard**. One task refreshes it daily; a separate managed-resource anomaly
task handles alerts. Everything means all resources in the agent's
Azure tenant, discovered from its root management group, including unmanaged resources. It requires
Cost Management Reader at that root, approved via OBO. Other skills remain managed-resource-only.
Selected subscriptions instead covers all resources in exactly the confirmed same-tenant
subscriptions, with per-subscription cost grants and separate agent/connector access verification.
Missing access leaves setup pending, never silently narrowing the selection.

Optional inputs (such as namespace allocation, business volumes, carbon data and billing exports)
show explicit setup, access or data gaps until connected, never made-up metrics.
See the [Cloudability reporting research and coverage matrix](docs/CLOUDABILITY-COVERAGE.md).
After installing a plugin update, type **"Update the FinOps setup"** in chat to refresh the
scheduled task prompts and rebuild the existing dashboard. The update preserves the saved scope,
subscriptions, alert settings, task selection and schedules. Obsolete schedules are retired only
after the refreshed dashboard saves successfully; historical reports are not deleted.

Details: [plugins/azure-finops/README.md](plugins/azure-finops/README.md) · Design: [docs/DESIGN.md](docs/DESIGN.md).

## FinOps dashboard

**FinOps: Dashboard** is one saved Live Report with 18 sections, rather than separate reports for
each analysis. Setup builds it immediately and schedules a daily refresh. It is a snapshot: opening
the report does not query Azure, and recent costs can be incomplete while billing data arrives.
The core cost views use Azure Cost Management pre-tax costs in USD, not invoice totals; budgets
retain their own currency and period.

| What you can explore | Dashboard sections |
|---|---|
| What to do next | Decision briefing |
| Spend and ownership | Overview, Cost explorer, Ownership & allocation, Billing & adjustments |
| Risk and efficiency | Budgets & forecasts, Anomalies, Rightsizing & waste, Commitments |
| Workload insights | Containers, AI spend, Unit economics, Sustainability |
| Decisions and coverage | Planning & scenarios, Governance & scorecards, Cost & reliability, Prioritized actions, Data & scope |

Sections show their source, time period, refresh time and status. Some need additional data or
permissions—for example container allocation, business volumes, sustainability measurements and
billing exports. When a source is missing or incomplete, its section explains the gap instead of
presenting invented values or a healthy zero. Recommendations are advisory; the dashboard does not
change resources or automatically realize savings.

Missing-data labels explain the difference: **Access needed**, **Setup needed**, **Read failed**,
or **Not collected**. **Not in scope** means a complete scoped inventory confirmed that the relevant
resources do not exist in the report's selected scope, not necessarily the whole subscription.
Successful empty results show **No matching records**, and successful budget checks with no budgets
show **No budgets configured** with a setup step. Sustainability can show sourced carbon emissions
without energy data; missing energy is labeled **Not supplied by source**, never zero.

### Decision briefing

The dashboard opens on **Decision briefing**, the agent's answer to "what should I do this week?".
It turns the analysis below into a short, ranked list of decisions instead of more charts. It covers
six recurring FinOps jobs:

| Job | What the agent does for you |
|---|---|
| Explain a cost spike | Matches each spike to Azure changes (scale, SKU change, new resource, deployment, commit or incident) from the 2 days before it, and gives a likely cause with High, Medium or Low confidence. |
| Weekly waste review | Groups savings by owner, so each team sees only its own list. |
| Fix tags for chargeback | Lists costly resources missing owner or cost-center tags and proposes values from the resource group or deployer. |
| Clean up orphaned and idle resources | Shows unattached disks, idle resources and similar items, with evidence. |
| Change a tier or size | Prepares the change as a pull request when a repository is configured, otherwise as an Azure change. |
| What did the agent cost? | Shows the agent's own monthly cost and the validated savings it found. |

Each decision shows the money involved, why it happened, the evidence, the owner and up to three
next actions. An action copies a ready-made prompt to paste into the agent chat. **Nothing is changed
automatically**: every prompt that would change something asks the agent to show the exact change and
wait for your approval. Each check also shows whether it ran, was not attempted, or could not run
(for example, missing access), so an empty list never hides a check that failed. Savings without
utilization data are shown but not counted as validated. Per-run agent cost appears only when that
data is available.
### Using the dashboard

- **Choose dates:** Overview and Cost explorer default to the latest 30 inclusive UTC days. When
  complete history is available, choose the last 90 days or a custom date range within those 90 days.
  Other sections retain their own source periods.
- **Explore costs:** Switch between service and region charts, select bars to filter, and drill into
  **Other**. Filter by service, subscription, resource type or region, and search resource records.
  Overview shows the top 15 services; **Browse every service** opens the Cost explorer filter.
- **Work with large lists:** The resource table shows the top 10 matching records by default, with
  options for 25 or 50. Charts, totals and CSV export use **all** matching records, not just the
  visible rows. Expand a resource to see its full details.
- **Export or print:** In the embedded Live Report, export shows selectable CSV text because the
  viewer blocks downloads. For a direct CSV download or print-to-PDF, use the Live Reports toolbar
  to download the HTML and open it locally. The report also supports light and dark themes.

Cost explorer shows cost records, **not a complete resource inventory**. For example, web
apps can share costs billed to an App Service plan, and a resource without a cost record may not
appear. The report shows its selected scope and data coverage; report viewers can inspect the whole
saved snapshot, so use platform access controls to limit who can view it.

For details on data sources, limitations and the dashboard contract, see
[DASHBOARD.md](plugins/azure-finops/skills/finops-report-renderer/DASHBOARD.md).

## Repository layout

```
.github/plugin/marketplace.json     marketplace manifest
plugins/azure-finops/plugin.json    plugin manifest  { "skills": ["skills/"] }
plugins/azure-finops/.mcp.json      ARM MCP server requirement (connector configured by the user)
plugins/azure-finops/skills/        12 skills (setup + update + 10 analysis skills)
tests/                              offline pytest suite
evals/                              offline eval harness from the source pack
docs/DESIGN.md                      one-page design spec
```

## Test

```bash
pip install -r requirements-dev.txt
pytest tests/
```

## Contributing to Azure/sre-agent-plugins

Copy `plugins/azure-finops/` into `Azure/sre-agent-plugins/plugins/` and add its entry to that repo's
marketplace manifest. Keep the description of `finops_intelligence` (in `azure-proactive-operations`)
focused on quick assessments, so the two skills don't compete for the same requests.
