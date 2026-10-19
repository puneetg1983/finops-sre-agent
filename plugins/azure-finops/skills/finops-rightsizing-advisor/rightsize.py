"""Rightsizing / idle-resource recommender for the FinOps SRE Agent pack
(skill: finops-rightsizing-advisor).

Pure, dependency-free logic so it is unit-testable offline. The agent gathers the
raw inputs via read-only `az` (Advisor cost recs, Resource Graph inventory, Azure
Monitor utilization, Consumption UsageDetails cost) and runs `recommend_rightsizing(...)`
with the runtime's sandbox Python tool. No Azure calls happen here.

The point of this module is to VALIDATE Advisor's suggestions and inventory heuristics
against real utilization and real cost before surfacing them, then rank everything by
estimated monthly savings. It never trusts a claim it cannot back with evidence:
Advisor recs with no utilization data are surfaced as `validated=None` ("unvalidated"),
and recs contradicted by high utilization are surfaced as `validated=False`.

Input shapes (all keyed/looked-up by resourceId, matched case-insensitively):

    resources    inventory rows from Resource Graph, one dict per resource:
        {
            "resourceId": "/subscriptions/.../virtualMachines/vm1",
            "type": "microsoft.compute/virtualmachines",   # lower-cased ARM type
            "sku": "Standard_D4s_v5",                        # optional
            "powerState": "stopped",                          # vm: running|stopped|deallocated
            "diskState": "Unattached",                        # disk: Attached|Unattached
            "numberOfSites": 0,                                # serverfarm: hosted site count
            "environmentId": "/subscriptions/.../managedEnvironments/env1",  # containerapp: parent ACA env
            "minReplicas": 1,                                  # containerapp: min replica count
            "readySessionInstances": 10,                       # sessionpool: pre-warmed (billed) sessions
            "tags": {"env": "prod"},                           # optional
        }

    utilization  {resourceId: {"cpu_p95": float, "cpu_avg": float,
                               "mem_p95": float|None, "sample_days": int}}
                 (percentages 0-100; from `az monitor metrics list`)

    activity     {resourceId: {"requests_total": float, "sample_days": int}}
                 invocation traffic per resource. For an Azure Container App this is the
                 Azure Monitor `Requests` metric; for a dynamic session pool it is the
                 `SessionApiRequestCount` metric. `requests_total == 0` over the window means
                 nobody used it — the driver of ACA idle detection (unused environments,
                 always-on apps with no hits, and warm session pools no one calls).

    costs        {resourceId: monthly_usd}   (aggregated from UsageDetails)

    advisor      list of Advisor cost recommendations:
        {
            "resourceId": "/subscriptions/.../virtualMachines/vm1",
            "problem": "Right-size or shut down underutilized virtual machines",
            "recommendation": "Resize to Standard_D2s_v5",   # optional free text
            "targetSku": "Standard_D2s_v5",                   # optional
            "savingsUsd": 42.10,                               # optional monthly USD
        }
"""

from __future__ import annotations

# Utilization thresholds (percent of capacity, evaluated on p95 CPU).
DEFAULT_CPU_IDLE_PCT = 5.0        # p95 CPU below this => effectively idle
DEFAULT_CPU_UNDERUTIL_PCT = 20.0  # p95 CPU below this => oversized (rightsize down)
DEFAULT_MIN_SAMPLE_DAYS = 7       # need at least this many days of metrics to trust util
DEFAULT_MIN_MONTHLY_SAVINGS_USD = 5.0
# Cost-led coverage sweep: any resource costing at least this much per month whose type has
# no idle rule (and that no other signal flagged) is surfaced as a "review" item so expensive
# resources are never silently dropped just because we lack a heuristic for their type.
DEFAULT_REVIEW_MIN_MONTHLY_USD = 20.0
# When we must estimate savings for a rightsize-down with no Advisor number, assume
# dropping one tier saves ~half. Conservative and clearly labelled as an estimate.
DEFAULT_RIGHTSIZE_SAVINGS_FRACTION = 0.5

_VM_TYPE = "microsoft.compute/virtualmachines"
_DISK_TYPE = "microsoft.compute/disks"
_PLAN_TYPE = "microsoft.web/serverfarms"
_ACA_ENV_TYPE = "microsoft.app/managedenvironments"
_ACA_APP_TYPE = "microsoft.app/containerapps"
_ACA_POOL_TYPE = "microsoft.app/sessionpools"


