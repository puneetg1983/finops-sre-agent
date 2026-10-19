"""Deterministic governed budget proposal tests."""

import importlib.util
import json
import shlex
import subprocess
from pathlib import Path

import pytest


PATH = (
    Path(__file__).resolve().parent.parent
    / "plugins"
    / "azure-finops"
    / "skills"
    / "finops-budget-editor"
    / "recommend.py"
)
SPEC = importlib.util.spec_from_file_location("budget_proposals", PATH)
budget_proposals = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(budget_proposals)

build = budget_proposals.build_budget_proposal
compare = budget_proposals.compare_budget_readback
derive = budget_proposals.derive_budget_amount

CONTACTS = ["finops@example.org"]
PERIOD = {
    "startDate": "2026-08-01T00:00:00Z",
    "endDate": "2027-08-01T00:00:00Z",
}
NOTIFICATIONS = {
    "existing": {
        "enabled": True,
        "operator": "GreaterThanOrEqualTo",
        "threshold": 90,
        "thresholdType": "Actual",
        "contactEmails": ["owner@example.org"],
    }
}


def _exact_budget(scope="/subscriptions/sub", name="ops-budget", **property_overrides):
    properties = {
        "amount": 1000.0,
        "category": "Cost",
        "timeGrain": "Monthly",
        "timePeriod": PERIOD,
        "filter": {
            "dimensions": {
                "name": "ResourceGroupName",
                "operator": "In",
                "values": ["prod"],
            }
        },
        "notifications": NOTIFICATIONS,
        "currentSpend": {"amount": 500, "unit": "USD"},
        "forecastSpend": {"amount": 1100, "unit": "USD"},
    }
    properties.update(property_overrides)
    return {
        "id": f"{scope}/providers/Microsoft.Consumption/budgets/{name}",
        "name": name,
        "eTag": '"etag-1"',
        "properties": properties,
    }


@pytest.mark.parametrize(
    ("scope", "expected_path"),
    [
        ("/subscriptions/sub", "/subscriptions/sub/"),
        (
            "/subscriptions/sub/resourceGroups/rg-one",
            "/subscriptions/sub/resourceGroups/rg-one/",
        ),
        (
            "/providers/Microsoft.Management/managementGroups/mg-one",
            "/providers/Microsoft.Management/managementGroups/mg-one/",
        ),
    ],
)
def test_create_supports_all_canonical_scopes(scope, expected_path):
    proposal = build(
        scope=scope,
        name="ops-budget",
        amount=1200,
        time_grain="Monthly",
        time_period=PERIOD,
        contacts=CONTACTS,
    )
    assert proposal["operation"] == "create"
    assert proposal["before"] is None
    assert expected_path in proposal["put_url"]
    assert proposal["post_write_get_url"] == proposal["put_url"]


def test_update_detected_from_exact_get_and_preserves_mutable_settings():
    existing = _exact_budget()
    proposal = build(
        scope="/subscriptions/sub",
        name="ops-budget",
        exact_budget=existing,
        amount=1500,
    )
    after = proposal["put_body"]["properties"]
    assert proposal["operation"] == "update"
    assert proposal["before"] == existing
    assert after["amount"] == 1500
    assert after["category"] == existing["properties"]["category"]
    assert after["timeGrain"] == existing["properties"]["timeGrain"]
    assert after["timePeriod"] == existing["properties"]["timePeriod"]
    assert after["filter"] == existing["properties"]["filter"]
    assert after["notifications"] == existing["properties"]["notifications"]
    assert "currentSpend" not in after
    assert "forecastSpend" not in after
    assert proposal["expected_before_properties"] == {
        key: value
        for key, value in existing["properties"].items()
        if key not in {"currentSpend", "forecastSpend"}
    }


def test_update_allows_explicit_setting_overrides():
    proposal = build(
        scope="/subscriptions/sub",
        name="ops-budget",
        exact_budget=_exact_budget(),
        amount=2000,
        time_grain="Quarterly",
        time_period={
            "startDate": "2026-08-01",
            "endDate": "2027-08-01",
        },
        as_of="2026-08-02",
        category="Cost",
        filters=None,
        notifications={
            "forecast": {
                "enabled": True,
                "threshold": 95,
                "thresholdType": "Forecasted",
                "operator": "EqualTo",
                "contactEmails": ["forecast@example.org"],
            }
        },
    )
    after = proposal["put_body"]["properties"]
    assert after["category"] == "Cost"
    assert after["timeGrain"] == "Quarterly"
    assert "filter" not in after
    assert set(after["notifications"]) == {"forecast"}


