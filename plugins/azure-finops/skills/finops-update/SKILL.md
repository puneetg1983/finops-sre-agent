---
name: finops-update
description: Updates an existing Azure FinOps installation after the plugin is updated. Trigger on "update the FinOps setup", "refresh my FinOps setup", or a request to bring FinOps reports and scheduled tasks up to date. Preserves saved scope, subscriptions, alert email, GitHub repository, task selection and schedules; refreshes tasks and rebuilds the existing dashboard.
---

# Update FinOps

Use this skill when the user explicitly asks to update an existing FinOps setup after updating
the plugin. Plugin installation does not update the customer's scheduled tasks or saved Live
Report. This skill updates those customer resources in place.

## Safety and preservation

- Never broaden or silently change the saved dashboard scope or selected subscription list.
- Preserve the configured alert email, GitHub repository, enabled FinOps task set and each task's
  current schedule. The customer may change these only by explicitly asking.
- Do not touch custom tasks, reports, Azure resources, tags, budgets or role assignments unless
  the setup process below establishes that a read-only role is missing and the user approves the
  on-behalf-of (OBO) prompt.
- Keep anomaly work managed-resource-only. A dashboard scope of Everything or Selected
  subscriptions does not widen anomaly alerts.
- Keep Agent mode in review. Do not select write tools or create additional report copies.
- On any failed dashboard build, keep the old report and existing schedules. Do not mark the
  update applied or retire tasks.

## Update procedure

1. **Find the existing setup.** Read `AGENT_RESOURCE_ID` from identity context. Call
   `ListScheduledTasks(currentThreadOnly=false)` and `ListReports`. Save the task response as
   `tmp/finops-setup/existing.json`. Find exactly one task named
   **FinOps: Dashboard (Live Report, Daily)** and exactly one report named
   **FinOps: Dashboard**. If either has duplicates, stop and explain the conflict. If no dashboard
   task/report exists, tell the user to run **Set up FinOps for me** instead. Locate the sibling
   `finops-setup` skill directory in this plugin and use its absolute path below as
   `<FINOPS_SETUP_SKILL_DIR>`; this skill's `$SKILL_DIR` is not the setup helper directory.

2. **Read and validate the saved setup state.** Run:

   ```text
   python3 <FINOPS_SETUP_SKILL_DIR>/setup.py task-plan --agent-resource-id <AGENT_RESOURCE_ID> --existing tmp/finops-setup/existing.json > tmp/finops-setup/update-plan.json
   ```

   The planner reads the versioned `FINOPS_SETUP_STATE_JSON` metadata in the dashboard task and
   returns the saved scope, subscription list, email, repository, task selection, schedules,
   current plugin version and revision, and whether an update is needed. It also compares the installed
   plugin files with the revision recorded after the last successful setup.

   For an older installation without metadata, the planner migrates values only when it can
   recover them unambiguously from existing task prompts, cron schedules and the saved dashboard
   scope policy. If the policy or any setting cannot be recovered, stop and ask only for that
   missing information. Do not infer Managed resources, empty email, no repository or default
   schedules as a replacement. A missing or malformed state marker is not permission to reset
   customer configuration.

3. **Check access for the saved boundary.** Use the saved `dashboardPolicy` in the planner output;
   never ask for or substitute a new scope during an update. Re-read the current agent identity and
   verify the saved selected subscriptions are still enabled and in the agent tenant. Check the
   agent identity's existing read-role assignments and the configured ARM MCP connector's access
   using `finops-setup`'s discovery and cost-probe procedures. Do not treat caller-visible
   subscriptions as proof of agent or connector access. If a read-only role needed for the same
   saved boundary is genuinely missing, explain the exact principal, role and scope, and use only
   the OBO approval flow in `finops-setup`. A declined grant or unavailable connector leaves the
   update pending; do not drop scopes or query as the user to hide the failure.
   Compare the installed plugin's `.mcp.json` and documented read tools with the agent's current
   connector configuration. A skill cannot silently reconfigure or reauthenticate a connector.
   If a newly required read tool is missing, give the user the exact connector settings to enable
   under **Plugins > azure-finops > Connectors > Configure** and leave the update pending until
   that access is available. Never enable connector write tools.

4. **Update configured tasks in place.** Use the `tasks` array from `update-plan.json`. It is
   limited to the saved configured FinOps tasks and uses their existing IDs and schedules.
   Update task prompts to the planner's exact `agentPrompt`; create only a configured task that is
   unexpectedly missing. For every updated or unchanged task, enforce
   `agentMode="review"` and `threadGrouping="new"`. Do not cancel or recreate the dashboard task.
   The planner stamps the desired plugin revision and per-task template hashes into the dashboard
   task's state marker. This records the requested revision, not a successful dashboard refresh.

5. **Refresh the existing report now.** Run the exact updated `dashboard-report` `agentPrompt`,
   even if the task content was already current. Follow the complete setup skill's
   **Build the single dashboard now** procedure: use the same report ID and exact name; run
   `SaveReport` once with `allowedTools: []`; never create a second report. Verify the returned
   report ID and version. If the build or save fails, stop with the old report and schedules
   intact, and do not mark the plugin revision applied.

6. **Mark success and finish migration.** After verifying the report save, refresh the scheduled
   task list and run:

   ```text
   python3 <FINOPS_SETUP_SKILL_DIR>/setup.py task-plan --agent-resource-id <AGENT_RESOURCE_ID> --existing tmp/finops-setup/existing.json --mark-applied > tmp/finops-setup/applied-plan.json
   ```

   Update the dashboard task with the planner's exact prompt to record the applied plugin
   revision and version. Enforce review mode. Only after the report is verified and the applied marker is
   saved, cancel the exact legacy tasks listed in `applied-plan.json`'s `retire` array. Never
   delete old Live Reports or custom tasks.

7. **Report the outcome.** Summarize the preserved scope and settings, tasks updated, dashboard
   report ID/version, plugin version and revision, any retired legacy tasks and remaining access or data gaps.
   If the report is current already, still state whether it was rebuilt during this command.

## Important distinction

Updating the plugin files alone does not update an installed agent's scheduled task prompts or its
saved report. The user must run this skill after updating the plugin. This skill is deliberately
explicit and does not run automatically during plugin installation.