def _key(resource_id) -> str:
    """Case-insensitive join key (ARM resource ids are case-insensitive)."""
    return str(resource_id or "").lower()


def _lookup(mapping: dict, resource_id):
    return mapping.get(_key(resource_id))


def _index_by_id(mapping) -> dict:
    """Normalize a {resourceId: value} dict to lower-cased keys."""
    return {_key(rid): val for rid, val in (mapping or {}).items()}


class _Finding:
    """Mutable accumulator so multiple signals for one resource merge into one row."""

    def __init__(self, resource_id, resource_type):
        self.resource_id = resource_id
        self.resource_type = resource_type
        self.kind = None
        self.current_sku = None
        self.recommended_action = None
        self.current_monthly_usd = None
        self.est_monthly_savings_usd = None
        self.validated = None
        self.evidence = []
        self.sources = []

    def add_source(self, source):
        if source not in self.sources:
            self.sources.append(source)

    def to_dict(self) -> dict:
        return {
            "resourceId": self.resource_id,
            "resourceType": self.resource_type,
            "kind": self.kind,
            "currentSku": self.current_sku,
            "recommendedAction": self.recommended_action,
            "currentMonthlyUsd": _round(self.current_monthly_usd),
            "estMonthlySavingsUsd": _round(self.est_monthly_savings_usd),
            "validated": self.validated,
            "evidence": list(self.evidence),
            "sources": list(self.sources),
        }


def _round(value):
    return round(value, 2) if isinstance(value, (int, float)) else value


