"""Deterministic static FinOps report renderer tests."""

import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


PATH = (
    Path(__file__).resolve().parents[1]
    / "plugins"
    / "azure-finops"
    / "skills"
    / "finops-report-renderer"
    / "render.py"
)
SPEC = importlib.util.spec_from_file_location("finops_report_renderer", PATH)
renderer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(renderer)

MODELS_PATH = PATH.with_name("models.py")
MODELS_SPEC = importlib.util.spec_from_file_location("finops_report_models", MODELS_PATH)
models = importlib.util.module_from_spec(MODELS_SPEC)
MODELS_SPEC.loader.exec_module(models)

REFRESHED_AT = "2026-08-07T01:30:00Z"
SCOPE = "/subscriptions/sub/resourceGroups/managed"
EVIL = '<script>alert("owned")</script>'


def _model():
    return {
        "title": "FinOps: Cost Overview",
        "description": "Daily snapshot <safe>",
        "refreshedAt": "2026-08-07T01:30:00Z",
        "scopeSummary": ["/subscriptions/sub/resourceGroups/managed"],
        "partial": True,
        "warnings": ["Billing is still settling."],
        "metrics": [
            {
                "label": "30-day spend",
                "value": "$123.45",
                "detail": "ActualCost",
                "tone": "warning",
            }
        ],
        "chart": {
            "type": "line",
            "label": "Daily cost",
            "labels": ["Aug 1", "Aug 2"],
            "values": [12.3, 14.8],
        },
        "sections": [
            {
                "title": "Top services",
                "description": "Ranked by cost.",
                "columns": ["Service", "Cost"],
                "rows": [["Storage<script>", "$80.00"]],
            }
        ],
    }


def test_render_is_deterministic_secure_and_static():
    first = renderer.render_report(_model())
    second = renderer.render_report(_model())

    assert first == second
    assert "Content-Security-Policy" in first
    assert "nonce=\"{REPORT_NONCE}\"" in first
    assert "sha384-vsrfeLOOY6KuIYKDlmVH5UiBmgIdB1oEf7p01YgWHuqmOHfZr374+odEv96n9tNC" in first
    assert "window.sreagent" not in first
    assert "innerHTML" not in first
    assert "Storage&lt;script&gt;" in first
    assert "Daily snapshot &lt;safe&gt;" in first
    assert "Billing is still settling." in first


def test_render_handles_empty_sections_and_no_chart():
    model = {
        "title": "Empty",
        "refreshedAt": "2026-08-07T01:30:00Z",
        "sections": [
            {
                "title": "Rows",
                "columns": ["Name"],
                "rows": [],
                "emptyMessage": "Nothing found.",
            }
        ],
    }
    output = renderer.render_report(model)

    assert "Nothing found." in output
    assert "chart.umd.min.js" not in output
    assert "No headline metrics were produced." in output


def test_render_rejects_invalid_shapes():
    with pytest.raises(ValueError, match="title is required"):
        renderer.render_report({"refreshedAt": "now"})
    with pytest.raises(ValueError, match="equal length"):
        renderer.render_report(
            {
                "title": "Bad",
                "refreshedAt": "now",
                "chart": {"label": "x", "labels": ["a"], "values": []},
            }
        )


def test_write_report_creates_file(tmp_path):
    path = tmp_path / "nested" / "report.html"
    assert renderer.write_report(_model(), path) == str(path)
    assert path.read_text().startswith("<!doctype html>")


def _context():
    return {
        "refreshed_at": REFRESHED_AT,
        "scope_summary": [SCOPE],
        "partial": True,
        "warnings": ["Scope z failed.", "Billing is settling."],
        "scope_coverage": [
            {
                "scope": SCOPE,
                "status": "partial",
                "included": 4,
                "excluded": 2,
                "unattributed": 1,
                "cost_usd": 30.03,
                "partial": True,
                "detail": EVIL,
            }
        ],
    }


