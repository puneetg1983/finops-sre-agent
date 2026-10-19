"""FinOps cost vs reliability — join Azure spend to read-only reliability pain signals.

Pure, offline, deterministic (no Azure calls). Given monthly Consumption UsageDetails line
items plus reliability signals already pulled with GET-only Azure APIs (Alerts Management,
Resource Health, Advisor HighAvailability, and optional Activity Log ResourceHealth events), it
ranks resources where spend and operational pain intersect.

The skill is deliberately **advisory**: alerts are treated as the primary pain driver, with
Resource Health and Advisor HighAvailability as secondary signals. It never recommends cutting a
resource only because it costs money; high-spend/no-pain rows are flagged to **verify before
rightsizing**, while high-pain/low-spend rows are flagged as candidates for reliability
investment.

v1 uses transparent weighted counts only. Duration, SLO/error-budget math, KQL incident joins,
metric-rate normalization, and causal analysis are deferred to v2 so the offline core stays small,
testable, and explainable.
"""

from collections import defaultdict

SEV_WEIGHTS = {"Sev0": 8, "Sev1": 5, "Sev2": 3, "Sev3": 1, "Sev4": 0.5}
UNKNOWN_SEVERITY_WEIGHT = 2
HEALTH_EVENT_WEIGHT = 4
ADVISOR_HA_WEIGHT = 2

RISK_HIGH = 10
RISK_MEDIUM = 5
RISK_LOW = 0

HIGH_PAIN_LOW_SPEND_SCORE = RISK_HIGH
LOW_SPEND_USD = 100.0
HIGH_SPEND_USD = 1000.0
_TOP_DRIVERS = 10

_BAD_HEALTH_STATES = {"unavailable", "degraded"}


def _norm(text) -> str:
    return str(text or "").strip().lower()


def _key(resource_id) -> str:
    """Case-insensitive resource-id key (ARM ids are case-insensitive)."""
    return _norm(resource_id)


def _resource_name(resource_id) -> str:
    """Last ARM path segment (the resource name), else the raw id."""
    rid = str(resource_id or "").strip()
    return rid.rsplit("/", 1)[-1] if "/" in rid else rid


def _resource_group(resource_id) -> str:
    parts = str(resource_id or "").strip("/").split("/")
    for i, part in enumerate(parts):
        if part.lower() == "resourcegroups" and i + 1 < len(parts):
            return parts[i + 1]
    return ""


def _provider_service(resource_id) -> str:
    parts = str(resource_id or "").strip("/").split("/")
    for i, part in enumerate(parts):
        if part.lower() == "providers" and i + 1 < len(parts):
            return parts[i + 1]
    return ""


def _get(obj, path, default=None):
    cur = obj or {}
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur.get(part)
    return cur


def _first(obj, *paths):
    for path in paths:
        value = _get(obj, path) if "." in path else (obj or {}).get(path)
        if value not in (None, ""):
            return value
    return None


def _cost(item):
    for path in ("cost", "costInUSD", "pretaxCost", "properties.cost", "properties.costInUSD", "properties.pretaxCost"):
        value = _get(item, path) if "." in path else (item or {}).get(path)
        if isinstance(value, (int, float)):
            return float(value)
        try:
            if value not in (None, ""):
                return float(value)
        except (TypeError, ValueError):
            pass
    return None


def _cost_resource_id(item) -> str:
    return str(_first(item, "resourceId", "instanceName", "properties.resourceId", "properties.instanceName") or "").strip()


def _signal_resource_id(item) -> str:
    rid = _first(
        item,
        "resourceId",
        "targetResourceId",
        "properties.resourceId",
        "properties.targetResourceId",
        "properties.impactedResourceId",
        "properties.resourceMetadata.resourceId",
        "resourceMetadata.resourceId",
        "impactedValue",
        "properties.impactedValue",
    )
    rid = str(rid or "").strip()
    if "/providers/microsoft.resourcehealth/availabilitystatuses/" in rid.lower():
        return rid[:rid.lower().find("/providers/microsoft.resourcehealth/availabilitystatuses/")]
    return rid if rid.startswith("/") else ""


