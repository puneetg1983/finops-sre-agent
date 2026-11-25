"""Decision briefing: explanations, owners, actions and honest check states."""

import importlib.util
from pathlib import Path

import pytest

SKILLS = Path(__file__).resolve().parents[1] / "plugins" / "azure-finops" / "skills" / "finops-report-renderer"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


briefing = load("briefing_under_test", SKILLS / "briefing.py")
dashboard = load("briefing_dashboard", SKILLS / "dashboard.py")
renderer = load("briefing_renderer", SKILLS / "render.py")
NOW = "2026-11-16T22:30:00Z"
WINDOW = ["2026-10-18", "2026-11-16"]
RG = "/subscriptions/sub/resourceGroups/web"
PLAN = RG + "/providers/Microsoft.Web/serverfarms/plan"
DISK = RG + "/providers/Microsoft.Compute/disks/old"


def inputs(**overrides):
    base = {
        "owners": [{"scope": RG, "owner": "web-team", "source": "tag"}],
        "anomalies": [{"dimension": "resourceGroup", "value": "web", "date": "2026-11-15", "kind": "spike",
                       "current_usd": 200, "baseline_mean_usd": 50, "impact_usd": 150}],
        "change_events": [
            {"time": "2026-11-14T10:00:00Z", "resourceId": PLAN, "kind": "sku",
             "summary": "P1v3 to P3v3", "actor": "bob@contoso.com"},
            {"time": "2026-11-01T10:00:00Z", "resourceId": PLAN, "kind": "deployment", "summary": "too old"},
        ],
        "rightsizing": [
            {"resourceId": DISK, "kind": "idle", "recommendedAction": "Delete unattached disk",
             "estMonthlySavingsUsd": 40, "validated": True, "evidence": ["Unattached for 60 days"]},
            {"resourceId": PLAN, "kind": "advisor", "recommendedAction": "Downsize to P1v3",
             "estMonthlySavingsUsd": 300, "validated": None},
            {"resourceId": PLAN, "kind": "advisor", "recommendedAction": "Duplicate", "estMonthlySavingsUsd": 300},
        ],
        "tag_gaps": [{"resourceId": "/subscriptions/sub/resourceGroups/x/providers/A/b/c", "monthly_usd": 12,
                      "missing": ["owner"], "proposed": {"owner": "x-team"}, "basis": "resource group tag"}],
        "agent_cost": {"period_usd": 60, "window": WINDOW, "source": "Cost Management"},
        "repo": "contoso/infra",
    }
    base.update(overrides)
    return base


def test_findings_explain_rank_assign_owner_and_require_approval():
    model = briefing.build_briefing(inputs(), generated_at=NOW, window=WINDOW)
    assert [item["job"] for item in model["findings"]] == ["tier", "spike", "orphan", "tags"]
    spike = model["findings"][1]
    assert spike["confidence"] == "High" and spike["owner"] == "web-team"
    assert "P1v3 to P3v3" in spike["why"] and not any("too old" in line for line in spike["evidence"])
    tier = model["findings"][0]
    assert tier["impactText"] == "$300.00"
    assert "pull request in contoso/infra" in tier["actions"][0]["prompt"]
    for item in model["findings"] + model["rollups"]:
        assert item["id"].startswith("F-")
        for action in item["actions"]:
            assert item["id"] in action["prompt"] and "{id}" not in action["prompt"]
            if action["approval"] == "Needs your approval":
                assert "do not make any change until I approve it" in action["prompt"]
    waste = next(item for item in model["rollups"] if item["job"] == "waste")
    assert waste["impactText"] == "$340.00" and "2 savings item" in waste["title"]
    assert "6 decision" not in model["headline"] and "$340.00 a month in possible savings" in model["headline"]
    assert model == briefing.build_briefing(inputs(), generated_at=NOW, window=WINDOW)


def test_unmatched_spike_is_low_confidence_and_missing_checks_are_explicit():
    model = briefing.build_briefing(
        {"anomalies": inputs()["anomalies"], "change_events": [],
         "agent_cost": {"status": "unavailable", "reason": "Cost Management read denied"}},
        generated_at=NOW, window=WINDOW)
    assert model["findings"][0]["confidence"] == "Low"
    assert model["findings"][0]["owner"] == "Unassigned"
    states = {check["job"]: check["state"] for check in model["checks"]}
    assert states == {"spike": "Checked", "waste": "Not checked", "tags": "Not checked",
                      "orphan": "Not checked", "tier": "Not checked", "agent-cost": "Could not check"}
    assert model["checks"][-1]["detail"] == "Cost Management read denied"
    with pytest.raises(ValueError, match="unknown briefing inputs"):
        briefing.build_briefing({"snooze": []}, generated_at=NOW, window=WINDOW)
    with pytest.raises(ValueError, match="kind"):
        briefing.build_briefing({"change_events": [{"time": NOW, "resourceId": RG, "kind": "x", "summary": "s"}]},
                                generated_at=NOW, window=WINDOW)


def test_briefing_is_first_tab_and_renders_escaped_copy_actions():
    evil = '"><img src=x onerror=alert(1)>'
    data = inputs(owners=[{"scope": RG, "owner": evil}])
    model = dashboard.build_dashboard_model({}, refreshed_at=NOW, scope_summary=[RG], window=WINDOW,
                                            briefing=data)
    first = model["tabs"][0]
    assert first["id"] == "briefing" and first["status"] == "ready"
    assert model["metrics"] == model["tabs"][1]["metrics"]
    output = renderer.render_report(model)
    assert 'id="tab-briefing"' in output and 'aria-selected="true"' in output
    assert output.count('class="copy-prompt"') >= 5
    assert evil not in output and "innerHTML" not in output and "navigator.clipboard" in output
    missing = dashboard.build_dashboard_model({}, refreshed_at=NOW, scope_summary=[RG], window=WINDOW)
    assert missing["tabs"][0]["displayStatus"] == "Not collected"
    assert "not built in this run" in renderer.render_report(missing)
