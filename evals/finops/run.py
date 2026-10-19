#!/usr/bin/env python3
"""Run deterministic, customer-data-free FinOps evaluation gates."""

import argparse
import hashlib
import html
from html.parser import HTMLParser
import json
import re
import subprocess
import sys
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from statistics import median


ROOT = Path(__file__).resolve().parents[2]
FINOPS = ROOT / "plugins" / "azure-finops"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
MANAGED_SKILL = FINOPS / "skills" / "finops-managed-scope"
RENDER_SKILL = FINOPS / "skills" / "finops-report-renderer"

BASELINE_SCHEDULED_PROMPT_BYTES = 52_449
FILTER_ROW_COUNT = 20_000
FILTER_SCOPE_COUNT = 30
FILTER_TIMING_SAMPLES = 3
FILTER_SPEEDUP_TARGET = Decimal("5")
PROMPT_REDUCTION_TARGET = Decimal("0.35")
ALLOWED_REPORT_SCRIPT_SOURCES = {
    "https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"
}
FORBIDDEN_VIEW_TIME_SCRIPT_MARKERS = (
    "window.sreagent",
    "fetch(",
    "xmlhttprequest",
    "websocket",
    "eventsource",
    "sendbeacon",
    "mcp",
    "toolcall",
    "tool_call",
)


def _load_runtime():
    sys.path.insert(0, str(MANAGED_SKILL))
    sys.path.insert(0, str(RENDER_SKILL))
    from scope import filter_usage_details, scope_contains
    from usage import prepare_usage_details
    from render import render_report

    return filter_usage_details, scope_contains, prepare_usage_details, render_report


