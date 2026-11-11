"""Tests for deterministic ARM MCP query_costs preparation."""

import importlib.util
import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest


SKILL = (
    Path(__file__).resolve().parents[1]
    / "plugins"
    / "azure-finops"
    / "skills"
    / "finops-managed-scope"
)
sys.path.insert(0, str(SKILL))
SPEC = importlib.util.spec_from_file_location("finops_cost_query", SKILL / "cost_query.py")
cost_query = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cost_query)

SUB = "/subscriptions/sub"
RG = "/subscriptions/sub/resourceGroups/managed"
OTHER_RG = "/subscriptions/other/resourceGroups/apps"


def _result(columns, rows, next_link=None):
    return {
        "structuredContent": {
            "result": {
                "properties": {
                    "nextLink": next_link,
                    "columns": [{"name": name, "type": "String"} for name in columns],
                    "rows": rows,
                }
            }
        }
    }


def _args(scope, start="2026-11-01", end="2026-11-02", **extra):
    return {"scope": scope, "from": start, "to": end, "metric": "PreTaxCostUSD", **extra}


DAILY = ["PreTaxCostUSD", "UsageDate", "ResourceId", "MeterCategory", "Currency"]


def test_plan_expands_management_groups_and_never_queries_broader_scope():
    plans = cost_query.plan_cost_queries(
        ["/providers/Microsoft.Management/managementGroups/mg", RG],
        management_group_expansions={"mg": ["/subscriptions/child"]},
        start="2026-11-01",
        end="2026-11-30",
        granularity="Daily",
        group_by=["ResourceGroupName", "ServiceName"],
    )

    assert [plan["scope"] for plan in plans] == ["/subscriptions/child", RG]
    assert all(plan["metric"] == "PreTaxCostUSD" for plan in plans)
    assert all(plan["top"] == 5000 for plan in plans)
    assert plans[0]["groupBy"] == "ResourceGroupName,ServiceName"

    with pytest.raises(ValueError, match="expanded"):
        cost_query.plan_cost_queries(
            ["/providers/Microsoft.Management/managementGroups/mg"],
            start="2026-11-01",
            end="2026-11-30",
        )


def test_split_halves_date_range_and_refuses_single_day():
    first, second = cost_query.split_cost_query(_args(SUB, "2026-11-01", "2026-11-30"))
    assert (first["from"], first["to"]) == ("2026-11-01", "2026-11-15")
    assert (second["from"], second["to"]) == ("2026-11-16", "2026-11-30")
    with pytest.raises(ValueError, match="single-day"):
        cost_query.split_cost_query(_args(SUB, "2026-11-01", "2026-11-01"))


def test_flattens_rows_and_filters_to_managed_scope():
    in_rg = f"{RG}/providers/Microsoft.Web/sites/app".lower()
    out_rg = "/subscriptions/sub/resourcegroups/elsewhere/providers/microsoft.web/sites/x"
    prepared = cost_query.prepare_cost_query(
        [
            {
                "arguments": _args(RG, granularity="Daily", groupBy="ResourceId,MeterCategory"),
                "result": _result(
                    DAILY,
                    [
                        [1.25, 20261101, in_rg, "App Service", "USD"],
                        [2.5, 20261102, in_rg, "App Service", "USD"],
                        [9.0, 20261102, out_rg, "App Service", "USD"],
                    ],
                ),
            }
        ],
        [RG],
        window=("2026-11-01", "2026-11-02"),
    )

    assert prepared["partial"] is False
    assert prepared["included_count"] == 2
    assert prepared["excluded_count"] == 1
    assert prepared["included_cost"] == Decimal("3.75")
    row = prepared["included_rows"][0]
    assert row["date"] == "2026-11-01"
    assert row["cost"] == 1.25
    assert row["meterCategory"] == "App Service"
    assert row["resourceGroup"] == "managed"
    assert row["subscriptionId"] == "sub"
    assert prepared["scope_coverage"][0]["status"] == "complete"
    assert prepared["scope_coverage"][0]["cost_usd"] == Decimal("3.75")


