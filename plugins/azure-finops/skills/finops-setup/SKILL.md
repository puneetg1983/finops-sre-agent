---
name: finops-setup
description: Sets up, repairs or refreshes Azure FinOps from chat. MUST ask whether the dashboard covers Managed resources, Everything or Selected subscriptions (one or multiple). Grants missing read-only roles on behalf of the user (OBO), creates one daily consolidated dashboard task plus managed-resource anomaly alerts, and builds the single Live Report immediately.
---

# FinOps setup

Run setup end to end. Re-running is idempotent: check roles, update tasks by exact name and reuse
the single dashboard report ID. Do not create the former six individual reports.

## Step 0: Mandatory dashboard scope choice

**MUST ask the end user for input and wait for an explicit answer before scope/grant discovery or task
changes**, including on setup/repair runs. Only read identity/subscription metadata needed to present
the choices before that answer. Use the platform's user-input tool, not a rhetorical
question or an inferred default:

> Should the FinOps dashboard cover Managed resources, Everything in this Azure tenant, or Selected subscriptions (one or multiple)?

Choices: **Managed resources**, **Everything**, and **Selected subscriptions**. Explain that Everything includes unmanaged
resources in the agent's tenant, requires a tenant-wide read-only cost grant, and broadens the
contents visible to dashboard viewers. Silence is not consent. This choice affects the dashboard
only; anomaly alerts and other standalone skills retain their managed-resource boundary.

- **Managed resources:** use `--scope-mode managed`. Read live managedResources every run; never
  widen a resource-group boundary to its containing subscription for analysis.
- **Everything:** read the agent subscription's tenant ID using the Azure CLI read tool. The scope
  is `/providers/Microsoft.Management/managementGroups/<tenantId>` (the tenant root management
  group), not `az account list` or whichever subscriptions are visible. Display the exact tenant
  and root scope with the permission explanation. Pass `--scope-mode everything
  --dashboard-scope <rootScope>` to both `discover` and `task-plan`. The root must match the agent's
  tenant, not another CLI context. Do not change the agent's managedResources.
- **Selected subscriptions:** read the agent subscription's verified tenant ID, then obtain
  `az account list --all -o json` through the Azure CLI read tool under the signed-in caller's
  context (with explicit OBO read consent if needed). Save as `subscriptions.json`. Present only
  Enabled subscriptions in that tenant, showing names and IDs, in a multi-select input. Preselect
  the subscription **hosting the agent** (not an unrelated CLI default) if visible and Enabled;
  otherwise present no default and disclose why. Require explicit user confirmation even for
  the preselection; allow one or multiple selections. If no multi-select tool exists, ask for a
  confirmed list of displayed IDs. No empty, malformed, disabled or out-of-tenant selection.
  Explain that this includes all resources, including unmanaged resources, in exactly those
  subscriptions, not the whole tenant or future subscriptions. Pass `--scope-mode subscriptions`
  and repeat `--dashboard-scope /subscriptions/<guid>` for every confirmed subscription to
  `discover` and `task-plan`. Also pass `--subscriptions-json <subscriptions.json>` and
  `--tenant-id <verified agent tenant ID>` to `discover`. The inventory establishes selectable
  choices only: it does not prove access for the agent identity or the ARM MCP connector user.
  Do not change managedResources.

Apart from confirming the selected subscription list, no other setup questions are necessary.
Use the default schedules, no email and no GitHub
correlation unless supplied. Never ask for secrets.

## Ground rules

- **Grant roles only on behalf of the user (OBO).** Run only `grant-plan`'s printed
  `az role assignment create` commands with the Azure CLI **write** tool. The identity cannot grant
  roles; the platform prompts the signed-in user for permission and runs the approved command as
  that user. Never use the terminal for `az`, or a separate role-assignment tool.
- Grant only **Reader** on the agent and **Cost Management Reader** on the selected cost scopes.
  These are Azure RBAC roles, not an Entra group. Never grant Owner, Contributor or User Access
  Administrator to the agent. Do not automatically grant more roles for optional telemetry.
- **Check roles by listing assignments**, including inherited assignments, not by trying reads
  that may silently retry as the user. Scheduled reads need the agent identity's own access.
- `SKILL_DIR` is this skill's sandbox path. `setup.py` is offline and makes no service calls.
  Keep working files in `tmp/finops-setup/`.
- The ARM MCP connector has a separate signed-in identity. OBO grants to the agent do not grant
  that connector user access; check and disclose both.

## Step 1: Discover

