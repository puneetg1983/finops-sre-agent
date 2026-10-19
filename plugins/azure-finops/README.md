# azure-finops plugin

Read-only Azure FinOps pack for Azure SRE Agent, packaged as a **standard plugin**. It contains only
skills, so it installs from the Plugin Marketplace with no external installer script.

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
| Validate | Runs the cost-overview task once and saves the first Live Report |
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

## Permissions summary

Only read roles are granted: **Reader** on the agent resource, and **Cost Management Reader** on each
managed subscription or management group. For resource-group boundaries the grant goes on the containing
subscription, because that is where Azure exposes cost data. No write roles, and no budget-write
permissions.