def test_accepts_content_text_and_derives_resource_group_from_resource_id():
    resource = "/subscriptions/sub/resourcegroups/web/providers/microsoft.web/sites/app"
    text = json.dumps(_result(["PreTaxCostUSD", "ResourceId", "Currency"], [[4, resource, "USD"]])["structuredContent"])
    prepared = cost_query.prepare_cost_query(
        [{"arguments": _args(SUB, groupBy="ResourceId"), "result": {"content": [{"type": "text", "text": text}]}}],
        [SUB],
    )
    assert prepared["included_rows"][0]["resourceGroup"] == "web"


def test_date_slices_are_summed_not_deduplicated():
    resource = f"{RG}/providers/Microsoft.Web/sites/app"
    columns = ["PreTaxCostUSD", "ResourceId", "Currency"]
    prepared = cost_query.prepare_cost_query(
        [
            {"arguments": _args(RG, "2026-11-01", "2026-11-15", groupBy="ResourceId"), "result": _result(columns, [[5, resource, "USD"]])},
            {"arguments": _args(RG, "2026-11-16", "2026-11-30", groupBy="ResourceId"), "result": _result(columns, [[5, resource, "USD"]])},
        ],
        [RG],
        window=("2026-11-01", "2026-11-30"),
    )
    assert prepared["duplicate_count"] == 0
    assert prepared["included_cost"] == Decimal("10")


@pytest.mark.parametrize(
    "records, message",
    [
        ([{"arguments": _args(OTHER_RG), "result": _result(DAILY, [])}], "not an effective managed scope"),
        ([{"arguments": {**_args(RG), "metric": "PreTaxCost"}, "result": _result(DAILY, [])}], "PreTaxCostUSD"),
        ([{"arguments": {"scope": RG, "timeframe": "MonthToDate", "metric": "PreTaxCostUSD"}, "result": _result(DAILY, [])}], "explicit from/to"),
        ([{"arguments": _args(RG), "result": _result(["PreTaxCost", "Currency"], [[1, "EUR"]])}], "not in USD"),
        ([{"arguments": _args(RG), "result": {"isError": True, "content": [{"type": "text", "text": "429 throttled"}]}}], "429 throttled"),
        (
            [
                {"arguments": _args(RG, "2026-11-01", "2026-11-10"), "result": _result(DAILY, [])},
                {"arguments": _args(RG, "2026-11-10", "2026-11-20"), "result": _result(DAILY, [])},
            ],
            "overlap",
        ),
        (
            [
                {"arguments": _args(RG, groupBy="ResourceId"), "result": _result(DAILY, [])},
                {"arguments": _args(RG, "2026-11-03", "2026-11-04", groupBy="ServiceName"), "result": _result(DAILY, [])},
            ],
            "mix query shapes",
        ),
    ],
)
def test_rejects_unsafe_or_ambiguous_results(records, message):
    with pytest.raises(ValueError, match=message):
        cost_query.prepare_cost_query(records, [RG])


def test_truncation_gaps_and_missing_scopes_fail_closed_or_report_partial():
    records = [
        {"arguments": _args(RG, "2026-11-01", "2026-11-10"), "result": _result(DAILY, [], next_link="https://next")},
        {"arguments": _args(RG, "2026-11-15", "2026-11-30"), "result": _result(DAILY, [])},
    ]
    managed = [RG, "/subscriptions/second"]
    with pytest.raises(ValueError, match="truncated"):
        cost_query.prepare_cost_query(records, managed, window=("2026-11-01", "2026-11-30"))

    partial = cost_query.prepare_cost_query(
        records, managed, window=("2026-11-01", "2026-11-30"), require_complete=False
    )
    assert partial["partial"] is True
    assert partial["truncated_slices"] == [{"scope": RG, "from": "2026-11-01", "to": "2026-11-10"}]
    assert partial["date_gaps"] == [{"scope": RG, "from": "2026-11-11", "to": "2026-11-14"}]
    assert partial["missing_scopes"] == ["/subscriptions/second"]
    statuses = {row["scope"]: row["status"] for row in partial["scope_coverage"]}
    assert statuses == {RG: "partial", "/subscriptions/second": "missing"}
    assert len(partial["warnings"]) == 3


