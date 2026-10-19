---
name: finops-setup
description: Sets up, re-runs, or repairs the Azure FinOps pack on this agent end to end from chat. Use when the user says "Set up FinOps for me", "set up the FinOps agent", "install/configure/enable FinOps", "turn on cost anomaly alerts / FinOps reports", "refresh all FinOps reports", or asks why FinOps scheduled reports are not working. Grants the agent identity read-only cost roles on behalf of the signed-in user (OBO), creates or updates the seven FinOps scheduled tasks, and builds all six Live Reports immediately. Asks no questions.
---

# FinOps setup

Do the entire setup yourself. The user's only action is approving the permission prompt, and
only when roles are missing. Re-running is safe: existing roles are skipped and existing tasks
are updated instead of duplicated.

## Ground rules

- **Ask no questions.** Use defaults: all seven tasks, default schedules, no email, no GitHub
  correlation. If the user's request already contains an email address or an `owner/repo`, use it.
  Mention the opt-ins once in the final summary.
- **Grant roles only on behalf of the user (OBO).** Run the `az role assignment create` commands from
  `grant-plan` with the Azure CLI **write** tool. The agent identity cannot grant roles, so each call
  is denied for it and the platform asks the user to approve and grant permissions. The command then
  runs as the user. Never use the terminal for `az`, never use a separate role-assignment tool, and
  never grant anything other than the roles `grant-plan` prints (Reader, Cost Management Reader).
- **Check roles by listing assignments, not by trying reads.** A failed read can be retried as the
  user, which hides whether the agent identity has access. The scheduled tasks run without a user,
  so the agent identity itself must hold the roles.
- `SKILL_DIR` is this skill's sandbox path (shown next to `finops-setup` in the skill list). Run the
  helper as `python3 $SKILL_DIR/setup.py ...`. It is offline and makes no Azure or agent API calls.
  Keep working files in `tmp/finops-setup/`.
