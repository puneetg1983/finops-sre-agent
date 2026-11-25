"""Turn FinOps evidence into a ranked decision briefing.

Pure, offline and deterministic. The scheduled task gathers evidence with read-only
tools, then calls build_briefing(). Every finding answers four questions: what it
costs, why it happened (with evidence), who owns it, and what to do next. Next
actions are prompts for the SRE Agent chat; every change still needs human approval.
"""

from decimal import Decimal
from hashlib import sha256
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from datetime import date, datetime, timedelta

_spec = spec_from_file_location("finops_briefing_models", Path(__file__).with_name("models.py"))
_models = module_from_spec(_spec)
_spec.loader.exec_module(_models)

JOBS = (
    ("spike", "Explain cost spikes", "Why spend jumped, matched to changes, deployments and incidents."),
    ("waste", "Weekly waste review", "Savings grouped by owner, so each team gets one list."),
    ("tags", "Tag fixes & chargeback", "Untagged spend with proposed owners, applied in one approved batch."),
    ("orphan", "Orphaned & idle resources", "Resources that bill but do no work; owners confirm before deletion."),
    ("tier", "Tier & size changes", "Advisor-backed SKU changes, proposed as a pull request or approved change."),
    ("agent-cost", "What the agent cost", "This agent's own spend compared with the savings it found."),
)
_JOB_LABELS = {key: label for key, label, _ in JOBS}
_INPUT_FOR_JOB = {"spike": "anomalies", "waste": "rightsizing", "tags": "tag_gaps",
                  "orphan": "rightsizing", "tier": "rightsizing", "agent-cost": "agent_cost"}
_CHANGE_KINDS = {"scale", "sku", "create", "deployment", "config", "commit", "incident"}
_DIRECT_COST_KINDS = {"scale", "sku", "create"}
_CONFIDENCE_ORDER = {"High": 0, "Medium": 1, "Low": 2}
_LOOKBACK_DAYS = 2
_MAX_FINDINGS = 25
_APPROVAL = ("Show me the exact change first and do not make any change until I approve it. "
             "Stay within the dashboard's scope.")


def _count(number, noun):
    return f"{number} {noun}" + ("" if number == 1 else "s")


def _id(*parts):
    return "F-" + sha256("|".join(str(part).casefold() for part in parts).encode()).hexdigest()[:8]


def _usd(value, field, optional=False):
    return _models._decimal(value, field, optional=optional)


def _short(resource_id):
    return _models._short_resource(resource_id)


def _resource_group(resource_id):
    parts = str(resource_id or "").split("/")
    lowered = [part.casefold() for part in parts]
    if "resourcegroups" in lowered:
        index = lowered.index("resourcegroups")
        if index + 1 < len(parts):
            return parts[index + 1]
    return ""


def _day(value, field):
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO date") from exc


def _unavailable(value):
    return isinstance(value, dict) and value.get("status") == "unavailable"


def _owner_index(owners):
    index = []
    for row in _models._objects(owners, "owners"):
        scope = _models._text(row.get("scope"), "owners.scope").strip().rstrip("/")
        owner = _models._text(row.get("owner"), "owners.owner").strip()
        if not scope or not owner:
            raise ValueError("owners rows require scope and owner")
        source = _models._text(row.get("source"), "owners.source", default="tag").strip() or "tag"
        index.append((scope.casefold(), owner, source))
    index.sort(key=lambda item: -len(item[0]))
    return index


def _owner_for(index, resource_id):
    key = str(resource_id or "").rstrip("/").casefold()
    for scope, owner, source in index:
        if key == scope or key.startswith(scope + "/"):
            return owner, source
    return "Unassigned", "No owner tag or deployment record found"


def _action(label, prompt, approval=True):
    return {"label": label, "prompt": prompt + (" " + _APPROVAL if approval else ""),
            "approval": "Needs your approval" if approval else "Read-only"}


def _change_matches(anomaly, event):
    dimension, value = anomaly["dimension"], str(anomaly["value"] or "").casefold()
    resource = event["resourceId"].casefold()
    if dimension == "resourceId":
        return resource == value or resource.startswith(value + "/") or value.startswith(resource + "/"), True
    if dimension == "resourceGroup":
        return _resource_group(event["resourceId"]).casefold() == value, True
    if dimension == "meterCategory":
        return event["service"].casefold() == value, False
    return False, False