def _overview():
    return {
        "total_usd": 30.03,
        "daily": [
            {"date": "2026-08-02", "cost_usd": 20.02},
            {"date": "2026-08-01", "cost_usd": 10.01},
        ],
        "by_service": [
            {"service": EVIL, "cost_usd": 15.015},
            {"service": "Storage", "cost_usd": 15.015},
        ],
        "by_resource_group": [
            {"resource_group": "rg-b", "cost_usd": 10},
            {"resource_group": "rg-a", "cost_usd": 20.03},
        ],
    }


def _rightsizing():
    return [
        {
            "resourceId": f"{SCOPE}/providers/Microsoft.Compute/virtualMachines/{EVIL}",
            "resourceType": "microsoft.compute/virtualmachines",
            "kind": "oversized",
            "currentSku": "D4",
            "recommendedAction": "Resize",
            "currentMonthlyUsd": 1.0,
            "estMonthlySavingsUsd": 0.1,
            "validated": True,
            "evidence": ["p95 CPU 4%"],
            "sources": ["azure-monitor"],
        },
        {
            "resourceId": f"{SCOPE}/providers/Microsoft.Compute/virtualMachines/vm-b",
            "resourceType": "microsoft.compute/virtualmachines",
            "kind": "advisor",
            "currentSku": "D2",
            "recommendedAction": "Verify and resize",
            "currentMonthlyUsd": 2.0,
            "estMonthlySavingsUsd": 0.2,
            "validated": None,
            "evidence": ["Advisor only"],
            "sources": ["advisor"],
        },
        {
            "resourceId": f"{SCOPE}/providers/Microsoft.Storage/storageAccounts/review",
            "resourceType": "microsoft.storage/storageaccounts",
            "kind": "review",
            "currentSku": "",
            "recommendedAction": "Review cost",
            "currentMonthlyUsd": 100,
            "estMonthlySavingsUsd": None,
            "validated": None,
            "evidence": [],
            "sources": ["cost"],
        },
    ]


def _budgets():
    return {
        "as_of": "2026-08-06",
        "budget_count": 2,
        "budgets": [
            {
                "name": "safe",
                "scope": SCOPE,
                "amount": 1000,
                "currency": "USD",
                "current_spend": 0,
                "pct_used": 0,
                "forecast_spend": 0,
                "forecast_source": "run-rate",
                "pct_forecast": 0,
                "status": "on_track",
                "breached_notifications": [],
                "mtd_crosscheck": None,
            },
            {
                "name": EVIL,
                "scope": SCOPE,
                "amount": 100,
                "currency": "USD",
                "current_spend": 110,
                "pct_used": 110,
                "forecast_spend": 150,
                "forecast_source": "azure",
                "pct_forecast": 150,
                "status": "over_budget",
                "breached_notifications": [
                    {"name": "actual_100", "threshold": 100, "type": "actual"}
                ],
                "mtd_crosscheck": None,
            },
        ],
        "summary": {
            "total_amount": 1100,
            "total_current": 110,
            "total_forecast": 150,
            "over_budget": 1,
            "forecast_over": 0,
            "at_risk": 0,
            "on_track": 1,
        },
        "gates": [{"name": EVIL, "reason": "current spend is over", "status": "over_budget"}],
        "no_budgets": False,
    }


