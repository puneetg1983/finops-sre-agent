# Cloudability reporting research and dashboard coverage

Researched **2026-11-16** using IBM/Apptio first-party feature documentation. This maps publicly
documented reporting families, not a claim that every SKU, report template, integration or private
API has been exhaustively audited. Cloudability packages evolve; features such as Commercial
Billing are add-ons. The goal is reporting breadth without copying its branding or interface.

## Findings and implementation mapping

| Cloudability reporting family | Documented capabilities | This dashboard and remaining boundary |
|---|---|---|
| [Dashboards and reports](https://www.apptio.com/products/cloudability/dashboards-reports/) | Default/custom dashboards, configurable widgets, resource detail, dimensions, shared filtered Views and provider refreshes | One scheduled dashboard; accessible sections, inline trend, searchable tables, print/PDF, visible freshness. No drag/drop widget builder, arbitrary saved pivot builder or live provider refresh inside the browser. |
| [True Cost Explorer](https://www.apptio.com/products/cloudability/true-cost-explorer/) | Interactive spend/usage dimension exploration including discounts, credits and amortization | Overview plus resource/subscription/RG/service/region/charge-type explorer. PreTaxCostUSD is explicit; billing bases are separate sourced rows, not inferred discounts/amortization. No interactive cross-cloud pivot engine. |
| [Business mapping](https://www.apptio.com/products/cloudability/business-mapping/) | Rules-based business dimensions, time-adjusted/nested rules, application/team ownership, CMDB integration | Ownership/allocation tab reuses tag showback and coverage. Historical business rules and ServiceNow integration are not implemented; supplied approved allocation outputs can inform billing/unit economics. |
| [Cost sharing](https://www.apptio.com/products/cloudability/cost-sharing/) | Fixed, proportional and telemetry-driven distribution, rule-order audit and source/destination cost flows | Shared/unallocated costs stay visible. No automatic 100% allocation or redistribution engine; approved external allocation methods must be supplied and documented. |
| [Commercial billing](https://www.apptio.com/products/cloudability/commercial-billing/) | Add-on for enterprise procurement, customized billing, business-unit chargeback and federated financial accountability | Billing/adjustments accepts scoped exports or approved chargeback outputs with basis, amount, currency, billing period and methodology. No custom invoice/rate-card/federated tenant billing engine. |
| [Budgets and forecasts](https://www.apptio.com/products/cloudability/budgets-forecasts/) | View-level budgets, MTD/previous-period trends, projected breach and intelligent forecasting | Budget analyzer shows native-period actuals/forecasts, thresholds and risk. Existing provider forecasts are not labelled as proprietary ML; missing forecasts remain gaps. No multi-model forecasting engine or budget-edit workflow in the dashboard. |
| [Anomaly detection](https://www.apptio.com/products/cloudability/anomaly-detection/) | Proactive anomalous-spend identification and notification | Anomalies tab reuses deterministic baseline/WoW/new-spend detection; independent managed-resource alert task handles delivery/correlation. Partial newest day is excluded. No claim to Cloudability's ML detector. |
| [Rightsizing](https://www.apptio.com/products/cloudability/rightsizing/) | Utilization-informed resource sizing and waste reduction | Existing Advisor/metrics analyzer; validated/verify-first states, ranked estimated savings and idle resources. Actual changes remain human decisions. |
| [Commitment discounts](https://www.apptio.com/products/cloudability/commitment-based-discounts/) | Portfolio, utilization/coverage/savings, expiration, purchases/exchanges, hourly demand, terms, cash flow and breakeven | Commitment rows calculate per-benefit utilization/coverage from matching units and show realized savings, unused cost, expiry and recommendation. Planning accepts supplied term/breakeven scenarios. No automatic purchasing, exchanges, optimizer or invented hourly history. |
| [Container cost allocation](https://www.apptio.com/products/cloudability/container-cost-allocation/) | Cluster/node/workload/container visibility, namespace/label allocation using utilization, integrated budgets/anomalies/efficiency | Containers tab accepts existing AKS/OpenCost namespace/workload allocation with idle/shared buckets and methodology. An Azure cluster bill alone is not pod allocation. No collector/add-on installation, node-placement or autoscaling engine. |
| [Unit economics](https://www.apptio.com/products/cloudability/unit-economics/) | Business outcome denominators, fully allocated costs, third-party/FOCUS ingestion and cost per transaction/customer | Decimal cloud-cost-per-unit calculation from matched-period, matched-scope cost/volume inputs. Zero volume is unavailable, not zero unit cost. No implied full-loaded profitability or third-party/FOCUS ingestion pipeline. |
| [Sustainability](https://www.apptio.com/products/cloudability/cloud-infrastructure-sustainability-reporting/) | Modelled emissions (MTCO2e), energy (kWh), team/application/resource-level attribution | Dedicated tab for supplied carbon/energy with source methodology, scope and period. Missing measurements stay unavailable; no fabricated dollars-to-carbon conversion or proprietary carbon model. |
| [Workload planning](https://www.apptio.com/products/cloudability/workload-planning/) | Saved/shared workload specifications, cross-provider/region/instance comparisons and custom pricing | Explicit supplied what-if scenarios, baseline/projected delta, assumptions, term and breakeven. No cross-cloud SKU recommendation/catalog or collaborative scenario authoring engine. |
| [Scorecards](https://www.apptio.com/products/cloudability/scorecards/) | Organization/team efficiency, pricing/elasticity/rightsizing scores and peer comparisons | Governance/scorecards table shows sourced observations/targets, owner, status and tracking action. Proprietary cohort benchmarks/percentiles and historical improvement scores require a real external source; none are fabricated. |
| Governance and integrations ([product index](https://www.apptio.com/products/cloudability/)) | Policy-led prioritization/automation and integrations such as Jira, Datadog, PagerDuty; cloud and AI accountability | Governance and prioritized actions accept scoped findings; existing optional GitHub/email hooks remain. No automatic tickets/remediation or implied integration provisioning. |
| Multi-cloud, AI and role-oriented Views ([product overview](https://www.apptio.com/products/cloudability/)) | Unified cloud/AI/SaaS visibility and different finance, engineering, product, leadership and procurement perspectives | Azure only. AI and reliability are retained first-class tabs; executive actions and engineering detail share one scope-aware snapshot. No AWS/GCP/OCI/SaaS ingestion or per-team data authorization implemented by browser filters. |

The public feature index also lists financial planning and tagging. Native budget/forecast inputs,
tag inventory/recommended coverage, source-specific dates and supplied scenarios provide related
reporting surfaces here; enterprise collaborative financial planning and proprietary tagging
automation are not implemented.

## Consolidation decisions

- One exact report identity: **FinOps: Dashboard**. Refresh the whole report once per daily task;
  don't let independent section tasks overwrite one another or combine different scope policies.
- Keep all 17 sections in navigation. Show `ready`, `partial`, `unavailable` or `unsupported`,
  source, actual source timestamp, period and a concrete gap/next action.
- Share prepared data in one run. Validate each query shape independently; don't double count
  daily and resource-total cost, credits and amortized bases, overlapping benefits, or idle/shared costs.
- Preserve separate semantics: realized savings vs opportunity, cost vs budget exposure, forecasts
  vs scenarios, provider carbon estimates vs measurements, and tagging vs approved business allocation.
- Broadly cover reporting topics while making integration gaps explicit. A visible tab is not a
  working collector, proprietary optimizer or verified product-parity claim.
- Keep the presentation independent: warm light/dark decision workspace, section rail, evidence-first
  cards/tables, no Cloudability logos, screenshot assets or cloned widget layout.

## Scope and permissions research

[Microsoft: Assign access to Cost Management data](https://learn.microsoft.com/azure/cost-management-billing/costs/assign-access-acm-data)
documents **Cost Management Reader** at management-group/subscription/RG scopes and **Owner or User
Access Administrator** to grant it. It also documents EA AO-view-charges and MCA Azure-charges policy
requirements. Role assignment alone doesn't bypass those policies.

Setup asks **Managed resources**, **Everything**, or **Selected subscriptions** before making changes.
Selected subscriptions allows one or multiple explicitly confirmed subscriptions in the verified
agent tenant, preselecting the hosting subscription when selectable. It includes unmanaged resources
within that exact list, not future subscriptions. Caller-visible inventory is only a selection
source; the agent's roles and active connector/fallback cost access must independently cover
every selection or setup/reporting fails closed. Everything means all
resources in the agent's tenant root management group, including future descendant subscriptions.
It explicitly requests an OBO grant to the actual **agent managed-identity principal**, not the
chatting user or the ARM MCP connector identity. Root scope is displayed before approval.
Root hierarchy discovery must be complete, and every cost query remains at a supported descendant
subscription scope. Cross-tenant/other-provider billing is not implied by Everything.

The separately signed-in ARM MCP user's cost permissions still matter. A root grant to the agent
does not authorize that user or manufacture unsupported billing/provider access. Every saved-report
viewer can inspect the complete snapshot; client-side section/search filters are not access control.