def recommend_rightsizing(
    resources=None,
    utilization=None,
    costs=None,
    advisor=None,
    activity=None,
    *,
    cpu_idle_pct: float = DEFAULT_CPU_IDLE_PCT,
    cpu_underutil_pct: float = DEFAULT_CPU_UNDERUTIL_PCT,
    min_sample_days: int = DEFAULT_MIN_SAMPLE_DAYS,
    min_monthly_savings_usd: float = DEFAULT_MIN_MONTHLY_SAVINGS_USD,
    rightsize_savings_fraction: float = DEFAULT_RIGHTSIZE_SAVINGS_FRACTION,
    review_min_monthly_usd: float = DEFAULT_REVIEW_MIN_MONTHLY_USD,
):
    """Merge Advisor recs, inventory heuristics, utilization, and cost into a single
    ranked list of read-only rightsizing / idle-cleanup recommendations.

    `activity` is the request-traffic signal used for Azure Container Apps idle detection:
    {resourceId: {"requests_total": float, "sample_days": int}} from the Azure Monitor
    `Requests` metric on each container app. It lets the skill flag unused Container Apps
    environments (empty, or all their apps received zero traffic) and always-on container
    apps (minReplicas>=1) that keep billing while receiving no requests.

    Returns a list of finding dicts sorted by estimated monthly savings (descending);
    findings whose savings can be quantified below `min_monthly_savings_usd` are dropped,
    while findings with unknown savings are kept (they cannot be ruled out) and sort last.
    """
    resources = resources or []
    util = _index_by_id(utilization)
    cost = _index_by_id(costs)
    activity = _index_by_id(activity)
    advisor = advisor or []

    findings: dict = {}

    def finding_for(resource_id, resource_type=None):
        key = _key(resource_id)
        if key not in findings:
            findings[key] = _Finding(resource_id, resource_type)
        elif resource_type and not findings[key].resource_type:
            findings[key].resource_type = resource_type
        return findings[key]

    # ---- 1. Inventory heuristics (no utilization required) -------------------
    for res in resources:
        rid = res.get("resourceId")
        if not rid:
            continue
        rtype = str(res.get("type") or "").lower()
        idle = _inventory_idle(res, rtype)
        if idle is None:
            continue
        kind, action, evidence = idle
        monthly = _lookup(cost, rid)
        f = finding_for(rid, rtype)
        f.kind = kind
        f.current_sku = res.get("sku")
        f.recommended_action = action
        f.current_monthly_usd = monthly
        f.est_monthly_savings_usd = monthly  # idle => the whole line item is recoverable
        f.validated = True                    # inventory state is a fact, not a guess
        f.evidence.append(evidence)
        f.add_source("resource-graph")

    # ---- 1b. Azure Container Apps idle detection ----------------------------
    # Unused ACA is pure waste: empty/no-traffic Container Apps environments, always-on
    # container apps (minReplicas>=1) with no requests, and dynamic session pools holding
    # warm readySessionInstances with no session activity (these often top the bill because
    # `az resource list` hides the sessionpools type, so they escape casual review).
    env_children: dict = {}
    for res in resources:
        if str(res.get("type") or "").lower() == _ACA_APP_TYPE:
            env_id = res.get("environmentId")
            if env_id:
                env_children.setdefault(_key(env_id), []).append(res)

    for res in resources:
        rid = res.get("resourceId")
        if not rid or _key(rid) in findings:
            continue
        rtype = str(res.get("type") or "").lower()
        if rtype == _ACA_ENV_TYPE:
            idle = _aca_env_idle(res, env_children.get(_key(rid), []), activity, min_sample_days)
        elif rtype == _ACA_APP_TYPE:
            idle = _aca_app_idle(res, activity, min_sample_days)
        elif rtype == _ACA_POOL_TYPE:
            idle = _aca_sessionpool_idle(res, activity, min_sample_days)
        else:
            continue
        if idle is None:
            continue
        action, evidence, from_activity, validated = idle
        monthly = _lookup(cost, rid)
        f = finding_for(rid, rtype)
        f.kind = "idle"
        f.current_sku = res.get("sku")
        f.recommended_action = action
        f.current_monthly_usd = monthly
        f.est_monthly_savings_usd = monthly  # idle => the whole line item is recoverable
        f.validated = validated
        f.evidence.append(evidence)
        f.add_source("resource-graph")
        if from_activity:
            f.add_source("azure-monitor")

    # ---- 2. Utilization-driven idle / oversized -----------------------------
    for res in resources:
        rid = res.get("resourceId")
        if not rid or _key(rid) in findings:
            continue  # already flagged idle by inventory
        rtype = str(res.get("type") or "").lower()
        if rtype != _VM_TYPE:
            continue  # only VM compute is rightsized from CPU metrics here
        metrics = _lookup(util, rid)
        verdict = _utilization_verdict(metrics, cpu_idle_pct, cpu_underutil_pct, min_sample_days)
        if verdict is None:
            continue
        kind, evidence = verdict
        monthly = _lookup(cost, rid)
        f = finding_for(rid, rtype)
        f.kind = kind
        f.current_sku = res.get("sku")
        f.current_monthly_usd = monthly
        f.validated = True
        f.evidence.append(evidence)
        f.add_source("azure-monitor")
        if kind == "idle":
            f.recommended_action = "Deallocate or delete (idle)."
            f.est_monthly_savings_usd = monthly
        else:  # oversized
            f.recommended_action = "Rightsize down one tier."
            f.est_monthly_savings_usd = (
                monthly * rightsize_savings_fraction if monthly is not None else None
            )

    # ---- 3. Advisor cost recs (validated against utilization) ---------------
    types_by_id = {_key(r.get("resourceId")): str(r.get("type") or "").lower() or None for r in resources}
    for rec in advisor:
        rid = rec.get("resourceId")
        if not rid:
            continue
        f = finding_for(rid, types_by_id.get(_key(rid)))
        f.add_source("advisor")
        if f.kind is None:
            f.kind = "advisor"
        if f.current_monthly_usd is None:
            f.current_monthly_usd = _lookup(cost, rid)

        target = rec.get("targetSku")
        if f.recommended_action is None:
            if target:
                f.recommended_action = f"Resize to {target}."
            else:
                f.recommended_action = rec.get("recommendation") or rec.get("problem") or "See Advisor cost recommendation."

        f.evidence.append("Azure Advisor: " + str(rec.get("problem") or rec.get("recommendation") or "cost recommendation"))

        metrics = _lookup(util, rid)
        validation = _validate_advisor(metrics, cpu_underutil_pct, min_sample_days)
        if validation is not None:
            f.validated, note = validation
            f.evidence.append(note)
        elif f.validated is None:
            f.evidence.append("No utilization metrics available — validate before acting.")

        estimated = _advisor_savings_estimate(
            _as_float(rec.get("savingsUsd")), f.current_monthly_usd, rightsize_savings_fraction
        )
        if estimated is not None and (
            f.est_monthly_savings_usd is None or estimated > f.est_monthly_savings_usd
        ):
            f.est_monthly_savings_usd = estimated

    # ---- 4. Cost-led coverage sweep -----------------------------------------
    # Guarantee no expensive resource is silently dropped just because we lack an idle rule
    # for its type. Any costly resource that no signal above evaluated is surfaced as a
    # "review" item (no savings claimed) so the report's coverage is complete by spend.
    _COVERED_TYPES = {
        _VM_TYPE, _DISK_TYPE, _PLAN_TYPE,
        _ACA_ENV_TYPE, _ACA_APP_TYPE, _ACA_POOL_TYPE,
    }
    for rid, monthly in (costs or {}).items():
        if _key(rid) in findings:
            continue  # already evaluated/flagged by a rule or Advisor
        if not isinstance(monthly, (int, float)) or monthly < review_min_monthly_usd:
            continue
        rtype = _type_from_id(rid)
        if rtype in _COVERED_TYPES:
            continue  # a rule covers this type and chose not to flag it => evaluated, fine
        f = finding_for(rid, rtype)
        f.kind = "review"
        f.current_monthly_usd = monthly
        f.est_monthly_savings_usd = None  # unknown — we make no savings claim
        f.validated = None
        f.recommended_action = "High monthly spend with no automated idle rule for this resource type — review manually."
        f.evidence.append(
            f"${monthly:.2f}/mo; type '{rtype or 'unknown'}' has no idle heuristic yet and Advisor did not flag it."
        )
        f.add_source("cost-coverage")

    return _rank(findings.values(), min_monthly_savings_usd)


