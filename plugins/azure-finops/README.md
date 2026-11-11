# azure-finops plugin

Read-only Azure FinOps pack for Azure SRE Agent, packaged as a **standard plugin**. It contains
skills and an `.mcp.json` that declares the Azure Resource Manager (ARM) MCP server, so it installs
from the Plugin Marketplace with no external installer script.

## ARM MCP connector

Cost and budget data comes from the **Azure Resource Manager (ARM) MCP server** instead of direct
Consumption UsageDetails calls, which throttle easily. The plugin's `.mcp.json` declares
`https://mcp.management.azure.com` with the `x-mcp-toolset: CostManagement` header (needed for
`list_budgets`, `get_budget`, and `forecast_costs`).

Installing the plugin doesn't create the connector:

1. Open **Plugins → azure-finops**, scroll to **Connectors**, and select **Configure** on
   `azure-resource-manager`. The URL and header are prefilled.
2. Change authentication to **OAuth** and enter the scope
   `api://22bfbae3-f4e7-485f-be43-8cee15065084/.default` (the marketplace can't prefill OAuth settings).
3. Sign in with an account that has **Cost Management Reader** on the managed scopes.
4. Select only the read tools: `query_costs`, `forecast_costs`, `list_budgets`, `get_budget`,
   `list_dimensions`, and `list_alerts`. Leave every write tool (`create_budget`,
   `create_or_update_resource`, `create_deployment`, ...) unselected.

ARM MCP calls run as the user who signed in to the connector, including scheduled runs. If the
connector is missing or its sign-in expires, skills fall back to Consumption UsageDetails and say so in
every report. `finops-setup` checks the connector and reports its state.

Known ARM MCP limits the skills handle: `query_costs` returns at most 5,000 rows and cannot page (a
truncated result is split by date and re-queried), reaches back only 92 days (budget-amount
derivation in `finops-budget-editor` therefore still uses UsageDetails), and cannot group by tag
(allocation joins resource IDs to Resource Graph tags).

## Setup: one sentence, at most one approval

1. Install **azure-finops** from the marketplace.
2. In chat: **"Set up FinOps for me"**.
3. If the agent's identity lacks cost access, approve the permission prompt. Choose *approve for this
   thread*.

The agent then does the rest itself:

| Step | What `finops-setup` does |
|---|---|
| Discover | Reads its own resource: managed scopes, and the identity its Azure tools run as |
| Permissions | Lists that identity's role assignments. If any are missing, it grants **Reader** on the agent and **Cost Management Reader** on each cost scope **on behalf of the signed-in user (OBO)**. The agent's own identity never holds rights to grant roles |
| Tasks | Creates or updates the 7 scheduled tasks in `review` mode (idempotent) |
| Reports | Builds all 6 Live Reports immediately (a few minutes); the tasks refresh them on schedule after that |
| Summary | Lists results and the opt-ins: email alerts, GitHub correlation, read-only subagent |

The approving user needs the agent **Administrator** role plus **Owner** or **User Access
Administrator** on the scopes. If they don't have that, the agent generates `finops-rbac.sh` for an
Owner to run once in Azure Cloud Shell.

## Skills

| Skill | Purpose |
|---|---|
| `finops-setup` | End-to-end setup and repair from chat |
| `finops-managed-scope` | Scope guard: reads the agent's live `managedResources`; scheduled runs fail closed |
| `finops-cost-anomaly-detection` | Daily spike detection with deployment, Activity Log and optional GitHub correlation |
| `finops-rightsizing-advisor` | Advisor plus utilization-based rightsizing and idle-resource cleanup |
| `finops-cost-allocation` | Showback by tag or resource group |
| `finops-budget-governance` | Budget status, forecast and gaps |
| `finops-budget-editor` | Advisory budget proposals; generates a script for a **human** to run |
| `finops-cost-optimization-report` | Executive rollup |
| `finops-for-ai` | Azure AI spend by resource, model and service family |
| `finops-cost-vs-reliability` | Cost joined to alerts, Resource Health and Advisor high-availability recommendations |
| `finops-report-renderer` | Deterministic, CSP-safe static Live Report HTML |

## Scheduled tasks (UTC)

| Task | Cron | Live Report |
|---|---|---|
| FinOps: Cost Anomaly Detection (Daily) | `0 14 * * *` | (result or email) |
| FinOps: Cost Overview (Live Report, Daily) | `30 14 * * *` | FinOps: Cost Overview |
| FinOps: Rightsizing Savings (Live Report, Weekly) | `0 15 * * 1` | FinOps: Rightsizing Savings |
| FinOps: Budget Status (Live Report, Daily) | `0 16 * * *` | FinOps: Budget Status |
| FinOps: Cost Optimization (Live Report, Weekly) | `0 17 * * 1` | FinOps: Cost Optimization |
| FinOps: AI Spend (Live Report, Weekly) | `0 18 * * 1` | FinOps: AI Spend |
| FinOps: Cost vs Reliability (Live Report, Weekly) | `0 19 * * 1` | FinOps: Cost vs Reliability |

## Differences from the script-installed pack

| Area | Script pack (`install-api.sh`) | This plugin |
|---|---|---|
| Install | Laptop script with `az`, `curl`, `python3` | Marketplace **Install** + one chat message |
| RBAC | Script grants roles; the caller must be Owner | Same roles and scopes, granted in chat on behalf of the user |
| Subagent | Created automatically; tasks target it | Optional, on request (one paste); tasks run on the main agent in `review` mode |
| Model tier / task owner | Set per task | Not exposed by the chat task tools; platform defaults apply |

## What the reports cover

The same scope as the source pack: the agent's **managed resources**, read live on every run.
Subscriptions and management groups are reported in full. Resource groups are reported as exactly those
resource groups: ARM MCP queries each managed resource group directly (the UsageDetails fallback fetches
the containing subscription and filters it to the managed resource groups). To report on a whole subscription, add the
subscription under **Settings → Managed resources** and say "Set up FinOps for me" again. That grants
Cost Management Reader on the new scope and rebuilds the reports.

## Permissions summary

Only read roles are granted: **Reader** on the agent resource, and **Cost Management Reader** on each
managed subscription or management group. For resource-group boundaries the grant goes on the containing
subscription, because that is where the UsageDetails fallback reads cost data. No write roles, and no
budget-write permissions. The ARM MCP connector uses its signed-in user's own access, so that user also
needs Cost Management Reader on the managed scopes.
