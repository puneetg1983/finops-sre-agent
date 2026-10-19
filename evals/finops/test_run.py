"""Focused tests for the synthetic FinOps evaluation runner."""

import importlib.util
from pathlib import Path


PATH = Path(__file__).with_name("run.py")
SPEC = importlib.util.spec_from_file_location("finops_eval_runner", PATH)
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def test_usage_gate_reconciles_cost_and_has_no_scope_leakage():
    _, scope_contains, prepare_usage_details, _ = runner._load_runtime()

    passed, measurement = runner._usage_gate(
        prepare_usage_details, scope_contains
    )

    assert passed is True
    assert measurement["raw_retrieved_cost"] == "122.75"
    assert measurement["duplicate_cost"] == "10.25"
    assert measurement["scope_leakage_count"] == 0
    assert all(measurement["checks"].values())


def test_usage_gate_rejects_an_included_row_outside_managed_scope():
    _, scope_contains, prepare_usage_details, _ = runner._load_runtime()

    def leaky_prepare(pages, managed_scopes):
        result = prepare_usage_details(pages, managed_scopes)
        result["included_rows"] = result["included_rows"] + [
            result["excluded_rows"][0]
        ]
        return result

    passed, measurement = runner._usage_gate(leaky_prepare, scope_contains)

    assert passed is False
    assert measurement["scope_leakage_count"] == 1
    assert measurement["checks"]["zero_included_scope_leakage"] is False


def test_render_gate_requires_escaping_csp_and_static_view():
    _, _, _, render_report = runner._load_runtime()

    passed, measurement = runner._render_gate(render_report)

    assert passed is True
    assert measurement["inline_script_count"] == 1
    assert all(measurement["checks"].values())


def test_render_gate_rejects_view_time_network_code():
    def unsafe_renderer(_model):
        return """<!doctype html>
<meta http-equiv="Content-Security-Policy"
 content="default-src 'none'; connect-src 'self'; script-src 'nonce-{REPORT_NONCE}'">
<style nonce="{REPORT_NONCE}"></style>
<script nonce="{REPORT_NONCE}">fetch('/customer-data')</script>"""

    passed, measurement = runner._render_gate(unsafe_renderer)

    assert passed is False
    assert measurement["checks"]["no_view_time_tools"] is False


def test_performance_gate_uses_medians_from_multiple_samples(monkeypatch):
    optimized_times = iter([1.0, 20.0, 1.0])
    baseline_times = iter([6.0, 6.0, 6.0])

    def optimized(rows, _scopes):
        return {
            "included_count": len(rows),
            "excluded_count": 0,
            "unattributed_count": 0,
        }

    def contains(_scopes, _resource_id):
        return True

    def timed_call(function, *args):
        result = function(*args)
        timings = optimized_times if function is optimized else baseline_times
        return next(timings), result

    monkeypatch.setattr(
        runner,
        "_benchmark_rows",
        lambda *_: (
            [{"resourceId": "/synthetic"}] * 6,
            ["/synthetic"] * 3,
        ),
    )
    passed, measurement = runner._filter_benchmark(
        optimized,
        contains,
        row_count=6,
        scope_count=3,
        samples=3,
        timed_call=timed_call,
    )

    assert passed is True
    assert measurement["optimized_median_seconds"] == 1.0
    assert measurement["median_speedup"] == 6.0
    assert measurement["checks"]["multiple_timing_samples"] is True


def test_gate_exceptions_become_structured_failures():
    def broken_gate():
        raise RuntimeError("synthetic failure")

    result = runner._run_gate(broken_gate)

    assert result["passed"] is False
    assert result["measurement"]["error"] == "RuntimeError: synthetic failure"