def _alert_targets(alert):
    targets = _first(alert, "properties.essentials.alertTargetIDs", "essentials.alertTargetIDs", "alertTargetIDs")
    if isinstance(targets, list):
        return [str(t).strip() for t in targets if str(t or "").strip()]
    rid = _signal_resource_id(alert)
    return [rid] if rid else []


def _severity(alert) -> str:
    raw = str(_first(alert, "severity", "properties.essentials.severity", "essentials.severity") or "").strip()
    if not raw:
        return "Unknown"
    low = raw.lower()
    if low.startswith("sev"):
        return "Sev" + raw[3:].strip()
    if low.isdigit():
        return "Sev" + raw
    return raw


def _severity_weight(severity) -> float:
    return SEV_WEIGHTS.get(str(severity or ""), UNKNOWN_SEVERITY_WEIGHT)


def _date(item):
    value = _first(
        item,
        "date",
        "usageDate",
        "properties.date",
        "startDateTime",
        "properties.essentials.startDateTime",
        "properties.occurredTime",
        "eventTimestamp",
        "properties.eventTimestamp",
        "properties.lastUpdated",
        "lastUpdated",
    )
    return str(value) if value not in (None, "") else None


def _service(item, resource_id) -> str:
    value = _first(item, "service", "consumedService", "meterCategory", "serviceName", "properties.consumedService", "properties.meterCategory")
    if value:
        return str(value)
    return _provider_service(resource_id)


def _pct(part, whole) -> float:
    if not whole:
        return 0.0
    return round(100.0 * part / whole, 1)


def _risk_band(score) -> str:
    if score >= RISK_HIGH:
        return "high"
    if score >= RISK_MEDIUM:
        return "medium"
    if score > RISK_LOW:
        return "low"
    return "none"


def _primary_signal(alert_score, health_score, advisor_score) -> str:
    if alert_score <= 0 and health_score <= 0 and advisor_score <= 0:
        return "none"
    if alert_score >= health_score and alert_score >= advisor_score:
        return "alert"
    if health_score >= advisor_score:
        return "health"
    return "advisor"


def _is_bad_health(event) -> bool:
    state = _first(event, "availabilityState", "properties.availabilityState", "status", "properties.status")
    if isinstance(state, dict):
        state = state.get("value")
    return _norm(state) in _BAD_HEALTH_STATES


def _is_advisor_ha(rec) -> bool:
    category = _first(rec, "category", "properties.category")
    if isinstance(category, dict):
        category = category.get("value")
    return _norm(category) == "highavailability"


def _is_cost_partial(line_items) -> bool:
    for item in line_items:
        if bool(_first(item, "cost_partial", "costPartial", "partial", "truncated")):
            return True
    return False


