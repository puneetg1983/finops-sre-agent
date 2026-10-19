"""Deterministic adapters from FinOps analysis outputs to report models."""

from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


_CENT = Decimal("0.01")
_STATUS_ORDER = {
    "over_budget": 0,
    "forecast_over": 1,
    "at_risk": 2,
    "on_track": 3,
}
_VERIFY_HINTS = {"commitment_opportunity", "compute_no_tokens_verify"}


def _field(obj, *names, default=None):
    for name in names:
        if name in obj:
            return obj[name]
    return default


def _object(value, field):
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return value


def _objects(value, field):
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field} must be an array")
    rows = []
    for index, row in enumerate(value):
        if not isinstance(row, dict):
            raise ValueError(f"{field}[{index}] must be an object")
        rows.append(row)
    return rows


def _strings(value, field):
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field} must be an array")
    result = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise ValueError(f"{field}[{index}] must be text")
        result.append(item)
    return result


def _text(value, field, *, default=""):
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    return value


def _boolean(value, field, *, default=False):
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be boolean")
    return value


def _decimal(value, field, *, optional=False):
    if value is None:
        if optional:
            return None
        return Decimal(0)
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError(f"{field} must be a finite number")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{field} must be a finite number")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"{field} must be a finite number") from exc
    if not result.is_finite():
        raise ValueError(f"{field} must be a finite number")
    return result


def _integer(value, field, *, default=0):
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    return value


def _money(value, currency="USD"):
    if value is None:
        return "—"
    amount = value.quantize(_CENT, rounding=ROUND_HALF_UP)
    if currency == "USD":
        sign = "-" if amount < 0 else ""
        return f"{sign}${abs(amount):,.2f}"
    return f"{amount:,.2f} {currency}"


def _percent(value):
    return "—" if value is None else f"{value.quantize(Decimal('0.1'), rounding=ROUND_HALF_UP):,.1f}%"


def _number(value):
    return "—" if value is None else f"{value.normalize():f}"


def _chart_number(value):
    return float(value)


def _decimal_key(value):
    return "" if value is None else format(value, "f")


def _stable_strings(values):
    return sorted(set(values), key=lambda item: (item.casefold(), item))


def _short_resource(resource_id):
    return resource_id.rstrip("/").rsplit("/", 1)[-1] if resource_id else "(unknown)"


def _validated(value, field):
    if value is None:
        return "verify first"
    valid = _boolean(value, field)
    return "validated" if valid else "contradicted — verify first"


def _scope_coverage_section(rows):
    coverage = []
    any_partial = False
    for index, row in enumerate(_objects(rows, "scope_coverage")):
        prefix = f"scope_coverage[{index}]"
        scope = _text(_field(row, "scope", "scopeId"), f"{prefix}.scope")
        status = _text(row.get("status"), f"{prefix}.status", default="complete")
        included = _field(row, "included", "included_count", "includedCount")
        excluded = _field(row, "excluded", "excluded_count", "excludedCount")
        unattributed = _field(
            row, "unattributed", "unattributed_count", "unattributedCount"
        )
        cost = _field(row, "cost_usd", "costUsd")
        detail = _text(
            _field(row, "detail", "warning"), f"{prefix}.detail", default=""
        )
        row_partial = _boolean(row.get("partial"), f"{prefix}.partial")
        any_partial = any_partial or row_partial
        coverage.append(
            (
                scope,
                status,
                "—" if included is None else str(_integer(included, f"{prefix}.included")),
                "—" if excluded is None else str(_integer(excluded, f"{prefix}.excluded")),
                "—"
                if unattributed is None
                else str(_integer(unattributed, f"{prefix}.unattributed")),
                _money(_decimal(cost, f"{prefix}.cost_usd", optional=True)),
                detail,
            )
        )
    coverage.sort(
        key=lambda row: (
            row[0].casefold(),
            row[0],
            row[1].casefold(),
            row[1],
            row[2],
            row[3],
            row[4],
            row[5],
            row[6].casefold(),
            row[6],
        )
    )
    if not coverage:
        return None, any_partial
    return {
        "title": "Scope coverage",
        "description": "Included, excluded, and unattributed results by managed scope.",
        "columns": [
            "Scope",
            "Status",
            "Included",
            "Excluded",
            "Unattributed",
            "Cost",
            "Detail",
        ],
        "rows": [list(row) for row in coverage],
        "emptyMessage": "No scope coverage rows were supplied.",
    }, any_partial


def _context(
    source,
    *,
    refreshed_at,
    scope_summary,
    partial,
    warnings,
    scope_coverage,
    extra_partial=False,
    extra_warnings=None,
):
    source = source if isinstance(source, dict) else {}
    refreshed = _text(refreshed_at, "refreshed_at").strip()
    if not refreshed:
        raise ValueError("refreshed_at is required")

    source_scopes = _field(source, "scope_summary", "scopeSummary", default=[])
    scopes = _strings(
        source_scopes if scope_summary is None else scope_summary, "scope_summary"
    )
    source_warnings = _strings(source.get("warnings"), "warnings")
    supplied_warnings = _strings(warnings, "warnings")
    generated_warnings = _strings(extra_warnings, "extra_warnings")
    source_partial = _boolean(source.get("partial"), "partial")
    supplied_partial = _boolean(partial, "partial")

    coverage_rows = scope_coverage
    if coverage_rows is None:
        coverage_rows = _field(source, "scope_coverage", "scopeCoverage", default=[])
    coverage_section, coverage_partial = _scope_coverage_section(coverage_rows)
    return {
        "refreshedAt": refreshed,
        "scopeSummary": _stable_strings(scopes),
        "partial": source_partial or supplied_partial or extra_partial or coverage_partial,
        "warnings": _stable_strings(
            source_warnings + supplied_warnings + generated_warnings
        ),
        "coverageSection": coverage_section,
    }


def _model(title, description, context, metrics, chart, sections):
    coverage = context["coverageSection"]
    if coverage:
        sections = list(sections) + [coverage]
    return {
        "title": title,
        "description": description,
        "refreshedAt": context["refreshedAt"],
        "scopeSummary": context["scopeSummary"],
        "partial": context["partial"],
        "warnings": context["warnings"],
        "metrics": metrics,
        "chart": chart,
        "sections": sections,
    }


