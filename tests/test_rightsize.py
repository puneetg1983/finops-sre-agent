"""Layer-1 unit tests for the rightsizing advisor (offline, deterministic, no Azure)."""

import importlib.util
from pathlib import Path

# Load rightsize.py directly from the skill folder (no package install needed).
_PATH = (
    Path(__file__).resolve().parent.parent
    / "plugins"
    / "azure-finops"
    / "skills"
    / "finops-rightsizing-advisor"
    / "rightsize.py"
)
_spec = importlib.util.spec_from_file_location("rightsize", _PATH)
rightsize = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rightsize)

recommend = rightsize.recommend_rightsizing

VM = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm1"
DISK = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/disks/disk1"
PLAN = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Web/serverfarms/plan1"
ACA_ENV = "/subscriptions/s/resourceGroups/mcp-env-rg-001/providers/Microsoft.App/managedEnvironments/mcp-env"
ACA_ENV2 = "/subscriptions/s/resourceGroups/extract_rg/providers/Microsoft.App/managedEnvironments/extract-env"
ACA_APP = "/subscriptions/s/resourceGroups/mcp-env-rg-001/providers/Microsoft.App/containerApps/app1"


def _by_id(findings):
    return {f["resourceId"].lower(): f for f in findings}


def test_empty_inputs_return_empty():
    assert recommend() == []
    assert recommend(resources=[], utilization={}, costs={}, advisor=[]) == []


def test_unattached_disk_is_idle_full_savings():
    res = [{"resourceId": DISK, "type": "microsoft.compute/disks", "diskState": "Unattached"}]
    out = recommend(resources=res, costs={DISK: 12.0})
    assert len(out) == 1
    f = out[0]
    assert f["kind"] == "idle"
    assert f["estMonthlySavingsUsd"] == 12.0
    assert f["validated"] is True
    assert "resource-graph" in f["sources"]
    assert "Delete" in f["recommendedAction"]


def test_attached_disk_not_flagged():
    res = [{"resourceId": DISK, "type": "microsoft.compute/disks", "diskState": "Attached"}]
    assert recommend(resources=res, costs={DISK: 12.0}) == []


def test_stopped_vm_is_idle_but_deallocated_is_not():
    stopped = [{"resourceId": VM, "type": "microsoft.compute/virtualmachines", "powerState": "stopped"}]
    out = recommend(resources=stopped, costs={VM: 60.0})
    assert out and out[0]["kind"] == "idle"
    assert "Deallocate" in out[0]["recommendedAction"]

    deallocated = [{"resourceId": VM, "type": "microsoft.compute/virtualmachines", "powerState": "deallocated"}]
    assert recommend(resources=deallocated, costs={VM: 60.0}) == []


def test_empty_app_service_plan_flagged():
    res = [{"resourceId": PLAN, "type": "microsoft.web/serverfarms", "numberOfSites": 0}]
    out = recommend(resources=res, costs={PLAN: 55.0})
    assert out and out[0]["kind"] == "idle" and out[0]["estMonthlySavingsUsd"] == 55.0

    busy = [{"resourceId": PLAN, "type": "microsoft.web/serverfarms", "numberOfSites": 3}]
    assert recommend(resources=busy, costs={PLAN: 55.0}) == []


def test_low_cpu_vm_flagged_idle_via_utilization():
    res = [{"resourceId": VM, "type": "microsoft.compute/virtualmachines", "sku": "Standard_D4s_v5"}]
    util = {VM: {"cpu_p95": 2.0, "cpu_avg": 1.0, "sample_days": 14}}
    out = recommend(resources=res, utilization=util, costs={VM: 100.0})
    assert out and out[0]["kind"] == "idle"
    assert out[0]["estMonthlySavingsUsd"] == 100.0
    assert "azure-monitor" in out[0]["sources"]