def test_create_rejects_filters():
    with pytest.raises(ValueError, match="scope-wide"):
        build(
            scope="/subscriptions/sub",
            name="filtered",
            amount=1000,
            time_grain="Monthly",
            time_period=PERIOD,
            contacts=CONTACTS,
            filters={"tags": {"name": "env", "operator": "In", "values": ["prod"]}},
        )


@pytest.mark.parametrize(
    ("grain", "as_of", "current", "prior", "expected"),
    [
        ("Monthly", "2026-07-15", 310, [500, 500, 500], 750.0),
        ("Quarterly", "2026-08-15", 460, [1000, 1000, 1000, 1000], 1200.0),
        ("Annually", "2026-07-02", 1830, [4000], 4600.0),
    ],
)
def test_derivation_formula_all_time_grains(grain, as_of, current, prior, expected):
    result = derive(
        grain,
        {
            "current_period_total": current,
            "prior_complete_period_totals": prior,
        },
        as_of=as_of,
        headroom_pct=15,
    )
    assert result["amount"] == expected
    assert result["evidence"]["method"] == "usageDetailsActualCost"
    assert result["evidence"]["basis"] == max(
        result["evidence"]["current_period"]["run_rate"],
        result["evidence"]["prior_average"],
    )


def test_partial_derived_amount_is_gated_without_executable_output():
    with pytest.raises(ValueError, match="explicit amount"):
        build(
            scope="/subscriptions/sub",
            name="derived",
            period_totals={
                "current_period_total": 310,
                "prior_complete_period_totals": [100, 200, 500, 500],
                "partial": True,
            },
            as_of="2026-07-15",
            headroom_pct=10,
            time_grain="Monthly",
            time_period=PERIOD,
            contacts=CONTACTS,
        )


def test_partial_evidence_with_explicit_amount_warns_and_can_generate_script():
    proposal = build(
        scope="/subscriptions/sub",
        name="explicit-with-partial-evidence",
        amount=750,
        period_totals={
            "current_period_total": 310,
            "prior_complete_period_totals": [100, 200, 500, 500],
            "incomplete": True,
            "warnings": ["one UsageDetails page failed"],
        },
        time_grain="Monthly",
        time_period=PERIOD,
        contacts=CONTACTS,
    )
    assert proposal["put_body"]["properties"]["amount"] == 750
    assert proposal["command"]
    assert proposal["application_script"]
    assert any("partial/incomplete" in warning for warning in proposal["warnings"])


def test_explicit_amount_is_not_rounded():
    proposal = build(
        scope="/subscriptions/sub",
        name="explicit",
        amount=1234.56,
        time_grain="Monthly",
        time_period=PERIOD,
        contacts=CONTACTS,
    )
    assert proposal["put_body"]["properties"]["amount"] == 1234.56
    assert proposal["derivation"] == {"method": "explicit", "amount": 1234.56}


@pytest.mark.parametrize("amount", [0, -1, float("inf"), "1000"])
def test_explicit_amount_must_be_positive_finite_number(amount):
    with pytest.raises(ValueError, match="positive"):
        build(
            scope="/subscriptions/sub",
            name="bad-amount",
            amount=amount,
            time_grain="Monthly",
            time_period=PERIOD,
            contacts=CONTACTS,
        )


def test_time_period_allows_optional_end_date():
    proposal = build(
        scope="/subscriptions/sub",
        name="open-ended",
        amount=1000,
        time_grain="Quarterly",
        time_period={"startDate": "2026-08-01T00:00:00Z"},
        as_of="2026-08-02",
        contacts=CONTACTS,
    )
    assert proposal["put_body"]["properties"]["timePeriod"] == {
        "startDate": "2026-08-01T00:00:00Z"
    }


