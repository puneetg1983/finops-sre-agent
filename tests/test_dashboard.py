"""Consolidation, evidence and explicit scope-policy regression tests."""

import copy
import importlib.util
import re
import xml.etree.ElementTree as ET
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


def test_selected_subscriptions_preserve_scope_and_render_distinct_label():
    selected = ["/subscriptions/00000000-0000-0000-0000-000000000001",
                "/subscriptions/00000000-0000-0000-0000-000000000002"]
    model = dashboard.build_dashboard_model(
        {}, refreshed_at=NOW, scope_summary=selected, window=WINDOW,
        scope_mode="subscriptions",
    )
    assert model["scopeMode"] == "subscriptions"
    assert model["scopeSummary"] == selected
    output = renderer.render_report(model)
    assert "Selected subscriptions" in output
    assert "Scope: Managed resources" not in output
    assert all(value in output for value in selected)


def test_unknown_dashboard_scope_mode_is_rejected_without_managed_fallback():
    with pytest.raises(ValueError, match="scope_mode"):
        build(scope_mode="unknown")
    model = build()
    model["scopeMode"] = "unknown"
    with pytest.raises(ValueError, match="scopeMode"):
        renderer.render_report(model)


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
    assert {"product-azure", "product-cost-management", "product-sre-agent"} <= set(uses)
    assert "Copyright (c) 2020 Microsoft Corporation" in output
    assert '<label for="section-select">Report section</label>' in output
    assert output.count("<option ") == 17
    assert "Overview - Unavailable" in output
    assert 'class="dashboard"' in output
    assert '<script src=' not in output


def test_official_product_sprite_is_offline_and_keeps_distinct_definitions():
    sprite = ET.parse(SKILLS / "finops-report-renderer" / "microsoft-product-icons.svg")
    elements = list(sprite.getroot().iter())
    ids = [element.attrib["id"] for element in elements if "id" in element.attrib]
    assert len(ids) == len(set(ids))
    assert {"product-azure", "product-sre-agent", "product-cost-management",
            "product-virtual-machine", "product-storage", "product-sql",
            "product-app-service"} <= set(ids)
    for element in elements:
        assert element.tag.rsplit("}", 1)[-1] not in {"script", "image", "foreignObject"}
        for key, value in element.attrib.items():
            assert not key.lower().startswith("on")
            if key.endswith("href"):
                assert value.startswith("#") and value[1:] in ids
            for reference in re.findall(r"url\(#([^)]+)\)", value):
                assert reference in ids


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


def test_explorer_breakdowns_keep_short_names_full_details_and_signed_cents():
    rows = [
        {"resourceId": SCOPE + "/providers/Microsoft.Web/sites/very-long-portal-name",
         "subscriptionId": "sub", "resourceGroup": "managed", "serviceName": "App Service",
         "resourceLocation": "East US", "chargeType": "Usage", "cost_usd": amount}
        for amount in [.1, .2, -1]
    ]
    model = build({"explorer": envelope(rows)})
    section = tab(model, "explorer")["sections"][0]
    assert section["breakdown"]["dimensions"] == [("Service", 3), ("Region", 4)]
    output = renderer._resource_browser(section)
    assert '<strong>-$0.70</strong>' in output
    assert '<span class="resource-name">very-long-portal-name</span>' in output
    assert '<dt>Resource</dt><dd>' + rows[0]["resourceId"] + '</dd>' in output
    assert 'data-amount="-100"' in output
    assert 'data-d0="App Service" data-d1="East US"' in output
    assert 'max="70" value="70"' in output


def test_region_pie_shows_exact_shares_and_omits_zero_slices():
    markup, total = renderer._region_pie(
        [("East <US>", [3000, 1, 0]), ("West", [1000, 1, 0]), ("Zero", [0, 1, 0])],
        {"East <US>": 0, "West": 1, "Zero": 2},
    )
    pie = ET.fromstring(markup)
    assert total == 4000
    assert len(pie.findall("path")) == 2
    assert pie.findall("path")[0].find("title").text == "East <US>: $30.00 (75.0%)"
    assert pie.findall("path")[1].find("title").text == "West: $10.00 (25.0%)"
    assert all(path.attrib["d"].count("A 100 100") == 2 for path in pie.findall("path"))
    assert 'data-value="East &lt;US&gt;"' in markup
    single, _ = renderer._region_pie([("East", [1, 1, 0])], {"East": 0})
    assert "(100.0%)" in single and "M 110 110 L" not in single


@pytest.mark.parametrize("entries,message", [
    ([], "No positive regional cost"),
    ([("East", [0, 1, 0])], "No positive regional cost"),
    ([("East", [3000, 1, 0]), ("West", [-1000, 1, 0])], "Signed amounts"),
])
def test_region_pie_never_invents_shares_for_empty_zero_or_negative_totals(entries, message):
    markup, total = renderer._region_pie(entries, {})
    assert message in markup
    assert total == 0
    assert "<svg" not in markup