def analyze_cost_vs_reliability(
    line_items=None,
    alerts=None,
    health_events=None,
    advisor_recommendations=None,
    *,
    top_n_resources=25,
    top_n_services=15,
):
    """Join monthly cost to reliability signals and rank resources by pain then spend.

    Returns a dict with headline totals, per-resource rankings, per-service rollups, high-priority
    drivers, advisory hints, unmatched reliability signals, and data-quality notes. Inputs are plain
    lists already fetched by the skill procedure; this function performs no I/O and never mutates them.
    """
    line_items = list(line_items or [])
    alerts = list(alerts or [])
    health_events = list(health_events or [])
    advisor_recommendations = list(advisor_recommendations or [])

    total = 0.0
    as_of = None
    cost_by_id = defaultdict(float)
    meta_by_id = {}
    cost_keys = set()

    for item in line_items:
        cost = _cost(item)
        if cost is None:
            continue
        total += cost
        date = _date(item)
        if date and (as_of is None or date > as_of):
            as_of = date

        rid = _cost_resource_id(item)
        if not rid:
            continue
        key = _key(rid)
        cost_keys.add(key)
        cost_by_id[key] += cost
        meta_by_id.setdefault(key, {
            "resourceId": rid,
            "resourceName": _resource_name(rid),
            "resourceGroup": _resource_group(rid),
            "service": _service(item, rid),
        })

    stats = defaultdict(lambda: {
        "alert_count": 0,
        "sev_counts": defaultdict(int),
        "health_event_count": 0,
        "advisor_reliability_count": 0,
        "alert_score": 0.0,
        "health_score": 0.0,
        "advisor_score": 0.0,
    })
    reliability_keys = set()
    unmatched = []
    subscription_level_event_count = 0
    reliability_signal_count = 0

    def record_signal(rid, signal_type, score, *, severity="", date=None, detail=""):
        nonlocal subscription_level_event_count, reliability_signal_count, as_of
        reliability_signal_count += 1
        if date and (as_of is None or str(date) > as_of):
            as_of = str(date)
        if not rid:
            subscription_level_event_count += 1
            return
        key = _key(rid)
        reliability_keys.add(key)
        if key not in cost_keys:
            unmatched.append({
                "resourceId": rid,
                "resourceName": _resource_name(rid),
                "signal_type": signal_type,
                "severity": severity,
                "score": score,
                "date": date,
                "detail": detail,
            })
            return
        row = stats[key]
        if signal_type == "alert":
            row["alert_count"] += 1
            row["sev_counts"][severity] += 1
            row["alert_score"] += score
        elif signal_type == "health":
            row["health_event_count"] += 1
            row["health_score"] += score
        elif signal_type == "advisor":
            row["advisor_reliability_count"] += 1
            row["advisor_score"] += score

    for alert in alerts:
        sev = _severity(alert)
        score = _severity_weight(sev)
        targets = _alert_targets(alert)
        detail = str(_first(alert, "name", "properties.essentials.alertRule", "essentials.alertRule") or "")
        if not targets:
            record_signal("", "alert", score, severity=sev, date=_date(alert), detail=detail)
            continue
        for rid in targets:
            record_signal(rid, "alert", score, severity=sev, date=_date(alert), detail=detail)

    for event in health_events:
        if not _is_bad_health(event):
            continue
        state = _first(event, "availabilityState", "properties.availabilityState", "status", "properties.status")
        if isinstance(state, dict):
            state = state.get("value")
        record_signal(
            _signal_resource_id(event),
            "health",
            HEALTH_EVENT_WEIGHT,
            date=_date(event),
            detail=str(state or "Resource Health"),
        )

    for rec in advisor_recommendations:
        if not _is_advisor_ha(rec):
            continue
        detail = str(_first(rec, "shortDescription.problem", "properties.shortDescription.problem", "recommendation", "name") or "HighAvailability")
        record_signal(_signal_resource_id(rec), "advisor", ADVISOR_HA_WEIGHT, date=_date(rec), detail=detail)

    by_resource = []
    for key, monthly in cost_by_id.items():
        meta = meta_by_id[key]
        row_stats = stats[key]
        alert_score = row_stats["alert_score"]
        health_score = row_stats["health_score"]
        advisor_score = row_stats["advisor_score"]
        score = alert_score + health_score + advisor_score
        pain = None if monthly == 0 else round(score / (monthly / 1000.0), 2)
        sev_counts = {sev: row_stats["sev_counts"].get(sev, 0) for sev in SEV_WEIGHTS}
        by_resource.append({
            "resourceId": meta["resourceId"],
            "resourceName": meta["resourceName"],
            "resourceGroup": meta["resourceGroup"],
            "service": meta["service"],
            "monthly_usd": round(monthly, 2),
            "pct": _pct(monthly, total),
            "alert_count": row_stats["alert_count"],
            "sev_counts": sev_counts,
            "sev0": sev_counts["Sev0"],
            "sev1": sev_counts["Sev1"],
            "sev2": sev_counts["Sev2"],
            "sev3": sev_counts["Sev3"],
            "sev4": sev_counts["Sev4"],
            "health_event_count": row_stats["health_event_count"],
            "advisor_reliability_count": row_stats["advisor_reliability_count"],
            "reliability_score": round(score, 2),
            "pain_per_1000_usd": pain,
            "risk_band": _risk_band(score),
            "primary_signal": _primary_signal(alert_score, health_score, advisor_score),
        })

    by_resource.sort(key=lambda r: (r["reliability_score"], r["monthly_usd"]), reverse=True)
    ranked_resources = by_resource[:top_n_resources]

    service_rows = {}
    for row in by_resource:
        svc = row["service"] or "unknown"
        out = service_rows.setdefault(svc, {
            "service": svc,
            "monthly_usd": 0.0,
            "resource_count": 0,
            "alert_count": 0,
            "health_event_count": 0,
            "advisor_reliability_count": 0,
            "reliability_score": 0.0,
        })
        out["monthly_usd"] += row["monthly_usd"]
        out["resource_count"] += 1
        out["alert_count"] += row["alert_count"]
        out["health_event_count"] += row["health_event_count"]
        out["advisor_reliability_count"] += row["advisor_reliability_count"]
        out["reliability_score"] += row["reliability_score"]

    by_service = []
    for row in service_rows.values():
        row["monthly_usd"] = round(row["monthly_usd"], 2)
        row["reliability_score"] = round(row["reliability_score"], 2)
        row["pct"] = _pct(row["monthly_usd"], total)
        row["risk_band"] = _risk_band(row["reliability_score"])
        by_service.append(row)
    by_service.sort(key=lambda r: (r["reliability_score"], r["monthly_usd"]), reverse=True)

    top_drivers = []
    for row in by_resource:
        if row["reliability_score"] <= 0:
            continue
        top_drivers.append({
            "type": "high_spend_high_pain",
            "target": row["resourceName"],
            "resourceId": row["resourceId"],
            "service": row["service"],
            "monthly_usd": row["monthly_usd"],
            "reliability_score": row["reliability_score"],
            "risk_band": row["risk_band"],
            "primary_signal": row["primary_signal"],
        })
    top_drivers = top_drivers[:_TOP_DRIVERS]

    hints = _build_hints(by_resource, unmatched)

    return {
        "as_of": as_of,
        "total_usd": round(total, 2),
        "resource_count": len(cost_by_id),
        "reliability_signal_count": reliability_signal_count,
        "coverage": {
            "cost_resource_count": len(cost_keys),
            "reliability_resource_count": len(reliability_keys),
            "joined_resource_count": len(cost_keys & reliability_keys),
            "unmatched_reliability_count": len(unmatched),
            "subscription_level_event_count": subscription_level_event_count,
        },
        "by_resource": ranked_resources,
        "by_service": by_service[:top_n_services],
        "top_drivers": top_drivers,
        "hints": hints,
        "unmatched_reliability": unmatched,
        "data_quality": {
            "cost_partial": _is_cost_partial(line_items),
            "sources_used": {
                "usage_details": bool(line_items),
                "alerts": bool(alerts),
                "resource_health": bool(health_events),
                "advisor_highavailability": bool(advisor_recommendations),
            },
            "limitations": [
                "Cost comes from Consumption UsageDetails GET only; Cost Management Query POST is unavailable.",
                "Alerts are weighted counts, not a complete incident system or duration measure.",
                "Service Health and subscription/region-scoped events may not join to resource cost.",
                "Advisor HighAvailability is current-state guidance, not historical reliability pain.",
                "Metrics, KQL, SLO/error-budget, and duration-based scoring are deferred to v2.",
            ],
        },
    }


