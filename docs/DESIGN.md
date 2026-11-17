# Azure FinOps dashboard design

A standard Azure SRE Agent plugin: 11 skills plus an ARM MCP server declaration. Plugin install
alone does not provision tasks, reports, RBAC or subagents; the in-chat setup skill closes that gap.

**Experience:** Install > "Set up FinOps for me" > choose Managed resources / Everything >
approve missing read-only OBO grants > open **FinOps: Dashboard**.

## One report, not a report collection

The daily **14:30 UTC** dashboard task renders 17 sections: overview, resource explorer,
ownership/allocation, billing/adjustments, budgets/forecasts, anomalies, rightsizing/waste,
commitments, containers, AI, unit economics, sustainability, planning, governance/scorecards,
cost/reliability, prioritized actions and source/scope coverage.

The report is a scheduled static snapshot with local accessible tabs, row search, inline SVG
charts, print/PDF and a shared light/dark theme. It does not imitate Cloudability's interface.
It has no external dashboard dependencies or view-time Azure calls; SaveReport uses `allowedTools: []`.
Every section discloses source, timestamp, period and status. Missing inputs remain visible as
unavailable, not healthy zeros. Different source periods (for example native monthly budgets) are
preserved. Source timestamps are not overwritten on reuse.

Existing deterministic analyzers feed the consolidated builder; they do not save their own reports
during a dashboard run. Daily and resource-total cost pulls are shared but never combined into an
overlapping financial total. Query shapes are planned and validated separately. Optional provider,
billing, allocation and business-metric inputs use explicit numeric contracts, not LLM calculations.
See [DASHBOARD.md](../plugins/azure-finops/skills/finops-report-renderer/DASHBOARD.md).

## Explicit scope and least privilege

Setup **must ask and wait** for the scope choice. It is not inferred from RBAC, previous chat or
the set of subscriptions visible to a user.

| Choice | Dashboard boundary | Read-only cost grant |
|---|---|---|
| Managed resources | Agent managedResources, fetched live every run; exact RG boundaries retained | Cost Management Reader on managed subscriptions/MGs (containing subscription for UsageDetails transport) |
| Everything | Agent tenant root MG, verified against the agent tenant, recursively expanded each run | Cost Management Reader on the root, inherited by existing and future subscriptions |

Both need Reader on the agent itself. All grants are explicit **OBO** Azure CLI write calls;
the agent never gets role-assignment privileges. A reviewable exact-principal fallback script is
provided when the user cannot grant roles. Root-wide access and the resulting report audience
exposure must be explained before consent. EA/MCA charge-visibility policy can still block cost
reads; setup does not bypass billing policy.

The dashboard task persists a validated policy in `DASHBOARD_POLICY_JSON`. Its isolated
`resolve_dashboard_scopes` path reuses pure query planners and filters with the chosen boundary.
Other skills, the optional subagent and the separate daily **14:00 UTC** anomaly alert retain
their original strict managed-resource policy. No changes are made to managedResources.
Missing or incomplete hierarchy discovery fails closed rather than shrinking "Everything" to
visible subscriptions. Root MG grants are inherited, but cost queries run on descendant subscriptions.

## Setup, repair and migration

1. Ask scope, discover identity, list inherited assignments and generate only missing read roles.
2. Obtain OBO approval for grants. Recheck identity access and hierarchy; never use a user's successful
   read to pretend the managed identity has access.
3. Check the separately authenticated ARM MCP connector. Its sign-in must cover the selected scopes;
   granting the agent a role does not grant that user a role.
4. Create/update the dashboard and anomaly tasks, explicitly enforcing review mode.
5. Render and save one dashboard immediately, reuse its exact report ID and confirm the version.
6. Only after a successful save, cancel the seven exact legacy task names in the manifest. Leave
   old report snapshots intact. Failed migration retains old schedules and discloses pending status.

Scope changes cause task updates; opt-in-only edits preserve the persisted policy. Duplicate exact
task/report names and malformed policies require explicit resolution, not silent selection.

## Retrieval and capability boundaries

ARM MCP `query_costs` is primary: explicit scopes/dates/USD, sequential requests, at most 5,000 rows,
date splitting for truncation and 92-day lookback. Missing/expired connector falls back to paginated
Consumption UsageDetails with an explicit warning. RG transport reads do not widen logical scope.
Budget-amount proposals still use longer UsageDetails history.

Optional telemetry, benefit, container, carbon, business-volume and billing-export APIs require their
own existing access/configuration. Scheduled runs do not grant roles, deploy collectors or buy
commitments. Azure writes, budget edits and remediation are not dashboard operations.

Cloudability informs reporting breadth, not a parity claim: non-Azure ingestion, proprietary
benchmarks, historical mapping rules, automated cost sharing, custom commercial billing, editable
widget canvases and automated optimization are not implemented. See the cited
[research and coverage matrix](CLOUDABILITY-COVERAGE.md).