def _events(change_events):
    events = []
    for index, row in enumerate(_models._objects(change_events, "change_events")):
        kind = _models._text(row.get("kind"), "change_events.kind").strip().casefold()
        if kind not in _CHANGE_KINDS:
            raise ValueError(f"change_events[{index}].kind must be one of " + ", ".join(sorted(_CHANGE_KINDS)))
        summary = _models._text(row.get("summary"), "change_events.summary").strip()
        resource = _models._text(row.get("resourceId"), "change_events.resourceId").strip()
        if not summary or not resource:
            raise ValueError(f"change_events[{index}] requires resourceId and summary")
        events.append({
            "time": _models._text(row.get("time"), "change_events.time"),
            "day": _day(row.get("time"), "change_events.time"),
            "kind": kind, "summary": summary, "resourceId": resource,
            "service": _models._text(row.get("service"), "change_events.service").strip(),
            "actor": _models._text(row.get("actor"), "change_events.actor").strip(),
            "reference": _models._text(row.get("reference"), "change_events.reference").strip(),
        })
    return sorted(events, key=lambda item: (item["time"], item["resourceId"].casefold(), item["summary"]))


def explain_spike(anomaly, change_events):
    """Return (confidence, why, evidence, cause) for one detect_anomalies() row."""
    anomaly = _models._object(anomaly, "anomaly")
    spike_day = _day(anomaly.get("date"), "anomaly.date")
    events = change_events if change_events and "day" in change_events[0] else _events(change_events)
    matched = []
    for event in events:
        if not spike_day - timedelta(days=_LOOKBACK_DAYS) <= event["day"] <= spike_day:
            continue
        hit, precise = _change_matches(anomaly, event)
        if hit:
            matched.append((event, precise))
    current = _usd(anomaly.get("current_usd"), "current_usd")
    baseline = _usd(anomaly.get("baseline_mean_usd"), "baseline_mean_usd")
    evidence = [f"{_models._money(current)} on {spike_day.isoformat()} versus a "
                f"{_models._money(baseline)} daily average."]
    for event, _ in matched:
        who = f" by {event['actor']}" if event["actor"] else ""
        ref = f" ({event['reference']})" if event["reference"] else ""
        evidence.append(f"{event['time'][:16].replace('T', ' ')} {event['kind']}: {event['summary']}{who}{ref}")
    if not matched:
        return ("Low", "No scale, SKU, deployment, configuration, commit or incident record matched this "
                f"scope in the {_LOOKBACK_DAYS} days before the spike. Likely usage growth, a new meter or a "
                "price change; check usage metrics next.", evidence, None)
    direct = [event for event, precise in matched if event["kind"] in _DIRECT_COST_KINDS and precise]
    cause = (direct or [event for event, _ in matched])[-1]
    confidence = "High" if direct else ("Medium" if any(precise for _, precise in matched) else "Low")
    why = (f"Spend rose after a {cause['kind']} change on {_short(cause['resourceId'])}: {cause['summary']}."
           if cause["kind"] != "incident" else
           f"Spend rose during an incident on {_short(cause['resourceId'])}: {cause['summary']}. "
           "Retries or emergency scale-out can raise cost.")
    if len(matched) > 1:
        why += f" {_count(len(matched) - 1, 'other change')} in the same window {'is' if len(matched) == 2 else 'are'} listed as evidence."
    return confidence, why, evidence, cause


