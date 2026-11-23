"""Offline consolidation of FinOps results into one evidence-aware dashboard."""

from datetime import date, datetime
from decimal import Decimal
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


_spec = spec_from_file_location("finops_dashboard_models", Path(__file__).with_name("models.py"))
_models = module_from_spec(_spec)
_spec.loader.exec_module(_models)

TABS = (
    ("overview", "Overview", "Spend, trend and primary cost drivers."),
    ("explorer", "Cost explorer", "Resource, subscription, region and charge-type drilldown."),
    ("allocation", "Ownership & allocation", "Showback, tag hygiene and unallocated spend."),
    ("billing", "Billing & adjustments", "Explicit actual/amortized bases, discounts, credits and reconciliation."),
    ("budgets", "Budgets & forecasts", "Budget actuals, forecast risk and thresholds."),
    ("anomalies", "Anomalies", "Unexpected spend and investigation evidence."),
    ("rightsizing", "Rightsizing & waste", "Utilization-backed, review-before-action opportunities."),
    ("commitments", "Commitments", "Reservation and savings-plan coverage, utilization and expiry."),
    ("containers", "Containers", "Cluster, namespace and workload costs, idle and shared cost."),
    ("ai", "AI spend", "AI services, models, tokens and compute attribution."),
    ("unit-economics", "Unit economics", "Cloud cost per business outcome; not total company profitability."),
    ("sustainability", "Sustainability", "Sourced emissions and energy, with methodology."),
    ("planning", "Planning & scenarios", "Explicit workload, rate and commitment assumptions."),
    ("governance", "Governance & scorecards", "Policy findings, accountable owners and measured progress."),
    ("reliability", "Cost & reliability", "Reliability risk that must be checked before cutting cost."),
    ("optimization", "Prioritized actions", "Savings, anomalies, budget risk and governance kept separate."),
    ("coverage", "Data & scope", "Freshness, access gaps, unsupported inputs and scope coverage."),
)

_ADAPTERS = {
    "overview": _models.build_cost_overview_model,
    "budgets": _models.build_budget_status_model,
    "rightsizing": _models.build_rightsizing_savings_model,
    "ai": _models.build_ai_spend_model,
    "reliability": _models.build_cost_vs_reliability_model,
    "optimization": _models.build_cost_optimization_model,
}


def _number(row, key):
    if key not in row or row[key] is None:
        raise ValueError(f"{key} is required")
    return _models._decimal(row[key], key)


def _text(row, key):
    value = _models._text(row.get(key), key).strip()
    if not value:
        raise ValueError(f"{key} is required")
    return value


def _rows(data):
    return _models._objects(data, "source.data")


def _section(title, columns, rows, description=""):
    return {
        "title": title, "description": description, "columns": columns, "rows": rows,
        "emptyMessage": "Source returned no matching records for this scope and period.",
    }


def _table(title, data, fields, description=""):
    rows = []
    for row in _rows(data):
        cells = []
        for key, _, kind in fields:
            if kind == "money":
                cells.append(_models._money(_number(row, key)))
            elif kind == "number":
                cells.append(_models._number(_number(row, key)))
            else:
                cells.append(_text(row, key))
        rows.append(cells)
    rows.sort(key=lambda row: tuple((str(cell).casefold(), str(cell)) for cell in row))
    return _section(title, [label for _, label, _ in fields], rows, description)


def _allocation(data):
    data = _models._object(data, "allocation")
    groups = []
    for row in _models._objects(data.get("groups"), "allocation.groups"):
        groups.append([
            _text(row, "owner"), _models._money(_number(row, "monthly_usd")),
            _models._percent(_number(row, "pct")), str(_models._integer(row.get("resource_count"), "resource_count")),
        ])
    inventory = _table("Tag inventory", data.get("tag_inventory", []), (
        ("key", "Tag", "text"), ("resource_count", "Resources", "number"),
        ("cost_usd", "Cost", "money"), ("pct", "Cost coverage %", "number"),
    ), "Tag coverage overlaps; do not sum across keys. Current tags are not historical ownership.")
    recommended = _table("Recommended ownership coverage", data.get("recommended_coverage", []), (
        ("key", "Tag", "text"), ("resource_count", "Resources", "number"),
        ("cost_usd", "Cost", "money"), ("pct", "Coverage %", "number"),
    ))
    untagged = _table("Untagged resources", data.get("untagged_resources", []), (
        ("resourceId", "Resource", "text"), ("monthly_usd", "Cost", "money"),
    ))
    return {
        "metrics": [
            {"label": "Allocated", "value": _models._money(_number(data, "allocated_usd"))},
            {"label": "Unallocated", "value": _models._money(_number(data, "unallocated_usd")), "tone": "warning"},
        ],
        "sections": [_section("Showback by " + _text(data, "dimension"),
                              ["Owner", "Cost", "Share", "Resources"], sorted(groups)),
                     inventory, recommended, untagged],
    }


