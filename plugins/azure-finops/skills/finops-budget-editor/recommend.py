"""Pure, deterministic Azure budget recommendations and governed proposals.

The module has no Azure dependency.  Callers supply the exact budget GET result and,
when an amount is derived, bounded UsageDetails ActualCost period aggregates.

This intentionally remains one dependency-free file: the skill's documented sandbox
flow loads ``recommend.py`` directly by path. Internal helpers keep the recommendation,
proposal, script-rendering, and read-back boundaries explicit without requiring sibling
modules that may not be copied into that sandbox.
"""

import copy
import json
import math
import re
import shlex
from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import quote, unquote


DEFAULT_BUFFER_PCT = 15.0
DEFAULT_HEADROOM_PCT = DEFAULT_BUFFER_PCT
_RAISE_MARGIN = 1.05
_TIGHTEN_MARGIN = 0.85
_API_VERSION = "2023-05-01"
_MANAGEMENT_ENDPOINT = "https://management.azure.com"
_UNSET = object()

_GRAIN_DAYS = {
    "monthly": 30,
    "billingmonth": 30,
    "quarterly": 91,
    "billingquarter": 91,
    "annually": 365,
    "billingannual": 365,
}
_SUPPORTED_GRAINS = {"monthly": "Monthly", "quarterly": "Quarterly", "annually": "Annually"}
_PRIOR_PERIOD_COUNTS = {"Monthly": 3, "Quarterly": 4, "Annually": 1}
_EMAIL_RE = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$")
_AZURE_COMPUTED_BUDGET_PROPERTIES = {"currentSpend", "forecastSpend"}
_SCOPE_RE = re.compile(
    r"^/subscriptions/([^/]+)(?:/resourceGroups/([^/]+))?$", re.IGNORECASE
)
_MG_SCOPE_RE = re.compile(
    r"^/providers/Microsoft\.Management/managementGroups/([^/]+)$", re.IGNORECASE
)
_READBACK_VERIFIER = r'''import json
import sys
from decimal import Decimal

expected = json.loads(sys.argv[1], parse_float=Decimal, parse_int=Decimal)
label = sys.argv[2]
actual_doc = json.load(sys.stdin, parse_float=Decimal, parse_int=Decimal)
actual = actual_doc.get("properties") if isinstance(actual_doc, dict) else None
if isinstance(actual, dict):
    actual = {
        key: value for key, value in actual.items()
        if key not in {"currentSpend", "forecastSpend"}
    }
    expected_time_period = expected.get("timePeriod")
    actual_time_period = actual.get("timePeriod")
    if (
        isinstance(expected_time_period, dict)
        and "endDate" not in expected_time_period
        and isinstance(actual_time_period, dict)
    ):
        actual_time_period.pop("endDate", None)
differences = []

def compare(want, got, path):
    if isinstance(want, dict):
        if not isinstance(got, dict):
            differences.append({"path": path, "expected": want, "actual": got})
            return
        for key in sorted(set(want) | set(got)):
            if key not in want:
                differences.append({
                    "path": f"{path}.{key}", "expected": None, "actual": got[key],
                    "kind": "unexpected",
                })
            elif key not in got:
                differences.append({
                    "path": f"{path}.{key}", "expected": want[key], "actual": None,
                    "kind": "missing",
                })
            else:
                compare(want[key], got[key], f"{path}.{key}")
    elif isinstance(want, list):
        if not isinstance(got, list):
            differences.append({"path": path, "expected": want, "actual": got})
            return
        if len(want) != len(got):
            differences.append({"path": path, "expected": want, "actual": got})
            return
        for index, value in enumerate(want):
            compare(value, got[index], f"{path}[{index}]")
    elif isinstance(want, Decimal):
        if not isinstance(got, Decimal) or want != got:
            differences.append({"path": path, "expected": want, "actual": got})
    elif want != got:
        differences.append({"path": path, "expected": want, "actual": got})

compare(expected, actual, "properties")
if differences:
    print(f"Budget {label} mismatch:", file=sys.stderr)
    print(json.dumps(differences, indent=2, sort_keys=True, default=str), file=sys.stderr)
    raise SystemExit(1)
print(f"Budget {label} matches expected persisted fields.")
'''