def test_moderate_cpu_vm_flagged_oversized_half_savings():
    res = [{"resourceId": VM, "type": "microsoft.compute/virtualmachines"}]
    util = {VM: {"cpu_p95": 12.0, "sample_days": 14}}
    out = recommend(resources=res, utilization=util, costs={VM: 100.0})
    assert out and out[0]["kind"] == "oversized"
    assert out[0]["estMonthlySavingsUsd"] == 50.0


def test_healthy_vm_not_flagged():
    res = [{"resourceId": VM, "type": "microsoft.compute/virtualmachines"}]
    util = {VM: {"cpu_p95": 65.0, "sample_days": 14}}
    assert recommend(resources=res, utilization=util, costs={VM: 100.0}) == []


def test_insufficient_sample_days_ignored():
    res = [{"resourceId": VM, "type": "microsoft.compute/virtualmachines"}]
    util = {VM: {"cpu_p95": 1.0, "sample_days": 2}}
    assert recommend(resources=res, utilization=util, costs={VM: 100.0}) == []


def test_advisor_without_utilization_is_unvalidated():
    advisor = [{"resourceId": VM, "problem": "Right-size underutilized VM", "savingsUsd": 30.0}]
    out = recommend(advisor=advisor, costs={VM: 100.0})
    assert len(out) == 1
    f = out[0]
    assert f["kind"] == "advisor"
    assert f["validated"] is None
    assert f["estMonthlySavingsUsd"] == 30.0
    assert any("validate before acting" in e.lower() for e in f["evidence"])


def test_advisor_confirmed_by_low_utilization():
    res = [{"resourceId": VM, "type": "microsoft.compute/virtualmachines"}]
    advisor = [{"resourceId": VM, "problem": "Right-size VM", "targetSku": "Standard_D2s_v5", "savingsUsd": 40.0}]
    util = {VM: {"cpu_p95": 8.0, "sample_days": 30}}
    out = recommend(resources=res, utilization=util, advisor=advisor, costs={VM: 120.0})
    f = out[0]
    # Utilization-driven oversized finding merges with the Advisor rec on the same id.
    assert f["validated"] is True
    assert "advisor" in f["sources"] and "azure-monitor" in f["sources"]
    # Advisor's explicit savings ($40) beats the 50%-estimate ($60)? No: max is taken.
    assert f["estMonthlySavingsUsd"] == 60.0


def test_advisor_contradicted_by_high_utilization():
    res = [{"resourceId": VM, "type": "microsoft.compute/virtualmachines"}]
    advisor = [{"resourceId": VM, "problem": "Right-size VM", "savingsUsd": 40.0}]
    util = {VM: {"cpu_p95": 75.0, "sample_days": 30}}
    out = recommend(resources=res, utilization=util, advisor=advisor, costs={VM: 120.0})
    assert len(out) == 1
    f = out[0]
    assert f["validated"] is False
    assert any("contradicts" in e.lower() for e in f["evidence"])


def test_case_insensitive_join_across_sources():
    res = [{"resourceId": VM.upper(), "type": "microsoft.compute/virtualmachines"}]
    advisor = [{"resourceId": VM.lower(), "problem": "Right-size VM", "savingsUsd": 20.0}]
    util = {VM: {"cpu_p95": 10.0, "sample_days": 30}}
    out = recommend(resources=res, utilization=util, advisor=advisor, costs={VM: 80.0})
    assert len(out) == 1  # all three signals collapse to one finding


def test_below_min_savings_dropped_but_unknown_kept():
    res = [
        {"resourceId": DISK, "type": "microsoft.compute/disks", "diskState": "Unattached"},
        {"resourceId": PLAN, "type": "microsoft.web/serverfarms", "numberOfSites": 0},
    ]
    # DISK savings $2 (< $5 default) dropped; PLAN has no cost => unknown, kept.
    out = recommend(resources=res, costs={DISK: 2.0})
    ids = _by_id(out)
    assert DISK.lower() not in ids
    assert PLAN.lower() in ids
    assert ids[PLAN.lower()]["estMonthlySavingsUsd"] is None


