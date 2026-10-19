# Azure FinOps Plugin for SRE Agent: Design (1-pager)

**Author:** Puneet Gupta · **Status:** Draft for review · **Repo:** github.com/puneetg1983/finops-sre-agent

## What
A standard Plugin Marketplace plugin (skills only) that turns SRE Agent into a FinOps assistant. It covers
cost anomalies, rightsizing, budgets, allocation, AI spend and cost vs. reliability, with 6 auto-refreshing
Live Reports.

**User experience:** Install → *"Set up FinOps for me"* → approve once (only if roles are missing) → done.

## What we ship: 11 skills
| Skill | What it does | Scheduled output (UTC) |
|---|---|---|
| `finops-setup` | One-sentence setup and repair: roles, tasks, reports | – |
| `finops-managed-scope` | Guardrail used by every skill: limits analysis to the agent's managed resources | – |
| `finops-cost-anomaly-detection` | Finds cost spikes and correlates them with deployments and config changes | Daily 14:00 (alert) |
| `finops-cost-allocation` | Showback: attributes spend to owner tags (team, env, cost center…) and surfaces unowned spend | On demand |
| `finops-budget-governance` | Each budget's spend and forecast vs. its amount and alert thresholds | **Budget Status** report, daily 16:00 |
| `finops-budget-editor` | Proposes budget changes as a script for a human to run | On demand |
| `finops-rightsizing-advisor` | Over-provisioned and idle resources, with estimated savings | **Rightsizing Savings** report, Mon 15:00 |
| `finops-for-ai` | AI spend (Azure OpenAI, AI Foundry) by resource, model and service | **AI Spend** report, Mon 18:00 |
| `finops-cost-vs-reliability` | Where spend doesn't buy reliability: cost joined with alerts and health | **Cost vs Reliability** report, Mon 19:00 |
| `finops-cost-optimization-report` | Executive rollup: anomalies, rightsizing savings, budgets, policy findings | **Cost Optimization** report, Mon 17:00 |
| `finops-report-renderer` | Renders every Live Report as safe static HTML, including **Cost Overview** (30-day spend, top services and RGs; daily 14:30) | – |

All analysis is read-only. Every skill also works in chat, for example *"Why did cost spike yesterday?"*

## Problem
> ⚠️ **Key constraint: the plugin framework can't deliver most of what FinOps needs.** Plugin install
> provisions only **skills** and **MCP server** definitions. Everything else must be created after install.

| FinOps needs | Delivered by plugin install? |
|---|---|
| Skills | ✅ Yes |
| Scheduled tasks | ❌ No |
| Live Reports | ❌ No |
| Subagents | ❌ No (and no chat tool can create one) |
| Azure RBAC roles for the agent identity | ❌ No |

We also don't want users running scripts. So the gap has to be closed **inside the chat**, using only
tools the agent already has.

## Design
A `finops-setup` skill does all post-install work in chat. Its decisions come from a deterministic
offline helper (`setup.py`); the LLM only runs the commands that helper prints.

| Step | Agent does | User does |
|---|---|---|
| 1. Discover | Reads its managed resources and the identity its Azure tools run as | – |
| 2. Permissions | Lists that identity's role assignments; grants only what's missing, **on behalf of the user (OBO)** | Approve once |
| 3. Tasks | Creates or updates 7 scheduled tasks (idempotent, review mode) | – |
| 4. Reports | Builds all 6 Live Reports immediately | – |
| 5. Summary | Status + opt-ins (email alerts, GitHub correlation, read-only subagent) | – |

No questions by default. Re-running the same sentence repairs or updates setup.

## Security
- **Least privilege:** only *Reader* (agent resource) and *Cost Management Reader* (cost scopes). The agent identity can never grant roles; the **user** grants them via OBO.
- **Approver** needs agent Administrator plus Owner or User Access Administrator. Otherwise the agent hands over a Cloud Shell script for an Owner.
- **Unattended runs are read-only:** tasks run in review mode, so no write can happen without a human.
- **Scope:** reports cover only the agent's **managed resources**, read live on every run, and fail closed. A resource-group scope reports only that resource group.

## Alternatives rejected
| Option | Why not |
|---|---|
| Installer script | Not a standard plugin; needs local tools |
| Give the agent permission to grant roles | Violates least privilege |
| Build reports only on schedule | Up to a week before first value |

## Limitations → platform asks
1. Subagents can't be created from chat → optional one-paste step. *Ask:* subagents and tasks in `plugin.json`.
2. One approval is always needed for roles. *Ask:* plugin-declared roles, granted at Install.
3. The chat task tool can't target a subagent or set model tier. *Ask:* add both.

## Status
Built and unit-tested (setup tests 26/26 pass; eval harness passes; OBO role grants are live fleet-wide).
**Next:** live end-to-end run on a test agent → team review → PR to `Azure/sre-agent-plugins`.
**Open:** overlap with the existing `finops_intelligence` skill (merge or narrow?).