def build_cost_overview_model(
    overview,
    *,
    refreshed_at,
    scope_summary=None,
    partial=False,
    warnings=None,
    scope_coverage=None,
):
    """Adapt a deterministic 30-day cost aggregation to the generic report model."""

    overview = _object(overview, "overview")
    total = _decimal(
        _field(overview, "total_usd", "totalUsd"), "overview.total_usd"
    )
    daily = []
    for index, row in enumerate(
        _objects(_field(overview, "daily", "daily_totals", default=[]), "overview.daily")
    ):
        date = _text(_field(row, "date", "label"), f"overview.daily[{index}].date")
        cost = _decimal(
            _field(row, "cost_usd", "costUsd", "cost"),
            f"overview.daily[{index}].cost_usd",
        )
        daily.append((date, cost))
    daily.sort(key=lambda row: (row[0], row[1]))

    services = []
    for index, row in enumerate(
        _objects(
            _field(overview, "by_service", "top_services", default=[]),
            "overview.by_service",
        )
    ):
        name = _text(
            _field(row, "service", "name"), f"overview.by_service[{index}].service"
        )
        cost = _decimal(
            _field(row, "cost_usd", "costUsd", "cost"),
            f"overview.by_service[{index}].cost_usd",
        )
        services.append((name, cost))
    services.sort(key=lambda row: (-row[1], row[0].casefold(), row[0]))

    groups = []
    for index, row in enumerate(
        _objects(
            _field(overview, "by_resource_group", "top_resource_groups", default=[]),
            "overview.by_resource_group",
        )
    ):
        name = _text(
            _field(row, "resource_group", "resourceGroup", "name"),
            f"overview.by_resource_group[{index}].resource_group",
        )
        cost = _decimal(
            _field(row, "cost_usd", "costUsd", "cost"),
            f"overview.by_resource_group[{index}].cost_usd",
        )
        groups.append((name or "(unattributed)", cost))
    groups.sort(key=lambda row: (-row[1], row[0].casefold(), row[0]))

    coverage = scope_coverage
    if coverage is None:
        coverage = _field(overview, "scope_coverage", "scopeCoverage", "coverage", default=[])
    context = _context(
        overview,
        refreshed_at=refreshed_at,
        scope_summary=scope_summary,
        partial=partial,
        warnings=warnings,
        scope_coverage=coverage,
    )
    chart = {
        "type": "line",
        "label": "Daily ActualCost (USD)",
        "labels": [date for date, _ in daily],
        "values": [_chart_number(cost) for _, cost in daily],
    }
    return _model(
        "FinOps: Cost Overview",
        "Daily-refreshed 30-day Azure ActualCost snapshot; recent UsageDetails can still be settling.",
        context,
        [
            {"label": "30-day spend", "value": _money(total), "detail": "ActualCost"},
            {
                "label": "Days represented",
                "value": str(len(daily)),
                "detail": "Daily cost points",
            },
            {
                "label": "Services represented",
                "value": str(len(services)),
                "detail": "Ranked service totals",
            },
        ],
        chart,
        [
            {
                "title": "Top services",
                "description": "Service totals ranked by cost.",
                "columns": ["Service", "Cost"],
                "rows": [[name, _money(cost)] for name, cost in services],
                "emptyMessage": "No service cost rows were produced.",
            },
            {
                "title": "Top resource groups",
                "description": "Resource-group totals ranked by cost.",
                "columns": ["Resource group", "Cost"],
                "rows": [[name, _money(cost)] for name, cost in groups],
                "emptyMessage": "No resource-group cost rows were produced.",
            },
        ],
    )


def build_rightsizing_savings_model(
    result,
    *,
    refreshed_at,
    scope_summary=None,
    partial=False,
    warnings=None,
    scope_coverage=None,
):
    """Adapt recommend_rightsizing output to the generic report model."""

    source = result if isinstance(result, dict) else {}
    raw_findings = _field(source, "findings", "recommendations", default=[]) if source else result
    findings = []
    for index, row in enumerate(_objects(raw_findings, "findings")):
        prefix = f"findings[{index}]"
        resource_id = _text(row.get("resourceId"), f"{prefix}.resourceId")
        resource_type = _text(row.get("resourceType"), f"{prefix}.resourceType")
        kind = _text(row.get("kind"), f"{prefix}.kind")
        sku = _text(row.get("currentSku"), f"{prefix}.currentSku")
        action = _text(row.get("recommendedAction"), f"{prefix}.recommendedAction")
        current = _decimal(
            row.get("currentMonthlyUsd"), f"{prefix}.currentMonthlyUsd", optional=True
        )
        savings = _decimal(
            row.get("estMonthlySavingsUsd"),
            f"{prefix}.estMonthlySavingsUsd",
            optional=True,
        )
        validated_raw = row.get("validated")
        validated = _validated(validated_raw, f"{prefix}.validated")
        evidence = _strings(row.get("evidence"), f"{prefix}.evidence")
        findings.append(
            {
                "resource_id": resource_id,
                "resource_type": resource_type,
                "kind": kind,
                "sku": sku,
                "action": action,
                "current": current,
                "savings": savings,
                "validated": validated,
                "validated_raw": validated_raw,
                "evidence": "; ".join(evidence),
            }
        )
    findings.sort(
        key=lambda row: (
            row["savings"] is None,
            -(row["savings"] or Decimal(0)),
            row["resource_id"].casefold(),
            row["resource_id"],
            row["action"].casefold(),
            row["action"],
            row["resource_type"].casefold(),
            row["resource_type"],
            row["kind"].casefold(),
            row["kind"],
            row["sku"].casefold(),
            row["sku"],
            _decimal_key(row["current"]),
            row["validated"],
            row["evidence"].casefold(),
            row["evidence"],
        )
    )
    quantified = [row["savings"] for row in findings if row["savings"] is not None]
    total_savings = sum(quantified, Decimal(0))
    verified_count = sum(row["validated_raw"] is True for row in findings)
    verify_count = len(findings) - verified_count
    chart_rows = [row for row in findings if row["savings"] is not None][:10]

    context = _context(
        source,
        refreshed_at=refreshed_at,
        scope_summary=scope_summary,
        partial=partial,
        warnings=warnings,
        scope_coverage=scope_coverage,
    )
    return _model(
        "FinOps: Rightsizing Savings",
        "Weekly read-only rightsizing and idle-resource savings snapshot.",
        context,
        [
            {
                "label": "Estimated monthly savings",
                "value": _money(total_savings),
                "detail": f"{len(quantified)} quantified recommendations",
                "tone": "good",
            },
            {
                "label": "Recommendations",
                "value": str(len(findings)),
                "detail": "Ranked opportunities",
            },
            {
                "label": "Validated",
                "value": str(verified_count),
                "detail": "Supported by available evidence",
                "tone": "good",
            },
            {
                "label": "Verify first",
                "value": str(verify_count),
                "detail": "Unvalidated or contradicted",
                "tone": "warning" if verify_count else "neutral",
            },
        ],
        {
            "type": "bar",
            "label": "Estimated monthly savings (USD)",
            "labels": [_short_resource(row["resource_id"]) for row in chart_rows],
            "values": [_chart_number(row["savings"]) for row in chart_rows],
        },
        [
            {
                "title": "Ranked savings opportunities",
                "description": "Known savings first; ties are ordered by resource ID.",
                "columns": [
                    "Resource",
                    "Type",
                    "Kind",
                    "Current SKU",
                    "Recommended action",
                    "Current monthly cost",
                    "Estimated monthly savings",
                    "Validation",
                    "Evidence",
                ],
                "rows": [
                    [
                        row["resource_id"],
                        row["resource_type"],
                        row["kind"],
                        row["sku"],
                        row["action"],
                        _money(row["current"]),
                        _money(row["savings"]),
                        row["validated"],
                        row["evidence"],
                    ]
                    for row in findings
                ],
                "emptyMessage": "No rightsizing opportunities were produced.",
            }
        ],
    )