def test_ranking_known_savings_first_desc_then_unknown():
    res = [
        {"resourceId": DISK, "type": "microsoft.compute/disks", "diskState": "Unattached"},
        {"resourceId": PLAN, "type": "microsoft.web/serverfarms", "numberOfSites": 0},
        {"resourceId": VM, "type": "microsoft.compute/virtualmachines", "powerState": "stopped"},
    ]
    costs = {VM: 200.0, DISK: 30.0}  # PLAN unknown
    out = recommend(resources=res, costs=costs)
    order = [f["resourceId"] for f in out]
    assert order == [VM, DISK, PLAN]  # 200, 30, then unknown last


# --- Azure Container Apps (ACA) idle detection ---------------------------------


def test_empty_aca_environment_flagged_idle():
    res = [{"resourceId": ACA_ENV, "type": "microsoft.app/managedenvironments"}]
    out = recommend(resources=res, costs={ACA_ENV: 40.0})
    assert out and out[0]["kind"] == "idle"
    assert out[0]["estMonthlySavingsUsd"] == 40.0
    assert out[0]["validated"] is True
    assert "empty Container Apps environment" in out[0]["recommendedAction"]
    assert "resource-graph" in out[0]["sources"]


def test_aca_environment_with_untraffic_apps_flagged_idle():
    res = [
        {"resourceId": ACA_ENV, "type": "microsoft.app/managedenvironments"},
        {"resourceId": ACA_APP, "type": "microsoft.app/containerapps",
         "environmentId": ACA_ENV, "minReplicas": 0},
    ]
    activity = {ACA_APP: {"requests_total": 0, "sample_days": 14}}
    out = recommend(resources=res, activity=activity, costs={ACA_ENV: 25.0, ACA_APP: 1.0})
    ids = _by_id(out)
    # Environment flagged as unused (all apps had zero traffic).
    assert ids[ACA_ENV.lower()]["kind"] == "idle"
    assert "none of its apps received traffic" in ids[ACA_ENV.lower()]["recommendedAction"]
    assert "azure-monitor" in ids[ACA_ENV.lower()]["sources"]


def test_aca_environment_with_traffic_not_flagged():
    res = [
        {"resourceId": ACA_ENV, "type": "microsoft.app/managedenvironments"},
        {"resourceId": ACA_APP, "type": "microsoft.app/containerapps",
         "environmentId": ACA_ENV, "minReplicas": 0},
    ]
    activity = {ACA_APP: {"requests_total": 5000, "sample_days": 14}}
    assert recommend(resources=res, activity=activity, costs={ACA_ENV: 25.0}) == []


def test_aca_environment_unconfirmed_activity_not_flagged():
    # An env with an app but no request-metric coverage must not be claimed idle.
    res = [
        {"resourceId": ACA_ENV, "type": "microsoft.app/managedenvironments"},
        {"resourceId": ACA_APP, "type": "microsoft.app/containerapps",
         "environmentId": ACA_ENV, "minReplicas": 0},
    ]
    assert recommend(resources=res, costs={ACA_ENV: 25.0}) == []


def test_always_on_aca_app_with_no_traffic_flagged_full_savings():
    res = [{"resourceId": ACA_APP, "type": "microsoft.app/containerapps",
            "environmentId": ACA_ENV, "minReplicas": 2}]
    activity = {ACA_APP: {"requests_total": 0, "sample_days": 30}}
    out = recommend(resources=res, activity=activity, costs={ACA_APP: 45.0})
    assert out and out[0]["kind"] == "idle"
    assert out[0]["estMonthlySavingsUsd"] == 45.0
    assert "always-on" in out[0]["recommendedAction"]
    assert out[0]["validated"] is True


def test_scale_to_zero_aca_app_low_cost_dropped():
    # minReplicas=0 app with no traffic scales to zero; residual cost below threshold drops it.
    res = [{"resourceId": ACA_APP, "type": "microsoft.app/containerapps",
            "environmentId": ACA_ENV, "minReplicas": 0}]
    activity = {ACA_APP: {"requests_total": 0, "sample_days": 30}}
    assert recommend(resources=res, activity=activity, costs={ACA_APP: 1.0}) == []