def test_create_start_date_constraints_and_end_order():
    with pytest.raises(ValueError, match="startDate"):
        build(
            scope="/subscriptions/sub",
            name="missing-start",
            amount=1000,
            time_grain="Monthly",
            time_period={"endDate": "2027-07-01"},
            contacts=CONTACTS,
        )
    with pytest.raises(ValueError, match="first day of a month"):
        build(
            scope="/subscriptions/sub",
            name="not-month-start",
            amount=1000,
            time_grain="Monthly",
            time_period={"startDate": "2026-08-02T00:00:00Z"},
            as_of="2026-08-02",
            contacts=CONTACTS,
        )
    with pytest.raises(ValueError, match="00:00:00 UTC"):
        build(
            scope="/subscriptions/sub",
            name="not-midnight",
            amount=1000,
            time_grain="Monthly",
            time_period={"startDate": "2026-08-01T00:00:01Z"},
            as_of="2026-08-02",
            contacts=CONTACTS,
        )
    with pytest.raises(ValueError, match="on or after 2017-06-01"):
        build(
            scope="/subscriptions/sub",
            name="too-old",
            amount=1000,
            time_grain="Monthly",
            time_period={"startDate": "2017-05-01T00:00:00Z"},
            as_of="2026-08-02",
            contacts=CONTACTS,
        )
    with pytest.raises(ValueError, match="on or before 2027-08-01"):
        build(
            scope="/subscriptions/sub",
            name="too-far",
            amount=1000,
            time_grain="Monthly",
            time_period={"startDate": "2027-09-01T00:00:00Z"},
            as_of="2026-08-02",
            contacts=CONTACTS,
        )
    with pytest.raises(ValueError, match="after startDate"):
        build(
            scope="/subscriptions/sub",
            name="bad-order",
            amount=1000,
            time_grain="Monthly",
            time_period={
                "startDate": "2027-07-01",
                "endDate": "2026-07-01",
            },
            contacts=CONTACTS,
        )


def test_update_preserves_existing_valid_time_period_exactly():
    existing_period = {"startDate": "2018-01-01T00:00:00Z"}
    proposal = build(
        scope="/subscriptions/sub",
        name="ops-budget",
        exact_budget=_exact_budget(timePeriod=existing_period),
        amount=1500,
        as_of="2026-08-02",
    )
    assert proposal["put_body"]["properties"]["timePeriod"] == existing_period


@pytest.mark.parametrize(
    ("grain", "start", "period_start"),
    [
        ("Monthly", "2026-07-01T00:00:00Z", "2026-08-01"),
        ("Quarterly", "2026-04-01T00:00:00Z", "2026-07-01"),
        ("Annually", "2025-12-01T00:00:00Z", "2026-01-01"),
    ],
)
def test_create_rejects_historical_start_before_current_grain(
    grain, start, period_start
):
    with pytest.raises(ValueError, match=f"period start {period_start}"):
        build(
            scope="/subscriptions/sub",
            name=f"historical-{grain}",
            amount=1000,
            time_grain=grain,
            time_period={"startDate": start},
            as_of="2026-08-02",
            contacts=CONTACTS,
        )


def test_explicit_update_rejects_historical_start_for_selected_grain():
    with pytest.raises(ValueError, match="current monthly period start 2026-08-01"):
        build(
            scope="/subscriptions/sub",
            name="ops-budget",
            exact_budget=_exact_budget(),
            amount=1500,
            time_period={"startDate": "2026-07-01T00:00:00Z"},
            as_of="2026-08-02",
        )


def test_create_requires_real_contact_and_adds_default_notifications():
    with pytest.raises(ValueError, match="contact"):
        build(
            scope="/subscriptions/sub",
            name="no-contact",
            amount=1000,
            time_grain="Monthly",
            time_period=PERIOD,
        )
    proposal = build(
        scope="/subscriptions/sub",
        name="has-contact",
        amount=1000,
        time_grain="Monthly",
        time_period=PERIOD,
        contacts=CONTACTS,
    )
    notifications = proposal["put_body"]["properties"]["notifications"]
    assert set(notifications) == {"actual_80", "forecasted_100"}
    assert notifications["actual_80"]["threshold"] == 80
    assert notifications["forecasted_100"]["threshold"] == 100


def test_update_without_usable_notifications_requires_contact():
    existing = _exact_budget(notifications={})
    with pytest.raises(ValueError, match="contact"):
        build(
            scope="/subscriptions/sub",
            name="ops-budget",
            exact_budget=existing,
            amount=1200,
        )


