"""Consolidation, evidence and explicit scope-policy regression tests."""

import copy
import importlib.util
import re
from pathlib import Path

import pytest

SKILLS = Path(__file__).resolve().parents[1] / "plugins" / "azure-finops" / "skills"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


dashboard = load("dashboard", SKILLS / "finops-report-renderer" / "dashboard.py")
renderer = load("dashboard_renderer", SKILLS / "finops-report-renderer" / "render.py")
scope = load("dashboard_scope", SKILLS / "finops-managed-scope" / "scope.py")
SCOPE = "/subscriptions/sub/resourceGroups/managed"
NOW = "2026-11-16T22:30:00Z"
WINDOW = ["2026-10-18", "2026-11-16"]
TENANT = "11111111-2222-3333-4444-555555555555"
ROOT = f"/providers/Microsoft.Management/managementGroups/{TENANT}"
CHILD = "/providers/Microsoft.Management/managementGroups/child"


def envelope(data, **overrides):
    return {
        "status": "ready", "source": "Scoped test source", "refreshed_at": NOW,
        "window": WINDOW, "scope_summary": [SCOPE], "data": data, **overrides,
    }


def build(sources=None, **context):
    return dashboard.build_dashboard_model(
        sources or {}, refreshed_at=NOW, scope_summary=[SCOPE], window=WINDOW, **context
    )


def tab(model, key):
    return next(row for row in model["tabs"] if row["id"] == key)


def test_consolidation_has_one_title_all_tabs_and_honest_missing_sources():
    model = build({"overview": envelope({
        "total_usd": 12.30, "daily": [{"date": "2026-11-15", "cost_usd": 12.30}],
    })})
    assert model["title"] == "FinOps: Dashboard"
    assert len(model["tabs"]) == 17
    assert tab(model, "overview")["status"] == "ready"
    assert tab(model, "commitments")["status"] == "unavailable"
    assert tab(model, "commitments")["metrics"] == []
    assert model["partial"] is True
    assert tab(model, "coverage")["sections"][0]["rows"][0][3] == NOW
    assert model == build({"overview": envelope({
        "total_usd": 12.30, "daily": [{"date": "2026-11-15", "cost_usd": 12.30}],
    })})


def test_underlying_partial_and_warnings_cannot_be_hidden_by_ready_envelope():
    model = build({"overview": envelope({"total_usd": 8, "partial": True,
                                       "warnings": ["Subscription denied"]})})
    assert tab(model, "overview")["status"] == "partial"
    assert "Overview: Subscription denied" in model["warnings"]


@pytest.mark.parametrize("change,match", [
    ({"status": "partial"}, "reason"),
    ({"status": "healthy"}, "status"),
    ({"source": ""}, "source"),
    ({"refreshed_at": "yesterday"}, "timestamp"),
    ({"refreshed_at": "2026-11-16"}, "timezone"),
    ({"window": ["2026-11-16", "2026-10-18"]}, "reversed"),
    ({"scope_summary": ["/subscriptions/unmanaged"]}, "scope_summary"),
])
def test_source_contract_rejects_false_coverage(change, match):
    with pytest.raises(ValueError, match=match):
        build({"overview": envelope({"total_usd": 1}, **change)})


def test_source_periods_and_freshness_remain_distinct():
    source = envelope([], window=["2026-11-01", "2026-11-30"],
                      refreshed_at="2026-11-14T01:00:00Z")
    model = build({"governance": source})
    assert tab(model, "governance")["period"] == "2026-11-01 to 2026-11-30"
    assert tab(model, "governance")["refreshedAt"] == "2026-11-14T01:00:00Z"


def test_missing_cost_is_not_a_healthy_zero():
    with pytest.raises(ValueError, match="missing cost is not zero"):
        build({"overview": envelope({})})
    with pytest.raises(ValueError, match="data is required"):
        build({"anomalies": envelope(None)})


def test_explorer_retains_unknown_charges_and_uses_decimal_aggregation():
    rows = dashboard.build_explorer_rows([
        {"resourceId": "RESOURCE", "cost": .1}, {"resourceId": "resource", "cost": .2},
        {"cost": 2},
    ])
    assert sum(row["cost_usd"] for row in rows) == 2.3
    assert rows[0]["resourceId"] == "(unknown)"
    assert rows[1]["cost_usd"] == .3
    model = build({"explorer": envelope(rows)})
    assert tab(model, "explorer")["sections"][0]["rows"][0][-1] == "$2.00"
    with pytest.raises(ValueError, match="finite"):
        dashboard.build_explorer_rows([{"cost": float("nan")}])