def _commitments(data):
    rows = []
    for row in _rows(data):
        used, purchased = _number(row, "used_quantity"), _number(row, "purchased_quantity")
        covered, eligible = _number(row, "covered_cost_usd"), _number(row, "eligible_cost_usd")
        if min(used, purchased, covered, eligible) < 0 or used > purchased or covered > eligible:
            raise ValueError("commitment utilization/coverage inputs are inconsistent")
        rows.append([
            _text(row, "benefit"), _text(row, "kind"), _text(row, "quantity_unit"),
            _models._percent(used / purchased * 100 if purchased else None),
            _models._percent(covered / eligible * 100 if eligible else None),
            _models._money(_number(row, "realized_savings_usd")),
            _models._money(_number(row, "unused_cost_usd")), _text(row, "expires_on"),
            _text(row, "recommendation"),
        ])
    return {"sections": [_section(
        "Benefit portfolio", ["Benefit", "Type", "Quantity unit", "Utilization", "Coverage",
                              "Realized savings", "Unused cost", "Expires", "Recommendation"],
        sorted(rows),
        "Ratios use matching units and periods per row. Do not average ratios, add overlapping benefits, "
        "or count purchase recommendations as realized savings. Purchases/exchanges need human review.",
    )]}


def _units(data):
    rows = []
    for row in _rows(data):
        units, cost = _number(row, "units"), _number(row, "cost_usd")
        if units < 0:
            raise ValueError("units must be non-negative")
        rows.append([
            _text(row, "business_unit"), _text(row, "unit"), _models._number(units),
            _models._money(cost), _models._number(cost / units) + " USD" if units else "Unavailable: zero units",
            _text(row, "allocation_method"),
        ])
    return {"sections": [_section(
        "Cost per outcome", ["Business unit", "Unit", "Volume", "Allocated cost", "Cost / unit", "Allocation method"],
        sorted(rows), "Numerator and denominator must share scope and period. Includes only the documented cost basis; "
        "no revenue, margins or full-loaded profitability inferred from cloud cost alone.",
    )]}


def _planning(data):
    rows = []
    for row in _rows(data):
        baseline = _number(row, "baseline_usd")
        projected = _number(row, "projected_usd")
        rows.append([
            _text(row, "scenario"), _models._money(baseline), _models._money(projected),
            _models._money(projected - baseline), _text(row, "assumptions"),
            _text(row, "term"), _text(row, "breakeven"),
        ])
    return {"sections": [_section(
        "What-if scenarios", ["Scenario", "Baseline", "Projected", "Delta", "Assumptions", "Term", "Breakeven"],
        sorted(rows), "Estimates, not forecasts or approved purchases. Provider quote and workload inputs required.",
    )]}