def test_category_is_cost_only_for_2023_05_01():
    with pytest.raises(ValueError, match="must be Cost"):
        build(
            scope="/subscriptions/sub",
            name="usage-category",
            amount=1000,
            category="Usage",
            time_grain="Monthly",
            time_period=PERIOD,
            contacts=CONTACTS,
        )


@pytest.mark.parametrize("operator", ["EqualTo", "GreaterThan", "GreaterThanOrEqualTo"])
def test_supported_notification_operators(operator):
    proposal = build(
        scope="/subscriptions/sub",
        name=f"operator-{operator}",
        amount=1000,
        time_grain="Monthly",
        time_period=PERIOD,
        notifications={
            "threshold": {
                "enabled": True,
                "operator": operator,
                "threshold": 90,
                "thresholdType": "Actual",
                "contactEmails": ["owner@example.org"],
            }
        },
    )
    assert (
        proposal["put_body"]["properties"]["notifications"]["threshold"]["operator"]
        == operator
    )


@pytest.mark.parametrize("operator", ["LessThan", "NotEqualTo"])
def test_unsupported_notification_operators_rejected(operator):
    with pytest.raises(ValueError, match="unsupported operator"):
        build(
            scope="/subscriptions/sub",
            name="bad-operator",
            amount=1000,
            time_grain="Monthly",
            time_period=PERIOD,
            notifications={
                "threshold": {
                    "enabled": True,
                    "operator": operator,
                    "threshold": 90,
                    "thresholdType": "Actual",
                    "contactEmails": ["owner@example.org"],
                }
            },
        )


def test_notification_count_is_limited_to_five():
    notifications = {
        f"n{index}": {
            "enabled": True,
            "operator": "GreaterThan",
            "threshold": 50 + index,
            "thresholdType": "Actual",
            "contactEmails": ["owner@example.org"],
        }
        for index in range(6)
    }
    with pytest.raises(ValueError, match="at most 5"):
        build(
            scope="/subscriptions/sub",
            name="too-many-notifications",
            amount=1000,
            time_grain="Monthly",
            time_period=PERIOD,
            notifications=notifications,
        )


def test_role_only_notification_does_not_satisfy_human_contact_policy():
    with pytest.raises(ValueError, match="email or action group"):
        build(
            scope="/subscriptions/sub",
            name="role-only",
            amount=1000,
            time_grain="Monthly",
            time_period=PERIOD,
            notifications={
                "owners": {
                    "enabled": True,
                    "operator": "GreaterThan",
                    "threshold": 90,
                    "thresholdType": "Actual",
                    "contactRoles": ["Owner"],
                }
            },
        )


def test_valid_action_group_satisfies_human_contact_policy():
    action_group = (
        "/subscriptions/sub/resourceGroups/ops/providers/"
        "Microsoft.Insights/actionGroups/finops"
    )
    proposal = build(
        scope="/subscriptions/sub",
        name="action-group",
        amount=1000,
        time_grain="Monthly",
        time_period=PERIOD,
        notifications={
            "actual": {
                "enabled": True,
                "operator": "GreaterThan",
                "threshold": 90,
                "thresholdType": "Actual",
                "contactGroups": [action_group],
            }
        },
    )
    assert (
        proposal["put_body"]["properties"]["notifications"]["actual"]["contactGroups"]
        == [action_group]
    )


def test_management_group_rejects_action_group_only():
    action_group = (
        "/subscriptions/sub/resourceGroups/ops/providers/"
        "Microsoft.Insights/actionGroups/finops"
    )
    with pytest.raises(ValueError, match="do not support contactGroups"):
        build(
            scope="/providers/Microsoft.Management/managementGroups/mg",
            name="mg-action-group-only",
            amount=1000,
            time_grain="Monthly",
            time_period=PERIOD,
            as_of="2026-08-02",
            notifications={
                "actual": {
                    "enabled": True,
                    "operator": "GreaterThan",
                    "threshold": 90,
                    "thresholdType": "Actual",
                    "contactGroups": [action_group],
                }
            },
        )


def test_management_group_requires_email_when_no_contact_groups():
    with pytest.raises(ValueError, match="management-group.*email"):
        build(
            scope="/providers/Microsoft.Management/managementGroups/mg",
            name="mg-role-only",
            amount=1000,
            time_grain="Monthly",
            time_period=PERIOD,
            as_of="2026-08-02",
            notifications={
                "actual": {
                    "enabled": True,
                    "operator": "GreaterThan",
                    "threshold": 90,
                    "thresholdType": "Actual",
                    "contactRoles": ["Owner"],
                }
            },
        )