def test_commitment_ratios_zero_denominators_and_estimates_are_not_summed():
    row = {
        "benefit": "reservation", "kind": "RI", "quantity_unit": "hours",
        "used_quantity": 50, "purchased_quantity": 100,
        "covered_cost_usd": 30, "eligible_cost_usd": 120,
        "realized_savings_usd": 10, "unused_cost_usd": 5,
        "expires_on": "2027-01-01", "recommendation": "Review renewal",
    }
    data = tab(build({"commitments": envelope([row])}), "commitments")
    assert data["sections"][0]["rows"][0][3:5] == ["50.0%", "25.0%"]
    zero = {**row, "used_quantity": 0, "purchased_quantity": 0,
            "covered_cost_usd": 0, "eligible_cost_usd": 0}
    data = tab(build({"commitments": envelope([zero])}), "commitments")
    assert data["sections"][0]["rows"][0][3:5] == ["\u2014", "\u2014"]
    with pytest.raises(ValueError, match="inconsistent"):
        build({"commitments": envelope([{**row, "used_quantity": 101}])})


def test_unit_economics_rejects_bad_denominators_and_preserves_small_unit_costs():
    row = {"business_unit": "Search", "unit": "request", "units": 1000000,
           "cost_usd": .30, "allocation_method": "Metered"}
    model = build({"unit-economics": envelope([row])})
    assert tab(model, "unit-economics")["sections"][0]["rows"][0][4] == "0.0000003 USD"
    row["units"] = 0
    model = build({"unit-economics": envelope([row])})
    assert "zero units" in tab(model, "unit-economics")["sections"][0]["rows"][0][4]
    row["units"] = -1
    with pytest.raises(ValueError, match="non-negative"):
        build({"unit-economics": envelope([row])})


@pytest.mark.parametrize("value", [True, float("inf"), "123", None])
def test_additional_sources_reject_invalid_measurements(value):
    with pytest.raises(ValueError):
        build({"sustainability": envelope([{"scope": SCOPE, "mtco2e": value, "kwh": 2,
                                           "methodology": "Provider estimate"}])})


def test_billing_keeps_currency_and_basis_and_planning_computes_delta():
    model = build({
        "billing": envelope([{"business_unit": "IT", "basis": "credit", "amount": -50,
                              "currency": "EUR", "billing_period": "2026-11", "methodology": "Invoice"}]),
        "planning": envelope([{"scenario": "Resize", "baseline_usd": 20.10, "projected_usd": 10,
                               "assumptions": "Same load", "term": "month", "breakeven": "N/A"}]),
    })
    assert tab(model, "billing")["sections"][0]["rows"][0][2:4] == ["-50", "EUR"]
    assert tab(model, "planning")["sections"][0]["rows"][0][3] == "-$10.10"


def test_render_dashboard_is_escaped_network_free_nonce_safe_and_accessible():
    evil = '</script><img src=x onerror=alert(1)>'
    sources = {"overview": envelope({
        "total_usd": 3, "daily": [{"date": evil, "cost_usd": 3}],
        "by_service": [{"service": evil, "cost_usd": 3}],
    }), "containers": {"status": "unavailable", "reason": evil}}
    original = copy.deepcopy(sources)
    model = build(sources)
    output = renderer.render_report(model)
    assert sources == original
    assert output == renderer.render_report(build(sources))
    assert output.count('role="tab" ') == 17
    assert output.count('role="tabpanel" ') == 17
    assert evil not in output
    assert "&lt;img" in output
    assert '<script src=' not in output
    assert 'fetch(' not in output and "innerHTML" not in output
    assert "connect-src 'none'" in output
    assert 'nonce="{REPORT_NONCE}"' in output
    assert 'data-theme' in output and "--cp-bg" in output
    assert "ArrowDown" in output and "matching rows in this section" in output
    assert "@media print" in output
    model["tabs"][1]["id"] = model["tabs"][0]["id"]
    with pytest.raises(ValueError, match="unique"):
        renderer.render_report(model)