def _extra(tab_id, data):
    if tab_id == "allocation":
        return _allocation(data)
    if tab_id == "commitments":
        return _commitments(data)
    if tab_id == "unit-economics":
        return _units(data)
    if tab_id == "planning":
        return _planning(data)
    definitions = {
        "billing": ("Billing reconciliation inputs", (
            ("business_unit", "Business unit", "text"), ("basis", "Cost basis / charge", "text"),
            ("amount", "Amount", "number"), ("currency", "Currency", "text"),
            ("billing_period", "Billing period", "text"), ("methodology", "Rate / allocation rule", "text"),
        ), "From scoped billing exports or approved chargeback outputs only. Do not sum actual and "
           "amortized costs, different currencies, or overlapping discounts. Not an invoice issuance system."),
        "explorer": ("Resource cost detail", (
            ("resourceId", "Resource", "text"), ("subscriptionId", "Subscription", "text"),
            ("resourceGroup", "Resource group", "text"), ("serviceName", "Service", "text"),
            ("resourceLocation", "Region", "text"), ("chargeType", "Charge type", "text"),
            ("cost_usd", "Cost", "money"),
        ), "One non-overlapping cost basis, USD. Unknown dimensions remain '(unknown)'. "
           "Use search to drill down; this is not invoice reconciliation."),
        "anomalies": ("Detected anomalies", (
            ("dimension", "Dimension", "text"), ("value", "Driver", "text"),
            ("current_usd", "Current", "money"), ("baseline_mean_usd", "Baseline", "money"),
            ("kind", "Kind", "text"),
        ), "From detect_anomalies; correlation is evidence, not proof of cause."),
        "containers": ("Kubernetes allocation", (
            ("cluster", "Cluster", "text"), ("namespace", "Namespace", "text"),
            ("workload", "Workload", "text"), ("cost_usd", "Allocated cost", "money"),
            ("idle_usd", "Idle cost", "money"), ("shared_usd", "Shared cost", "money"),
            ("allocation_method", "Allocation method", "text"),
        ), "Use AKS cost allocation / OpenCost data; idle and shared costs must not be double counted. "
           "A cluster resource bill alone cannot establish namespace or pod allocation."),
        "sustainability": ("Environmental impact", (
            ("scope", "Resource / scope", "text"), ("mtco2e", "Emissions (MTCO2e)", "number"),
            ("kwh", "Energy (kWh)", "number"), ("methodology", "Methodology", "text"),
        ), "Provider or documented model estimates only. No dollar-to-carbon conversion; "
           "do not invent energy where the provider supplies only emissions."),
        "governance": ("Accountability and scorecards", (
            ("control", "Control / metric", "text"), ("owner", "Owner", "text"),
            ("observed", "Observed", "text"), ("target", "Target", "text"),
            ("status", "Status", "text"), ("action", "Action / tracking", "text"),
        ), "Measured tagging, allocation, optimization adoption and policy compliance; "
           "targets and external benchmarks require an explicit source. No automatic remediation."),
    }
    title, fields, description = definitions[tab_id]
    return {"sections": [_table(title, data, fields, description)]}


def _timestamp(value, field):
    text = _models._text(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include timezone")
    return text


_EXPLORER_FIELDS = ("resourceId", "subscriptionId", "resourceGroup", "serviceName",
                    "resourceLocation", "chargeType")


def _explorer_fields(row):
    return [_models._text(row.get(field), field).strip() or "(unknown)"
            for field in _EXPLORER_FIELDS]


def _explorer_key(row):
    values = _explorer_fields(row)
    return tuple(value.casefold() if index in {0, 1, 2, 4} else value
                 for index, value in enumerate(values))


def build_explorer_rows(included_rows):
    """Aggregate already validated resource-cost rows; never discard unknown charges."""
    totals, display = {}, {}
    for row in _rows(included_rows):
        key = _explorer_key(row)
        display.setdefault(key, _explorer_fields(row))
        totals[key] = totals.get(key, Decimal(0)) + _number(row, "cost")
    return [
        dict(zip(_EXPLORER_FIELDS, display[key]), cost_usd=totals[key])
        for key in sorted(totals, key=lambda key: tuple((value.casefold(), value) for value in key))
    ]


def _day(value, field):
    if not isinstance(value, str) or len(value) != 10:
        raise ValueError(f"{field} must be a UTC date in YYYY-MM-DD format")
    try:
        result = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be a UTC date in YYYY-MM-DD format") from exc
    if result.isoformat() != value:
        raise ValueError(f"{field} must be a UTC date in YYYY-MM-DD format")
    return result


def _window(value, field):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"{field} must contain start and end dates")
    start, end = (_day(item, field) for item in value)
    if start > end:
        raise ValueError(f"{field} start must not follow end")
    return start, end


def slice_daily_cost_rows(rows, window):
    """Select inclusive UTC dates from already scope-filtered daily cost rows."""
    start, end = _window(window, "window")
    return [row for row in _rows(rows) if start <= _day(row.get("date"), "cost date") <= end]