def test_management_group_rejects_contact_group_even_with_email():
    action_group = (
        "/subscriptions/sub/resourceGroups/ops/providers/"
        "Microsoft.Insights/actionGroups/finops"
    )
    with pytest.raises(ValueError, match="do not support contactGroups"):
        build(
            scope="/providers/Microsoft.Management/managementGroups/mg",
            name="mg-email-and-action-group",
            amount=1000,
            time_grain="Monthly",
            time_period=PERIOD,
            as_of="2026-08-02",
            notifications={
                "actual": {
                    "enabled": True,
                    "operator": "GreaterThan",
                    "threshold": 90,
                    "thresholdType": "Actual",
                    "contactEmails": ["mg-owner@example.org"],
                    "contactGroups": [action_group],
                }
            },
        )


def test_management_group_accepts_real_email_contact():
    proposal = build(
        scope="/providers/Microsoft.Management/managementGroups/mg",
        name="mg-email",
        amount=1000,
        time_grain="Monthly",
        time_period=PERIOD,
        as_of="2026-08-02",
        contacts=["mg-owner@example.org"],
    )
    assert proposal["application_script"]


@pytest.mark.parametrize(
    "placeholder",
    ["<your-email@example.com>", "your-email@example.com", "placeholder@example.org"],
)
def test_placeholder_email_is_rejected_without_executable_payload(placeholder):
    with pytest.raises(ValueError, match="placeholder"):
        build(
            scope="/subscriptions/sub",
            name="placeholder",
            amount=1000,
            time_grain="Monthly",
            time_period=PERIOD,
            contacts=[placeholder],
        )


def test_exact_budget_id_must_match_scope_and_name():
    with pytest.raises(ValueError, match="does not match"):
        build(
            scope="/subscriptions/other",
            name="ops-budget",
            exact_budget=_exact_budget(),
            amount=1500,
        )


def test_command_is_shell_escaped_and_body_is_exact_json():
    name = "ops' $(echo bad); budget"
    proposal = build(
        scope="/subscriptions/sub",
        name=name,
        amount=1000,
        time_grain="Monthly",
        time_period=PERIOD,
        contacts=CONTACTS,
    )
    args = shlex.split(proposal["command"])
    assert args[:4] == ["az", "rest", "--method", "put"]
    assert args[args.index("--url") + 1] == proposal["put_url"]
    assert json.loads(args[args.index("--body") + 1]) == proposal["put_body"]
    headers_index = args.index("--headers")
    assert args[headers_index + 1:headers_index + 3] == [
        "Content-Type=application/json",
        "If-None-Match=*",
    ]
    assert "%27" in proposal["put_url"]
    assert "$(" not in proposal["put_url"]


def test_update_command_and_script_use_captured_etag():
    proposal = build(
        scope="/subscriptions/sub",
        name="ops-budget",
        exact_budget=_exact_budget(),
        amount=1500,
    )
    assert proposal["etag"] == '"etag-1"'
    assert proposal["conditional_header"] == 'If-Match="etag-1"'
    assert proposal["put_body"]["eTag"] == '"etag-1"'
    args = shlex.split(proposal["command"])
    headers_index = args.index("--headers")
    assert args[headers_index + 1:headers_index + 3] == [
        "Content-Type=application/json",
        'If-Match="etag-1"',
    ]
    assert json.loads(args[args.index("--body") + 1])["eTag"] == '"etag-1"'
    expected_header = 'If-Match="etag-1"'
    assert (
        f"readonly CONDITIONAL_HEADER={shlex.quote(expected_header)}"
        in proposal["application_script"]
    )
    assert '"$CONDITIONAL_HEADER"' in proposal["application_script"]


def test_update_without_top_level_etag_emits_no_executable_plan():
    existing = _exact_budget()
    existing.pop("eTag")
    with pytest.raises(ValueError, match="top-level eTag"):
        build(
            scope="/subscriptions/sub",
            name="ops-budget",
            exact_budget=existing,
            amount=1500,
        )