def _type_from_id(resource_id):
    """Parse the lower-cased ARM type (`namespace/type`) from a resource id, or None."""
    parts = str(resource_id or "").lower().split("/providers/")
    if len(parts) < 2:
        return None
    seg = parts[-1].split("/")
    return f"{seg[0]}/{seg[1]}" if len(seg) >= 2 else None


def _inventory_idle(res: dict, rtype: str):
    """Return (kind, action, evidence) for inventory-only idle patterns, else None."""
    if rtype == _DISK_TYPE and str(res.get("diskState") or "").lower() == "unattached":
        return ("idle", "Delete the unattached managed disk.", "Managed disk is Unattached.")

    if rtype == _VM_TYPE:
        power = str(res.get("powerState") or "").lower()
        # "stopped" (not "deallocated") still bills for compute — deallocate to stop it.
        if power == "stopped" or power.endswith("/stopped"):
            return (
                "idle",
                "Deallocate the VM (Stopped state still incurs compute charges).",
                "VM is Stopped but not Deallocated.",
            )

    if rtype == _PLAN_TYPE and _as_int(res.get("numberOfSites")) == 0:
        return (
            "idle",
            "Delete or scale down the empty App Service plan.",
            "App Service plan hosts 0 sites.",
        )

    return None


def _aca_activity_is_idle(act, min_sample_days):
    """True when the Requests signal has enough coverage AND shows zero traffic,
    False when it shows traffic, None when coverage is insufficient to judge."""
    if not act or _as_int(act.get("sample_days")) < min_sample_days:
        return None
    requests_total = _as_float(act.get("requests_total"))
    if requests_total is None:
        return None
    return requests_total <= 0


def _aca_app_idle(res, activity, min_sample_days):
    """Return (action, evidence, from_activity, validated) for an idle Container App, else None.

    Needs the Requests signal (from Azure Monitor) to claim "no traffic" — without it the
    app is left unvalidated rather than guessed. An always-on app (minReplicas>=1) with no
    traffic bills continuously and is the strongest signal.
    """
    is_idle = _aca_activity_is_idle(_lookup(activity, res.get("resourceId")), min_sample_days)
    if not is_idle:  # None (no coverage) or False (has traffic) => don't flag
        return None
    days = _as_int(_lookup(activity, res.get("resourceId")).get("sample_days"))
    min_replicas = _as_int(res.get("minReplicas"))
    if min_replicas >= 1:
        return (
            "Delete the Container App or set minReplicas=0 — it is always-on with no traffic.",
            f"0 requests over {days}d and minReplicas={min_replicas} (always-on, billing while idle).",
            True,
            True,
        )
    return (
        "Delete the unused Container App (no traffic).",
        f"0 requests over {days}d (scales to zero; low residual cost).",
        True,
        True,
    )