def _optimization():
    finding = _rightsizing()[1]
    return {
        "as_of": "2026-08-06",
        "headline": {
            "total_monthly_spend": 500,
            "potential_monthly_savings": 0.2,
            "anomaly_count": 1,
            "top_anomaly_impact_usd": 90,
            "budgets_over": 0,
            "budgets_forecast_over": 1,
            "budgets_at_risk": 0,
            "untagged_usd": 40,
            "unallocated_pct": 8,
        },
        "priorities": [
            {
                "rank": 2,
                "category": "rightsizing",
                "impact_type": "savings",
                "impact_usd": 0.2,
                "title": "Resize vm-b",
                "detail": "advisor",
                "action": "Verify and resize",
                "validated": None,
            },
            {
                "rank": 1,
                "category": "budget",
                "impact_type": "overrun",
                "impact_usd": 200,
                "title": EVIL,
                "detail": "forecast over",
                "action": "Review",
                "validated": None,
            },
        ],
        "rightsizing": {
            "potential_monthly_savings": 0.2,
            "count": 1,
            "top": [finding],
        },
        "anomalies": {
            "count": 1,
            "top_impact_usd": 90,
            "top": [
                {
                    "kind": "spike",
                    "dimension": "service",
                    "value": "Compute",
                    "current_usd": 100,
                    "baseline_mean_usd": 10,
                    "impact_usd": 90,
                }
            ],
        },
        "budgets": {
            "no_budgets": False,
            "summary": {},
            "over": [],
            "forecast_over": [
                {
                    "name": "prod",
                    "status": "forecast_over",
                    "currency": "USD",
                    "current_spend": 800,
                    "amount": 1000,
                    "forecast_spend": 1200,
                    "forecast_source": "run-rate",
                }
            ],
            "at_risk": [],
        },
        "governance": {
            "untagged_usd": 40,
            "unallocated_usd": 40,
            "unallocated_pct": 8,
            "untagged_top": [{"resourceId": EVIL, "monthly_usd": 40}],
            "tag_hygiene": [
                {
                    "canonical": "prod",
                    "variants": {"Prod": 20, "prod": 10},
                    "cost_affected": 30,
                }
            ],
            "budget_gates": [
                {
                    "name": "prod",
                    "status": "forecast_over",
                    "overrun_usd": 200,
                    "currency": "USD",
                }
            ],
        },
    }


def _ai_spend():
    return {
        "as_of": "2026-08-06",
        "total_ai_usd": 30.03,
        "resource_count": 2,
        "model_count": 2,
        "by_service_family": [
            {"service_family": "Machine Learning", "monthly_usd": 10, "pct": 33.3},
            {
                "service_family": "Cognitive Services / OpenAI",
                "monthly_usd": 20.03,
                "pct": 66.7,
            },
        ],
        "by_meter_type": [
            {"meter_type": "compute", "monthly_usd": 0.2, "pct": 0.7},
            {"meter_type": "model_token", "monthly_usd": 0.1, "pct": 0.3},
            {"meter_type": "model_token", "monthly_usd": 19.73, "pct": 65.7},
            {"meter_type": "other_cognitive", "monthly_usd": 10, "pct": 33.3},
        ],
        "by_resource": [
            {
                "resourceId": f"{SCOPE}/providers/Microsoft.CognitiveServices/accounts/a",
                "resourceName": EVIL,
                "kind": "AIServices",
                "service_family": "Cognitive Services / OpenAI",
                "monthly_usd": 20.03,
                "pct": 66.7,
                "top_model": "gpt-4o",
            }
        ],
        "by_model": [
            {"model": "gpt-4o-mini", "monthly_usd": 5, "pct": 25.2, "resource_count": 1},
            {"model": "gpt-4o", "monthly_usd": 14.83, "pct": 74.8, "resource_count": 1},
        ],
        "top_drivers": [
            {
                "resourceName": EVIL,
                "service_family": "Cognitive Services / OpenAI",
                "model": "gpt-4o",
                "meter_type": "model_token",
                "monthly_usd": 14.83,
            }
        ],
        "hints": [
            {
                "type": "commitment_opportunity",
                "target": EVIL,
                "detail": "Evaluate PTU.",
                "monthly_usd": 14.83,
            }
        ],
    }