def test_categorical_charts_are_not_presented_as_time_trends():
    chart = {"label": "Savings by resource", "type": "bar",
             "labels": ["A", "B"], "values": [20, -5]}
    rendered = renderer._dashboard_chart({"chart": chart})
    assert rendered.count("<rect ") == 2
    assert "<polyline" not in rendered
    assert 'height="-' not in rendered
    chart["type"] = "line"
    assert "<polyline" in renderer._dashboard_chart({"chart": chart})


def test_dashboard_icons_and_mobile_section_selector_are_self_contained():
    output = renderer.render_report(build())
    symbols = set(re.findall(r'<symbol id="([^"]+)"', output))
    uses = re.findall(r'<use href="#([^"]+)"', output)
    assert len(symbols) >= 17
    assert set(uses) <= symbols
    assert {"fluent-cloud", "fluent-money-calculator", "fluent-agents"} <= set(uses)
    assert "Copyright (c) 2020 Microsoft Corporation" in output
    assert '<label for="section-select">Report section</label>' in output
    assert output.count("<option ") == 17
    assert "Overview - Unavailable" in output
    assert 'class="dashboard"' in output
    assert '<script src=' not in output


def test_single_point_chart_shows_a_label_and_a_visible_point():
    chart = {"label": "Daily cost (USD)", "type": "line",
             "labels": ["2026-11-16"], "values": [42.5]}
    rendered = renderer._dashboard_chart({"chart": chart})
    assert '<h3>Daily cost (USD)</h3>' in rendered
    assert '<circle cx="400.00"' in rendered
    assert "2026-11-16" in rendered
    assert "View chart data" in rendered


def test_wide_dashboard_tables_have_accessible_scroll_regions():
    model = build({"anomalies": envelope([
        {"dimension": "Service", "value": "Compute", "current_usd": 12,
         "baseline_mean_usd": 5, "kind": "Increase"},
    ])})
    output = renderer.render_report(model)
    assert 'role="region" tabindex="0" aria-label="Detected anomalies table"' in output
    assert "Scroll horizontally to view more columns." in output
    assert '<th scope="col">Dimension</th>' in output


def test_managed_dashboard_uses_live_changes_and_rejects_any_fixed_override():
    policy = {"mode": "managed", "scopes": []}
    assert scope.resolve_dashboard_scopes([SCOPE], policy)["effective_scopes"] == [SCOPE]
    new = SCOPE + "-new"
    assert scope.resolve_dashboard_scopes([new], policy)["effective_scopes"] == [new]
    with pytest.raises(ValueError, match="no effective scopes"):
        scope.resolve_dashboard_scopes([], policy)
    with pytest.raises(ValueError, match="live managedResources"):
        scope.resolve_dashboard_scopes([SCOPE], {"mode": "managed", "scopes": [ROOT]})


def test_everything_requires_matching_tenant_and_complete_recursive_hierarchy():
    policy = {"mode": "everything", "scopes": [ROOT]}
    hierarchy = {ROOT: {"subscriptions": ["/subscriptions/a"], "management_groups": [CHILD]}}
    with pytest.raises(ValueError, match="GUID"):
        scope.resolve_dashboard_scopes([], policy, hierarchy)
    with pytest.raises(ValueError, match="incomplete"):
        scope.resolve_dashboard_scopes([], policy, hierarchy, tenant_id=TENANT)
    hierarchy[CHILD] = {"subscriptions": ["/subscriptions/b"]}
    resolved = scope.resolve_dashboard_scopes([SCOPE], policy, hierarchy, tenant_id=TENANT)
    assert set(resolved["effective_scopes"]) == {"/subscriptions/a", "/subscriptions/b"}
    hierarchy[CHILD]["subscriptions"].append("/subscriptions/new")
    assert "/subscriptions/new" in scope.resolve_dashboard_scopes(
        [], policy, hierarchy, tenant_id=TENANT
    )["effective_scopes"]
    with pytest.raises(ValueError, match="current agent tenant"):
        scope.resolve_dashboard_scopes([], policy, hierarchy,
                                       tenant_id="99999999-2222-3333-4444-555555555555")
    # Explicit dashboard policy does not weaken the existing scheduled-skill policy.
    decision = scope.decide_scope_policy(["/subscriptions/a"], [SCOPE], mode="scheduled")
    assert decision["allowed"] is False