def slice_daily_cost_query(prepared, window):
    """Slice a prepared daily query without retaining the longer window's totals."""
    prepared = _models._object(prepared, "prepared")
    if not isinstance(prepared.get("included_rows"), list):
        raise ValueError("prepared.included_rows must be an array")
    buckets = ("included", "excluded", "unattributed")
    result = {
        f"{bucket}_rows": slice_daily_cost_rows(prepared.get(f"{bucket}_rows", []), window)
        for bucket in buckets
    }
    result["partial"] = _models._boolean(prepared.get("partial"), "prepared.partial")
    result["warnings"] = _models._strings(prepared.get("warnings"), "prepared.warnings")
    for field in ("source", "currency", "metric"):
        if field in prepared:
            result[field] = prepared[field]
    original_coverage = _models._objects(prepared.get("scope_coverage", []), "scope_coverage")
    coverage_scopes = {_models._text(item.get("scope"), "scope").casefold() for item in original_coverage}
    complete_attribution = (
        all(isinstance(prepared.get(f"{bucket}_rows"), list) for bucket in buckets)
        and all(coverage_scopes)
        and all(_models._text(row.get("queryScope"), "queryScope").casefold() in coverage_scopes
                for bucket in buckets for row in result[f"{bucket}_rows"])
    )
    tallies = {scope: {"included": 0, "excluded": 0, "unattributed": 0, "cost_usd": Decimal(0)}
               for scope in coverage_scopes}
    if complete_attribution:
        for bucket in buckets:
            for row in result[f"{bucket}_rows"]:
                scope = _models._text(row.get("queryScope"), "queryScope").casefold()
                tallies[scope][bucket] += 1
                if bucket == "included":
                    tallies[scope]["cost_usd"] += _number(row, "cost")
    coverage = []
    for item in original_coverage:
        entry = dict(item)
        previous_detail = _models._text(item.get("detail"), "scope_coverage.detail")
        if complete_attribution:
            scope = _models._text(item.get("scope"), "scope").casefold()
            entry.update(tallies[scope])
            note = "Default-period query coverage."
        else:
            for field in ("included", "excluded", "unattributed", "cost_usd"):
                entry[field] = None
            note = "Default-period counts and cost unavailable for this source."
        entry["detail"] = f"{previous_detail} {note}".strip()
        coverage.append(entry)
    result["scope_coverage"] = coverage
    return result


