"""Tests for deterministic ARM MCP query_costs preparation."""

import importlib.util
import json
import sys
from decimal import Decimal
from datetime import date, timedelta
from collections import Counter
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


def test_presliced_histories_cover_every_day_once_and_preserve_shape_and_scope():
    for days, expected_count in ((21, 5), (7, 13)):
        plans = cost_query.plan_cost_queries(
            [RG, OTHER_RG], start="2026-08-27", end="2026-11-24",
            granularity="Daily", group_by=["ResourceId", "ServiceName"],
            slice_days=days, as_of="2026-11-24",
        )
        assert len(plans) == expected_count * 2
        for scope in (RG, OTHER_RG):
            scoped = [plan for plan in plans if plan["scope"] == scope]
            covered = []
            for plan in scoped:
                first, last = date.fromisoformat(plan["from"]), date.fromisoformat(plan["to"])
                assert 1 <= (last - first).days + 1 <= days
                assert plan["groupBy"] == "ResourceId,ServiceName"
                assert plan["metric"] == "PreTaxCostUSD" and plan["top"] == 5000
                covered.extend(first + timedelta(days=i) for i in range((last - first).days + 1))
            assert covered == [date(2026, 8, 27) + timedelta(days=i) for i in range(90)]


@pytest.mark.parametrize("slice_days", [0, -1, True, 1.5, "7"])
def test_preslicing_rejects_invalid_sizes(slice_days):
    with pytest.raises(ValueError, match="slice_days"):
        cost_query.plan_cost_queries([SUB], start="2026-11-01", end="2026-11-30",
                                     granularity="Daily", slice_days=slice_days)


def test_preslicing_requires_daily_granularity():
    with pytest.raises(ValueError, match="Daily"):
        cost_query.plan_cost_queries([SUB], start="2026-11-01", end="2026-11-30",
                                     granularity="Monthly", slice_days=7)


def test_parallel_batches_interleave_shapes_and_share_subscription_limits_across_resource_groups():
    overview = cost_query.plan_cost_queries(
        [RG, "/subscriptions/sub/resourceGroups/second", OTHER_RG],
        start="2026-08-27", end="2026-11-24", granularity="Daily",
        group_by="ResourceGroupName,ServiceName", slice_days=21,
    )
    explorer = cost_query.plan_cost_queries(
        [RG, OTHER_RG], start="2026-08-27", end="2026-11-24", granularity="Daily",
        group_by="ResourceId,ServiceName,ResourceLocation,ChargeType", slice_days=7,
    )
    queries = overview + explorer
    batches = cost_query.batch_cost_queries(queries)
    assert {plan["groupBy"] for plan in batches[0]} == {overview[0]["groupBy"], explorer[0]["groupBy"]}
    assert any(len(batch) == 4 for batch in batches)
    assert len(batches) < len(queries)
    for batch in batches:
        assert 1 <= len(batch) <= 4
        assert max(Counter(plan["scope"].split("/")[2].lower() for plan in batch).values()) <= 2
    frozen = lambda plan: json.dumps(plan, sort_keys=True)
    assert Counter(map(frozen, queries)) == Counter(frozen(plan) for batch in batches for plan in batch)

    throttled = cost_query.batch_cost_queries(queries, max_per_subscription=1)
    for batch in throttled:
        assert len({plan["scope"].split("/")[2].lower() for plan in batch}) == len(batch)
    assert cost_query.batch_cost_queries([]) == []


@pytest.mark.parametrize("limits", [
    {"max_concurrent": 0}, {"max_concurrent": 5}, {"max_concurrent": True},
    {"max_per_subscription": 0}, {"max_per_subscription": 3},
])
def test_parallel_batch_limits_cannot_exceed_safe_bounds(limits):
    with pytest.raises(ValueError, match="must be an integer"):
        cost_query.batch_cost_queries([], **limits)


def test_parallel_batches_reject_duplicates_and_do_not_mutate_arguments():
    arguments = _args(RG, groupBy="ResourceId")
    batch = cost_query.batch_cost_queries([arguments])
    batch[0][0]["from"] = "2026-11-02"
    assert arguments["from"] == "2026-11-01"
    with pytest.raises(ValueError, match="duplicate pending"):
        cost_query.batch_cost_queries([arguments, {**arguments, "scope": RG.upper()}])
    with pytest.raises(ValueError, match="expanded"):
        cost_query.batch_cost_queries([_args("/providers/Microsoft.Management/managementGroups/mg")])