def build_budget_status_model(
    result,
    *,
    refreshed_at,
    scope_summary=None,
    partial=False,
    warnings=None,
    scope_coverage=None,
):
    """Adapt evaluate_budgets output to the generic report model."""

    result = _object(result, "result")
    summary = _object(result.get("summary") or {}, "result.summary")
    budgets = []
    has_run_rate = False
    has_zero_spend = False
    for index, row in enumerate(_objects(result.get("budgets"), "result.budgets")):
        prefix = f"result.budgets[{index}]"
        name = _text(row.get("name"), f"{prefix}.name")
        scope = _text(row.get("scope"), f"{prefix}.scope")
        currency = _text(row.get("currency"), f"{prefix}.currency", default="USD") or "USD"
        amount = _decimal(row.get("amount"), f"{prefix}.amount", optional=True)
        current = _decimal(row.get("current_spend"), f"{prefix}.current_spend")
        used = _decimal(row.get("pct_used"), f"{prefix}.pct_used", optional=True)
        forecast = _decimal(
            row.get("forecast_spend"), f"{prefix}.forecast_spend", optional=True
        )
        forecast_pct = _decimal(
            row.get("pct_forecast"), f"{prefix}.pct_forecast", optional=True
        )
        forecast_source = _text(
            row.get("forecast_source"), f"{prefix}.forecast_source"
        )
        status = _text(row.get("status"), f"{prefix}.status")
        breached = []
        for notice_index, notice in enumerate(
            _objects(row.get("breached_notifications"), f"{prefix}.breached_notifications")
        ):
            notice_name = _text(
                notice.get("name"),
                f"{prefix}.breached_notifications[{notice_index}].name",
            )
            threshold = _decimal(
                notice.get("threshold"),
                f"{prefix}.breached_notifications[{notice_index}].threshold",
            )
            notice_type = _text(
                notice.get("type"),
                f"{prefix}.breached_notifications[{notice_index}].type",
            )
            breached.append(f"{notice_name}: {_percent(threshold)} {notice_type}")
        breached.sort(key=lambda item: (item.casefold(), item))
        has_run_rate = has_run_rate or forecast_source == "run-rate"
        has_zero_spend = has_zero_spend or current == 0
        budgets.append(
            {
                "name": name,
                "scope": scope,
                "currency": currency,
                "amount": amount,
                "current": current,
                "used": used,
                "forecast": forecast,
                "forecast_pct": forecast_pct,
                "forecast_source": forecast_source,
                "status": status,
                "breached": "; ".join(breached),
            }
        )
    budgets.sort(
        key=lambda row: (
            _STATUS_ORDER.get(row["status"], 99),
            -(row["used"] or Decimal(-1)),
            row["name"].casefold(),
            row["name"],
            row["scope"].casefold(),
            row["scope"],
            _decimal_key(row["amount"]),
            _decimal_key(row["current"]),
            _decimal_key(row["forecast"]),
            _decimal_key(row["forecast_pct"]),
            row["forecast_source"],
            row["breached"].casefold(),
            row["breached"],
        )
    )

    gates = []
    for index, row in enumerate(_objects(result.get("gates"), "result.gates")):
        name = _text(row.get("name"), f"result.gates[{index}].name")
        reason = _text(row.get("reason"), f"result.gates[{index}].reason")
        status = _text(row.get("status"), f"result.gates[{index}].status")
        gates.append((status, name, reason))
    gates.sort(
        key=lambda row: (
            _STATUS_ORDER.get(row[0], 99),
            row[1].casefold(),
            row[1],
            row[2].casefold(),
            row[2],
        )
    )

    generated = []
    if has_run_rate:
        generated.append("Run-rate forecasts are estimates, not Azure forecastSpend.")
    if has_zero_spend:
        generated.append(
            "Azure updates currentSpend asynchronously; zero spend on a new budget may not be synchronized yet."
        )
    context = _context(
        result,
        refreshed_at=refreshed_at,
        scope_summary=scope_summary,
        partial=partial,
        warnings=warnings,
        scope_coverage=scope_coverage,
        extra_warnings=generated,
    )

    chart_labels = []
    chart_values = []
    for row in budgets[:12]:
        if row["used"] is not None:
            chart_labels.append(f"{row['name']} — used")
            chart_values.append(_chart_number(row["used"]))
        if row["forecast_pct"] is not None:
            chart_labels.append(f"{row['name']} — forecast")
            chart_values.append(_chart_number(row["forecast_pct"]))

    total_amount = _decimal(
        summary.get("total_amount"), "result.summary.total_amount"
    )
    total_current = _decimal(
        summary.get("total_current"), "result.summary.total_current"
    )
    total_forecast = _decimal(
        summary.get("total_forecast"), "result.summary.total_forecast"
    )
    over = _integer(summary.get("over_budget"), "result.summary.over_budget")
    forecast_over = _integer(
        summary.get("forecast_over"), "result.summary.forecast_over"
    )
    at_risk = _integer(summary.get("at_risk"), "result.summary.at_risk")
    on_track = _integer(summary.get("on_track"), "result.summary.on_track")

    return _model(
        "FinOps: Budget Status",
        "Daily Azure budget-governance snapshot; currentSpend is updated asynchronously.",
        context,
        [
            {"label": "Portfolio budget", "value": _money(total_amount), "detail": "Configured amount"},
            {"label": "Current spend", "value": _money(total_current), "detail": "Azure currentSpend"},
            {
                "label": "Forecast spend",
                "value": _money(total_forecast),
                "detail": "Azure or labeled run-rate forecast",
            },
            {
                "label": "Gated budgets",
                "value": str(len(gates)),
                "detail": f"{over} over, {forecast_over} forecast over",
                "tone": "critical" if gates else "good",
            },
            {
                "label": "Portfolio states",
                "value": str(len(budgets)),
                "detail": f"{at_risk} at risk, {on_track} on track",
            },
        ],
        {
            "type": "bar",
            "label": "Percent of budget (100% = budget limit)",
            "labels": chart_labels,
            "values": chart_values,
        },
        [
            {
                "title": "Gated budgets",
                "description": "Budgets requiring a human review before additional spend.",
                "columns": ["Status", "Budget", "Reason"],
                "rows": [list(row) for row in gates],
                "emptyMessage": "No budgets currently require a governance gate.",
            },
            {
                "title": "Budget status",
                "description": "Gated states first, followed by at-risk and on-track budgets.",
                "columns": [
                    "Budget",
                    "Scope",
                    "Amount",
                    "Spent",
                    "Used",
                    "Forecast",
                    "Forecast source",
                    "Forecast %",
                    "Status",
                    "Breached thresholds",
                ],
                "rows": [
                    [
                        row["name"],
                        row["scope"],
                        _money(row["amount"], row["currency"]),
                        _money(row["current"], row["currency"]),
                        _percent(row["used"]),
                        _money(row["forecast"], row["currency"]),
                        row["forecast_source"],
                        _percent(row["forecast_pct"]),
                        row["status"],
                        row["breached"],
                    ]
                    for row in budgets
                ],
                "emptyMessage": (
                    "No budgets are defined. Create an Azure budget to establish spend guardrails."
                    if _boolean(result.get("no_budgets"), "result.no_budgets")
                    else "No evaluated budget rows were produced."
                ),
            },
        ],
    )