def _spike_findings(anomalies, events, owners):
    findings = []
    for row in _models._objects(anomalies, "anomalies"):
        if row.get("kind") == "drop":
            continue
        confidence, why, evidence, cause = explain_spike(row, events)
        value = str(row.get("value") or "(unknown)")
        scope_id = cause["resourceId"] if cause else value
        owner, owner_source = _owner_for(owners, scope_id)
        impact = _usd(row.get("impact_usd"), "impact_usd")
        name = _short(value) if row.get("dimension") == "resourceId" else value
        actions = [_action(
            f"Ask {owner} if this was intended" if owner != "Unassigned" else "Find the owner",
            f"For FinOps finding {{id}}: {name} spend rose to "
            f"{_models._money(_usd(row.get('current_usd'), 'current_usd'))} on {row.get('date')}. "
            + (f"Draft a short message to {owner} explaining the likely cause ({why}) and asking whether "
               "the change was intended." if owner != "Unassigned" else
               "Use Activity Log callers, deployments and tags to identify the likely owner and draft a message."),
        )]
        if cause and cause["kind"] in _DIRECT_COST_KINDS:
            actions.append(_action(
                "Propose a rollback",
                f"For FinOps finding {{id}}: prepare a rollback plan for '{cause['summary']}' on "
                f"{cause['resourceId']}, with the expected daily saving and the reliability risk.",
            ))
        actions.append(_action(
            "Investigate further",
            f"For FinOps finding {{id}}: investigate why {name} cost rose on {row.get('date')}. Compare usage "
            "metrics, request volume, scaling history and meter-level cost for the 7 days before and after.",
            approval=False,
        ))
        findings.append({
            "key": ("spike", row.get("dimension"), value, row.get("date")),
            "job": "spike", "title": f"Cost spike: {name}", "impact": impact,
            "impactLabel": "extra spend in the spike", "owner": owner, "ownerSource": owner_source,
            "confidence": confidence, "why": why, "evidence": evidence, "actions": actions,
        })
    return findings


def _rightsizing_findings(rows, owners, repo):
    findings, queue = [], {}
    seen = set()
    for row in _models._objects(rows, "rightsizing"):
        resource = _models._text(row.get("resourceId"), "rightsizing.resourceId").strip()
        if not resource or resource.casefold() in seen:
            continue
        seen.add(resource.casefold())
        savings = _usd(row.get("estMonthlySavingsUsd"), "estMonthlySavingsUsd", optional=True)
        validated = row.get("validated")
        owner, owner_source = _owner_for(owners, resource)
        kind = row.get("kind")
        countable = savings is not None and validated is not False
        bucket = queue.setdefault(owner, {"owner": owner, "items": 0, "savings": Decimal(0), "unknown": 0})
        bucket["items"] += 1
        if countable:
            bucket["savings"] += savings
        else:
            bucket["unknown"] += 1
        if kind not in {"idle", "advisor"}:
            continue
        action_text = _models._text(row.get("recommendedAction"), "recommendedAction").strip().rstrip(".") \
            or "Review this resource"
        action_sentence = action_text + "."
        evidence = [str(item) for item in (row.get("evidence") or [])]
        sources = ", ".join(str(item) for item in (row.get("sources") or [])) or "rightsizing analysis"
        evidence.append(f"Sources: {sources}.")
        confidence = "High" if validated is True else ("Low" if validated is False else "Medium")
        if kind == "idle":
            why = f"It bills but does no work. {evidence[0] if evidence else ''}".strip()
            actions = [
                _action(f"Ask {owner} to confirm" if owner != "Unassigned" else "Find the owner",
                        f"For FinOps finding {{id}}: draft a message to {owner} asking them to confirm that "
                        f"{resource} is no longer needed. Include its monthly cost and the evidence."),
                _action("Delete after approval",
                        f"For FinOps finding {{id}}: prepare to remove {resource}. Take a snapshot or backup "
                        "first where the resource holds data, list every dependency, and state the monthly "
                        "saving."),
            ]
            job = "orphan"
        else:
            why = (f"Azure Advisor recommends: {action_sentence}"
                   + (" Utilization data supports it." if validated is True else
                      " Utilization data does not support it yet; check before acting." if validated is False else
                      " Utilization was not validated."))
            change = (f"draft a pull request in {repo} that makes this change in the infrastructure code"
                      if repo else "prepare the exact ARM change")
            actions = [
                _action("Draft the change",
                        f"For FinOps finding {{id}}: {change} for {resource}: {action_sentence} "
                        "Include the expected monthly saving and the rollback steps."),
                _action("Check reliability first",
                        f"For FinOps finding {{id}}: check the last 30 days of CPU, memory, latency and alerts "
                        f"for {resource} and say whether '{action_text}' is safe.", approval=False),
            ]
            job = "tier"
        findings.append({
            "key": (job, resource), "job": job, "title": f"{_short(resource)}: {action_text}",
            "impact": savings if countable else None, "impactLabel": "monthly saving",
            "owner": owner, "ownerSource": owner_source, "confidence": confidence,
            "why": why, "evidence": evidence, "actions": actions,
        })
    owner_queue = sorted(queue.values(), key=lambda item: (-item["savings"], item["owner"].casefold()))
    for bucket in owner_queue:
        if bucket["savings"] <= 0 and not bucket["items"]:
            continue
        unknown = f", {bucket['unknown']} with no saving estimate" if bucket["unknown"] else ""
        findings.append({
            "key": ("waste", bucket["owner"]), "job": "waste",
            "title": f"{bucket['owner']}: {_count(bucket['items'], 'savings item')} to review",
            "impact": bucket["savings"], "impactLabel": "monthly saving",
            "owner": bucket["owner"], "ownerSource": "Grouped by owner",
            "confidence": "Medium",
            "why": f"{_count(bucket['items'], 'resource')} owned by {bucket['owner']} {'was' if bucket['items'] == 1 else 'were'} flagged by utilization, "
                   f"inventory or Advisor checks{unknown}.",
            "evidence": [f"Estimated {_models._money(bucket['savings'])} a month if every validated item is "
                         "actioned. Items with no estimate are not counted."],
            "actions": [_action(
                f"Open one ticket for {bucket['owner']}",
                f"For FinOps finding {{id}}: open one ticket for {bucket['owner']} listing all of their "
                "rightsizing and idle items from the latest FinOps dashboard, ranked by saving, with evidence "
                "and owner actions. Use the configured ticketing connector or GitHub issues.")],
        })
    return findings, owner_queue