- Keep the user informed with one short line per step ("Checking permissions…", "Creating 7 scheduled
  tasks…"). Do not paste raw command output.

## Step 1: Discover (read-only)

1. `AGENT_RESOURCE_ID` is this agent's ARM resource ID from your identity context.
2. Run with the Azure CLI read tool and pass `--subscription <agent subscription>`:
   - `az account show --query user.assignedIdentityInfo -o tsv`. This is the identity your Azure tools
     run as (`MSIClient-<clientId>`, or `MSI` for system-assigned).
   - `az resource show --ids <AGENT_RESOURCE_ID> -o json`. Save the output to `tmp/finops-setup/agent.json`.
     If a permission prompt appears, it's because the agent cannot read itself yet. Ask the user to
     approve it; this is expected on a first run.
3. `python3 $SKILL_DIR/setup.py discover --agent-json tmp/finops-setup/agent.json --identity-info "<value>" > tmp/finops-setup/discovery.json`
   - If it fails with "no managed resources", stop and tell the user to add subscriptions or resource
     groups under the agent's managed resources, then say "Set up FinOps for me" again.

## Step 2: Check and grant roles

1. For each entry in `discovery.json` `requirements`, run its `checkCommand` with the Azure CLI
   read tool and save the raw JSON output to `tmp/finops-setup/checks/<key>.json`. If a check
   errors, save nothing for that key.
2. `python3 $SKILL_DIR/setup.py grant-plan --discovery tmp/finops-setup/discovery.json --checks-dir tmp/finops-setup/checks > tmp/finops-setup/grants.json`
3. If `grants` is empty, report "Permissions already in place" and go to step 3.
4. Otherwise, tell the user in one short message what will be granted and why. For example: *"I need
   to give my identity read-only access to cost data: Reader on this agent and Cost Management Reader
   on 2 subscriptions. You'll be asked to approve. Choose **approve for this thread** so it's one
   approval."* Then run each `command` exactly as printed with the Azure CLI **write** tool.
5. Handle results:
   - Success, or "role assignment already exists": the grant is done.
   - Denied for the user as well, i.e. the user is not Owner or User Access Administrator on that
     scope, or declined: generate the fallback
     `python3 $SKILL_DIR/setup.py rbac-script --agent-resource-id <id> --client-id <clientId>`,
     save it for download, and say: *"You don't have rights to grant this. Send this file to a
     subscription Owner to run once in Azure Cloud Shell (`bash finops-rbac.sh`), then say 'Set up
     FinOps for me' again."* Continue to step 3 anyway. Tasks are created now and start working once
     the roles exist. Mark the missing scopes in the summary.
   - For a system-assigned identity, use `identity.principalId` as printed. Never substitute another
     identity.

## Step 3: Create or update the scheduled tasks

1. Call `ListScheduledTasks` (`currentThreadOnly=false`) and save the raw result to `tmp/finops-setup/existing.json`.
2. `python3 $SKILL_DIR/setup.py task-plan --agent-resource-id <AGENT_RESOURCE_ID> --existing tmp/finops-setup/existing.json [--alert-email <e>] [--github-repo <owner/repo>] > tmp/finops-setup/plan.json`
3. For each entry in `tasks`, pass the strings **exactly as given**. Never paraphrase `agentPrompt`.
   - `create`: `CreateScheduledMonitoringTask(taskName, description, cronExpression, agentPrompt, useCurrentThread=false)`,
     then `UpdateScheduledTask(taskId=<new id>, agentMode="review", threadGrouping="new")`.
   - `update`: `UpdateScheduledTask(taskId=existingTaskId, cronExpression, agentPrompt)`.
   - `unchanged`: skip it.
4. For each entry in `retire` (an obsolete duplicate from the old installer), cancel it with
   `CancelScheduledTask` and list it in the summary. This removes only that specific obsolete task name.

Why `review` mode: tasks run on the main agent, which has write tools. In review mode, a write cannot
execute unattended. FinOps runs are reads plus `SaveReport` of a static report, and neither needs approval.

## Step 4: Build all six Live Reports now

Don't make the user wait for the schedules. Tell the user: *"Building your 6 FinOps reports now. This
takes a few minutes."* Then run, in this order, the `agentPrompt` of every `plan.json` task whose
`liveReport` is non-empty, following each one exactly:

1. `cost-overview-report` (also acts as the end-to-end check)
2. `budget-status-report`
3. `rightsizing-savings-report`
4. `ai-spend-report`
5. `cost-vs-reliability-report`
6. `cost-optimization-report` (last: it rolls up the others)

Rules:
- Include tasks whose action is `unchanged`; the goal is a fresh set of reports.
- If `cost-overview-report` fails because cost reads are denied right after granting, wait about 2
  minutes and retry it once. Cost RBAC can take a few minutes to apply. If it still fails, skip the
  remaining reports and say they will appear on their schedules.
- If any other report fails, record the reason, continue with the next, and list it in the summary.
  Its scheduled run will retry.
- After each report, post one line: "✓ FinOps: Budget Status saved". Do not paste analysis
  tables; the reports hold the content.
- If the user asked for a quick setup ("just the overview", "skip the reports"), build only
  `cost-overview-report`.
- If the roles are still missing (fallback case), skip this step and say the reports will appear
  after the Owner runs the script.

## Step 5: Summarize

| Item | Status |
|---|---|
| Agent identity | name / principal ID |
| Managed scopes | n subscriptions / resource groups / management groups |
| Permissions | already in place / granted now (list) / pending Owner script (list) |
| Scheduled tasks | created x, updated y, unchanged z, retired w |
| Live Reports | 6 built now (list any that failed and why); open them under Operations Hub → Live Reports |
| Report scope | the managed scopes above; cost outside them is excluded by design |

Then add:
- Schedules are UTC. Daily: anomaly 14:00, cost overview 14:30, budget status 16:00. Weekly on
  Monday, 15:00 to 19:00: rightsizing, cost optimization, AI spend, cost vs reliability.
- "Say **'send FinOps alerts to you@contoso.com'** or **'correlate FinOps anomalies with repo owner/repo'**
  to turn on email or GitHub correlation. Say **'create the FinOps subagent'** for a dedicated
  read-only FinOps assistant."
- If every managed scope is a resource group, add: "Reports cover only these resource groups. To report
  on a whole subscription, add it under **Settings → Managed resources**, then say 'Set up FinOps for
  me' again."

## On request only

- **Email / GitHub opt-in:** re-run step 3 with `--alert-email` / `--github-repo`. This updates the
  anomaly and rightsizing tasks. Email delivery uses the agent's configured email tool. If that tool
  needs approval in review mode, tell the user the two emailing tasks can be switched to
  `autonomous`.
- **Schedule change:** re-run step 3 with `--cron <id>=<expr>`.
- **Subagent:** `python3 $SKILL_DIR/setup.py subagent-yaml --agent-resource-id <id> > tmp/finops-setup/finops-investigator.yaml`
  (add `--python-tool ExecutePythonCode` if your toolset has that tool instead of `RunInTerminal`).
  Save it for download. No chat tool can create subagents, so tell the user: **Agent Canvas → + →
  Subagent → Create → paste → Save**.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Task result says "scope error" | Reader on the agent missing, or managed resources empty | Say "Set up FinOps for me" again |
| AuthorizationFailed on UsageDetails / null `costInUSD` | Cost Management Reader missing on a subscription or management group | Say "Set up FinOps for me" again; if the user is not Owner, use the fallback script |
| Grant denied after approval | The approving user is not Owner / User Access Administrator, or not an agent Administrator | Fallback script for an Owner |
| Duplicate FinOps tasks | Tasks created manually with the same names | `task-plan` refuses; cancel duplicates and re-run |