1. Get `AGENT_RESOURCE_ID` from identity context. With the Azure CLI read tool and explicit agent
   subscription, run `az account show --query user.assignedIdentityInfo -o tsv` and
   `az resource show --ids <AGENT_RESOURCE_ID> -o json`. Save the latter to
   `tmp/finops-setup/agent.json`. A permission prompt to read the agent itself may be expected.
2. Run `python3 $SKILL_DIR/setup.py discover --agent-json tmp/finops-setup/agent.json
   --identity-info "<value>" <selected scope flags> > tmp/finops-setup/discovery.json`.
   In Everything or Selected subscriptions mode, also pass `--tenant-id <verified agent subscription tenant ID>` to discover
   (not task-plan). It must match `identity.tenantId` when present and the selected root, so a
   wrong-tenant grant plan is rejected before any OBO write. Selected subscriptions additionally
   requires `--subscriptions-json` with the caller-visible inventory saved above.
3. Managed mode with empty/malformed managedResources fails closed: ask the user to configure
   **Settings > Managed resources**, then re-run setup. Everything and Selected subscriptions can work without managedResources;
   in that case create only the dashboard task (`--only dashboard-report`) and disclose that
   anomaly alerts require managedResources. Malformed nonempty managedResources still fails.
4. For Everything, verify the root identity against the agent tenant and attempt a complete
   recursive hierarchy read. If permission is missing, proceed to the explicit OBO grant in step 2;
   never treat a partial subscription inventory as Everything.
5. In Selected subscriptions mode, discovery validates each selected ID's tenant and Enabled state.
   `selectableSubscriptions` and `defaultSubscriptionScope` are UI hints, not permission evidence.
   `accessVerification` remains pending until independent role checks and cost probes succeed.

## Step 2: Check roles and obtain OBO consent

1. Run each discovery `requirements[].checkCommand` with the Azure CLI read tool. Save raw JSON
   to `tmp/finops-setup/checks/<key>.json`; don't save successful-looking output on errors.
2. Run `python3 $SKILL_DIR/setup.py grant-plan --discovery tmp/finops-setup/discovery.json
   --checks-dir tmp/finops-setup/checks > tmp/finops-setup/grants.json`.
3. If grants are empty, report permissions already in place. Otherwise explain the exact principal,
   roles and scopes, then execute each printed command with the Azure CLI **write** tool.
   For **Everything**, explicitly say:

   > To query costs for Everything, I need to assign my managed identity `<principalId>` the
   > **Cost Management Reader** Azure RBAC role at tenant root `<rootScope>`, inherited by all
   > subscriptions, including future ones. This is read-only cost access, not permission to change
   > resources. Approve the following on-behalf-of (OBO) permission prompt to grant this access.

   Also disclose any Reader grant on the agent and cost grants needed for the separate
   managed-resource anomaly task. Do not promise that there will be exactly one platform prompt.
   For **Selected subscriptions**, explain and obtain OBO consent for **Cost Management Reader**
   on each exact selected subscription for the resolved agent principal, plus any separate
   managed-resource anomaly scopes printed by the plan. No tenant root grant is needed for
   the dashboard. Never grant a broader scope just because some selected subscriptions fail.
4. Success or "role assignment already exists" is done. If consent is declined, do not retry,
   change scope, or execute a grant elsewhere. If the user lacks grant rights, generate
   `python3 $SKILL_DIR/setup.py grant-script --discovery tmp/finops-setup/discovery.json
   --checks-dir tmp/finops-setup/checks > tmp/finops-setup/finops-rbac.sh`.
   Offer this **reviewable download only** for an authorized Owner / User Access Administrator.
   This fallback preserves Everything or Selected subscriptions and supports either system- or
   user-assigned principal IDs. Never use the old managed-only `rbac-script` for either mode.
5. Re-list assignments after grants. If access or hierarchy discovery remains blocked, mark
   pending, skip the immediate build and don't claim coverage. Do not query under the user's
   identity to disguise missing agent access. EA AO-view-charges / MCA Azure-charges policy may
   also need enabling by a billing administrator; a role grant alone doesn't override it.

## Step 3: Check the cost connector

Look for `*_query_costs`. If missing, disclose the UsageDetails fallback rather than stopping setup.
After scope and roles are confirmed, probe a small seven-day query on the first resolved effective
subscription/RG. For Selected subscriptions, probe **every** confirmed subscription independently,
and verify the agent's cost-read assignments on every selected scope as well. If the connector
is missing, verify the disclosed UsageDetails fallback on every selection under the agent identity.
Missing, denied or incomplete access on any selection leaves setup pending: no immediate build,
no success claim and no silently reduced selection. Authentication errors mean sign-in expired; authorization errors mean the connector
user lacks cost access, not that the agent's OBO role grant failed.