def _reliability():
    return {
        "as_of": "2026-08-06",
        "total_usd": 1200,
        "resource_count": 2,
        "reliability_signal_count": 4,
        "coverage": {
            "cost_resource_count": 2,
            "reliability_resource_count": 2,
            "joined_resource_count": 1,
            "unmatched_reliability_count": 1,
            "subscription_level_event_count": 1,
        },
        "by_resource": [
            {
                "resourceId": f"{SCOPE}/providers/Microsoft.Compute/virtualMachines/a",
                "resourceName": EVIL,
                "resourceGroup": "managed",
                "service": "Compute",
                "monthly_usd": 1200,
                "pct": 100,
                "alert_count": 2,
                "sev_counts": {"Sev0": 0, "Sev1": 1, "Sev2": 1, "Sev3": 0, "Sev4": 0},
                "sev0": 0,
                "sev1": 1,
                "sev2": 1,
                "sev3": 0,
                "sev4": 0,
                "health_event_count": 1,
                "advisor_reliability_count": 1,
                "reliability_score": 14,
                "pain_per_1000_usd": 11.67,
                "risk_band": "high",
                "primary_signal": "alert",
            }
        ],
        "by_service": [
            {
                "service": "Compute",
                "monthly_usd": 1200,
                "resource_count": 1,
                "alert_count": 2,
                "health_event_count": 1,
                "advisor_reliability_count": 1,
                "reliability_score": 14,
                "pct": 100,
                "risk_band": "high",
            }
        ],
        "top_drivers": [],
        "hints": [
            {
                "type": "high_incident_low_spend",
                "target": "api",
                "detail": "Invest in resilience.",
                "monthly_usd": 50,
                "reliability_score": 12,
            },
            {
                "type": "high_spend_no_pain",
                "target": "batch",
                "detail": "Verify before rightsizing.",
                "monthly_usd": 2000,
                "reliability_score": 0,
            },
        ],
        "unmatched_reliability": [
            {
                "resourceId": f"{SCOPE}/providers/Microsoft.Web/sites/missing",
                "resourceName": "missing",
                "signal_type": "alert",
                "severity": "Sev2",
                "score": 3,
                "date": "2026-08-05",
                "detail": EVIL,
            }
        ],
        "data_quality": {
            "cost_partial": True,
            "sources_used": {
                "usage_details": True,
                "alerts": True,
                "resource_health": False,
                "advisor_highavailability": True,
            },
            "limitations": ["Alerts are weighted counts.", EVIL],
        },
    }


def _digest(value):
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def _contains_exact(value, wanted):
    if isinstance(value, dict):
        return any(_contains_exact(item, wanted) for item in value.values())
    if isinstance(value, list):
        return any(_contains_exact(item, wanted) for item in value)
    return value == wanted


def _reverse_lists(value):
    copied = copy.deepcopy(value)
    if isinstance(copied, list):
        return list(reversed(copied))
    for key, item in copied.items():
        if isinstance(item, list):
            copied[key] = list(reversed(item))
        elif isinstance(item, dict):
            for child_key, child in item.items():
                if isinstance(child, list):
                    item[child_key] = list(reversed(child))
    return copied


def test_cost_overview_adapter_preserves_partial_scope_and_sorts():
    model = models.build_cost_overview_model(_overview(), **_context())

    assert model["partial"] is True
    assert model["refreshedAt"] == REFRESHED_AT
    assert model["scopeSummary"] == [SCOPE]
    assert model["chart"]["labels"] == ["2026-08-01", "2026-08-02"]
    assert model["sections"][0]["rows"] == [
        [EVIL, "$15.02"],
        ["Storage", "$15.02"],
    ]
    assert model["sections"][-1]["title"] == "Scope coverage"


def test_rightsizing_adapter_uses_decimal_boundaries_and_verify_labels():
    model = models.build_rightsizing_savings_model(_rightsizing(), **_context())

    assert model["metrics"][0]["value"] == "$0.30"
    assert model["metrics"][3]["value"] == "2"
    assert model["chart"]["values"] == [0.2, 0.1]
    assert model["sections"][0]["rows"][-1][6] == "—"
    assert model["sections"][0]["rows"][0][7] == "verify first"


def test_budget_adapter_gates_first_and_labels_estimates():
    model = models.build_budget_status_model(_budgets(), **_context())

    assert model["sections"][0]["rows"][0][0] == "over_budget"
    assert model["sections"][1]["rows"][0][0] == EVIL
    assert "Run-rate forecasts are estimates" in " ".join(model["warnings"])
    assert "asynchronously" in " ".join(model["warnings"])
    assert model["chart"]["label"].endswith("(100% = budget limit)")