def _run_application_script(
    proposal,
    *,
    preflight=None,
    readback=None,
    confirmation="",
    preflight_missing=False,
):
    preflight_json = json.dumps(
        preflight if preflight is not None else {},
        sort_keys=True,
        separators=(",", ":"),
    )
    readback_json = json.dumps(readback, sort_keys=True, separators=(",", ":"))
    harness = f"""
STATE_FILE=".test-budget-script-state-$$"
trap 'rm -f "$STATE_FILE"' EXIT
az() {{
  local method=""
  while [[ "$#" -gt 0 ]]; do
    case "$1" in
      --method) method="$2"; shift 2 ;;
      *) shift ;;
    esac
  done
  if [[ "$method" == "put" ]]; then
    : > "$STATE_FILE"
    printf '%s' '{{"write":"ok"}}'
  elif [[ -f "$STATE_FILE" ]]; then
    printf '%s' {shlex.quote(readback_json)}
  elif [[ {str(preflight_missing).lower()} == true ]]; then
    printf '%s' '{{"error":{{"code":"NotFound","message":"404 not found"}}}}' >&2
    return 1
  else
    printf '%s' {shlex.quote(preflight_json)}
  fi
}}
"""
    return subprocess.run(
        ["bash", "-c", harness + proposal["application_script"]],
        input=confirmation + "\n",
        text=True,
        capture_output=True,
        check=False,
    )


def test_application_script_is_governed_shell_safe_and_exact():
    name = "ops' $(echo bad); budget"
    proposal = build(
        scope="/subscriptions/sub",
        name=name,
        amount=1000,
        time_grain="Monthly",
        time_period=PERIOD,
        contacts=CONTACTS,
    )
    script = proposal["application_script"]
    assert proposal["script"] == script
    assert script.startswith("#!/usr/bin/env bash\nset -euo pipefail\n")
    assert "The FinOps agent never executes this script." in script
    assert f"readonly TARGET_URL={shlex.quote(proposal['put_url'])}" in script
    expected = json.dumps(proposal["put_body"], sort_keys=True, separators=(",", ":"))
    assert f"readonly EXPECTED_BODY={shlex.quote(expected)}" in script
    expected_before = ""
    assert (
        f"readonly EXPECTED_BEFORE_PROPERTIES={shlex.quote(expected_before)}"
        in script
    )
    assert "IFS= read -r confirmation" in script
    assert '[[ "$confirmation" != "$CONFIRMATION_PHRASE" ]]' in script
    assert 'az rest --method put --url "$TARGET_URL"' in script
    assert 'az rest --method get --url "$TARGET_URL"' in script
    assert 'Budget {label} mismatch:' in script
    assert "$(" not in proposal["put_url"]
    syntax = subprocess.run(
        ["bash", "-n"], input=script, text=True, capture_output=True, check=False,
    )
    assert syntax.returncode == 0, syntax.stderr


def test_application_script_requires_exact_confirmation_before_put():
    proposal = build(
        scope="/subscriptions/sub",
        name="ops-budget",
        exact_budget=_exact_budget(),
        amount=1500,
    )
    result = _run_application_script(
        proposal,
        preflight=_exact_budget(),
        readback={"properties": proposal["put_body"]["properties"]},
        confirmation="yes",
    )
    assert result.returncode == 2
    assert "no write was attempted" in result.stderr
    assert "PUT result" not in result.stdout


def test_application_script_aborts_stale_update_before_confirmation():
    proposal = build(
        scope="/subscriptions/sub",
        name="ops-budget",
        exact_budget=_exact_budget(),
        amount=1500,
    )
    stale = _exact_budget(amount=1100)
    result = _run_application_script(
        proposal,
        preflight=stale,
        readback={"properties": proposal["put_body"]["properties"]},
        confirmation="APPLY AZURE BUDGET UPDATE: /subscriptions/sub :: ops-budget",
    )
    assert result.returncode == 5
    assert "preflight state mismatch" in result.stderr
    assert "changed after the proposal was built" in result.stderr
    assert "Type the exact confirmation" not in result.stdout
    assert "PUT result" not in result.stdout