def test_aca_app_insufficient_activity_days_ignored():
    res = [{"resourceId": ACA_APP, "type": "microsoft.app/containerapps",
            "environmentId": ACA_ENV, "minReplicas": 2}]
    activity = {ACA_APP: {"requests_total": 0, "sample_days": 3}}
    assert recommend(resources=res, activity=activity, costs={ACA_APP: 45.0}) == []


# --- Session pools -----------------------------------------------------------

POOL = "/subscriptions/s/resourceGroups/extract_rg/providers/Microsoft.App/sessionPools/extractpooltwo"


def test_session_pool_warm_and_idle_flagged_full_savings():
    res = [{"resourceId": POOL, "type": "microsoft.app/sessionpools", "readySessionInstances": 10}]
    activity = {POOL: {"requests_total": 0, "sample_days": 14}}
    out = recommend(resources=res, activity=activity, costs={POOL: 331.0})
    ids = _by_id(out)
    f = ids[POOL.lower()]
    assert f["kind"] == "idle"
    assert f["estMonthlySavingsUsd"] == 331.0
    assert f["validated"] is True
    assert "readySessionInstances=0" in f["recommendedAction"]
    assert "azure-monitor" in f["sources"]


def test_session_pool_warm_no_metric_surfaced_unvalidated():
    res = [{"resourceId": POOL, "type": "microsoft.app/sessionpools", "readySessionInstances": 5}]
    out = recommend(resources=res, costs={POOL: 331.0})  # no activity metric
    ids = _by_id(out)
    f = ids[POOL.lower()]
    assert f["kind"] == "idle"
    assert f["validated"] is None  # kept but unvalidated — verify usage
    assert f["estMonthlySavingsUsd"] == 331.0


def test_session_pool_zero_ready_not_flagged():
    res = [{"resourceId": POOL, "type": "microsoft.app/sessionpools", "readySessionInstances": 0}]
    activity = {POOL: {"requests_total": 0, "sample_days": 14}}
    assert recommend(resources=res, activity=activity, costs={POOL: 331.0}) == []


def test_session_pool_in_use_not_flagged():
    res = [{"resourceId": POOL, "type": "microsoft.app/sessionpools", "readySessionInstances": 10}]
    activity = {POOL: {"requests_total": 4200, "sample_days": 14}}
    assert recommend(resources=res, activity=activity, costs={POOL: 331.0}) == []


# --- Cost-led coverage sweep -------------------------------------------------

REDIS = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Cache/Redis/cache1"


def test_costly_uncovered_type_surfaced_as_review():
    # Redis has no idle rule and no Advisor rec — but it is expensive, so it must not vanish.
    out = recommend(costs={REDIS: 52.0})
    ids = _by_id(out)
    f = ids[REDIS.lower()]
    assert f["kind"] == "review"
    assert f["estMonthlySavingsUsd"] is None
    assert f["validated"] is None
    assert "cost-coverage" in f["sources"]


def test_cheap_uncovered_type_not_reviewed():
    assert recommend(costs={REDIS: 3.0}) == []  # below review threshold


def test_covered_type_not_flagged_is_not_reviewed():
    # A VM that no idle rule flagged was still evaluated — don't double-report it as "review".
    res = [{"resourceId": VM, "type": "microsoft.compute/virtualmachines"}]
    util = {VM: {"cpu_p95": 80.0, "sample_days": 30}}
    assert recommend(resources=res, utilization=util, costs={VM: 200.0}) == []


def test_review_does_not_duplicate_flagged_resource():
    # A pool already flagged idle must not also appear as a cost-coverage review item.
    res = [{"resourceId": POOL, "type": "microsoft.app/sessionpools", "readySessionInstances": 10}]
    activity = {POOL: {"requests_total": 0, "sample_days": 14}}
    out = recommend(resources=res, activity=activity, costs={POOL: 331.0})
    assert len(out) == 1
    assert out[0]["kind"] == "idle"