def test_cost_optimization_adapter_keeps_impact_types_separate():
    model = models.build_cost_optimization_model(_optimization(), **_context())

    priorities = model["sections"][0]["rows"]
    assert priorities[0][2] == "overrun"
    assert priorities[1][2] == "savings"
    assert model["metrics"][1]["value"] == "$0.20"
    assert model["metrics"][4]["value"] == "$40.00"
    assert priorities[1][-1] == "verify first"
    assert "Run-rate budget forecasts are estimates." in model["warnings"]


def test_ai_spend_adapter_keeps_token_and_compute_separate():
    model = models.build_ai_spend_model(_ai_spend(), **_context())

    assert model["metrics"][3]["value"] == "$19.83"
    assert model["metrics"][4]["value"] == "$0.20"
    assert model["chart"]["labels"] == ["gpt-4o", "gpt-4o-mini"]
    assert model["sections"][-3]["rows"][0][-1] == "$14.83"
    assert model["sections"][-2]["rows"][0][-1] == "verify first"


def test_cost_vs_reliability_adapter_preserves_quality_and_signal_fields():
    model = models.build_cost_vs_reliability_model(_reliability(), **_context())

    assert model["partial"] is True
    assert model["metrics"][3]["value"] == "50.0%"
    spend_row = model["sections"][0]["rows"][0]
    assert spend_row[5:13] == ["2", "0", "1", "1", "0", "0", "1", "1"]
    assert spend_row[13:] == ["14", "11.67", "high", "alert"]
    assert model["sections"][2]["rows"][0][0] == "high_incident_low_spend"
    assert model["sections"][3]["rows"][0][0] == "high_spend_no_pain"
    assert any(row[0] == "limitation" for row in model["sections"][5]["rows"])
    assert "complete incident system" in " ".join(model["warnings"])


@pytest.mark.parametrize(
    ("builder", "empty"),
    [
        (
            models.build_cost_overview_model,
            {"total_usd": 0, "daily": [], "by_service": [], "by_resource_group": []},
        ),
        (models.build_rightsizing_savings_model, []),
        (
            models.build_budget_status_model,
            {
                "budgets": [],
                "summary": {
                    "total_amount": 0,
                    "total_current": 0,
                    "total_forecast": 0,
                    "over_budget": 0,
                    "forecast_over": 0,
                    "at_risk": 0,
                    "on_track": 0,
                },
                "gates": [],
                "no_budgets": True,
            },
        ),
        (
            models.build_cost_optimization_model,
            {
                "headline": {},
                "priorities": [],
                "rightsizing": {},
                "anomalies": {},
                "budgets": {},
                "governance": {},
            },
        ),
        (
            models.build_ai_spend_model,
            {
                "total_ai_usd": 0,
                "resource_count": 0,
                "model_count": 0,
                "by_service_family": [],
                "by_meter_type": [],
                "by_resource": [],
                "by_model": [],
                "top_drivers": [],
                "hints": [],
            },
        ),
        (
            models.build_cost_vs_reliability_model,
            {
                "total_usd": 0,
                "resource_count": 0,
                "reliability_signal_count": 0,
                "coverage": {},
                "by_resource": [],
                "by_service": [],
                "hints": [],
                "unmatched_reliability": [],
                "data_quality": {},
            },
        ),
    ],
)
def test_all_adapters_render_empty_states(builder, empty):
    model = builder(empty, refreshed_at=REFRESHED_AT)
    output = renderer.render_report(model)

    assert output.startswith("<!doctype html>")
    assert "No chart data was produced." in output
    assert "class=\"empty\"" in output