def _num(value):
    """Return value as float, or None if it is not a finite real number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _spend_amount(block):
    if not isinstance(block, dict):
        return None, None
    return _num(block.get("amount")), block.get("unit")


def _parse_date(text):
    if isinstance(text, datetime):
        return text.date()
    if isinstance(text, date):
        return text
    if not text:
        return None
    try:
        return date.fromisoformat(str(text)[:10])
    except ValueError:
        return None


def _as_of_date(value):
    if value is None:
        return date.today()
    parsed = _parse_date(value)
    if parsed is None:
        raise ValueError("as_of must be an ISO date")
    return parsed


def _canonical_timestamp(value, field):
    """Validate an ISO timestamp and return Azure's canonical UTC timestamp."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    elif isinstance(value, str):
        text = value.strip()
        try:
            if len(text) == 10:
                dt = datetime.fromisoformat(text).replace(tzinfo=timezone.utc)
            else:
                dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{field} must be an ISO date or timestamp") from exc
    else:
        raise ValueError(f"{field} must be an ISO date or timestamp")

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    if dt.microsecond:
        return dt.isoformat(timespec="microseconds").replace("+00:00", "Z")
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def _first_day_months_after(value, months):
    month_index = value.year * 12 + value.month - 1 + months
    return date(month_index // 12, month_index % 12 + 1, 1)


def _validate_time_period(
    time_period,
    *,
    time_grain,
    require_start,
    as_of=None,
    enforce_current_period=False,
    preserve=False,
):
    if not isinstance(time_period, dict):
        raise ValueError("time_period must be an object")
    unknown = set(time_period) - {"startDate", "endDate"}
    if unknown:
        raise ValueError(f"time_period contains unsupported fields: {sorted(unknown)}")
    start_value = time_period.get("startDate")
    if require_start and not start_value:
        raise ValueError("time_period.startDate is required")
    start = _canonical_timestamp(start_value, "startDate") if start_value else None
    if start:
        start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        if start_dt.day != 1:
            raise ValueError("startDate must be the first day of a month")
        if any((start_dt.hour, start_dt.minute, start_dt.second, start_dt.microsecond)):
            raise ValueError("startDate must be at 00:00:00 UTC")
        start_day = start_dt.date()
        if start_day < date(2017, 6, 1):
            raise ValueError("startDate must be on or after 2017-06-01")
        maximum_start = _first_day_months_after(
            (_as_of_date(as_of).replace(day=1)), 12
        )
        if start_day > maximum_start:
            raise ValueError(
                f"startDate must be on or before {maximum_start.isoformat()}"
            )
        if enforce_current_period:
            current_period_start, _ = usage_period_bounds(time_grain, as_of)
            if start_day < current_period_start:
                raise ValueError(
                    f"startDate must be on or after the current {time_grain.lower()} "
                    f"period start {current_period_start.isoformat()}"
                )
    end_value = time_period.get("endDate")
    end = _canonical_timestamp(end_value, "endDate") if end_value else None
    if (
        start and end
        and datetime.fromisoformat(end.replace("Z", "+00:00"))
        <= datetime.fromisoformat(start.replace("Z", "+00:00"))
    ):
        raise ValueError("endDate must be after startDate")
    if preserve:
        return copy.deepcopy(time_period)
    result = {}
    if start:
        result["startDate"] = start
    if end:
        result["endDate"] = end
    return result


def _normalise_grain(value):
    grain = _SUPPORTED_GRAINS.get(str(value or "").strip().lower())
    if not grain:
        raise ValueError("time_grain must be Monthly, Quarterly, or Annually")
    return grain


def _validate_segment(value, field):
    text = str(value or "")
    if not text or text != text.strip():
        raise ValueError(f"{field} must be non-empty and have no surrounding whitespace")
    if any(ord(ch) < 32 for ch in text) or any(ch in text for ch in "/\\?#"):
        raise ValueError(f"{field} contains unsafe path characters")
    return text


def canonical_scope(scope):
    """Validate one supported budget scope and preserve its canonical ARM spelling."""
    scope = str(scope or "")
    match = _SCOPE_RE.fullmatch(scope)
    if match:
        subscription_id = _validate_segment(match.group(1), "subscriptionId")
        if match.group(2) is None:
            return f"/subscriptions/{subscription_id}"
        resource_group = _validate_segment(match.group(2), "resourceGroupName")
        return f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
    match = _MG_SCOPE_RE.fullmatch(scope)
    if match:
        management_group = _validate_segment(match.group(1), "managementGroupId")
        return f"/providers/Microsoft.Management/managementGroups/{management_group}"
    raise ValueError(
        "scope must be a subscription, resource group, or management group canonical ARM scope"
    )


def _validate_name(name):
    name = str(name or "")
    if not 1 <= len(name) <= 100 or name != name.strip():
        raise ValueError("budget name must be 1-100 characters with no surrounding whitespace")
    if any(ord(ch) < 32 for ch in name) or any(ch in name for ch in "/\\?#"):
        raise ValueError("budget name contains unsafe path characters")
    return name


def _encoded_scope(scope):
    parts = scope.split("/")
    return "/".join(quote(part, safe="-._~") for part in parts)


def budget_resource_id(scope, name):
    scope = canonical_scope(scope)
    name = _validate_name(name)
    return f"{_encoded_scope(scope)}/providers/Microsoft.Consumption/budgets/{quote(name, safe='-._~')}"


def budget_url(scope, name):
    return f"{_MANAGEMENT_ENDPOINT}{budget_resource_id(scope, name)}?api-version={_API_VERSION}"


def usage_period_bounds(time_grain, as_of=None):
    """Return the calendar period used for deterministic UsageDetails run-rate math."""
    grain = _normalise_grain(time_grain)
    today = _as_of_date(as_of)
    if grain == "Monthly":
        start = date(today.year, today.month, 1)
        if today.month == 12:
            end = date(today.year + 1, 1, 1)
        else:
            end = date(today.year, today.month + 1, 1)
    elif grain == "Quarterly":
        first_month = ((today.month - 1) // 3) * 3 + 1
        start = date(today.year, first_month, 1)
        if first_month == 10:
            end = date(today.year + 1, 1, 1)
        else:
            end = date(today.year, first_month + 3, 1)
    else:
        start = date(today.year, 1, 1)
        end = date(today.year + 1, 1, 1)
    return start, end


def _round_up_nice(amount):
    """Round an amount up to the existing clean 10/50/100/1000 steps."""
    if amount <= 0:
        return 0.0
    if amount < 100:
        step = 10
    elif amount < 1000:
        step = 50
    elif amount < 10000:
        step = 100
    else:
        step = 1000
    return float(step * math.ceil(amount / step))


def derive_budget_amount(time_grain, period_totals, *, as_of=None,
                         headroom_pct=DEFAULT_HEADROOM_PCT):
    """Derive an amount from pre-aggregated UsageDetails ActualCost totals.

    ``period_totals`` is intentionally Azure-independent:

    ``{"current_period_total": 400, "prior_complete_period_totals": [300, 350, 375]}``
    """
    grain = _normalise_grain(time_grain)
    if not isinstance(period_totals, dict):
        raise ValueError("period_totals must be a mapping of UsageDetails aggregates")
    if _period_totals_incomplete(period_totals):
        raise ValueError(
            "UsageDetails aggregates are partial/incomplete; an explicit amount is required"
        )
    current = _num(period_totals.get("current_period_total"))
    prior = period_totals.get("prior_complete_period_totals")
    headroom = _num(headroom_pct)
    if current is None or current < 0:
        raise ValueError("current_period_total must be a non-negative number")
    if headroom is None or headroom < 0:
        raise ValueError("headroom_pct must be a non-negative number")
    if not isinstance(prior, (list, tuple)):
        raise ValueError("prior_complete_period_totals must be a list")
    required = _PRIOR_PERIOD_COUNTS[grain]
    if len(prior) < required:
        raise ValueError(f"{grain} derivation requires {required} prior complete period total(s)")
    prior_values = []
    for value in prior[-required:]:
        number = _num(value)
        if number is None or number < 0:
            raise ValueError("prior complete period totals must be non-negative numbers")
        prior_values.append(number)

    today = _as_of_date(as_of)
    start, end = usage_period_bounds(grain, today)
    if not start <= today < end:
        raise ValueError("as_of must fall inside the derived current period")
    elapsed_days = (today - start).days + 1
    total_days = (end - start).days
    run_rate = current * total_days / elapsed_days
    prior_average = sum(prior_values) / required
    basis = max(run_rate, prior_average)
    raw_amount = basis * (1 + headroom / 100)
    amount = _round_up_nice(raw_amount)
    warnings = list(period_totals.get("warnings") or [])
    if len(prior) > required:
        warnings.append(f"only the most recent {required} prior complete totals were used")
    evidence = {
        "method": "usageDetailsActualCost",
        "time_grain": grain,
        "as_of": today.isoformat(),
        "current_period": {
            "startDate": start.isoformat(),
            "endDateExclusive": end.isoformat(),
            "actual_cost": round(current, 2),
            "elapsed_days": elapsed_days,
            "total_days": total_days,
            "run_rate": round(run_rate, 2),
        },
        "prior_complete_period_totals": [round(value, 2) for value in prior_values],
        "prior_average": round(prior_average, 2),
        "basis": round(basis, 2),
        "headroom_pct": headroom,
        "pre_round_amount": round(raw_amount, 2),
        "rounded_amount": amount,
    }
    return {"amount": amount, "evidence": evidence, "warnings": warnings}


def _period_totals_incomplete(period_totals):
    return bool(
        isinstance(period_totals, dict)
        and (
            period_totals.get("partial")
            or period_totals.get("incomplete")
            or period_totals.get("complete") is False
        )
    )


def _is_placeholder(value):
    lowered = str(value or "").strip().lower()
    return (
        "<" in lowered
        or ">" in lowered
        or "your-email@" in lowered
        or "placeholder@" in lowered
        or lowered in {"email@example.com", "user@example.com"}
    )


def _normalise_contact_spec(contacts):
    if contacts is _UNSET:
        return _UNSET
    if isinstance(contacts, (list, tuple)):
        return {"contactEmails": list(contacts)}
    if isinstance(contacts, dict):
        return copy.deepcopy(contacts)
    raise ValueError("contacts must be an email list or a contact mapping")


def _default_notifications(contacts):
    contact_spec = _normalise_contact_spec(contacts)
    if contact_spec is _UNSET:
        contact_spec = {}
    base = {
        "enabled": True,
        "operator": "GreaterThan",
    }
    return {
        "actual_80": {
            **base,
            "threshold": 80,
            "thresholdType": "Actual",
            **copy.deepcopy(contact_spec),
        },
        "forecasted_100": {
            **base,
            "threshold": 100,
            "thresholdType": "Forecasted",
            **copy.deepcopy(contact_spec),
        },
    }


def _validate_notifications(
    notifications, *, require_contact, require_email=False, preserve=False
):
    if not isinstance(notifications, dict) or not notifications:
        if require_contact:
            raise ValueError("at least one usable budget notification contact is required")
        return {}

    if len(notifications) > 5:
        raise ValueError("Azure budgets support at most 5 notifications")

    usable_human_contact = False
    usable_email_contact = False
    validated = {}
    for key, notification in notifications.items():
        if not isinstance(key, str) or not key or len(key) > 260:
            raise ValueError("notification names must be non-empty strings")
        if not isinstance(notification, dict):
            raise ValueError(f"notification {key!r} must be an object")
        item = copy.deepcopy(notification)
        if "enabled" in item and not isinstance(item["enabled"], bool):
            raise ValueError(f"notification {key!r} enabled must be a boolean")
        item["enabled"] = item.get("enabled", True)
        threshold = _num(item.get("threshold"))
        if threshold is None or threshold < 0 or threshold > 1000:
            raise ValueError(f"notification {key!r} threshold must be between 0 and 1000")
        threshold_type = str(item.get("thresholdType") or "")
        if threshold_type.lower() not in {"actual", "forecasted"}:
            raise ValueError(f"notification {key!r} thresholdType must be Actual or Forecasted")
        item["thresholdType"] = (
            "Actual" if threshold_type.lower() == "actual" else "Forecasted"
        )
        operator = str(item.get("operator") or "GreaterThan")
        operators = {
            "equalto": "EqualTo",
            "greaterthan": "GreaterThan",
            "greaterthanorequalto": "GreaterThanOrEqualTo",
        }
        if operator.lower() not in operators:
            raise ValueError(f"notification {key!r} has an unsupported operator")
        item["operator"] = operators[operator.lower()]

        human_contact_found = False
        emails = item.get("contactEmails") or []
        if not isinstance(emails, list):
            raise ValueError(f"notification {key!r} contactEmails must be a list")
        normalised_emails = []
        for email in emails:
            if _is_placeholder(email):
                raise ValueError("placeholder email addresses are forbidden")
            if not isinstance(email, str) or not _EMAIL_RE.fullmatch(email.strip()):
                raise ValueError(f"notification {key!r} contains an invalid email address")
            normalised_emails.append(email.strip())
            human_contact_found = True
        if "contactEmails" in item:
            item["contactEmails"] = normalised_emails
        for field in ("contactGroups", "contactRoles"):
            values = item.get(field) or []
            if not isinstance(values, list):
                raise ValueError(f"notification {key!r} {field} must be a list")
            if require_email and field == "contactGroups" and values:
                raise ValueError(
                    "management-group budget notifications do not support contactGroups"
                )
            normalised_values = []
            for value in values:
                if not isinstance(value, str) or not value.strip() or _is_placeholder(value):
                    raise ValueError(f"notification {key!r} contains an invalid {field} value")
                cleaned = value.strip()
                if field == "contactGroups":
                    if not re.fullmatch(
                        r"/subscriptions/[^/]+/resourceGroups/[^/]+/providers/"
                        r"Microsoft\.Insights/actionGroups/[^/]+",
                        cleaned,
                        re.IGNORECASE,
                    ):
                        raise ValueError(
                            f"notification {key!r} contactGroups must contain Azure action group IDs"
                        )
                    human_contact_found = True
                elif cleaned.lower() not in {"owner", "contributor", "reader"}:
                    raise ValueError(
                        f"notification {key!r} contactRoles must be Owner, Contributor, or Reader"
                    )
                normalised_values.append(cleaned)
            if field in item:
                item[field] = normalised_values
        if item["enabled"] and human_contact_found:
            usable_human_contact = True
            if normalised_emails:
                usable_email_contact = True
        validated[key] = item
    if require_email and not usable_email_contact:
        raise ValueError(
            "management-group budgets require at least one enabled notification "
            "with a real email contact"
        )
    if require_contact and not usable_human_contact:
        raise ValueError(
            "at least one enabled notification with a real email or action group is required"
        )
    return copy.deepcopy(notifications) if preserve else validated


def _conditional_header(operation, etag=None):
    if operation == "create":
        return "If-None-Match=*"
    if not isinstance(etag, str) or not etag.strip():
        raise ValueError("update exact budget GET must include a non-empty top-level eTag")
    etag = etag.strip()
    if any(ord(character) < 32 for character in etag):
        raise ValueError("eTag contains unsafe control characters")
    return f"If-Match={etag}"


def _shell_command(url, body, operation, etag=None):
    compact_body = json.dumps(body, sort_keys=True, separators=(",", ":"))
    conditional_header = _conditional_header(operation, etag)
    return (
        "az rest --method put"
        f" --url {shlex.quote(url)}"
        " --headers 'Content-Type=application/json'"
        f" {shlex.quote(conditional_header)}"
        f" --body {shlex.quote(compact_body)}"
        " -o json"
    )


def _application_script(
    operation, scope, name, amount, url, body, expected_before, etag=None
):
    """Render a self-contained, human-run budget application script."""
    expected_body = json.dumps(body, sort_keys=True, separators=(",", ":"))
    confirmation = f"APPLY AZURE BUDGET {operation.upper()}: {scope} :: {name}"
    conditional_header = _conditional_header(operation, etag)
    expected_properties = json.dumps(
        body["properties"], sort_keys=True, separators=(",", ":")
    )
    expected_before_json = (
        json.dumps(expected_before, sort_keys=True, separators=(",", ":"))
        if expected_before is not None else ""
    )
    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "",
        "# Generated by finops-budget-editor. Review, save, and run manually.",
        "# The FinOps agent never executes this script.",
        f"readonly OPERATION={shlex.quote(operation)}",
        f"readonly SCOPE={shlex.quote(scope)}",
        f"readonly BUDGET_NAME={shlex.quote(name)}",
        f"readonly AMOUNT={shlex.quote(str(amount))}",
        f"readonly TARGET_URL={shlex.quote(url)}",
        f"readonly EXPECTED_BODY={shlex.quote(expected_body)}",
        f"readonly EXPECTED_PROPERTIES={shlex.quote(expected_properties)}",
        f"readonly EXPECTED_BEFORE_PROPERTIES={shlex.quote(expected_before_json)}",
        f"readonly CONDITIONAL_HEADER={shlex.quote(conditional_header)}",
        f"readonly CONFIRMATION_PHRASE={shlex.quote(confirmation)}",
        "",
        'command -v az >/dev/null 2>&1 || { echo "az is required." >&2; exit 127; }',
        'command -v python3 >/dev/null 2>&1 || { echo "python3 is required." >&2; exit 127; }',
        "",
        'if [[ "$#" -ne 0 ]]; then',
        '  echo "Usage: $0" >&2',
        "  exit 64",
        "fi",
        "",
        'printf "Operation: %s\\nScope: %s\\nBudget: %s\\nAmount: %s\\nTarget: %s\\n" \\',
        '  "$OPERATION" "$SCOPE" "$BUDGET_NAME" "$AMOUNT" "$TARGET_URL"',
        "",
        'echo "Reading exact current budget state..."',
        'if CURRENT_STATE="$(az rest --method get --url "$TARGET_URL" -o json 2>&1)"; then',
        '  printf "%s\\n" "$CURRENT_STATE"',
        '  if [[ "$OPERATION" == "create" ]]; then',
        '    echo "Refusing create: a budget now exists at the exact target URL." >&2',
        "    exit 3",
        "  fi",
        '  if ! printf "%s" "$CURRENT_STATE" | python3 -c '
        + shlex.quote(_READBACK_VERIFIER)
        + ' "$EXPECTED_BEFORE_PROPERTIES" "preflight state"; then',
        '    echo "Refusing update: the budget changed after the proposal was built." >&2',
        "      exit 5",
        "  fi",
        "else",
        '  if [[ "$OPERATION" == "update" ]]; then',
        '    printf "Update preflight GET failed:\\n%s\\n" "$CURRENT_STATE" >&2',
        "    exit 4",
        "  fi",
        '  case "$CURRENT_STATE" in',
        '    *404*|*NotFound*|*"not found"*|*"Not Found"*)',
        '      printf "Create preflight confirmed no current budget:\\n%s\\n" "$CURRENT_STATE" >&2',
        "      ;;",
        "    *)",
        '      printf "Create preflight could not confirm absence:\\n%s\\n" "$CURRENT_STATE" >&2',
        '      echo "Resolve the read failure before applying this plan." >&2',
        "      exit 4",
        "      ;;",
        "  esac",
        "fi",
        "",
        'printf "Type the exact confirmation phrase to continue:\\n%s\\n> " "$CONFIRMATION_PHRASE"',
        "IFS= read -r confirmation",
        'if [[ "$confirmation" != "$CONFIRMATION_PHRASE" ]]; then',
        '  echo "Confirmation did not match; no write was attempted." >&2',
        "  exit 2",
        "fi",
        "",
        'WRITE_RESULT="$(az rest --method put --url "$TARGET_URL" \\',
        '  --headers "Content-Type=application/json" "$CONDITIONAL_HEADER" \\',
        '  --body "$EXPECTED_BODY" -o json)"',
        'printf "PUT result:\\n%s\\n" "$WRITE_RESULT"',
        "",
        'READBACK_JSON="$(az rest --method get --url "$TARGET_URL" -o json)"',
        'printf "Post-write GET result:\\n%s\\n" "$READBACK_JSON"',
        "",
        'printf "%s" "$READBACK_JSON" | python3 -c '
        + shlex.quote(_READBACK_VERIFIER)
        + ' "$EXPECTED_PROPERTIES" "read-back"',
    ]
    return "\n".join(lines) + "\n"


def _existing_matches(scope, name, budget):
    budget_id = str(budget.get("id") or "")
    if not budget_id:
        return
    expected = unquote(budget_resource_id(scope, name))
    actual = unquote(budget_id.split("?", 1)[0])
    if actual.lower() != expected.lower():
        raise ValueError("exact_budget id does not match the requested scope and name")


def _persisted_properties(budget_or_properties):
    if not isinstance(budget_or_properties, dict):
        return None
    properties = budget_or_properties.get("properties", budget_or_properties)
    if not isinstance(properties, dict):
        return None
    return {
        key: copy.deepcopy(value)
        for key, value in properties.items()
        if key not in _AZURE_COMPUTED_BUDGET_PROPERTIES
    }


def build_budget_proposal(
    *,
    scope,
    name,
    exact_budget=None,
    amount=_UNSET,
    period_totals=None,
    as_of=None,
    headroom_pct=DEFAULT_HEADROOM_PCT,
    category=_UNSET,
    time_grain=_UNSET,
    time_period=_UNSET,
    filters=_UNSET,
    notifications=_UNSET,
    contacts=_UNSET,
):
    """Build a deterministic create/update proposal without performing an Azure call."""
    scope = canonical_scope(scope)
    name = _validate_name(name)
    operation = "update" if exact_budget is not None else "create"
    if exact_budget is not None and not isinstance(exact_budget, dict):
        raise ValueError("exact_budget must be the exact budget GET object or None for create")
    existing = copy.deepcopy(exact_budget) if exact_budget is not None else None
    if existing is not None:
        _existing_matches(scope, name, existing)
    etag = existing.get("eTag") if existing is not None else None
    conditional_header = _conditional_header(operation, etag)
    props = copy.deepcopy((existing or {}).get("properties") or {})

    chosen_category = props.get("category", "Cost") if category is _UNSET else category
    if str(chosen_category).lower() != "cost":
        raise ValueError("category must be Cost for API version 2023-05-01")
    chosen_category = "Cost"

    grain_value = props.get("timeGrain", "Monthly") if time_grain is _UNSET else time_grain
    chosen_grain = _normalise_grain(grain_value)

    if time_period is _UNSET:
        chosen_period = props.get("timePeriod")
    else:
        chosen_period = time_period
    if chosen_period is None and operation == "create":
        raise ValueError("time_period.startDate is required for creates")
    if chosen_period is not None:
        chosen_period = _validate_time_period(
            chosen_period,
            time_grain=chosen_grain,
            require_start=(operation == "create" or time_period is not _UNSET),
            as_of=as_of,
            enforce_current_period=(
                operation == "create" or time_period is not _UNSET
            ),
            preserve=(operation == "update" and time_period is _UNSET),
        )

    if operation == "create" and filters is not _UNSET and filters not in (None, {}):
        raise ValueError("new budgets must be scope-wide; filters are not allowed")
    chosen_filter = props.get("filter") if filters is _UNSET else filters
    if chosen_filter is not None and not isinstance(chosen_filter, dict):
        raise ValueError("filters must be an object or None")

    if notifications is not _UNSET and contacts is not _UNSET:
        raise ValueError("provide either notifications or contacts, not both")
    if notifications is not _UNSET:
        chosen_notifications = notifications
    elif contacts is not _UNSET:
        chosen_notifications = _default_notifications(contacts)
    elif operation == "update":
        chosen_notifications = props.get("notifications")
    else:
        chosen_notifications = None
    chosen_notifications = _validate_notifications(
        chosen_notifications,
        require_contact=True,
        require_email=scope.lower().startswith(
            "/providers/microsoft.management/managementgroups/"
        ),
        preserve=(
            operation == "update"
            and notifications is _UNSET
            and contacts is _UNSET
        ),
    )

    derivation = None
    warnings = []
    aggregates_incomplete = _period_totals_incomplete(period_totals)
    if amount is not _UNSET and period_totals is not None and not aggregates_incomplete:
        raise ValueError("provide either an explicit amount or period_totals, not both")
    if amount is not _UNSET:
        chosen_amount = _num(amount)
        if chosen_amount is None or chosen_amount <= 0:
            raise ValueError("amount must be a positive finite number")
        derivation = {"method": "explicit", "amount": chosen_amount}
        if aggregates_incomplete:
            warnings.extend(list(period_totals.get("warnings") or []))
            warnings.append(
                "UsageDetails derivation evidence was partial/incomplete; "
                "the supplied explicit amount was used"
            )
    elif period_totals is not None:
        result = derive_budget_amount(
            chosen_grain,
            period_totals,
            as_of=as_of,
            headroom_pct=headroom_pct,
        )
        chosen_amount = result["amount"]
        derivation = result["evidence"]
        warnings.extend(result["warnings"])
    else:
        chosen_amount = _num(props.get("amount"))
        if chosen_amount is None or chosen_amount <= 0:
            raise ValueError("a positive explicit amount or period_totals is required")
        derivation = {"method": "preserved", "amount": chosen_amount}

    body_props = {
        "category": chosen_category,
        "amount": chosen_amount,
        "timeGrain": chosen_grain,
        "notifications": chosen_notifications,
    }
    if chosen_period is not None:
        body_props["timePeriod"] = chosen_period
    if chosen_filter:
        body_props["filter"] = copy.deepcopy(chosen_filter)
    body = {"properties": body_props}
    if operation == "update":
        body["eTag"] = etag
    url = budget_url(scope, name)
    expected_before_properties = _persisted_properties(existing) if existing is not None else None
    proposal = {
        "operation": operation,
        "scope": scope,
        "name": name,
        "before": existing,
        "after": copy.deepcopy(body),
        "derivation": derivation,
        "warnings": warnings,
        "put_url": url,
        "put_body": body,
        "command": _shell_command(url, body, operation, etag),
        "post_write_get_url": url,
        "expected_before_properties": expected_before_properties,
        "etag": etag,
        "conditional_header": conditional_header,
    }
    proposal["script"] = _application_script(
        operation, scope, name, chosen_amount, url, body, expected_before_properties, etag
    )
    proposal["application_script"] = proposal["script"]
    if existing is not None and compare_budget_readback(proposal, existing)["match"]:
        proposal["warnings"].append("proposal is a no-op relative to the supplied exact budget GET")
    return proposal


def _compare_expected(expected, actual, path, differences):
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            differences.append({"path": path, "expected": expected, "actual": actual})
            return
        for key in sorted(set(expected) | set(actual)):
            if key not in expected:
                differences.append({
                    "path": f"{path}.{key}",
                    "expected": None,
                    "actual": actual[key],
                    "kind": "unexpected",
                })
            elif key not in actual:
                differences.append({
                    "path": f"{path}.{key}",
                    "expected": expected[key],
                    "actual": None,
                    "kind": "missing",
                })
            else:
                _compare_expected(
                    expected[key], actual[key], f"{path}.{key}", differences
                )
    elif isinstance(expected, list):
        if not isinstance(actual, list) or len(expected) != len(actual):
            differences.append({"path": path, "expected": expected, "actual": actual})
            return
        for index, value in enumerate(expected):
            _compare_expected(value, actual[index], f"{path}[{index}]", differences)
    elif isinstance(expected, (int, float, Decimal)) and not isinstance(expected, bool):
        try:
            expected_number = Decimal(str(expected))
            actual_number = (
                Decimal(str(actual))
                if isinstance(actual, (int, float, Decimal)) and not isinstance(actual, bool)
                else None
            )
        except (InvalidOperation, ValueError):
            actual_number = None
        if actual_number is None or expected_number != actual_number:
            differences.append({"path": path, "expected": expected, "actual": actual})
    elif actual is _UNSET or expected != actual:
        differences.append({
            "path": path,
            "expected": expected,
            "actual": None if actual is _UNSET else actual,
        })


def compare_budget_readback(proposal_or_body, readback):
    """Compare only PUT-controlled fields, ignoring Azure-computed response fields."""
    if not isinstance(proposal_or_body, dict):
        raise ValueError("proposal_or_body must be a proposal or PUT body")
    expected = proposal_or_body.get("put_body", proposal_or_body)
    actual = _persisted_properties(readback)
    expected_props = expected.get("properties") if isinstance(expected, dict) else None
    if not isinstance(expected_props, dict) or not isinstance(actual, dict):
        return {
            "match": False,
            "differences": [{
                "path": "properties",
                "expected": expected_props,
                "actual": actual,
            }],
        }
    actual = copy.deepcopy(actual)
    expected_time_period = expected_props.get("timePeriod")
    actual_time_period = actual.get("timePeriod")
    if (
        isinstance(expected_time_period, dict)
        and "endDate" not in expected_time_period
        and isinstance(actual_time_period, dict)
    ):
        actual_time_period.pop("endDate", None)
    differences = []
    _compare_expected(expected_props, actual, "properties", differences)
    return {"match": not differences, "differences": differences}


# ---------------------------------------------------------------------------
# Backward-compatible recommendation API

def _grain_fraction_elapsed(time_grain, time_period, as_of):
    grain = str(time_grain or "monthly").strip().lower()
    days_in_window = _GRAIN_DAYS.get(grain, 30)
    start = _parse_date((time_period or {}).get("startDate"))
    end = _parse_date((time_period or {}).get("endDate"))
    if start and end and 0 < (end - start).days <= days_in_window + 2:
        days_in_window = (end - start).days
        elapsed = (as_of - start).days + 1
    elif grain in ("monthly", "billingmonth"):
        elapsed = min(as_of.day, days_in_window)
    else:
        elapsed = None
    if elapsed is None or days_in_window <= 0:
        return None
    elapsed = max(1, min(elapsed, days_in_window))
    return elapsed / days_in_window


def _forecast(props, current, as_of):
    azure_forecast, _ = _spend_amount(props.get("forecastSpend"))
    if azure_forecast is not None:
        return azure_forecast, "azure"
    frac = _grain_fraction_elapsed(props.get("timeGrain"), props.get("timePeriod"), as_of)
    if frac and current:
        return round(current / frac, 2), "run-rate"
    return None, "unavailable"


def _scope_of(budget, props):
    budget_id = str(budget.get("id") or "")
    marker = "/providers/Microsoft.Consumption"
    if marker.lower() in budget_id.lower():
        return budget_id[:budget_id.lower().index(marker.lower())]
    return props.get("category") or "subscription"


def _recommendation_payload(props, recommended, contacts):
    if str(props.get("category") or "Cost").lower() != "cost":
        return None, False
    existing = props.get("notifications")
    if isinstance(existing, dict) and existing:
        notifications = _validate_notifications(existing, require_contact=True, preserve=True)
        added = False
    elif contacts is not _UNSET:
        notifications = _validate_notifications(_default_notifications(contacts), require_contact=True)
        added = True
    else:
        return None, True
    body = {
        "properties": {
            "category": "Cost",
            "amount": recommended,
            "timeGrain": props.get("timeGrain") or "Monthly",
            "notifications": notifications,
        }
    }
    if isinstance(props.get("timePeriod"), dict) and props["timePeriod"].get("startDate"):
        body["properties"]["timePeriod"] = copy.deepcopy(props["timePeriod"])
    if isinstance(props.get("filter"), dict):
        body["properties"]["filter"] = copy.deepcopy(props["filter"])
    return body, added


def _classify(current_amount, recommended):
    if not current_amount:
        return "set"
    if recommended > current_amount * _RAISE_MARGIN:
        return "raise"
    if recommended < current_amount * _TIGHTEN_MARGIN:
        return "tighten"
    return "keep"


def _rationale(action, current_amount, forecast, forecast_source, recommended, buffer_pct, currency):
    basis = (
        f"forecast {forecast} {currency} ({forecast_source})"
        if forecast is not None else "current spend"
    )
    buffer_text = f"+{buffer_pct:g}% buffer"
    if action == "set":
        return f"no usable current amount; size to {basis} {buffer_text} -> {recommended} {currency}"
    if action == "raise":
        return (
            f"{basis} {buffer_text} exceeds current {current_amount} {currency}; "
            f"raise to {recommended} {currency}"
        )
    if action == "tighten":
        return (
            f"{basis} {buffer_text} is well under current {current_amount} {currency}; "
            f"tighten to {recommended} {currency}"
        )
    return f"current {current_amount} {currency} already covers {basis} {buffer_text}; keep"


def _recommend_budget(budget, *, today, buffer_pct, buffer_mult, contacts):
    props = budget.get("properties") or {}
    name = budget.get("name") or props.get("name") or "(unnamed)"
    current_amount = _num(props.get("amount"))
    current_spend, currency = _spend_amount(props.get("currentSpend"))
    current_spend = current_spend or 0.0
    currency = currency or "USD"
    forecast, forecast_source = _forecast(props, current_spend, today)
    basis = max(forecast or 0.0, current_spend, 0.0)
    rounded_current_amount = (
        round(current_amount, 2) if current_amount is not None else None
    )
    if basis <= 0:
        return {
            "name": name,
            "scope": _scope_of(budget, props),
            "action": "insufficient_data",
            "current_amount": rounded_current_amount,
            "currency": currency,
            "forecast_spend": forecast,
            "forecast_source": forecast_source,
            "recommended_amount": None,
            "notifications_added": False,
            "requires_contacts": False,
            "rationale": "no forecast or spend signal yet (currentSpend may be unsynced); "
            "cannot size a budget — get a spend figure first",
            "put_url": None,
            "put_body": None,
            "command": None,
        }

    recommended = _round_up_nice(basis * buffer_mult)
    action = _classify(current_amount, recommended)
    body, added = _recommendation_payload(props, recommended, contacts)
    budget_id = budget.get("id")
    if budget_id:
        url = (
            budget_id
            if str(budget_id).startswith("https://")
            else f"{_MANAGEMENT_ENDPOINT}{budget_id}"
        )
        put_url = f"{url.split('?', 1)[0]}?api-version={_API_VERSION}"
    else:
        put_url = None
    command = (
        _shell_command(put_url, body, "update", budget.get("eTag"))
        if put_url and body and budget.get("eTag")
        else None
    )
    return {
        "name": name,
        "scope": _scope_of(budget, props),
        "action": action,
        "current_amount": rounded_current_amount,
        "currency": currency,
        "forecast_spend": forecast,
        "forecast_source": forecast_source,
        "recommended_amount": recommended,
        "notifications_added": added,
        "requires_contacts": body is None,
        "rationale": _rationale(
            action,
            current_amount,
            forecast,
            forecast_source,
            recommended,
            buffer_pct,
            currency,
        ),
        "put_url": put_url if body else None,
        "put_body": body,
        "command": command,
    }


def recommend_budgets(budgets=None, *, as_of=None, buffer_pct=DEFAULT_BUFFER_PCT,
                      contacts=_UNSET):
    """Preserve the original right-sizing report, now without placeholder payloads."""
    budgets = budgets or []
    today = as_of or date.today()
    buffer_mult = 1.0 + (buffer_pct / 100.0)
    empty_summary = {"raise": 0, "tighten": 0, "keep": 0, "set": 0, "insufficient_data": 0}
    if not budgets:
        return {
            "as_of": today.isoformat(),
            "buffer_pct": buffer_pct,
            "budget_count": 0,
            "recommendations": [],
            "summary": empty_summary,
            "no_budgets": True,
        }

    recommendations = []
    tally = defaultdict(int)
    for budget in budgets:
        recommendation = _recommend_budget(
            budget,
            today=today,
            buffer_pct=buffer_pct,
            buffer_mult=buffer_mult,
            contacts=contacts,
        )
        recommendations.append(recommendation)
        tally[recommendation["action"]] += 1

    recommendations.sort(key=lambda item: _ACTION_ORDER.get(item["action"], 0), reverse=True)
    return {
        "as_of": today.isoformat(),
        "buffer_pct": buffer_pct,
        "budget_count": len(budgets),
        "recommendations": recommendations,
        "summary": {key: tally[key] for key in empty_summary},
        "no_budgets": False,
    }


_ACTION_ORDER = {"raise": 4, "set": 3, "tighten": 2, "insufficient_data": 1, "keep": 0}