def _optimization_rightsizing(rows):
    result = []
    for index, row in enumerate(_objects(rows, "result.rightsizing.top")):
        prefix = f"result.rightsizing.top[{index}]"
        resource_id = _text(row.get("resourceId"), f"{prefix}.resourceId")
        savings = _decimal(
            row.get("estMonthlySavingsUsd"),
            f"{prefix}.estMonthlySavingsUsd",
            optional=True,
        )
        result.append(
            {
                "resource_id": resource_id,
                "kind": _text(row.get("kind"), f"{prefix}.kind"),
                "action": _text(
                    row.get("recommendedAction"), f"{prefix}.recommendedAction"
                ),
                "savings": savings,
                "validation": _validated(row.get("validated"), f"{prefix}.validated"),
            }
        )
    result.sort(
        key=lambda row: (
            row["savings"] is None,
            -(row["savings"] or Decimal(0)),
            row["resource_id"].casefold(),
            row["resource_id"],
            row["kind"].casefold(),
            row["kind"],
            row["action"].casefold(),
            row["action"],
            row["validation"],
        )
    )
    return result


def build_cost_optimization_model(
    result,
    *,
    refreshed_at,
    scope_summary=None,
    partial=False,
    warnings=None,
    scope_coverage=None,
):
    """Adapt summarize_optimization output to the generic report model."""

    result = _object(result, "result")
    headline = _object(result.get("headline") or {}, "result.headline")
    rightsizing_block = _object(result.get("rightsizing") or {}, "result.rightsizing")
    anomaly_block = _object(result.get("anomalies") or {}, "result.anomalies")
    budget_block = _object(result.get("budgets") or {}, "result.budgets")
    governance = _object(result.get("governance") or {}, "result.governance")
    rightsizing = _optimization_rightsizing(rightsizing_block.get("top"))

    priorities = []
    for index, row in enumerate(_objects(result.get("priorities"), "result.priorities")):
        prefix = f"result.priorities[{index}]"
        rank = _integer(row.get("rank"), f"{prefix}.rank", default=index + 1)
        impact = _decimal(row.get("impact_usd"), f"{prefix}.impact_usd", optional=True)
        priorities.append(
            {
                "rank": rank,
                "category": _text(row.get("category"), f"{prefix}.category"),
                "impact_type": _text(
                    row.get("impact_type"), f"{prefix}.impact_type"
                ),
                "impact": impact,
                "title": _text(row.get("title"), f"{prefix}.title"),
                "detail": _text(row.get("detail"), f"{prefix}.detail"),
                "action": _text(row.get("action"), f"{prefix}.action"),
                "validation": _validated(
                    row.get("validated"), f"{prefix}.validated"
                )
                if row.get("category") == "rightsizing"
                else "",
            }
        )
    priorities.sort(
        key=lambda row: (
            row["rank"],
            row["category"].casefold(),
            row["category"],
            row["title"].casefold(),
            row["title"],
            row["impact_type"].casefold(),
            row["impact_type"],
            _decimal_key(row["impact"]),
            row["detail"].casefold(),
            row["detail"],
            row["action"].casefold(),
            row["action"],
            row["validation"],
        )
    )

    anomalies = []
    for index, row in enumerate(
        _objects(anomaly_block.get("top"), "result.anomalies.top")
    ):
        prefix = f"result.anomalies.top[{index}]"
        impact = _decimal(row.get("impact_usd"), f"{prefix}.impact_usd", optional=True)
        anomalies.append(
            {
                "kind": _text(row.get("kind"), f"{prefix}.kind"),
                "dimension": _text(row.get("dimension"), f"{prefix}.dimension"),
                "value": _text(row.get("value"), f"{prefix}.value"),
                "current": _decimal(
                    row.get("current_usd"), f"{prefix}.current_usd", optional=True
                ),
                "baseline": _decimal(
                    row.get("baseline_mean_usd"),
                    f"{prefix}.baseline_mean_usd",
                    optional=True,
                ),
                "impact": impact,
            }
        )
    anomalies.sort(
        key=lambda row: (
            row["impact"] is None,
            -(row["impact"] or Decimal(0)),
            row["dimension"].casefold(),
            row["dimension"],
            row["value"].casefold(),
            row["value"],
            row["kind"].casefold(),
            row["kind"],
            _decimal_key(row["current"]),
            _decimal_key(row["baseline"]),
        )
    )

    budget_rows = []
    has_run_rate = False
    for bucket in ("over", "forecast_over", "at_risk"):
        for index, row in enumerate(
            _objects(budget_block.get(bucket), f"result.budgets.{bucket}")
        ):
            prefix = f"result.budgets.{bucket}[{index}]"
            source = _text(row.get("forecast_source"), f"{prefix}.forecast_source")
            has_run_rate = has_run_rate or source == "run-rate"
            budget_rows.append(
                {
                    "name": _text(row.get("name"), f"{prefix}.name"),
                    "status": _text(row.get("status"), f"{prefix}.status"),
                    "currency": _text(
                        row.get("currency"), f"{prefix}.currency", default="USD"
                    )
                    or "USD",
                    "current": _decimal(
                        row.get("current_spend"), f"{prefix}.current_spend", optional=True
                    ),
                    "amount": _decimal(
                        row.get("amount"), f"{prefix}.amount", optional=True
                    ),
                    "forecast": _decimal(
                        row.get("forecast_spend"),
                        f"{prefix}.forecast_spend",
                        optional=True,
                    ),
                    "forecast_source": source,
                }
            )
    budget_rows.sort(
        key=lambda row: (
            _STATUS_ORDER.get(row["status"], 99),
            row["name"].casefold(),
            row["name"],
            row["currency"],
            _decimal_key(row["current"]),
            _decimal_key(row["amount"]),
            _decimal_key(row["forecast"]),
            row["forecast_source"],
        )
    )

    governance_rows = []
    for index, row in enumerate(
        _objects(governance.get("untagged_top"), "result.governance.untagged_top")
    ):
        rid = _text(row.get("resourceId"), f"result.governance.untagged_top[{index}].resourceId")
        cost = _decimal(
            row.get("monthly_usd"),
            f"result.governance.untagged_top[{index}].monthly_usd",
            optional=True,
        )
        governance_rows.append(("untagged resource", rid, _money(cost), "", "Add ownership tags."))
    for index, row in enumerate(
        _objects(governance.get("tag_hygiene"), "result.governance.tag_hygiene")
    ):
        canonical = _text(
            row.get("canonical"), f"result.governance.tag_hygiene[{index}].canonical"
        )
        variants = _object(
            row.get("variants") or {},
            f"result.governance.tag_hygiene[{index}].variants",
        )
        variant_names = []
        for name in variants:
            variant_names.append(
                _text(name, f"result.governance.tag_hygiene[{index}].variants key")
            )
        affected = _decimal(
            row.get("cost_affected"),
            f"result.governance.tag_hygiene[{index}].cost_affected",
            optional=True,
        )
        governance_rows.append(
            (
                "tag hygiene",
                canonical,
                _money(affected),
                ", ".join(_stable_strings(variant_names)),
                "Consolidate tag values.",
            )
        )
    for index, row in enumerate(
        _objects(governance.get("budget_gates"), "result.governance.budget_gates")
    ):
        name = _text(row.get("name"), f"result.governance.budget_gates[{index}].name")
        status = _text(
            row.get("status"), f"result.governance.budget_gates[{index}].status"
        )
        overrun = _decimal(
            row.get("overrun_usd"),
            f"result.governance.budget_gates[{index}].overrun_usd",
            optional=True,
        )
        governance_rows.append(("budget gate", name, _money(overrun), status, "Review gated spend."))
    governance_rows.sort(
        key=lambda row: (
            row[0].casefold(),
            row[0],
            row[1].casefold(),
            row[1],
            row[2],
        )
    )

    context = _context(
        result,
        refreshed_at=refreshed_at,
        scope_summary=scope_summary,
        partial=partial,
        warnings=warnings,
        scope_coverage=scope_coverage,
        extra_warnings=(
            ["Run-rate budget forecasts are estimates."] if has_run_rate else []
        ),
    )
    chart_rows = [row for row in rightsizing if row["savings"] is not None][:10]
    monthly_spend = _decimal(
        headline.get("total_monthly_spend"),
        "result.headline.total_monthly_spend",
        optional=True,
    )
    potential_savings = _decimal(
        headline.get("potential_monthly_savings"),
        "result.headline.potential_monthly_savings",
    )
    anomaly_count = _integer(
        headline.get("anomaly_count"), "result.headline.anomaly_count"
    )
    budgets_over = _integer(
        headline.get("budgets_over"), "result.headline.budgets_over"
    )
    budgets_forecast = _integer(
        headline.get("budgets_forecast_over"),
        "result.headline.budgets_forecast_over",
    )
    untagged = _decimal(
        headline.get("untagged_usd"), "result.headline.untagged_usd"
    )

    return _model(
        "FinOps: Cost Optimization",
        "Weekly executive rollup; cost data can lag by roughly one day.",
        context,
        [
            {"label": "Monthly spend", "value": _money(monthly_spend), "detail": "Allocation total"},
            {
                "label": "Potential monthly savings",
                "value": _money(potential_savings),
                "detail": "Rightsizing only",
                "tone": "good",
            },
            {"label": "Anomalies", "value": str(anomaly_count), "detail": "Cost spikes/new spend"},
            {
                "label": "Budget gates",
                "value": str(budgets_over + budgets_forecast),
                "detail": f"{budgets_over} over, {budgets_forecast} forecast over",
                "tone": "critical" if budgets_over + budgets_forecast else "good",
            },
            {"label": "Untagged spend", "value": _money(untagged), "detail": "Governance exposure"},
        ],
        {
            "type": "bar",
            "label": "Estimated monthly savings (USD)",
            "labels": [_short_resource(row["resource_id"]) for row in chart_rows],
            "values": [_chart_number(row["savings"]) for row in chart_rows],
        },
        [
            {
                "title": "Top priorities",
                "description": "Impact types remain separate; values are not added together.",
                "columns": [
                    "Rank",
                    "Category",
                    "Impact type",
                    "Impact",
                    "Title",
                    "Detail",
                    "Action",
                    "Validation",
                ],
                "rows": [
                    [
                        str(row["rank"]),
                        row["category"],
                        row["impact_type"],
                        _money(row["impact"]),
                        row["title"],
                        row["detail"],
                        row["action"],
                        row["validation"],
                    ]
                    for row in priorities
                ],
                "emptyMessage": "No blended priorities were produced.",
            },
            {
                "title": "Rightsizing",
                "description": "Savings opportunities from the rightsizing analysis.",
                "columns": ["Resource", "Kind", "Action", "Savings", "Validation"],
                "rows": [
                    [
                        row["resource_id"],
                        row["kind"],
                        row["action"],
                        _money(row["savings"]),
                        row["validation"],
                    ]
                    for row in rightsizing
                ],
                "emptyMessage": "No rightsizing detail was produced.",
            },
            {
                "title": "Cost anomalies",
                "description": "Anomalies ranked by dollar impact.",
                "columns": ["Kind", "Dimension", "Value", "Current", "Baseline", "Impact"],
                "rows": [
                    [
                        row["kind"],
                        row["dimension"],
                        row["value"],
                        _money(row["current"]),
                        _money(row["baseline"]),
                        _money(row["impact"]),
                    ]
                    for row in anomalies
                ],
                "emptyMessage": "No cost anomalies were produced.",
            },
            {
                "title": "Budget status",
                "description": "Over-budget, forecast-over, and at-risk budgets.",
                "columns": ["Budget", "Status", "Current", "Amount", "Forecast", "Forecast source"],
                "rows": [
                    [
                        row["name"],
                        row["status"],
                        _money(row["current"], row["currency"]),
                        _money(row["amount"], row["currency"]),
                        _money(row["forecast"], row["currency"]),
                        row["forecast_source"],
                    ]
                    for row in budget_rows
                ],
                "emptyMessage": "No budget exceptions were produced.",
            },
            {
                "title": "Governance and policy",
                "description": "Tagging and budget-gate findings; exposure is not claimed as savings.",
                "columns": ["Type", "Target", "Impact", "Detail", "Action"],
                "rows": [list(row) for row in governance_rows],
                "emptyMessage": "No governance findings were produced.",
            },
        ],
    )