def _tag_findings(rows, owners):
    gaps = _models._objects(rows, "tag_gaps")
    if not gaps:
        return []
    total, proposed, evidence = Decimal(0), 0, []
    for row in gaps:
        resource = _models._text(row.get("resourceId"), "tag_gaps.resourceId").strip()
        if not resource:
            raise ValueError("tag_gaps rows require resourceId")
        total += _usd(row.get("monthly_usd"), "tag_gaps.monthly_usd")
        proposal = _models._object(row.get("proposed") or {}, "tag_gaps.proposed")
        missing = ", ".join(_models._strings(row.get("missing"), "tag_gaps.missing")) or "owner tags"
        if proposal:
            proposed += 1
            basis = _models._text(row.get("basis"), "tag_gaps.basis").strip() or "deployment history"
            values = ", ".join(f"{key}={value}" for key, value in sorted(proposal.items()))
            evidence.append(f"{_short(resource)}: set {values} (from {basis}).")
        else:
            evidence.append(f"{_short(resource)}: missing {missing}; no owner could be inferred.")
    return [{
        "key": ("tags",), "job": "tags",
        "title": f"{_count(len(gaps), 'resource')} cannot be charged back",
        "impact": total, "impactLabel": "monthly spend with no owner",
        "owner": "Cloud Center of Excellence", "ownerSource": "Tag policy owner",
        "confidence": "High" if proposed == len(gaps) else "Medium",
        "why": f"Their spend cannot be charged to a team. The agent proposed owners for {proposed} of "
               f"{len(gaps)} from tags on the resource group, deployment history or Activity Log callers.",
        "evidence": evidence[:12] + ([f"…and {len(evidence) - 12} more."] if len(evidence) > 12 else []),
        "actions": [
            _action("Request batch approval",
                    "For FinOps finding {id}: list every proposed tag change from the latest FinOps dashboard "
                    "as one batch I can approve or edit, then apply only the approved rows."),
            _action("Send the list to owners",
                    "For FinOps finding {id}: draft one message per likely owner listing their untagged "
                    "resources and the proposed tags."),
        ],
    }]