def test_application_script_aborts_create_race_when_budget_now_exists():
    proposal = build(
        scope="/subscriptions/sub",
        name="new-budget",
        amount=1000,
        time_grain="Monthly",
        time_period=PERIOD,
        contacts=CONTACTS,
    )
    result = _run_application_script(
        proposal,
        preflight=_exact_budget(name="new-budget"),
        readback={"properties": proposal["put_body"]["properties"]},
        confirmation="APPLY AZURE BUDGET CREATE: /subscriptions/sub :: new-budget",
    )
    assert result.returncode == 3
    assert "budget now exists" in result.stderr
    assert "PUT result" not in result.stdout


def test_application_script_allows_create_after_confirmed_404():
    proposal = build(
        scope="/subscriptions/sub",
        name="new-budget",
        amount=1000,
        time_grain="Monthly",
        time_period=PERIOD,
        contacts=CONTACTS,
    )
    result = _run_application_script(
        proposal,
        preflight_missing=True,
        readback={"properties": proposal["put_body"]["properties"]},
        confirmation="APPLY AZURE BUDGET CREATE: /subscriptions/sub :: new-budget",
    )
    assert result.returncode == 0, result.stderr
    assert "Create preflight confirmed no current budget" in result.stderr
    assert "PUT result" in result.stdout


def test_application_script_put_and_readback_match_succeed():
    proposal = build(
        scope="/subscriptions/sub",
        name="ops-budget",
        exact_budget=_exact_budget(),
        amount=1500,
    )
    confirmation = "APPLY AZURE BUDGET UPDATE: /subscriptions/sub :: ops-budget"
    result = _run_application_script(
        proposal,
        preflight=_exact_budget(),
        readback={
            "properties": {
                **proposal["put_body"]["properties"],
                "currentSpend": {"amount": 700, "unit": "USD"},
            }
        },
        confirmation=confirmation,
    )
    assert result.returncode == 0, result.stderr
    assert 'PUT result:\n{"write":"ok"}' in result.stdout
    assert "Post-write GET result:" in result.stdout
    assert "read-back matches" in result.stdout


def test_application_script_readback_mismatch_exits_nonzero():
    proposal = build(
        scope="/subscriptions/sub",
        name="ops-budget",
        exact_budget=_exact_budget(),
        amount=1500,
    )
    confirmation = "APPLY AZURE BUDGET UPDATE: /subscriptions/sub :: ops-budget"
    readback = {"properties": dict(proposal["put_body"]["properties"])}
    readback["properties"]["amount"] = 1499
    result = _run_application_script(
        proposal,
        preflight=_exact_budget(),
        readback=readback,
        confirmation=confirmation,
    )
    assert result.returncode != 0
    assert "Budget read-back mismatch:" in result.stderr
    assert "properties.amount" in result.stderr


def test_application_script_rejects_unexpected_persisted_recipient():
    proposal = build(
        scope="/subscriptions/sub",
        name="ops-budget",
        exact_budget=_exact_budget(),
        amount=1500,
    )
    readback = json.loads(json.dumps({"properties": proposal["put_body"]["properties"]}))
    readback["properties"]["notifications"]["existing"]["contactEmails"].append(
        "unexpected@example.org"
    )
    result = _run_application_script(
        proposal,
        preflight=_exact_budget(),
        readback=readback,
        confirmation="APPLY AZURE BUDGET UPDATE: /subscriptions/sub :: ops-budget",
    )
    assert result.returncode != 0
    assert "Budget read-back mismatch:" in result.stderr
    assert "contactEmails" in result.stderr


def test_application_script_accepts_server_default_end_date_only():
    proposal = build(
        scope="/subscriptions/sub",
        name="open-ended",
        amount=1000,
        time_grain="Monthly",
        time_period={"startDate": "2026-08-01T00:00:00Z"},
        as_of="2026-08-02",
        contacts=CONTACTS,
    )
    readback = {"properties": json.loads(json.dumps(proposal["put_body"]["properties"]))}
    readback["properties"]["timePeriod"]["endDate"] = "2036-08-01T00:00:00Z"
    result = _run_application_script(
        proposal,
        preflight_missing=True,
        readback=readback,
        confirmation="APPLY AZURE BUDGET CREATE: /subscriptions/sub :: open-ended",
    )
    assert result.returncode == 0, result.stderr