def _json_safe(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _ci_get(mapping, *names):
    if not isinstance(mapping, dict):
        return None
    lowered = {str(key).casefold(): value for key, value in mapping.items()}
    for name in names:
        value = lowered.get(name.casefold())
        if value not in (None, ""):
            return value
    properties = lowered.get("properties")
    return _ci_get(properties, *names) if isinstance(properties, dict) else None


def _row_cost(row):
    value = _ci_get(row, "cost", "costInUSD", "pretaxCost")
    try:
        cost = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("0")
    return cost if cost.is_finite() else Decimal("0")


def _row_target(row):
    return _ci_get(row, "instanceName", "resourceId")


def _flatten_page_rows(pages):
    return [row for page in pages for row in page.get("value", page.get("rows", []))]


def _usage_gate(prepare_usage_details, scope_contains):
    fixture = json.loads(
        (FIXTURES / "usage-two-page.json").read_text(encoding="utf-8")
    )
    pages = fixture["pages"]
    managed_scopes = fixture["managedScopes"]
    result = prepare_usage_details(pages, managed_scopes)

    exact_checks = {
        key: result.get(key) == expected
        for key, expected in fixture["expected"].items()
    }
    cost_checks = {
        key: Decimal(result.get(key)) == Decimal(expected)
        for key, expected in fixture["expectedCosts"].items()
    }

    included_leaks = [
        row
        for row in result["included_rows"]
        if not _row_target(row)
        or not scope_contains(managed_scopes, _row_target(row))
    ]
    excluded_managed = [
        row
        for row in result["excluded_rows"]
        if _row_target(row)
        and scope_contains(managed_scopes, _row_target(row))
    ]
    attributed_unscoped = [
        row
        for row in result["included_rows"] + result["excluded_rows"]
        if not _row_target(row)
    ]

    raw_cost = sum(
        (_row_cost(row) for row in _flatten_page_rows(pages)), Decimal("0")
    )
    unique_cost = (
        result["included_cost"]
        + result["excluded_cost"]
        + result["unattributed_cost"]
    )
    reconciled_cost = unique_cost + result["duplicate_cost"]
    count_reconciled = result["retrieved_row_count"] == (
        result["included_count"]
        + result["excluded_count"]
        + result["unattributed_count"]
        + result["duplicate_count"]
    )
    reconciliation_checks = {
        "raw_cost_equals_unique_plus_duplicate": raw_cost == reconciled_cost,
        "reported_total_cost_equals_unique_cost": result["total_cost"] == unique_cost,
        "retrieved_count_reconciles": count_reconciled,
    }

    checks = {
        **{f"exact_{key}": passed for key, passed in exact_checks.items()},
        **{f"exact_{key}": passed for key, passed in cost_checks.items()},
        **reconciliation_checks,
        "zero_included_scope_leakage": not included_leaks,
        "zero_managed_rows_excluded": not excluded_managed,
        "zero_unscoped_rows_attributed": not attributed_unscoped,
    }
    measurement = {
        "checks": checks,
        "page_count": result["page_count"],
        "page_row_counts": result["page_row_counts"],
        "request_urls": result["request_urls"],
        "retrieved_row_count": result["retrieved_row_count"],
        "included_count": result["included_count"],
        "excluded_count": result["excluded_count"],
        "unattributed_count": result["unattributed_count"],
        "duplicate_count": result["duplicate_count"],
        "included_cost": result["included_cost"],
        "excluded_cost": result["excluded_cost"],
        "unattributed_cost": result["unattributed_cost"],
        "duplicate_cost": result["duplicate_cost"],
        "raw_retrieved_cost": raw_cost,
        "scope_leakage_count": len(included_leaks),
    }
    return all(checks.values()), _json_safe(measurement)


class _ReportAudit(HTMLParser):
    def __init__(self):
        super().__init__()
        self.csp = []
        self.external_scripts = []
        self.inline_script_nonces = []
        self.style_nonces = []
        self.inline_scripts = []
        self.event_handlers = []
        self._script_data = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        self.event_handlers.extend(
            name for name, _value in attrs if name.casefold().startswith("on")
        )
        if tag == "meta" and attributes.get("http-equiv", "").casefold() == (
            "content-security-policy"
        ):
            self.csp.append(attributes.get("content", ""))
        elif tag == "style":
            self.style_nonces.append(attributes.get("nonce"))
        elif tag == "script":
            source = attributes.get("src")
            if source:
                self.external_scripts.append(source)
            else:
                self.inline_script_nonces.append(attributes.get("nonce"))
                self._script_data = []

    def handle_data(self, data):
        if self._script_data is not None:
            self._script_data.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self._script_data is not None:
            self.inline_scripts.append("".join(self._script_data))
            self._script_data = None


def _render_gate(render_report):
    fixture = json.loads((FIXTURES / "report-model.json").read_text(encoding="utf-8"))
    evaluation = fixture.pop("_evaluation")
    first = render_report(fixture)
    second = render_report(fixture)
    audit = _ReportAudit()
    audit.feed(first)

    csp = audit.csp[0] if len(audit.csp) == 1 else ""
    directives = {}
    for directive in csp.split(";"):
        fields = directive.split()
        if fields:
            directives[fields[0].casefold()] = fields[1:]
    report_text = first.casefold()
    escape_checks = {}
    for index, raw in enumerate(evaluation["mustEscape"]):
        escaped = html.escape(raw, quote=True)
        escape_checks[f"escaped_value_{index}"] = escaped in first and raw not in first

    checks = {
        "deterministic": first == second,
        "single_csp": len(audit.csp) == 1,
        "csp_default_deny": directives.get("default-src") == ["'none'"],
        "csp_connect_restricted": directives.get("connect-src") == ["'self'"],
        "csp_nonce_placeholder": "'nonce-{REPORT_NONCE}'"
        in directives.get("script-src", []),
        "csp_disallows_unsafe_inline_scripts": "'unsafe-inline'"
        not in directives.get("script-src", []),
        "all_styles_nonce_protected": bool(audit.style_nonces)
        and set(audit.style_nonces) == {"{REPORT_NONCE}"},
        "all_inline_scripts_nonce_protected": bool(audit.inline_script_nonces)
        and set(audit.inline_script_nonces) == {"{REPORT_NONCE}"},
        "only_pinned_report_scripts": set(audit.external_scripts)
        == ALLOWED_REPORT_SCRIPT_SOURCES,
        "no_inline_event_handlers": not audit.event_handlers,
        "no_view_time_tools": not any(
            marker in report_text for marker in FORBIDDEN_VIEW_TIME_SCRIPT_MARKERS
        ),
        **escape_checks,
    }
    return all(checks.values()), {
        "checks": checks,
        "sha256": hashlib.sha256(first.encode("utf-8")).hexdigest(),
        "bytes": len(first.encode("utf-8")),
        "external_scripts": audit.external_scripts,
        "inline_script_count": len(audit.inline_scripts),
    }


def _benchmark_rows(row_count=FILTER_ROW_COUNT, scope_count=FILTER_SCOPE_COUNT):
    subscription = "00000000-0000-0000-0000-000000000001"
    scopes = [
        f"/subscriptions/{subscription}/resourceGroups/managed-{index}"
        for index in range(scope_count)
    ]
    rows = []
    for index in range(row_count):
        resource_group = f"managed-{index % scope_count}"
        rows.append(
            {
                "id": f"synthetic-usage-{index}",
                "date": "2026-08-01",
                "cost": "0.01",
                "resourceGroup": resource_group,
                "resourceId": (
                    f"/subscriptions/{subscription}/resourceGroups/{resource_group}/"
                    f"providers/Microsoft.Compute/virtualMachines/vm-{index}"
                ),
            }
        )
    return rows, scopes


def _legacy_containment_count(rows, scopes, scope_contains):
    return sum(scope_contains(scopes, row["resourceId"]) for row in rows)


def _timed_call(function, *args):
    started = time.perf_counter()
    result = function(*args)
    return time.perf_counter() - started, result


def _filter_benchmark(
    filter_usage_details,
    scope_contains,
    *,
    row_count=FILTER_ROW_COUNT,
    scope_count=FILTER_SCOPE_COUNT,
    samples=FILTER_TIMING_SAMPLES,
    timed_call=_timed_call,
):
    if samples < 3:
        raise ValueError("at least three timing samples are required")
    rows, scopes = _benchmark_rows(row_count, scope_count)

    filter_usage_details(rows[:100], scopes)
    _legacy_containment_count(rows[:100], scopes, scope_contains)

    optimized_timings = []
    baseline_timings = []
    optimized_result = None
    baseline_count = None
    for sample in range(samples):
        calls = (
            (
                (filter_usage_details, rows, scopes),
                (_legacy_containment_count, rows, scopes, scope_contains),
            )
            if sample % 2 == 0
            else (
                (_legacy_containment_count, rows, scopes, scope_contains),
                (filter_usage_details, rows, scopes),
            )
        )
        for function, *arguments in calls:
            elapsed, result = timed_call(function, *arguments)
            if function is filter_usage_details:
                optimized_timings.append(elapsed)
                optimized_result = result
            else:
                baseline_timings.append(elapsed)
                baseline_count = result

    optimized_median = median(optimized_timings)
    baseline_median = median(baseline_timings)
    speedup = Decimal(str(baseline_median)) / Decimal(str(optimized_median))
    output_correct = (
        optimized_result["included_count"] == row_count
        and optimized_result["excluded_count"] == 0
        and optimized_result["unattributed_count"] == 0
        and baseline_count == row_count
    )
    checks = {
        "exact_workload_size": len(rows) == row_count and len(scopes) == scope_count,
        "multiple_timing_samples": len(optimized_timings) >= 3
        and len(baseline_timings) >= 3,
        "equivalent_inclusion_result": output_correct,
        "median_speedup_at_least_5x": speedup >= FILTER_SPEEDUP_TARGET,
    }
    return all(checks.values()), {
        "checks": checks,
        "rows": row_count,
        "scopes": scope_count,
        "samples": samples,
        "optimized_seconds": optimized_timings,
        "baseline_seconds": baseline_timings,
        "optimized_median_seconds": optimized_median,
        "baseline_median_seconds": baseline_median,
        "median_speedup": float(speedup),
    }


def _prompt_gate():
    # Scheduled tasks are now rendered in chat by finops-setup/setup.py; measure those prompts.
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "finops_setup", FINOPS / "skills" / "finops-setup" / "setup.py"
    )
    setup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(setup)
    sample_agent = (
        "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg"
        "/providers/Microsoft.App/agents/agent"
    )
    task_files = setup.task_plan(sample_agent)["tasks"]
    size = sum(len(task["agentPrompt"].encode("utf-8")) for task in task_files)
    baseline = Decimal(BASELINE_SCHEDULED_PROMPT_BYTES)
    reduction = Decimal("1") - (Decimal(size) / baseline)
    maximum_bytes = int(baseline * (Decimal("1") - PROMPT_REDUCTION_TARGET))
    checks = {
        "scheduled_tasks_found": bool(task_files),
        "reduction_at_least_35_percent": reduction >= PROMPT_REDUCTION_TARGET,
    }
    return all(checks.values()), {
        "checks": checks,
        "files": len(task_files),
        "bytes": size,
        "maximum_bytes": maximum_bytes,
        "baseline_bytes": BASELINE_SCHEDULED_PROMPT_BYTES,
        "reduction": float(reduction),
    }