def _aca_env_idle(res, children, activity, min_sample_days):
    """Return (action, evidence, from_activity, validated) for an idle Container Apps environment.

    Empty environments (0 apps) are an inventory fact. An environment that still hosts apps
    is flagged only when EVERY app has request coverage and zero traffic — if any app has
    traffic, or any lacks coverage, the environment is left alone (conservative).
    """
    if not children:
        return (
            "Delete the empty Container Apps environment.",
            "Environment hosts 0 container apps.",
            False,
            True,
        )
    for app in children:
        if _aca_activity_is_idle(_lookup(activity, app.get("resourceId")), min_sample_days) is not True:
            return None  # some app has traffic, or we can't confirm it doesn't
    return (
        "Delete the Container Apps environment — none of its apps received traffic.",
        f"All {len(children)} app(s) had 0 requests over the window (environment unused).",
        True,
        True,
    )


def _aca_sessionpool_idle(res, activity, min_sample_days):
    """Return (action, evidence, from_activity, validated) for an idle dynamic session pool, else None.

    A dynamic session pool keeps `readySessionInstances` pre-warmed containers that bill
    continuously — real cost only exists when that count is >= 1. `activity["requests_total"]`
    carries the pool's SessionApiRequestCount (session invocations): zero over the window means
    nobody used the pool, so the whole warm pool is recoverable. When the pool has ready
    instances but the usage metric is unavailable, it is surfaced as UNVALIDATED (verify usage)
    rather than dropped — these pools are frequently the largest untracked line items.
    """
    ready = _as_int(res.get("readySessionInstances"))
    if ready < 1:
        return None  # no warm instances => no idle cost to reclaim
    is_idle = _aca_activity_is_idle(_lookup(activity, res.get("resourceId")), min_sample_days)
    if is_idle is False:
        return None  # the pool is in use
    action = f"Set readySessionInstances=0 or delete the session pool ({ready} warm sessions billing while idle)."
    if is_idle is True:
        days = _as_int(_lookup(activity, res.get("resourceId")).get("sample_days"))
        return (action, f"0 session requests over {days}d with {ready} ready sessions (warm pool billing, unused).", True, True)
    return (action, f"{ready} ready sessions configured; no usage metric available — verify before reducing.", False, None)


def _utilization_verdict(metrics, cpu_idle_pct, cpu_underutil_pct, min_sample_days):
    """Classify a VM from its utilization metrics, or None if insufficient/healthy."""
    if not metrics or _as_int(metrics.get("sample_days")) < min_sample_days:
        return None
    cpu_p95 = _as_float(metrics.get("cpu_p95"))
    if cpu_p95 is None:
        return None
    sample_days = _as_int(metrics.get("sample_days"))
    if cpu_p95 < cpu_idle_pct:
        return ("idle", f"p95 CPU {cpu_p95:.1f}% over {sample_days}d (idle).")
    if cpu_p95 < cpu_underutil_pct:
        return ("oversized", f"p95 CPU {cpu_p95:.1f}% over {sample_days}d (underutilized).")
    return None


def _validate_advisor(metrics, cpu_underutil_pct, min_sample_days):
    """Return (validated_bool, note) when utilization data is present, else None."""
    if not metrics or _as_int(metrics.get("sample_days")) < min_sample_days:
        return None
    cpu_p95 = _as_float(metrics.get("cpu_p95"))
    if cpu_p95 is None:
        return None
    if cpu_p95 < cpu_underutil_pct:
        return (True, f"Utilization confirms: p95 CPU {cpu_p95:.1f}% (< {cpu_underutil_pct:.0f}%).")
    return (False, f"Utilization contradicts Advisor: p95 CPU {cpu_p95:.1f}% (>= {cpu_underutil_pct:.0f}%) — verify before acting.")


def _advisor_savings_estimate(advisor_savings, monthly, fraction):
    if advisor_savings is not None:
        return advisor_savings
    if monthly is not None:
        return monthly * fraction
    return None


def _rank(findings, min_monthly_savings_usd):
    result = []
    for f in findings:
        d = f.to_dict()
        savings = d["estMonthlySavingsUsd"]
        if isinstance(savings, (int, float)) and savings < min_monthly_savings_usd:
            continue  # quantified and too small to bother
        result.append(d)

    # Known savings first (desc); unknown-savings findings last.
    def sort_key(d):
        savings = d["estMonthlySavingsUsd"]
        known = isinstance(savings, (int, float))
        return (0 if known else 1, -(savings if known else 0))

    result.sort(key=sort_key)
    return result


def _as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