def _agent_cost(value, savings):
    if value is None:
        return [], [], None
    if _unavailable(value):
        return [], [], _models._text(value.get("reason"), "agent_cost.reason") or "Agent cost is unavailable."
    data = _models._object(value, "agent_cost")
    period = _usd(data.get("period_usd"), "agent_cost.period_usd")
    window = data.get("window") or []
    days = (_day(window[1], "window") - _day(window[0], "window")).days + 1 if len(window) == 2 else 30
    monthly = (period * Decimal(30) / Decimal(days)).quantize(Decimal("0.01"))
    rows = []
    for run in _models._objects(data.get("runs"), "agent_cost.runs"):
        rows.append([_models._text(run.get("name"), "runs.name") or "(unnamed)",
                     _models._text(run.get("started_at"), "runs.started_at"),
                     _models._money(_usd(run.get("cost_usd"), "runs.cost_usd"))])
    rows.sort(key=lambda row: (row[1], row[0]), reverse=True)
    ratio = (savings / monthly).quantize(Decimal("0.1")) if monthly > 0 and savings > 0 else None
    why = (f"This agent costs about {_models._money(monthly)} a month. "
           + (f"It found {_models._money(savings)} a month in validated savings, {ratio}× its own cost."
              if ratio is not None else "It has not found validated savings in this window yet."))
    evidence = [f"{_models._money(period)} from {window[0]} to {window[1]} ({data.get('source') or 'Cost Management'})."
                if len(window) == 2 else f"{_models._money(period)} in the reported period."]
    if not rows:
        evidence.append("Per-run cost is not exposed by the platform yet; see Settings > Agent consumption.")
    finding = {
        "key": ("agent-cost",), "job": "agent-cost", "title": "What this agent cost",
        "impact": monthly, "impactLabel": "agent cost a month", "owner": "Agent administrator",
        "ownerSource": "SRE Agent resource", "confidence": "High", "why": why, "evidence": evidence,
        "actions": [_action(
            "Find the costliest tasks",
            "For FinOps finding {id}: list this agent's scheduled tasks and recent threads by cost or "
            "agent units, and suggest which schedules could run less often without losing findings.",
            approval=False)],
    }
    metric = {"label": "Agent cost / month", "value": _models._money(monthly),
              "detail": f"{ratio}× return in validated savings" if ratio is not None else "No validated savings yet"}
    return [finding], [metric, rows], None