def _history(source_history, sources, scopes, default_window):
    """Validate and compact the two independently scoped daily cost query shapes."""
    history = _models._object(source_history, "cost_history")
    default_start, default_end = _window(default_window, "window")
    if (default_end - default_start).days != 29:
        raise ValueError("cost_history requires a 30-day dashboard window")
    if history.get("status") == "unavailable":
        reason = _models._text(history.get("reason"), "cost_history.reason").strip()
        if not reason:
            raise ValueError("unavailable cost_history requires a reason")
        return {"available": False, "reason": reason}

    start, end = _window(history.get("window"), "cost_history.window")
    if (end - start).days != 89 or end != default_end or default_start < start:
        raise ValueError("cost_history must cover 90 inclusive days ending with the 30-day dashboard window")

    rows_by_source = {}
    for key in ("overview", "explorer"):
        source = _models._object(sources.get(key), f"sources.{key}")
        item = _models._object(history.get(key), f"cost_history.{key}")
        if source.get("status") not in {"ready", "partial"} or item.get("status") != source.get("status"):
            raise ValueError(f"cost_history.{key} status must match the dashboard source")
        if source.get("window") != list(default_window):
            raise ValueError(f"sources.{key} window must match the 30-day dashboard window")
        for field in ("source", "refreshed_at"):
            if not item.get(field) or item[field] != source.get(field):
                raise ValueError(f"cost_history.{key} {field} must match the dashboard source")
        if item.get("window") != list(history["window"]):
            raise ValueError(f"cost_history.{key} window must match the 90-day history")
        item_scopes = _models._strings(item.get("scope_summary"), "scope_summary")
        if {s.casefold() for s in item_scopes} != {s.casefold() for s in scopes}:
            raise ValueError(f"cost_history.{key} scope_summary must match dashboard scope")
        if not isinstance(item.get("data"), list):
            raise ValueError(f"cost_history.{key} data must be an array")
        rows = _rows(item["data"])
        for row in rows:
            day = _day(row.get("date"), f"cost_history.{key} date")
            if not start <= day <= end:
                raise ValueError(f"cost_history.{key} contains a date outside its window")
            _number(row, "cost")
        rows_by_source[key] = rows

    overview = {}
    groups = {}
    for row in rows_by_source["overview"]:
        day = (_day(row["date"], "cost date") - start).days
        service = _models._text(row.get("serviceName"), "serviceName").strip() or "(unknown)"
        group = _models._text(row.get("resourceGroup"), "resourceGroup").strip() or "(unattributed)"
        groups.setdefault(group.casefold(), group)
        key = (day, service, group.casefold())
        overview[key] = overview.get(key, Decimal(0)) + _number(row, "cost")

    explorer, display = {}, {}
    for row in rows_by_source["explorer"]:
        key = _explorer_key(row)
        display.setdefault(key, _explorer_fields(row))
        day = (_day(row["date"], "cost date") - start).days
        series = explorer.setdefault(key, {})
        series[day] = series.get(day, Decimal(0)) + _number(row, "cost")

    # The static, no-JavaScript default and the selectable data must describe the same 30 days.
    default_offset = (default_start - start).days
    overview_total = sum((amount for (day, _, _), amount in overview.items()
                          if day >= default_offset), Decimal(0))
    overview_data = _models._object(sources["overview"].get("data"), "overview.data")
    expected_total = _models._decimal(_models._field(overview_data, "total_usd", "totalUsd"),
                                      "overview.total_usd")
    if _models._money(overview_total) != _models._money(expected_total):
        raise ValueError("cost_history overview does not match the default dashboard window")
    daily_totals, service_totals, group_totals = {}, {}, {}
    for (day, service, group), amount in overview.items():
        if day < default_offset:
            continue
        daily_totals[day] = daily_totals.get(day, Decimal(0)) + amount
        service_totals[service] = service_totals.get(service, Decimal(0)) + amount
        group_totals[group] = group_totals.get(group, Decimal(0)) + amount

    def ranked(values):
        return sorted(values, key=lambda item: (-item[1], item[0].casefold(), item[0]))[:15]

    expected_daily = _models._objects(
        _models._field(overview_data, "daily", "daily_totals", default=[]), "overview.daily")
    expected_services = _models._objects(
        _models._field(overview_data, "by_service", "top_services", default=[]), "overview.by_service")
    expected_groups = _models._objects(
        _models._field(overview_data, "by_resource_group", "top_resource_groups", default=[]),
        "overview.by_resource_group")
    actual_daily = sorted((day, _models._money(value)) for day, value in daily_totals.items())
    supplied_daily = sorted(
        ((_day(_models._field(row, "date", "label"), "overview date") - start).days,
         _models._money(_models._decimal(_models._field(row, "cost_usd", "costUsd", "cost"), "daily cost")))
        for row in expected_daily
    )
    actual_services = [(key, _models._money(value)) for key, value in ranked(service_totals.items())]
    supplied_services = ranked(
        (_models._text(_models._field(row, "service", "name"), "service"),
         _models._decimal(_models._field(row, "cost_usd", "costUsd", "cost"), "service cost"))
        for row in expected_services
    )
    supplied_services = [(key, _models._money(value)) for key, value in supplied_services]
    actual_groups = [(key, _models._money(value)) for key, value in ranked(group_totals.items())]
    supplied_groups = ranked(
        ((_models._text(_models._field(row, "resource_group", "resourceGroup", "name"),
                        "resource group") or "(unattributed)").casefold(),
         _models._decimal(_models._field(row, "cost_usd", "costUsd", "cost"), "group cost"))
        for row in expected_groups
    )
    supplied_groups = [(key, _models._money(value)) for key, value in supplied_groups]
    if (len(expected_services) != len(actual_services) or len(expected_groups) != len(actual_groups)
            or actual_daily != supplied_daily or actual_services != supplied_services
            or actual_groups != supplied_groups):
        raise ValueError("cost_history overview breakdowns do not match the default dashboard window")

    expected_explorer = {}
    for row in _rows(sources["explorer"].get("data")):
        key = _explorer_key(row)
        if key in expected_explorer:
            raise ValueError("default explorer contains duplicate resource records")
        expected_explorer[key] = _models._money(_number(row, "cost_usd"))
    default_explorer = {
        key: _models._money(sum((amount for day, amount in series.items() if day >= default_offset),
                               Decimal(0)))
        for key, series in explorer.items() if any(day >= default_offset for day in series)
    }
    if default_explorer != expected_explorer:
        raise ValueError("cost_history explorer does not match the default dashboard window")

    amounts = list(overview.values()) + [amount for series in explorer.values() for amount in series.values()]
    scale = max([2] + [-amount.as_tuple().exponent for amount in amounts])
    if scale > 28:
        raise ValueError("cost_history exceeds the supported 28 decimal places")

    def units(amount):
        return format(amount, f".{scale}f").replace(".", "")

    services = sorted({service for _, service, _ in overview})
    group_keys = sorted(groups)
    service_index = {name: index for index, name in enumerate(services)}
    group_index = {key: index for index, key in enumerate(group_keys)}
    overview_rows = [
        [day, service_index[service], group_index[group], units(amount)]
        for (day, service, group), amount in sorted(overview.items())
    ]
    records = []
    for key in sorted(explorer, key=lambda item: tuple((value.casefold(), value) for value in item)):
        series = explorer[key]
        records.append({
            "fields": display[key],
            "active": key in default_explorer,
            "series": [[day, units(amount)] for day, amount in sorted(series.items())],
        })
    return {
        "available": True, "window": list(history["window"]), "scale": scale,
        "overview": {"services": services, "groups": [groups[key] for key in group_keys],
                     "rows": overview_rows},
        "explorer": records,
    }


