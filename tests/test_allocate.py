"""Layer-1 unit tests for the cost-allocation (showback) skill (offline, deterministic)."""

import importlib.util
from pathlib import Path

_PATH = (
    Path(__file__).resolve().parent.parent
    / "plugins"
    / "azure-finops"
    / "skills"
    / "finops-cost-allocation"
    / "allocate.py"
)
_spec = importlib.util.spec_from_file_location("allocate", _PATH)
allocate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(allocate)

allocate_costs = allocate.allocate_costs

A = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/a"
B = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/b"
C = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Cache/Redis/c"
D = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.App/sessionPools/d"


def test_empty_inputs():
    out = allocate_costs()
    assert out["total_usd"] == 0.0
    assert out["groups"] == []
    assert out["unallocated_usd"] == 0.0


def test_group_by_team_and_totals():
    costs = {A: 100.0, B: 40.0, C: 60.0}
    tags = {A: {"team": "payments"}, B: {"team": "payments"}, C: {"team": "search"}}
    out = allocate_costs(costs, tags, dimension="team")
    assert out["total_usd"] == 200.0
    assert out["allocated_usd"] == 200.0
    assert out["unallocated_usd"] == 0.0
    g = {x["owner"]: x for x in out["groups"]}
    assert g["payments"]["monthly_usd"] == 140.0
    assert g["payments"]["resource_count"] == 2
    assert g["payments"]["pct"] == 70.0
    assert g["search"]["monthly_usd"] == 60.0
    # groups sorted by cost desc
    assert out["groups"][0]["owner"] == "payments"


def test_unallocated_bucket_not_force_allocated():
    costs = {A: 100.0, C: 331.0}
    tags = {A: {"team": "payments"}}  # C has no team tag
    out = allocate_costs(costs, tags, dimension="team")
    assert out["allocated_usd"] == 100.0
    assert out["unallocated_usd"] == 331.0
    assert out["unallocated_pct"] == 76.8
    assert out["unallocated"]["resource_count"] == 1
    assert out["unallocated"]["resources"][0]["resourceId"] == C
    # the single group is unchanged — shared cost is NOT spread onto payments
    assert out["groups"][0]["monthly_usd"] == 100.0


def test_untagged_means_no_tags_at_all():
    costs = {A: 10.0, B: 20.0, C: 30.0}
    # A has only a NON-recommended tag (foo); B has nothing; C has a recommended tag
    tags = {A: {"foo": "bar"}, C: {"owner": "nir"}}
    out = allocate_costs(costs, tags, dimension="team")
    untagged_ids = [r["resourceId"] for r in out["untagged_resources"]]
    assert untagged_ids == [B]  # only B has NO tags at all; A's non-recommended tag still counts
    assert out["untagged_usd"] == 20.0


def test_case_insensitive_tag_key_and_resource_id():
    costs = {A.upper(): 50.0}
    tags = {A: {"Team": "Payments"}}  # different id casing + capitalized key
    out = allocate_costs(costs, tags, dimension="team")
    assert out["unallocated_usd"] == 0.0
    assert out["groups"][0]["owner"] == "Payments"
    assert out["groups"][0]["monthly_usd"] == 50.0


def test_tag_hygiene_flags_variant_spellings():
    costs = {A: 100.0, B: 60.0, C: 10.0}
    tags = {A: {"team": "Prod"}, B: {"team": "prod"}, C: {"team": "PROD"}}
    out = allocate_costs(costs, tags, dimension="team")
    # all three collapse to one owner group
    assert len(out["groups"]) == 1
    assert out["groups"][0]["monthly_usd"] == 170.0
    assert out["groups"][0]["owner"] == "Prod"  # most costly raw spelling
    hy = out["tag_hygiene"]
    assert len(hy) == 1
    assert hy[0]["canonical"] == "Prod"
    assert hy[0]["cost_affected"] == 170.0
    assert set(hy[0]["variants"].keys()) == {"Prod", "prod", "PROD"}


def test_no_hygiene_flag_when_consistent():
    costs = {A: 100.0, B: 60.0}
    tags = {A: {"team": "payments"}, B: {"team": "payments"}}
    out = allocate_costs(costs, tags, dimension="team")
    assert out["tag_hygiene"] == []


def test_dimension_selects_key():
    costs = {A: 100.0, B: 40.0}
    tags = {A: {"team": "pay", "env": "prod"}, B: {"team": "pay", "env": "dev"}}
    out = allocate_costs(costs, tags, dimension="env")
    owners = {x["owner"]: x["monthly_usd"] for x in out["groups"]}
    assert owners == {"prod": 100.0, "dev": 40.0}


def test_non_numeric_cost_ignored():
    costs = {A: 100.0, B: None, C: "oops"}
    tags = {A: {"team": "x"}}
    out = allocate_costs(costs, tags, dimension="team")
    assert out["total_usd"] == 100.0


def test_empty_inputs_have_new_tag_fields():
    out = allocate_costs()
    assert out["tag_inventory"] == []
    assert out["missing_recommended"] == list(allocate.RECOMMENDED_TAG_KEYS)
    assert all(c["present"] is False for c in out["recommended_coverage"])


def test_tag_inventory_lists_every_key_present_ranked_by_cost():
    costs = {A: 100.0, B: 40.0, C: 60.0}
    # A: team+env, B: env only, C: a non-recommended key
    tags = {A: {"team": "pay", "env": "prod"}, B: {"env": "dev"}, C: {"foo": "bar"}}
    out = allocate_costs(costs, tags, dimension="team")
    inv = {t["key"]: t for t in out["tag_inventory"]}
    assert inv["env"]["resource_count"] == 2
    assert inv["env"]["cost_usd"] == 140.0     # A(100) + B(40)
    assert inv["team"]["cost_usd"] == 100.0
    assert inv["foo"]["cost_usd"] == 60.0       # non-recommended keys still inventoried
    # ranked by cost desc: env(140) > team(100) > foo(60)
    assert [t["key"] for t in out["tag_inventory"]] == ["env", "team", "foo"]


def test_recommended_coverage_present_and_missing():
    costs = {A: 100.0, B: 100.0}
    tags = {A: {"team": "pay"}, B: {"env": "prod"}}
    out = allocate_costs(costs, tags, dimension="team")
    cov = {c["key"]: c for c in out["recommended_coverage"]}
    assert cov["team"]["present"] is True
    assert cov["team"]["cost_usd"] == 100.0
    assert cov["team"]["pct"] == 50.0
    assert cov["service"]["present"] is False
    assert cov["service"]["cost_usd"] == 0.0
    # recommended order is preserved
    assert [c["key"] for c in out["recommended_coverage"]] == list(allocate.RECOMMENDED_TAG_KEYS)
    assert "team" not in out["missing_recommended"]
    assert "service" in out["missing_recommended"]
    assert "costCenter" in out["missing_recommended"]


def test_recommended_keys_overridable():
    costs = {A: 100.0}
    tags = {A: {"project": "atlas"}}
    out = allocate_costs(costs, tags, dimension="project", recommended_keys=("project", "owner"))
    cov = {c["key"]: c for c in out["recommended_coverage"]}
    assert set(cov) == {"project", "owner"}
    assert cov["project"]["present"] is True
    assert out["missing_recommended"] == ["owner"]