def test_parallel_split_leaves_prepare_in_any_completion_order_without_parent_double_count():
    plans = cost_query.plan_cost_queries([RG], start="2026-11-01", end="2026-11-09",
                                        granularity="Daily", group_by="ResourceId", slice_days=7)
    children = cost_query.split_cost_query(plans[0])
    leaves = children + plans[1:]
    batches = cost_query.batch_cost_queries(leaves)
    assert len(batches[0]) == 2
    resource = f"{RG}/providers/Microsoft.Web/sites/app"
    results = [{
        "arguments": plan,
        "result": _result(["PreTaxCostUSD", "UsageDate", "ResourceId", "Currency"], [
            [1, int((date.fromisoformat(plan["from"]) + timedelta(days=i)).strftime("%Y%m%d")), resource, "USD"]
            for i in range((date.fromisoformat(plan["to"]) - date.fromisoformat(plan["from"])).days + 1)
        ]),
    } for batch in batches for plan in batch]
    prepared = cost_query.prepare_cost_query(list(reversed(results)), [RG],
                                            window=["2026-11-01", "2026-11-09"])
    assert prepared["partial"] is False
    assert prepared["included_count"] == 9
    assert prepared["included_cost"] == Decimal("9")


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


def test_daily_query_shapes_produce_scoped_90_day_history_and_30_day_baseline():
    renderer_dir = SKILL.parent / "finops-report-renderer"
    spec = importlib.util.spec_from_file_location("history_dashboard", renderer_dir / "dashboard.py")
    dashboard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dashboard)
    start, end = "2026-08-19", "2026-11-16"
    default_window = ["2026-10-18", end]
    scoped_resource = f"{RG}/providers/Microsoft.Web/sites/app"
    outside_resource = "/subscriptions/sub/resourceGroups/elsewhere/providers/Microsoft.Web/sites/other"
    overview_args = cost_query.plan_cost_queries(
        [RG], start=start, end=end, granularity="Daily",
        group_by=["ResourceGroupName", "ServiceName"], as_of=end,
    )[0]
    explorer_args = cost_query.plan_cost_queries(
        [RG], start=start, end=end, granularity="Daily",
        group_by=["ResourceId", "ServiceName", "ResourceLocation", "ChargeType"], as_of=end,
    )[0]
    overview = cost_query.prepare_cost_query([{
        "arguments": overview_args,
        "result": _result(["PreTaxCostUSD", "UsageDate", "ResourceGroupName", "ServiceName", "Currency"], [
            [10.75, 20260819, "managed", "App Service", "USD"],
            [2.25, 20261018, "managed", "App Service", "USD"],
            [3.75, 20261116, "managed", "App Service", "USD"],
        ]),
    }], [RG], window=[start, end])
    explorer = cost_query.prepare_cost_query([{
        "arguments": explorer_args,
        "result": _result(
            ["PreTaxCostUSD", "UsageDate", "ResourceId", "ServiceName", "ResourceLocation", "ChargeType", "Currency"],
            [[10.75, 20260819, scoped_resource, "App Service", "East US", "Usage", "USD"],
             [2.25, 20261018, scoped_resource, "App Service", "East US", "Usage", "USD"],
             [3.75, 20261116, scoped_resource, "App Service", "East US", "Usage", "USD"],
             [99, 20261116, outside_resource, "App Service", "East US", "Usage", "USD"]],
        ),
    }], [RG], window=[start, end])
    overview_30 = dashboard.slice_daily_cost_query(overview, default_window)
    explorer_30 = dashboard.slice_daily_cost_query(explorer, default_window)
    assert overview_30["scope_coverage"][0]["cost_usd"] == Decimal("6")
    assert explorer_30["scope_coverage"][0]["excluded"] == 1
    metadata = {"status": "ready", "source": "ARM MCP query_costs",
                "refreshed_at": "2026-11-16T22:30:00Z", "scope_summary": [RG]}
    sources = {
        "overview": {**metadata, "window": default_window,
                     "data": cost_query.summarize_cost_overview(overview_30)},
        "explorer": {**metadata, "window": default_window,
                     "data": dashboard.build_explorer_rows(explorer_30["included_rows"])},
    }
    history = {
        "window": [start, end],
        "overview": {**metadata, "window": [start, end], "data": overview["included_rows"]},
        "explorer": {**metadata, "window": [start, end], "data": explorer["included_rows"]},
    }
    model = dashboard.build_dashboard_model(
        sources, refreshed_at=metadata["refreshed_at"], scope_summary=[RG],
        window=default_window, scope_coverage=overview_30["scope_coverage"], cost_history=history,
    )
    assert model["costHistory"]["available"]
    assert model["tabs"][0]["metrics"][0]["value"] == "$6.00"
    assert all("elsewhere" not in str(record["fields"]).lower() for record in model["costHistory"]["explorer"])


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
