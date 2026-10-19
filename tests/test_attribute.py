"""Layer-1 unit tests for the finops-for-ai skill (offline, deterministic)."""

import importlib.util
from pathlib import Path

_PATH = (
    Path(__file__).resolve().parent.parent
    / "plugins"
    / "azure-finops"
    / "skills"
    / "finops-for-ai"
    / "attribute.py"
)
_spec = importlib.util.spec_from_file_location("attribute", _PATH)
attribute = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(attribute)

attribute_ai_costs = attribute.attribute_ai_costs

AOAI_ID = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/myaoai"
FOUNDRY_ID = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/myfoundry"
ML_ID = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.MachineLearningServices/workspaces/myml"


def _line(cost, *, consumed="Microsoft.CognitiveServices", cat="Azure OpenAI",
          sub="", name="", rid=AOAI_ID, date="2026-07-15"):
    return {
        "cost": cost,
        "consumedService": consumed,
        "meterCategory": cat,
        "meterSubCategory": sub,
        "meterName": name,
        "resourceId": rid,
        "date": date,
    }


def test_empty_input_is_all_zero_not_error():
    out = attribute_ai_costs([])
    assert out["total_ai_usd"] == 0.0
    assert out["resource_count"] == 0
    assert out["by_resource"] == []
    assert out["by_model"] == []
    assert out["hints"] == []
    assert out["as_of"] is None


def test_none_input_is_safe():
    out = attribute_ai_costs(None)
    assert out["total_ai_usd"] == 0.0


def test_foundry_aiservices_token_spend_is_counted():
    # An OpenAI model deployed in a Foundry (kind=AIServices) account still bills under
    # Microsoft.CognitiveServices with a token meter — it MUST be captured.
    items = [
        _line(100.0, cat="Cognitive Services", sub="gpt-4o",
              name="gpt-4o-0513 Inp glbl Tokens", rid=FOUNDRY_ID),
    ]
    out = attribute_ai_costs(items)
    assert out["total_ai_usd"] == 100.0
    assert out["by_meter_type"][0]["meter_type"] == "model_token"
    assert out["by_model"][0]["model"] == "gpt-4o"
    assert out["by_service_family"][0]["service_family"] == "Cognitive Services / OpenAI"


def test_kind_gating_would_not_drop_foundry():
    # Same data as above but assert the resource shows up regardless of kind label.
    out = attribute_ai_costs(
        [_line(50.0, name="gpt-4o Tokens", rid=FOUNDRY_ID)],
        resource_kinds={FOUNDRY_ID: "AIServices"},
    )
    assert out["by_resource"][0]["kind"] == "AIServices"
    assert out["by_resource"][0]["monthly_usd"] == 50.0


def test_machine_learning_compute_included():
    out = attribute_ai_costs([
        _line(200.0, consumed="Microsoft.MachineLearningServices", cat="Azure Machine Learning",
              name="Compute Instance vCPU", rid=ML_ID),
    ])
    assert out["total_ai_usd"] == 200.0
    assert out["by_service_family"][0]["service_family"] == "Machine Learning"
    assert out["by_meter_type"][0]["meter_type"] == "compute"


def test_non_ai_line_is_ignored():
    out = attribute_ai_costs([
        _line(100.0, name="gpt-4o Tokens"),
        _line(999.0, consumed="Microsoft.Compute", cat="Virtual Machines", name="D2s v5"),
    ])
    assert out["total_ai_usd"] == 100.0
    assert out["resource_count"] == 1


def test_meter_type_split_token_vs_compute_vs_other():
    out = attribute_ai_costs([
        _line(60.0, name="gpt-4o Inp glbl Tokens"),           # model_token
        _line(30.0, cat="Cognitive Services", sub="Speech",
              name="Speech to Text Standard", rid=FOUNDRY_ID),  # other_cognitive
        _line(10.0, consumed="Microsoft.MachineLearningServices",
              name="Compute vCPU", rid=ML_ID),                 # compute
    ])
    by_type = {r["meter_type"]: r["monthly_usd"] for r in out["by_meter_type"]}
    assert by_type["model_token"] == 60.0
    assert by_type["other_cognitive"] == 30.0
    assert by_type["compute"] == 10.0


def test_model_parsed_from_subcategory_preferred():
    out = attribute_ai_costs([
        _line(10.0, sub="gpt-4o-mini", name="something Tokens"),
    ])
    assert out["by_model"][0]["model"] == "gpt-4o-mini"


def test_model_parsed_from_meter_name_when_no_subcategory():
    out = attribute_ai_costs([
        _line(10.0, sub="", name="text-embedding-3-large Tokens"),
    ])
    assert out["by_model"][0]["model"] == "text-embedding-3-large"


def test_model_costs_aggregate_across_resources():
    out = attribute_ai_costs([
        _line(10.0, sub="gpt-4o", name="gpt-4o Tokens", rid=AOAI_ID),
        _line(15.0, sub="gpt-4o", name="gpt-4o Tokens", rid=FOUNDRY_ID),
    ])
    row = out["by_model"][0]
    assert row["model"] == "gpt-4o"
    assert row["monthly_usd"] == 25.0
    assert row["resource_count"] == 2


def test_concentration_hint_fires_when_one_model_dominates():
    out = attribute_ai_costs([
        _line(900.0, sub="gpt-4o", name="gpt-4o Tokens"),
        _line(100.0, sub="gpt-4o-mini", name="gpt-4o-mini Tokens"),
    ])
    types = {h["type"] for h in out["hints"]}
    assert "model_concentration" in types


def test_commitment_hint_fires_on_high_steady_spend():
    out = attribute_ai_costs([
        _line(1500.0, sub="gpt-4o", name="gpt-4o Tokens"),
    ])
    types = {h["type"] for h in out["hints"]}
    assert "commitment_opportunity" in types


def test_compute_no_tokens_verify_hint():
    out = attribute_ai_costs([
        _line(300.0, consumed="Microsoft.MachineLearningServices",
              name="Managed Online Endpoint vCPU", rid=ML_ID),
    ])
    types = {h["type"] for h in out["hints"]}
    assert "compute_no_tokens_verify" in types


def test_top_drivers_ranked_and_capped():
    items = [_line(float(i), sub=f"m{i}", name=f"m{i} Tokens",
                   rid=f"{AOAI_ID}{i}") for i in range(1, 15)]
    out = attribute_ai_costs(items)
    assert len(out["top_drivers"]) == 10
    costs = [d["monthly_usd"] for d in out["top_drivers"]]
    assert costs == sorted(costs, reverse=True)
    assert costs[0] == 14.0


def test_as_of_is_max_date():
    out = attribute_ai_costs([
        _line(1.0, name="gpt-4o Tokens", date="2026-07-10"),
        _line(1.0, name="gpt-4o Tokens", date="2026-07-15"),
    ])
    assert out["as_of"] == "2026-07-15"