def test_readback_comparison_ignores_computed_fields_and_reports_mismatch():
    proposal = build(
        scope="/subscriptions/sub",
        name="ops-budget",
        exact_budget=_exact_budget(),
        amount=1500,
    )
    readback = {
        "properties": {
            **proposal["put_body"]["properties"],
            "currentSpend": {"amount": 700, "unit": "USD"},
        }
    }
    assert compare(proposal, readback) == {"match": True, "differences": []}
    readback["properties"]["amount"] = 1499
    mismatch = compare(proposal, readback)
    assert mismatch["match"] is False
    assert mismatch["differences"][0]["path"] == "properties.amount"


@pytest.mark.parametrize(
    ("path", "mutate"),
    [
        (
            "properties.filter",
            lambda properties: properties.update({
                "filter": {
                    "dimensions": {
                        "name": "ResourceGroupName",
                        "operator": "In",
                        "values": ["unexpected"],
                    }
                }
            }),
        ),
        (
            "properties.notifications.unexpected",
            lambda properties: properties["notifications"].update({
                "unexpected": {
                    "enabled": True,
                    "operator": "GreaterThan",
                    "threshold": 99,
                    "thresholdType": "Actual",
                    "contactEmails": ["other@example.org"],
                }
            }),
        ),
    ],
)
def test_readback_rejects_unexpected_controlled_settings(path, mutate):
    proposal = build(
        scope="/subscriptions/sub",
        name="new-budget",
        amount=1000,
        time_grain="Monthly",
        time_period=PERIOD,
        contacts=CONTACTS,
    )
    properties = json.loads(json.dumps(proposal["put_body"]["properties"]))
    mutate(properties)
    result = compare(proposal, {"properties": properties})
    assert result["match"] is False
    assert any(difference["path"] == path for difference in result["differences"])


def test_readback_rejects_unexpected_notification_recipient():
    proposal = build(
        scope="/subscriptions/sub",
        name="new-budget",
        amount=1000,
        time_grain="Monthly",
        time_period=PERIOD,
        contacts=CONTACTS,
    )
    properties = json.loads(json.dumps(proposal["put_body"]["properties"]))
    properties["notifications"]["actual_80"]["contactEmails"].append(
        "unexpected@example.org"
    )
    result = compare(proposal, {"properties": properties})
    assert result["match"] is False
    assert result["differences"][0]["path"].endswith("contactEmails")


def test_readback_numeric_comparison_rejects_half_dollar_at_billion_scale():
    proposal = build(
        scope="/subscriptions/sub",
        name="large-budget",
        amount=1_000_000_000,
        time_grain="Monthly",
        time_period=PERIOD,
        contacts=CONTACTS,
    )
    readback = {"properties": json.loads(json.dumps(proposal["put_body"]["properties"]))}
    readback["properties"]["amount"] = 1_000_000_000.50
    result = compare(proposal, readback)
    assert result["match"] is False
    assert result["differences"][0]["path"] == "properties.amount"


def test_readback_ignores_only_server_default_end_date_when_omitted():
    proposal = build(
        scope="/subscriptions/sub",
        name="open-ended",
        amount=1000,
        time_grain="Monthly",
        time_period={"startDate": "2026-08-01T00:00:00Z"},
        as_of="2026-08-02",
        contacts=CONTACTS,
    )
    readback = {"properties": json.loads(json.dumps(proposal["put_body"]["properties"]))}
    readback["properties"]["timePeriod"]["endDate"] = "2036-08-01T00:00:00Z"
    assert compare(proposal, readback) == {"match": True, "differences": []}
    readback["properties"]["timePeriod"]["unexpected"] = "value"
    assert compare(proposal, readback)["match"] is False


def test_derivation_rejects_missing_required_history():
    with pytest.raises(ValueError, match="4 prior"):
        derive(
            "Quarterly",
            {
                "current_period_total": 100,
                "prior_complete_period_totals": [90, 95, 100],
            },
            as_of="2026-08-15",
        )


def test_invalid_scope_and_name_are_rejected():
    with pytest.raises(ValueError, match="scope"):
        build(
            scope="/tenants/t",
            name="budget",
            amount=1000,
            time_grain="Monthly",
            time_period=PERIOD,
            contacts=CONTACTS,
        )
    with pytest.raises(ValueError, match="unsafe"):
        build(
            scope="/subscriptions/sub",
            name="bad/name",
            amount=1000,
            time_grain="Monthly",
            time_period=PERIOD,
            contacts=CONTACTS,
        )