def build_dashboard_model(sources, *, refreshed_at, scope_summary, window,
                          scope_mode="managed", scope_coverage=None, warnings=None,
                          cost_history=None):
    """Consolidate source envelopes; absent or failed inputs never become healthy zeros.

    Sources are keyed by tab id. Each envelope contains status (ready/partial/unavailable/
    unsupported), source, refreshed_at, window, scope_summary, data and optional reason.
    All calculations use provider-validated, prefiltered, non-overlapping inputs.
    """
    sources = _models._object(sources, "sources")
    unknown = set(sources) - {tab[0] for tab in TABS if tab[0] != "coverage"}
    if unknown:
        raise ValueError("unknown dashboard sources: " + ", ".join(sorted(unknown)))
    if scope_mode not in ("managed", "everything", "subscriptions"):
        raise ValueError("scope_mode must be managed, everything or subscriptions")
    refreshed_at = _timestamp(refreshed_at, "refreshed_at")
    scopes = _models._stable_strings(_models._strings(scope_summary, "scope_summary"))
    if not scopes:
        raise ValueError("scope_summary must not be empty")
    if not isinstance(window, (list, tuple)) or len(window) != 2:
        raise ValueError("window must contain start and end dates")
    start, end = [date.fromisoformat(value) for value in window]
    if start > end:
        raise ValueError("window start must not follow end")
    history = _history(cost_history, sources, scopes, window) if cost_history is not None else None
    period = " to ".join(window)
    tabs, coverage, all_warnings = [], [], _models._strings(warnings, "warnings")
    any_partial = False
    for tab_id, title, description in TABS:
        if tab_id == "coverage":
            continue
        envelope = _models._object(sources.get(tab_id, {}), f"sources.{tab_id}")
        status = envelope.get("status", "unavailable")
        if status not in {"ready", "partial", "unavailable", "unsupported"}:
            raise ValueError(f"invalid source status for {tab_id}")
        reason = _models._text(envelope.get("reason"), "reason")
        if status in {"partial", "unavailable", "unsupported"} and envelope and not reason:
            raise ValueError(f"{tab_id} requires a reason")
        reason = reason or ("Source not connected or not collected; configure this input." if not envelope else "")
        source = _models._text(envelope.get("source"), "source", default="Not collected")
        source_time = _models._text(envelope.get("refreshed_at"), "refreshed_at")
        panel = {"metrics": [], "sections": []}
        if status in {"ready", "partial"}:
            if not source.strip() or source == "Not collected":
                raise ValueError(f"{tab_id} requires a source")
            source_time = _timestamp(source_time, f"{tab_id}.refreshed_at")
            source_window = envelope.get("window")
            if not isinstance(source_window, list) or len(source_window) != 2:
                raise ValueError(f"{tab_id} requires a source window")
            if date.fromisoformat(source_window[0]) > date.fromisoformat(source_window[1]):
                raise ValueError(f"{tab_id} source window is reversed")
            supplied = _models._strings(envelope.get("scope_summary"), "scope_summary")
            if {s.casefold() for s in supplied} != {s.casefold() for s in scopes}:
                raise ValueError(f"{tab_id} scope_summary must match dashboard scope")
            if envelope.get("data") is None:
                raise ValueError(f"{tab_id} data is required")
            if tab_id == "overview" and not any(
                key in envelope["data"] for key in ("total_usd", "totalUsd")
            ):
                raise ValueError("overview total_usd is required; missing cost is not zero")
            if tab_id in _ADAPTERS:
                panel = _ADAPTERS[tab_id](
                    envelope["data"], refreshed_at=source_time, scope_summary=scopes,
                    partial=status == "partial", warnings=[reason] if reason else [],
                )
            else:
                data = envelope["data"]
                if tab_id == "explorer" and history and history["available"]:
                    existing = {_explorer_key(row): row for row in _rows(data)}
                    data = [existing.get(_explorer_key(dict(zip(_EXPLORER_FIELDS, record["fields"])))) or
                            dict(zip(_EXPLORER_FIELDS, record["fields"]), cost_usd=0)
                            for record in history["explorer"]]
                panel = _extra(tab_id, data)
            if tab_id == "overview":
                panel["sections"][0]["visualization"] = "service-bars"
                if history and history["available"]:
                    panel["sections"][0]["toneValues"] = history["overview"]["services"]
                    for section in panel["sections"]:
                        if section.get("title") == "Scope coverage":
                            section["description"] += " Counts and cost refer to the default 30-day window."
            if tab_id in {"explorer", "rightsizing"}:
                section = panel["sections"][0]
                section["breakdown"] = (
                    {"kind": "cost", "dimensions": [("Service", 3), ("Region", 4)], "amount": 6}
                    if tab_id == "explorer" else
                    {"kind": "savings", "dimensions": [("Resource type", 1), ("Opportunity", 2)], "amount": 6}
                )
                if tab_id == "explorer" and history and history["available"]:
                    section["historyRecords"] = [
                        {"fields": record["fields"], "active": record["active"]}
                        for record in history["explorer"]
                    ]
                panel["chart"] = None
            if panel.get("partial"):
                status = "partial"
            all_warnings.extend(f"{title}: {warning}" for warning in panel.get("warnings", []))
        if status != "ready":
            any_partial = True
        source_period = " to ".join(envelope["window"]) if status in {"ready", "partial"} else period
        coverage.append([title, status, source, source_time or "Unknown", source_period, reason])
        tabs.append({
            "id": tab_id, "title": title, "description": description,
            "status": status, "reason": reason, "source": source,
            "refreshedAt": source_time, "period": source_period,
            "metrics": panel.get("metrics", []), "chart": panel.get("chart"),
            "sections": panel.get("sections", []),
        })
    scope_section, partial_coverage = _models._scope_coverage_section(scope_coverage)
    if scope_section and history and history["available"]:
        scope_section["description"] += " Counts and cost refer to the default 30-day window."
    tabs.append({
        "id": "coverage", "title": "Data & scope", "description": "Coverage is not a claim of product parity.",
        "status": "partial" if any_partial or partial_coverage else "ready",
        "reason": "", "source": "Dashboard source inventory", "refreshedAt": refreshed_at,
        "period": period, "metrics": [],
        "sections": [
            _section("Source coverage", ["Section", "Status", "Source", "As of", "Window", "Reason / next step"], coverage),
            *([scope_section] if scope_section else []),
            _section("Capability boundaries", ["Area", "Coverage"], [
                ["Clouds", "Azure only; AWS, GCP, OCI and SaaS integrations are not provided."],
                ["Financial basis", "Cost Management USD basis; invoice credits, tax, FX, custom rates and amortization require matching billing exports."],
                ["Allocation", "Tag showback is available; historical business rules and shared-cost redistribution need supplied approved allocation outputs."],
                ["Access", "Client-side search is not authorization. Report viewers can inspect the whole saved snapshot; use platform report access controls."],
                ["Delivery", "One scheduled snapshot, no view-time Azure calls. Billing settles after ingestion; refresh is not real-time billing."],
            ]),
        ],
    })
    result = {
        "title": "FinOps: Dashboard",
        "description": "Cloud economics, operational risk and accountability in one workspace.",
        "refreshedAt": refreshed_at, "scopeSummary": scopes, "scopeMode": scope_mode,
        "window": list(window), "partial": any_partial or partial_coverage,
        "warnings": _models._stable_strings(all_warnings),
        "metrics": tabs[0]["metrics"], "sections": [], "tabs": tabs,
    }
    if history is not None:
        result["costHistory"] = history
    return result