def _build_hints(by_resource, unmatched):
    hints = []

    for row in by_resource:
        if row["reliability_score"] >= HIGH_PAIN_LOW_SPEND_SCORE and row["monthly_usd"] < LOW_SPEND_USD:
            hints.append({
                "type": "high_incident_low_spend",
                "target": row["resourceName"],
                "detail": f"{row['resourceName']} has high reliability pain but low spend — consider HA/resilience investment before optimizing cost.",
                "monthly_usd": row["monthly_usd"],
                "reliability_score": row["reliability_score"],
            })

    for row in by_resource:
        if row["monthly_usd"] >= HIGH_SPEND_USD and row["reliability_score"] == 0:
            hints.append({
                "type": "high_spend_no_pain",
                "target": row["resourceName"],
                "detail": f"{row['resourceName']} is high spend with no reliability pain in these signals — verify utilization and business criticality before rightsizing.",
                "monthly_usd": row["monthly_usd"],
                "reliability_score": row["reliability_score"],
            })

    if unmatched:
        total_score = sum(item.get("score", 0) for item in unmatched)
        hints.append({
            "type": "unmatched_reliability",
            "target": f"{len(unmatched)} signals",
            "detail": "Reliability signals did not join to UsageDetails resource cost — check resource id projection, deleted resources, or subscription/region-scoped events.",
            "monthly_usd": 0.0,
            "reliability_score": round(total_score, 2),
        })

    hints.sort(key=lambda h: (h["reliability_score"], h["monthly_usd"]), reverse=True)
    return hints