def test_confirmed_interactive_scope_is_allowed_and_kept():
    resource = f"{OTHER_RG}/providers/Microsoft.Web/sites/app"
    prepared = cost_query.prepare_cost_query(
        [{"arguments": _args(OTHER_RG, groupBy="ResourceId"), "result": _result(["PreTaxCostUSD", "ResourceId", "Currency"], [[3, resource, "USD"]])}],
        [RG],
        expected_scopes=[OTHER_RG],
        confirmed_scopes=[OTHER_RG],
    )
    assert prepared["included_cost"] == Decimal("3")


def test_cost_overview_summary_feeds_renderer_model():
    renderer = SKILL.parent / "finops-report-renderer"
    spec = importlib.util.spec_from_file_location("finops_models", renderer / "models.py")
    models = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(models)
    columns = ["PreTaxCostUSD", "UsageDate", "ResourceGroupName", "ServiceName", "Currency"]
    prepared = cost_query.prepare_cost_query(
        [
            {
                "arguments": _args(SUB, granularity="Daily", groupBy="ResourceGroupName,ServiceName"),
                "result": _result(
                    columns,
                    [
                        [1.10, 20261101, "web", "Storage", "USD"],
                        [2.20, 20261102, "WEB", "Storage", "USD"],
                        [0.70, 20261102, "", "Microsoft Defender for Cloud", "USD"],
                    ],
                ),
            }
        ],
        [SUB],
        window=("2026-11-01", "2026-11-02"),
    )

    overview = cost_query.summarize_cost_overview(prepared)

    assert overview["total_usd"] == Decimal("4.00")
    assert overview["daily"] == [
        {"date": "2026-11-01", "cost_usd": Decimal("1.1")},
        {"date": "2026-11-02", "cost_usd": Decimal("2.90")},
    ]
    assert overview["by_resource_group"][0] == {"resource_group": "web", "cost_usd": Decimal("3.30")}
    model = models.build_cost_overview_model(overview, refreshed_at="2026-11-03T00:00:00Z")
    assert model["metrics"][0]["value"] == "$4.00"
    assert ["(unattributed)", "$0.70"] in model["sections"][1]["rows"]
    assert model["partial"] is False

def test_plan_rejects_start_older_than_lookback_window():
    plans = cost_query.plan_cost_queries([SUB], start="2026-08-11", end="2026-11-10", as_of="2026-11-10")
    assert plans[0]["from"] == "2026-08-11"
    with pytest.raises(ValueError, match="earliest start is 2026-08-11"):
        cost_query.plan_cost_queries([SUB], start="2026-08-10", end="2026-11-10", as_of="2026-11-10")

def test_sum_cost_by_is_case_insensitive_and_separates_unkeyed_cost():
    rows = [
        {"resourceId": "/subscriptions/s/resourceGroups/a/providers/x/y/App", "cost": 0.1},
        {"resourceId": "/subscriptions/s/resourcegroups/a/providers/x/y/app", "cost": 0.2},
        {"resourceId": "", "cost": 1.5},
    ]
    totals, unkeyed = cost_query.sum_cost_by(rows, "resourceId")
    assert totals == {"/subscriptions/s/resourceGroups/a/providers/x/y/App": 0.3}
    assert unkeyed == 1.5

def test_resource_type_rows_gain_consumed_service_namespace():
    rid = "/subscriptions/sub/resourcegroups/ai/providers/microsoft.cognitiveservices/accounts/a"
    prepared = cost_query.prepare_cost_query(
        [{"arguments": _args(SUB, groupBy="ResourceId,ResourceType"),
          "result": _result(["PreTaxCostUSD", "ResourceId", "ResourceType", "Currency"],
                            [[1, rid, "microsoft.cognitiveservices/accounts", "USD"]])}],
        [SUB],
    )
    assert prepared["included_rows"][0]["consumedService"] == "microsoft.cognitiveservices"