Configure through **Plugins > azure-finops > Connectors > Configure**, **OAuth** with scope
`api://22bfbae3-f4e7-485f-be43-8cee15065084/.default`. Select only read tools (`query_costs`,
`forecast_costs`, `list_budgets`, `get_budget`, `list_dimensions`, `list_alerts`, and supported
read-only benefit/container tools listed by finops-managed-scope). Deselect write tools.

Everything uses fresh root descendants every run, with cost queries at descendant subscription
scope to avoid management-group billing-agreement query restrictions. Connector access must cover
those subscriptions too. Never silently narrow to the connector user's visible subscriptions.
Selected subscriptions never expands to caller/connector-visible inventory or newly added
subscriptions; changing the list requires explicit setup confirmation.

## Step 4: Plan tasks

1. Call ListScheduledTasks (`currentThreadOnly=false`), save raw output to
   `tmp/finops-setup/existing.json`.
2. Run `python3 $SKILL_DIR/setup.py task-plan --agent-resource-id <AGENT_RESOURCE_ID>
   --existing tmp/finops-setup/existing.json <selected scope flags>
   [--alert-email <e>] [--github-repo <owner/repo>] > tmp/finops-setup/plan.json`.
3. Copy each task's strings exactly; never paraphrase `agentPrompt`.
   Create via CreateScheduledMonitoringTask with `useCurrentThread=false`, then
   UpdateScheduledTask with `agentMode="review", threadGrouping="new"`.
   Update existing tasks with the planned cron and prompt, and explicitly enforce `agentMode="review"`
   on updated or unchanged tasks too. Skip only unchanged content, not this safety setting.
4. Schedule one **dashboard-report** task at **14:30 UTC daily**, and a separate managed-resource
   **cost-anomaly** alert at **14:00 UTC daily**. Only the dashboard task saves a report.
5. **Migration:** create/update the replacement task first. Build the dashboard in step 5 and verify
   SaveReport returned its ID/version **before** cancelling `plan.retire` entries. On failure retain
   the old schedules and disclose migration pending; do not remove working reports first. Retire
   only the exact old task names in the manifest, not custom tasks. Never delete existing historical
   reports automatically; tell the user they remain as old snapshots with no future refresh once
   migration completes.

Review mode prevents unattended Azure writes; report snapshots need no Azure write access.

## Step 5: Build the single dashboard now

Run the exact **`dashboard-report`** agentPrompt, even if its task action is unchanged. Say
"Building your consolidated FinOps dashboard." It renders all sections via
`build_dashboard_model` and `write_report`, then calls SaveReport once with `allowedTools: []`.

If fresh cost permissions haven't propagated, wait about two minutes and retry once. If still
denied, mark pending; do not save an all-healthy empty dashboard. Optional source failures retain
their tab, reason and next action; they do not prevent the core dashboard from being saved.
For managedResources-empty Everything or Selected subscriptions setups, do not run the managed-only anomaly task.

Reuse the exact-name **FinOps: Dashboard** report ID. Duplicate exact names are an explicit conflict,
not permission to choose or create another. Verify the saved version, then complete task retirement.
This is a scheduled static snapshot with local tab/search/print interactions, not browser-time Azure
access or real-time billing.

## Step 6: Summary

Summarize identity, **chosen dashboard scope and exact boundary**, OBO grants made or pending,
connector identity/status, created/updated/retired task counts, saved dashboard ID/version, last
refresh time, and partial/unavailable sections with remediation. Link/open the one report under
**Operations Hub > Live Reports > FinOps: Dashboard**. Do not say "all aspects implemented" when
sources or integrations are missing.

Mention email/GitHub opt-ins once. Do not email cost details to an inferred recipient. Do not claim
client-side filters restrict access: every report viewer can inspect the whole saved snapshot.

## On request only

- Email / GitHub correlation: re-run task-plan with the opt-in. Preserve the existing
  DASHBOARD_POLICY_JSON; omitted scope flags preserve it deterministically. If the old policy is
  missing/malformed, stop and ask the mandatory scope question again. Email is sent by the anomaly
  task, not duplicated by the dashboard.
- Schedule: use `--cron dashboard-report="<five-field cron>"` (or `cost-anomaly`).
- Read-only subagent: use `subagent-yaml --agent-resource-id <id>` with the documented optional
  `--python-tool` and `--arm-mcp-connection`. Save for download; the user adds it in Agent Canvas.
  Its standalone requests remain managed-only; it is not the Everything or Selected subscriptions dashboard task runner.
- The legacy `rbac-script --agent-resource-id <id> --client-id <id>` remains for managed-only,
  user-assigned-identity compatibility. Prefer the discovery-based `grant-script`.