def build_ai_spend_model(
    result,
    *,
    refreshed_at,
    scope_summary=None,
    partial=False,
    warnings=None,
    scope_coverage=None,
):
    """Adapt attribute_ai_costs output to the generic report model."""

    result = _object(result, "result")
    total = _decimal(result.get("total_ai_usd"), "result.total_ai_usd")
    resource_count = _integer(result.get("resource_count"), "result.resource_count")
    model_count = _integer(result.get("model_count"), "result.model_count")

    meter_rows = []
    token_spend = Decimal(0)
    compute_spend = Decimal(0)
    for index, row in enumerate(
        _objects(result.get("by_meter_type"), "result.by_meter_type")
    ):
        prefix = f"result.by_meter_type[{index}]"
        meter_type = _text(row.get("meter_type"), f"{prefix}.meter_type")
        cost = _decimal(row.get("monthly_usd"), f"{prefix}.monthly_usd")
        pct = _decimal(row.get("pct"), f"{prefix}.pct", optional=True)
        if meter_type == "model_token":
            token_spend += cost
        elif meter_type == "compute":
            compute_spend += cost
        meter_rows.append((meter_type, cost, pct))
    meter_rows.sort(
        key=lambda row: (-row[1], row[0].casefold(), row[0], _decimal_key(row[2]))
    )

    family_rows = []
    for index, row in enumerate(
        _objects(result.get("by_service_family"), "result.by_service_family")
    ):
        prefix = f"result.by_service_family[{index}]"
        name = _text(row.get("service_family"), f"{prefix}.service_family")
        cost = _decimal(row.get("monthly_usd"), f"{prefix}.monthly_usd")
        pct = _decimal(row.get("pct"), f"{prefix}.pct", optional=True)
        family_rows.append((name, cost, pct))
    family_rows.sort(
        key=lambda row: (-row[1], row[0].casefold(), row[0], _decimal_key(row[2]))
    )

    models = []
    for index, row in enumerate(_objects(result.get("by_model"), "result.by_model")):
        prefix = f"result.by_model[{index}]"
        name = _text(row.get("model"), f"{prefix}.model")
        cost = _decimal(row.get("monthly_usd"), f"{prefix}.monthly_usd")
        pct = _decimal(row.get("pct"), f"{prefix}.pct", optional=True)
        count = _integer(row.get("resource_count"), f"{prefix}.resource_count")
        models.append((name, cost, pct, count))
    models.sort(
        key=lambda row: (
            -row[1],
            row[0].casefold(),
            row[0],
            _decimal_key(row[2]),
            row[3],
        )
    )

    resources = []
    for index, row in enumerate(
        _objects(result.get("by_resource"), "result.by_resource")
    ):
        prefix = f"result.by_resource[{index}]"
        rid = _text(row.get("resourceId"), f"{prefix}.resourceId")
        name = _text(row.get("resourceName"), f"{prefix}.resourceName")
        kind = _text(row.get("kind"), f"{prefix}.kind")
        family = _text(row.get("service_family"), f"{prefix}.service_family")
        cost = _decimal(row.get("monthly_usd"), f"{prefix}.monthly_usd")
        pct = _decimal(row.get("pct"), f"{prefix}.pct", optional=True)
        top_model = _text(row.get("top_model"), f"{prefix}.top_model")
        resources.append((name, rid, kind, family, cost, pct, top_model))
    resources.sort(
        key=lambda row: (
            -row[4],
            row[0].casefold(),
            row[0],
            row[1].casefold(),
            row[1],
            row[2].casefold(),
            row[2],
            row[3].casefold(),
            row[3],
            _decimal_key(row[5]),
            row[6].casefold(),
            row[6],
        )
    )

    drivers = []
    for index, row in enumerate(
        _objects(result.get("top_drivers"), "result.top_drivers")
    ):
        prefix = f"result.top_drivers[{index}]"
        name = _text(row.get("resourceName"), f"{prefix}.resourceName")
        family = _text(row.get("service_family"), f"{prefix}.service_family")
        model = _text(row.get("model"), f"{prefix}.model")
        meter_type = _text(row.get("meter_type"), f"{prefix}.meter_type")
        cost = _decimal(row.get("monthly_usd"), f"{prefix}.monthly_usd")
        drivers.append((name, family, model, meter_type, cost))
    drivers.sort(
        key=lambda row: (
            -row[4],
            row[0].casefold(),
            row[0],
            row[2].casefold(),
            row[2],
            row[3],
            row[1].casefold(),
            row[1],
        )
    )

    hints = []
    for index, row in enumerate(_objects(result.get("hints"), "result.hints")):
        prefix = f"result.hints[{index}]"
        hint_type = _text(row.get("type"), f"{prefix}.type")
        target = _text(row.get("target"), f"{prefix}.target")
        detail = _text(row.get("detail"), f"{prefix}.detail")
        cost = _decimal(row.get("monthly_usd"), f"{prefix}.monthly_usd", optional=True)
        action = "verify first" if hint_type in _VERIFY_HINTS else "review"
        hints.append((hint_type, target, detail, cost, action))
    hints.sort(
        key=lambda row: (
            row[3] is None,
            -(row[3] or Decimal(0)),
            row[0].casefold(),
            row[0],
            row[1].casefold(),
            row[1],
            row[2].casefold(),
            row[2],
            row[4],
        )
    )

    context = _context(
        result,
        refreshed_at=refreshed_at,
        scope_summary=scope_summary,
        partial=partial,
        warnings=warnings,
        scope_coverage=scope_coverage,
    )
    return _model(
        "FinOps: AI Spend",
        "Weekly Azure OpenAI, Foundry, Cognitive Services, and Machine Learning cost snapshot.",
        context,
        [
            {"label": "Total AI spend", "value": _money(total), "detail": "Monthly ActualCost"},
            {"label": "AI resources", "value": str(resource_count), "detail": "Attributed resources"},
            {"label": "Models", "value": str(model_count), "detail": "Token-meter models"},
            {"label": "Model-token spend", "value": _money(token_spend), "detail": "Kept separate from compute"},
            {"label": "Compute spend", "value": _money(compute_spend), "detail": "Endpoints, training, and compute"},
        ],
        {
            "type": "bar",
            "label": "Model-token spend (USD)",
            "labels": [row[0] for row in models[:10]],
            "values": [_chart_number(row[1]) for row in models[:10]],
        },
        [
            {
                "title": "Service and meter split",
                "description": "AI service families and meter types remain separate.",
                "columns": ["Breakdown", "Type", "Cost", "Share"],
                "rows": (
                    [["service family", name, _money(cost), _percent(pct)] for name, cost, pct in family_rows]
                    + [["meter type", name, _money(cost), _percent(pct)] for name, cost, pct in meter_rows]
                ),
                "emptyMessage": "No AI service or meter breakdown was produced.",
            },
            {
                "title": "By model",
                "description": "Model-token spend ranked by cost.",
                "columns": ["Model", "Cost", "Share", "Resources"],
                "rows": [
                    [name, _money(cost), _percent(pct), str(count)]
                    for name, cost, pct, count in models
                ],
                "emptyMessage": "No model-token spend was found.",
            },
            {
                "title": "By resource",
                "description": "AI resources ranked by attributed cost.",
                "columns": ["Resource", "Resource ID", "Kind", "Service family", "Cost", "Share", "Top model"],
                "rows": [
                    [name, rid, kind, family, _money(cost), _percent(pct), top_model]
                    for name, rid, kind, family, cost, pct, top_model in resources
                ],
                "emptyMessage": "No AI resource spend was found.",
            },
            {
                "title": "Top cost drivers",
                "description": "Largest resource/model/meter combinations.",
                "columns": ["Resource", "Service family", "Model", "Meter type", "Cost"],
                "rows": [
                    [name, family, model, meter_type, _money(cost)]
                    for name, family, model, meter_type, cost in drivers
                ],
                "emptyMessage": "No AI cost drivers were produced.",
            },
            {
                "title": "Where to look first",
                "description": "Read-only optimization hints; marked items require verification.",
                "columns": ["Type", "Target", "Detail", "Cost", "Action"],
                "rows": [
                    [hint_type, target, detail, _money(cost), action]
                    for hint_type, target, detail, cost, action in hints
                ],
                "emptyMessage": (
                    "No AI spend was found in the supplied managed scopes."
                    if total == 0
                    else "No AI optimization hints were produced."
                ),
            },
        ],
    )