def build_briefing(inputs, *, generated_at, window):
    """Return the decision-briefing model.

    inputs keys (all optional; None means not checked, {"status":"unavailable","reason":...}
    means the check failed):
      owners        [{scope, owner, source}] scope is a resource, RG or subscription ID.
      anomalies     detect_anomalies() rows.
      change_events [{time, resourceId, kind, summary, service?, actor?, reference?}]
                    kind: scale, sku, create, deployment, config, commit or incident.
      rightsizing   recommend_rightsizing() rows.
      tag_gaps      [{resourceId, monthly_usd, missing:[...], proposed:{tag:value}, basis}]
      agent_cost    {period_usd, window:[start,end], source, runs:[{name, started_at, cost_usd}]}
      repo          "owner/name" used for pull-request actions.
    """
    inputs = _models._object(inputs, "briefing")
    unknown = set(inputs) - {"owners", "anomalies", "change_events", "rightsizing", "tag_gaps",
                             "agent_cost", "repo"}
    if unknown:
        raise ValueError("unknown briefing inputs: " + ", ".join(sorted(unknown)))
    datetime.fromisoformat(str(generated_at).replace("Z", "+00:00"))
    owners = _owner_index(None if _unavailable(inputs.get("owners")) else inputs.get("owners"))
    repo = _models._text(inputs.get("repo"), "repo").strip()
    events_input = inputs.get("change_events")
    events = [] if events_input is None or _unavailable(events_input) else _events(events_input)

    checks, findings, owner_queue = [], [], []
    for job, label, description in JOBS:
        value = inputs.get(_INPUT_FOR_JOB[job])
        if value is None:
            checks.append({"job": job, "label": label, "state": "Not checked",
                           "detail": "No evidence was collected for this check in this run."})
        elif _unavailable(value):
            checks.append({"job": job, "label": label, "state": "Could not check",
                           "detail": _models._text(value.get("reason"), "reason") or "The read failed."})
        else:
            checks.append({"job": job, "label": label, "state": "Checked", "detail": description})
    if events_input is None or _unavailable(events_input):
        checks[0]["detail"] = ("Change records were not collected, so spikes are reported without a cause."
                               if events_input is None else
                               "Change records could not be read: " + str(events_input.get("reason") or ""))
    if not owners:
        for check in checks:
            if check["job"] in {"waste", "orphan", "tier"} and check["state"] == "Checked":
                check["detail"] += " No owner map was supplied, so items show as Unassigned."

    if inputs.get("anomalies") is not None and not _unavailable(inputs["anomalies"]):
        findings += _spike_findings(inputs["anomalies"], events, owners)
    if inputs.get("rightsizing") is not None and not _unavailable(inputs["rightsizing"]):
        extra, owner_queue = _rightsizing_findings(inputs["rightsizing"], owners, repo)
        findings += extra
    if inputs.get("tag_gaps") is not None and not _unavailable(inputs["tag_gaps"]):
        findings += _tag_findings(inputs["tag_gaps"], owners)

    savings = sum((item["impact"] for item in findings
                   if item["job"] in {"orphan", "tier"} and item["impact"] is not None
                   and item["confidence"] == "High"), Decimal(0))
    agent_findings, agent_extra, agent_reason = _agent_cost(inputs.get("agent_cost"), savings)
    if agent_reason:
        checks[-1]["detail"] = agent_reason
    findings += agent_findings

    for item in findings:
        item["id"] = _id(*item.pop("key"))
        for action in item["actions"]:
            action["prompt"] = action["prompt"].replace("{id}", item["id"])
    decisions = [item for item in findings if item["job"] not in {"waste", "agent-cost"}]
    decisions.sort(key=lambda item: (-(item["impact"] or Decimal(-1)), _CONFIDENCE_ORDER[item["confidence"]],
                                     item["title"].casefold(), item["id"]))
    rollups = [item for item in findings if item["job"] in {"waste", "agent-cost"}]

    potential = sum((item["impact"] for item in findings
                     if item["job"] in {"orphan", "tier"} and item["impact"] is not None), Decimal(0))
    spikes = [item for item in findings if item["job"] == "spike"]
    explained = sum(1 for item in spikes if item["confidence"] != "Low")
    owners_with_work = len({item["owner"] for item in decisions if item["job"] in {"orphan", "tier"}})
    parts = [(_count(len(decisions), "decision") + (" needs" if len(decisions) == 1 else " need") + " attention.") if decisions else "No decisions need attention."]
    if potential:
        parts.append(f"{_models._money(potential)} a month in possible savings across {_count(owners_with_work, 'owner')}.")
    if spikes:
        parts.append(f"{_count(len(spikes), 'cost spike')}; {explained} with a likely cause.")
    untagged = next((item for item in findings if item["job"] == "tags"), None)
    if untagged:
        parts.append(f"{_models._money(untagged['impact'])} a month cannot be charged back to a team.")
    if agent_findings:
        parts.append(agent_findings[0]["why"])
    checked = sum(1 for check in checks if check["state"] == "Checked")
    metrics = [
        {"label": "Decisions", "value": str(len(decisions)), "detail": "Ranked by monthly impact",
         "tone": "warning" if decisions else "good"},
        {"label": "Possible savings / month", "value": _models._money(potential),
         "detail": f"{_models._money(savings)} validated", "tone": "good" if potential else "neutral"},
        {"label": "Spikes explained", "value": f"{explained} of {len(spikes)}",
         "detail": "Matched to a change or incident", "tone": "neutral"},
    ]
    if agent_extra:
        metrics.append(agent_extra[0])
    for item in findings:
        item["jobLabel"] = _JOB_LABELS[item["job"]]
        item["impactText"] = "No estimate" if item["impact"] is None else _models._money(item["impact"])
        item["impact"] = None if item["impact"] is None else str(item["impact"])
    return {
        "generatedAt": generated_at, "window": list(window),
        "headline": " ".join(parts), "metrics": metrics,
        "checks": checks, "checksSummary": f"{checked} of {len(checks)} checks ran",
        "findings": decisions[:_MAX_FINDINGS], "hiddenFindings": max(0, len(decisions) - _MAX_FINDINGS),
        "rollups": rollups,
        "ownerQueue": [[row["owner"], str(row["items"]), _models._money(row["savings"]), str(row["unknown"])]
                       for row in owner_queue],
        "agentRuns": agent_extra[1] if agent_extra else [],
    }