@pytest.mark.parametrize(
    ("builder", "fixture", "model_hash", "html_hash"),
    [
        (
            models.build_cost_overview_model,
            _overview,
            "2db6ec2066af56a3980bde5a4e452f1e40f5c45d91d42ba83e6bcfd1767f98e6",
            "9182994ba8ec44703c987b25f9ac68d34f8453e5808d2a10af9cd4abfac81a53",
        ),
        (
            models.build_rightsizing_savings_model,
            _rightsizing,
            "582d55595fb372e18e87690e04380614a2ba67cc84a3535f0441674e5095801f",
            "ba693c1029bc43b11eac5d6a18a0af4859a55dd3d44062ea1ea928410a01245e",
        ),
        (
            models.build_budget_status_model,
            _budgets,
            "07f8af03e2ff258a9252aa7ef8697235ec6379ddf6ad10bf6ab031ef929c4732",
            "b766edf21d5b96db14785fd5aee857c42a4d1a83234f3af7396b7aee29c81d8f",
        ),
        (
            models.build_cost_optimization_model,
            _optimization,
            "b042471933ac40875fa4f538021bd9bf9ab111e4908d53ee06047e53ecdd3146",
            "47bcbea0ab72243a9ff27c5d54d8af5460103073bd3064c3503ae2cdbb249b78",
        ),
        (
            models.build_ai_spend_model,
            _ai_spend,
            "25086384ce560ced1ffd97a95af5c8f0a36d721bb92826239c5c33ae955dddd4",
            "6862824e1ff5b5912e19d93468720f3c96b94b4b9db2c55eaddc5f7eb502402c",
        ),
        (
            models.build_cost_vs_reliability_model,
            _reliability,
            "cfbb7d50575bdcb7bc6b99159fae40c368d8324a429b48ce13f95be809109b1c",
            "ed12a0061be4f377a10aca6ecfad2dd8c3b9271132dff4590035d9cdb60ba991",
        ),
    ],
)
def test_adapter_models_and_html_have_stable_hashes(
    builder, fixture, model_hash, html_hash
):
    original = builder(fixture(), **_context())
    reordered = builder(_reverse_lists(fixture()), **_context())
    rendered = renderer.render_report(original)

    assert _digest(original) == _digest(reordered) == model_hash
    assert hashlib.sha256(rendered.encode()).hexdigest() == html_hash
    assert hashlib.sha256(renderer.render_report(reordered).encode()).hexdigest() == html_hash


@pytest.mark.parametrize(
    ("builder", "fixture"),
    [
        (models.build_cost_overview_model, _overview),
        (models.build_rightsizing_savings_model, _rightsizing),
        (models.build_budget_status_model, _budgets),
        (models.build_cost_optimization_model, _optimization),
        (models.build_ai_spend_model, _ai_spend),
        (models.build_cost_vs_reliability_model, _reliability),
    ],
)
def test_malicious_strings_are_unescaped_in_models_and_escaped_at_render(builder, fixture):
    model = builder(fixture(), **_context())
    output = renderer.render_report(model)

    assert _contains_exact(model, EVIL)
    assert EVIL not in output
    assert "&lt;script&gt;alert(&quot;owned&quot;)&lt;/script&gt;" in output


def test_adapters_require_supplied_refresh_and_reject_unsafe_numbers():
    with pytest.raises(ValueError, match="refreshed_at is required"):
        models.build_cost_overview_model({"total_usd": 0}, refreshed_at="")
    with pytest.raises(ValueError, match="finite number"):
        models.build_cost_overview_model(
            {"total_usd": "1.00"}, refreshed_at=REFRESHED_AT
        )
    with pytest.raises(ValueError, match="finite number"):
        models.build_ai_spend_model(
            {
                "total_ai_usd": float("nan"),
                "resource_count": 0,
                "model_count": 0,
            },
            refreshed_at=REFRESHED_AT,
        )


def test_model_adapters_have_no_clock_or_network_access():
    source = MODELS_PATH.read_text()

    assert "datetime" not in source
    assert "date.today" not in source
    assert "socket" not in source
    assert "requests" not in source
    assert "urllib" not in source


def test_skill_documents_all_adapter_apis():
    skill = MODELS_PATH.with_name("SKILL.md").read_text()

    for name in models.__all__:
        assert name in skill
    assert "refreshed_at" in skill
    assert "scope_coverage" in skill
    assert "write_report(model, output_path)" in skill