def build_cost_vs_reliability_model(
    result,
    *,
    refreshed_at,
    scope_summary=None,
    partial=False,
    warnings=None,
    scope_coverage=None,
):
    """Adapt analyze_cost_vs_reliability output to the generic report model."""

    result = _object(result, "result")
    coverage = _object(result.get("coverage") or {}, "result.coverage")
    quality = _object(result.get("data_quality") or {}, "result.data_quality")
    total = _decimal(result.get("total_usd"), "result.total_usd")
    resource_count = _integer(result.get("resource_count"), "result.resource_count")
    signal_count = _integer(
        result.get("reliability_signal_count"), "result.reliability_signal_count"
    )
    cost_count = _integer(
        coverage.get("cost_resource_count"), "result.coverage.cost_resource_count"
    )
    joined_count = _integer(
        coverage.get("joined_resource_count"), "result.coverage.joined_resource_count"
    )
    join_pct = (
        Decimal(joined_count) * Decimal(100) / Decimal(cost_count)
        if cost_count
        else None
    )

    resources = []
    for index, row in enumerate(
        _objects(result.get("by_resource"), "result.by_resource")
    ):
        prefix = f"result.by_resource[{index}]"
        name = _text(row.get("resourceName"), f"{prefix}.resourceName")
        rid = _text(row.get("resourceId"), f"{prefix}.resourceId")
        group = _text(row.get("resourceGroup"), f"{prefix}.resourceGroup")
        service = _text(row.get("service"), f"{prefix}.service")
        cost = _decimal(row.get("monthly_usd"), f"{prefix}.monthly_usd")
        alerts = _integer(row.get("alert_count"), f"{prefix}.alert_count")
        severities = [
            _integer(row.get(f"sev{level}"), f"{prefix}.sev{level}")
            for level in range(5)
        ]
        health = _integer(
            row.get("health_event_count"), f"{prefix}.health_event_count"
        )
        advisor = _integer(
            row.get("advisor_reliability_count"),
            f"{prefix}.advisor_reliability_count",
        )
        score = _decimal(row.get("reliability_score"), f"{prefix}.reliability_score")
        pain = _decimal(
            row.get("pain_per_1000_usd"),
            f"{prefix}.pain_per_1000_usd",
            optional=True,
        )
        risk = _text(row.get("risk_band"), f"{prefix}.risk_band")
        primary = _text(row.get("primary_signal"), f"{prefix}.primary_signal")
        resources.append(
            (
                name,
                rid,
                group,
                service,
                cost,
                alerts,
                severities,
                health,
                advisor,
                score,
                pain,
                risk,
                primary,
            )
        )
    resources.sort(
        key=lambda row: (
            -row[9],
            -row[4],
            row[0].casefold(),
            row[0],
            row[1].casefold(),
            row[1],
            row[2].casefold(),
            row[2],
            row[3].casefold(),
            row[3],
            row[5],
            row[6],
            row[7],
            row[8],
            _decimal_key(row[10]),
            row[11],
            row[12],
        )
    )

    services = []
    for index, row in enumerate(
        _objects(result.get("by_service"), "result.by_service")
    ):
        prefix = f"result.by_service[{index}]"
        service = _text(row.get("service"), f"{prefix}.service")
        cost = _decimal(row.get("monthly_usd"), f"{prefix}.monthly_usd")
        count = _integer(row.get("resource_count"), f"{prefix}.resource_count")
        alerts = _integer(row.get("alert_count"), f"{prefix}.alert_count")
        health = _integer(
            row.get("health_event_count"), f"{prefix}.health_event_count"
        )
        advisor = _integer(
            row.get("advisor_reliability_count"),
            f"{prefix}.advisor_reliability_count",
        )
        score = _decimal(row.get("reliability_score"), f"{prefix}.reliability_score")
        risk = _text(row.get("risk_band"), f"{prefix}.risk_band")
        services.append((service, cost, count, alerts, health, advisor, score, risk))
    services.sort(
        key=lambda row: (
            -row[6],
            -row[1],
            row[0].casefold(),
            row[0],
            row[2],
            row[3],
            row[4],
            row[5],
            row[7],
        )
    )

    hints = []
    for index, row in enumerate(_objects(result.get("hints"), "result.hints")):
        prefix = f"result.hints[{index}]"
        hint_type = _text(row.get("type"), f"{prefix}.type")
        target = _text(row.get("target"), f"{prefix}.target")
        detail = _text(row.get("detail"), f"{prefix}.detail")
        cost = _decimal(row.get("monthly_usd"), f"{prefix}.monthly_usd", optional=True)
        score = _decimal(
            row.get("reliability_score"),
            f"{prefix}.reliability_score",
            optional=True,
        )
        hints.append((hint_type, target, detail, cost, score))
    hints.sort(
        key=lambda row: (
            row[4] is None,
            -(row[4] or Decimal(0)),
            row[3] is None,
            -(row[3] or Decimal(0)),
            row[0].casefold(),
            row[0],
            row[1].casefold(),
            row[1],
            row[2].casefold(),
            row[2],
        )
    )
    investment = [row for row in hints if row[0] == "high_incident_low_spend"]
    verify = [row for row in hints if row[0] == "high_spend_no_pain"]

    unmatched = []
    for index, row in enumerate(
        _objects(result.get("unmatched_reliability"), "result.unmatched_reliability")
    ):
        prefix = f"result.unmatched_reliability[{index}]"
        unmatched.append(
            (
                _text(row.get("resourceName"), f"{prefix}.resourceName"),
                _text(row.get("resourceId"), f"{prefix}.resourceId"),
                _text(row.get("signal_type"), f"{prefix}.signal_type"),
                _text(row.get("severity"), f"{prefix}.severity"),
                _decimal(row.get("score"), f"{prefix}.score"),
                _text(row.get("date"), f"{prefix}.date"),
                _text(row.get("detail"), f"{prefix}.detail"),
            )
        )
    unmatched.sort(
        key=lambda row: (
            -row[4],
            row[0].casefold(),
            row[0],
            row[2].casefold(),
            row[2],
            row[5],
            row[1].casefold(),
            row[1],
            row[3].casefold(),
            row[3],
            row[6].casefold(),
            row[6],
        )
    )

    sources = _object(quality.get("sources_used") or {}, "result.data_quality.sources_used")
    source_rows = []
    for name in sorted(sources, key=lambda item: (item.casefold(), item)):
        source_name = _text(name, "result.data_quality.sources_used key")
        used = _boolean(sources[name], f"result.data_quality.sources_used.{name}")
        source_rows.append(["source", source_name, "used" if used else "not available"])
    coverage_rows = [
        ["coverage", "Cost resources", str(cost_count)],
        [
            "coverage",
            "Reliability resources",
            str(
                _integer(
                    coverage.get("reliability_resource_count"),
                    "result.coverage.reliability_resource_count",
                )
            ),
        ],
        ["coverage", "Joined resources", str(joined_count)],
        [
            "coverage",
            "Unmatched reliability signals",
            str(
                _integer(
                    coverage.get("unmatched_reliability_count"),
                    "result.coverage.unmatched_reliability_count",
                )
            ),
        ],
        [
            "coverage",
            "Subscription-level events",
            str(
                _integer(
                    coverage.get("subscription_level_event_count"),
                    "result.coverage.subscription_level_event_count",
                )
            ),
        ],
    ]
    limitations = _stable_strings(
        _strings(quality.get("limitations"), "result.data_quality.limitations")
    )
    quality_rows = coverage_rows + source_rows + [
        ["limitation", str(index), limitation]
        for index, limitation in enumerate(limitations, start=1)
    ]

    generated = [
        "Cost data can lag by roughly one day.",
        "Weighted alert counts are not a complete incident system or duration measure.",
    ]
    cost_partial = _boolean(
        quality.get("cost_partial"), "result.data_quality.cost_partial"
    )
    context = _context(
        result,
        refreshed_at=refreshed_at,
        scope_summary=scope_summary,
        partial=partial,
        warnings=warnings,
        scope_coverage=scope_coverage,
        extra_partial=cost_partial,
        extra_warnings=generated,
    )
    chart_rows = resources[:10]
    return _model(
        "FinOps: Cost vs Reliability",
        "Weekly view of Azure spend against transparent weighted reliability signals.",
        context,
        [
            {"label": "Monthly spend", "value": _money(total), "detail": "ActualCost"},
            {"label": "Costed resources", "value": str(resource_count), "detail": "UsageDetails resources"},
            {"label": "Reliability signals", "value": str(signal_count), "detail": "Alerts, health, and Advisor HA"},
            {
                "label": "Join coverage",
                "value": _percent(join_pct),
                "detail": f"{joined_count} of {cost_count} costed resources",
                "tone": "warning" if join_pct is not None and join_pct < 100 else "good",
            },
        ],
        {
            "type": "bar",
            "label": "Reliability score",
            "labels": [f"{row[0]} ({_money(row[4])})" for row in chart_rows],
            "values": [_chart_number(row[9]) for row in chart_rows],
        },
        [
            {
                "title": "Spend and pain",
                "description": "Resources ranked by reliability score, then monthly cost.",
                "columns": [
                    "Resource",
                    "Resource ID",
                    "Resource group",
                    "Service",
                    "Monthly cost",
                    "Alerts",
                    "Sev0",
                    "Sev1",
                    "Sev2",
                    "Sev3",
                    "Sev4",
                    "Health events",
                    "Advisor HA",
                    "Reliability score",
                    "Pain per $1K",
                    "Risk",
                    "Primary signal",
                ],
                "rows": [
                    [
                        row[0],
                        row[1],
                        row[2],
                        row[3],
                        _money(row[4]),
                        str(row[5]),
                        *[str(value) for value in row[6]],
                        str(row[7]),
                        str(row[8]),
                        _number(row[9]),
                        _number(row[10]),
                        row[11],
                        row[12],
                    ]
                    for row in resources
                ],
                "emptyMessage": "No costed resource rows were produced.",
            },
            {
                "title": "Service rollup",
                "description": "Cost and reliability signals aggregated by service.",
                "columns": ["Service", "Cost", "Resources", "Alerts", "Health events", "Advisor HA", "Score", "Risk"],
                "rows": [
                    [
                        row[0],
                        _money(row[1]),
                        str(row[2]),
                        str(row[3]),
                        str(row[4]),
                        str(row[5]),
                        _number(row[6]),
                        row[7],
                    ]
                    for row in services
                ],
                "emptyMessage": "No service reliability rollup was produced.",
            },
            {
                "title": "Reliability investment candidates",
                "description": "High-pain, low-spend resources where resilience may matter more than cost cutting.",
                "columns": ["Type", "Target", "Detail", "Monthly cost", "Score"],
                "rows": [
                    [row[0], row[1], row[2], _money(row[3]), _number(row[4])]
                    for row in investment
                ],
                "emptyMessage": "No high-pain, low-spend investment candidates were produced.",
            },
            {
                "title": "Verify before cutting",
                "description": "High-spend resources with no pain in the available signals.",
                "columns": ["Type", "Target", "Detail", "Monthly cost", "Score"],
                "rows": [
                    [row[0], row[1], row[2], _money(row[3]), _number(row[4])]
                    for row in verify
                ],
                "emptyMessage": "No high-spend/no-pain verification candidates were produced.",
            },
            {
                "title": "Unmatched reliability signals",
                "description": "Signals that did not join to a UsageDetails resource ID.",
                "columns": ["Resource", "Resource ID", "Signal", "Severity", "Score", "Date", "Detail"],
                "rows": [
                    [row[0], row[1], row[2], row[3], _number(row[4]), row[5], row[6]]
                    for row in unmatched
                ],
                "emptyMessage": "All resource-level reliability signals joined to cost.",
            },
            {
                "title": "Data quality",
                "description": "Coverage, source availability, and known limitations.",
                "columns": ["Type", "Item", "Value"],
                "rows": quality_rows,
                "emptyMessage": "No data-quality details were supplied.",
            },
        ],
    )


__all__ = [
    "build_ai_spend_model",
    "build_budget_status_model",
    "build_cost_optimization_model",
    "build_cost_overview_model",
    "build_cost_vs_reliability_model",
    "build_rightsizing_savings_model",
]