def _test_gate():
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests",
            "evals/finops/test_run.py",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=600,
    )
    output = process.stdout + process.stderr
    match = re.search(r"(\d+) passed", output)
    return process.returncode == 0, {
        "passed": int(match.group(1)) if match else None,
        "returncode": process.returncode,
        "tail": "\n".join(output.strip().splitlines()[-5:]),
    }


def _run_gate(function, *args):
    try:
        passed, measurement = function(*args)
        return {"passed": bool(passed), "measurement": measurement}
    except Exception as error:
        return {
            "passed": False,
            "measurement": {
                "error": f"{type(error).__name__}: {error}",
            },
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--with-tests", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    (
        filter_usage_details,
        scope_contains,
        prepare_usage_details,
        render_report,
    ) = _load_runtime()
    gates = {
        "usage_pipeline": _run_gate(
            _usage_gate, prepare_usage_details, scope_contains
        ),
        "report_rendering": _run_gate(_render_gate, render_report),
        "scope_filter_performance": _run_gate(
            _filter_benchmark, filter_usage_details, scope_contains
        ),
        "scheduled_prompt_reduction": _run_gate(_prompt_gate),
    }
    if args.with_tests:
        gates["pytest_suite"] = _run_gate(_test_gate)

    result = {
        "schema_version": 2,
        "passed": all(gate["passed"] for gate in gates.values()),
        "gates": gates,
    }
    rendered = json.dumps(_json_safe(result), indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