@pytest.mark.parametrize("suffix,expected", [
    ("/providers/Microsoft.Compute/virtualMachines/vm", "microsoft.compute/virtualmachines"),
    ("/providers/Microsoft.Compute/virtualMachines/providers", "microsoft.compute/virtualmachines"),
    ("/providers/Microsoft.Sql/servers/sql/databases/db", "microsoft.sql/servers/databases"),
    ("/providers/Microsoft.Compute/virtualMachines/vm/providers/Microsoft.Insights/diagnosticSettings/logs",
     "microsoft.insights/diagnosticsettings"),
    ("/providers/Microsoft.Compute/virtualMachines", "(unknown)"),
    ("", "(unknown)"),
])
def test_explorer_resource_type_uses_arm_types_not_resource_names(suffix, expected):
    assert renderer._resource_type_from_id(SCOPE + suffix) == expected


def test_explorer_dropdowns_keep_all_dimensions_and_escape_options():
    rows = [
        {"resourceId": SCOPE + "/providers/Microsoft.Compute/virtualMachines/vm",
         "subscriptionId": 'sub<one>', "serviceName": "Virtual Machines",
         "resourceGroup": "managed", "chargeType": "Usage",
         "resourceLocation": "East US", "cost_usd": 3},
        {"resourceId": "(unknown)", "subscriptionId": "sub-two",
         "resourceGroup": "(unknown)", "chargeType": "Usage",
         "serviceName": "Storage", "resourceLocation": "West Europe", "cost_usd": 1},
    ]
    output = renderer._resource_browser(tab(build({"explorer": envelope(rows)}), "explorer")["sections"][0])
    assert re.findall(r'data-filter="([^"]+)" disabled>', output) == ["subscription", "type", "d1"]
    assert output.count('<option value="">All</option>') == 3
    assert output.count('data-filter-all=') == 3
    assert 'data-subscription="sub&lt;one&gt;" data-type="Virtual machines"' in output
    assert '<option value="sub&lt;one&gt;">sub&lt;one&gt;</option>' in output
    assert '<option value="(unknown)">(unknown)</option>' in output
    assert '<strong>$3.00 · 75.0%</strong>' in output
    assert 'data-visualization="region-pie"' in output


def test_savings_breakdowns_preserve_unknowns_evidence_and_validation():
    findings = [
        {"resourceId": SCOPE + "/providers/Microsoft.Compute/virtualMachines/vm-one",
         "resourceType": "microsoft.compute/virtualmachines", "kind": "oversized",
         "estMonthlySavingsUsd": 25, "recommendedAction": "Review resize",
         "validated": False, "evidence": ['Low CPU <script>alert("x")</script>']},
        {"resourceId": "vm-two", "resourceType": "microsoft.compute/virtualmachines",
         "kind": "idle", "estMonthlySavingsUsd": None},
    ]
    model = build({"rightsizing": envelope(findings)})
    assert tab(model, "rightsizing")["chart"] is None
    output = renderer._resource_browser(tab(model, "rightsizing")["sections"][0])
    assert 'data-d0="Virtual machines"' in output
    assert '<strong>$25.00</strong>' in output
    assert '1 unquantified' in output
    assert 'data-amount=""' in output
    assert 'contradicted' in output
    assert '&lt;script&gt;' in output
    assert '<script>' not in output
    assert '<dt>Evidence</dt>' in output
    assert '<dt>Type</dt><dd>microsoft.compute/virtualmachines</dd>' in output
    unknown_model = build({"rightsizing": envelope(findings[1:])})
    unknown_output = renderer._resource_browser(tab(unknown_model, "rightsizing")["sections"][0])
    assert 'Not quantified</strong>' in unknown_output
    assert 'No quantified amount' in unknown_output
    assert '$0.00' not in unknown_output


def test_empty_breakdowns_and_hostile_dimension_labels():
    section = tab(build({"explorer": envelope([])}), "explorer")["sections"][0]
    assert '<p class="breakdown-empty">' in renderer._resource_browser(section)
    section["rows"] = [['name', 'sub', 'group', '"><img src=x>', 'region', 'Usage', '$1.00']]
    output = renderer._resource_browser(section)
    assert '<img' not in output
    assert 'data-d0="&quot;&gt;&lt;img src=x&gt;"' in output


def test_top_services_renders_signed_horizontal_bars_and_preserves_data_table():
    model = build({"overview": envelope({
        "total_usd": 1233.56,
        "by_service": [{"service": "Virtual Machines", "cost_usd": 1234.56},
                       {"service": "Credits <script>", "cost_usd": -1}],
    })})
    output = renderer.render_report(model)
    assert '<ol class="service-bars" aria-label="Top services by cost">' in output
    assert '<strong>$1,234.56</strong>' in output
    assert '<strong>-$1.00</strong>' in output
    assert 'max="123456" value="100"' in output
    assert 'Credits &lt;script&gt;' in output
    assert 'href="#product-virtual-machine"' in output
    assert 'View service cost data' in output
    assert 'Live Reports <span aria-hidden="true">/</span> Financial operations' in output
    assert 'class="workspace-identity"' in output
    assert '<h1>FinOps: Dashboard</h1>' in output
    assert 'Powered by Azure SRE Agent' in output


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
