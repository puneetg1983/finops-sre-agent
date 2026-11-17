"""Deterministic CSP-compliant static HTML renderer for FinOps reports."""

import html
import json
import math
from pathlib import Path


_CSP = """default-src 'none';
script-src 'self' 'nonce-{REPORT_NONCE}' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com https://unpkg.com https://esm.sh;
style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdn.jsdelivr.net https://cdnjs.cloudflare.com;
font-src 'self' data: https://fonts.gstatic.com https://cdn.jsdelivr.net https://cdnjs.cloudflare.com;
img-src 'self' data: blob: https:;
connect-src 'self';"""

_CHART_JS = (
    '<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js" '
    'integrity="sha384-vsrfeLOOY6KuIYKDlmVH5UiBmgIdB1oEf7p01YgWHuqmOHfZr374+odEv96n9tNC" '
    'crossorigin="anonymous"></script>'
)

_TONES = {"neutral", "good", "warning", "critical"}
_THEME = """
<script nonce="{REPORT_NONCE}">
(() => {
  const param = new URLSearchParams(window.location.search).get("scoutTheme");
  const theme = ["light", "dark"].includes(param) ? param :
    (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  document.documentElement.setAttribute("data-theme", theme);
})();
</script>"""

_DASHBOARD_SCRIPT = """
<script nonce="{REPORT_NONCE}">
(() => {
  const tabs = [...document.querySelectorAll('[role="tab"]')];
  const panels = [...document.querySelectorAll('[role="tabpanel"]')];
  const search = document.getElementById('table-search');
  const count = document.getElementById('search-count');
  function filter() {
    const term = search.value.toLocaleLowerCase().trim();
    let matches = 0;
    const panel = panels.find(p => !p.hidden);
    for (const row of panel.querySelectorAll('tbody tr')) {
      row.hidden = !row.textContent.toLocaleLowerCase().includes(term);
      if (!row.hidden) matches++;
    }
    count.textContent = term ? matches + ' matching rows in this section' : '';
  }
  function select(index, focus) {
    tabs.forEach((tab, i) => {
      const active = i === index;
      tab.setAttribute('aria-selected', String(active));
      tab.tabIndex = active ? 0 : -1;
      panels[i].hidden = !active;
    });
    if (focus) tabs[index].focus();
    filter();
  }
  tabs.forEach((tab, index) => {
    tab.addEventListener('click', () => select(index, false));
    tab.addEventListener('keydown', event => {
      let next;
      if (event.key === 'ArrowDown' || event.key === 'ArrowRight') next = (index + 1) % tabs.length;
      if (event.key === 'ArrowUp' || event.key === 'ArrowLeft') next = (index - 1 + tabs.length) % tabs.length;
      if (event.key === 'Home') next = 0;
      if (event.key === 'End') next = tabs.length - 1;
      if (next !== undefined) { event.preventDefault(); select(next, true); }
    });
  });
  search.addEventListener('input', filter);
  document.getElementById('print-report').addEventListener('click', () => window.print());
  select(0, false);
})();
</script>"""


def _escape(value):
    return html.escape(str(value if value is not None else ""), quote=True)


def _list(value, field):
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field} must be an array")
    return value


def _required_text(model, field):
    value = str(model.get(field) or "").strip()
    if not value:
        raise ValueError(f"{field} is required")
    return value


def _metrics(model):
    rendered = []
    for index, metric in enumerate(_list(model.get("metrics"), "metrics")):
        if not isinstance(metric, dict):
            raise ValueError(f"metrics[{index}] must be an object")
        label = _required_text(metric, "label")
        value = _required_text(metric, "value")
        detail = str(metric.get("detail") or "")
        tone = str(metric.get("tone") or "neutral").lower()
        if tone not in _TONES:
            raise ValueError(f"metrics[{index}].tone is invalid")
        rendered.append(
            f'<article class="metric {tone}"><div class="metric-label">{_escape(label)}</div>'
            f'<div class="metric-value">{_escape(value)}</div>'
            f'<div class="metric-detail">{_escape(detail)}</div></article>'
        )
    if not rendered:
        return '<p class="empty">No headline metrics were produced.</p>'
    return '<div class="metrics">' + "".join(rendered) + "</div>"


def _sections(model):
    rendered = []
    for index, section in enumerate(_list(model.get("sections"), "sections")):
        if not isinstance(section, dict):
            raise ValueError(f"sections[{index}] must be an object")
        title = _required_text(section, "title")
        description = str(section.get("description") or "")
        columns = [str(item) for item in _list(section.get("columns"), "columns")]
        rows = _list(section.get("rows"), "rows")
        empty_message = str(section.get("emptyMessage") or "No rows.")
        if rows and not columns:
            raise ValueError(f"sections[{index}].columns is required when rows exist")

        body = []
        for row_index, row in enumerate(rows):
            if not isinstance(row, list) or len(row) != len(columns):
                raise ValueError(
                    f"sections[{index}].rows[{row_index}] must match columns"
                )
            body.append(
                "<tr>" + "".join(f"<td>{_escape(cell)}</td>" for cell in row) + "</tr>"
            )

        if body:
            content = (
                "<div class=\"table-wrap\"><table><thead><tr>"
                + "".join(f"<th>{_escape(column)}</th>" for column in columns)
                + "</tr></thead><tbody>"
                + "".join(body)
                + "</tbody></table></div>"
            )
        else:
            content = f'<p class="empty">{_escape(empty_message)}</p>'
        rendered.append(
            f'<section><h2>{_escape(title)}</h2><p class="section-description">'
            f"{_escape(description)}</p>{content}</section>"
        )
    return "".join(rendered)


def _chart(model):
    chart = model.get("chart")
    if chart is None:
        return "", ""
    if not isinstance(chart, dict):
        raise ValueError("chart must be an object")
    chart_type = str(chart.get("type") or "bar")
    if chart_type not in {"bar", "line", "doughnut"}:
        raise ValueError("chart.type must be bar, line, or doughnut")
    label = _required_text(chart, "label")
    labels = _list(chart.get("labels"), "chart.labels")
    values = _list(chart.get("values"), "chart.values")
    if len(labels) != len(values):
        raise ValueError("chart labels and values must have equal length")
    if not labels:
        return '<p class="empty">No chart data was produced.</p>', ""
    data = {
        "type": chart_type,
        "label": label,
        "labels": [str(item) for item in labels],
        "values": values,
    }
    payload = json.dumps(data, ensure_ascii=True, separators=(",", ":")).replace(
        "</", "<\\/"
    )
    markup = '<section><h2>Trend</h2><div class="chart-wrap"><canvas id="finops-chart"></canvas></div></section>'
    script = f"""
<script nonce="{{REPORT_NONCE}}">
const chartData = {payload};
const chartCanvas = document.getElementById('finops-chart');
if (chartCanvas && window.Chart) {{
  new Chart(chartCanvas, {{
    type: chartData.type,
    data: {{
      labels: chartData.labels,
      datasets: [{{
        label: chartData.label,
        data: chartData.values,
        borderColor: getComputedStyle(document.documentElement).getPropertyValue('--cp-accent').trim(),
        backgroundColor: getComputedStyle(document.documentElement).getPropertyValue('--cp-accent-soft').trim(),
        borderWidth: 2,
        tension: 0.25
      }}]
    }},
    options: {{responsive: true, maintainAspectRatio: false}}
  }});
}}
</script>"""
    return markup, script


def _dashboard_chart(model):
    chart = model.get("chart")
    if chart is None:
        return ""
    labels = _list(chart.get("labels"), "chart.labels")
    values = _list(chart.get("values"), "chart.values")
    if len(labels) != len(values):
        raise ValueError("chart labels and values must have equal length")
    if not values:
        return ""
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in values):
        raise ValueError("chart values must be finite numbers")
    chart_type = chart.get("type", "bar")
    if chart_type not in {"line", "bar", "doughnut"}:
        raise ValueError("invalid dashboard chart type")
    low, high = min(min(values), 0), max(max(values), 0)
    span = high - low or 1
    if not math.isfinite(span):
        raise ValueError("chart range must be finite")
    if chart_type == "line":
        points = " ".join(
            f"{8 + index * 784 / max(1, len(values) - 1):.2f},{152 - (value - low) / span * 144:.2f}"
            for index, value in enumerate(values)
        )
        shapes = (f'<polyline points="{points}" fill="none" stroke="currentColor" '
                  'stroke-width="3" vector-effect="non-scaling-stroke"/>')
    else:
        zero = 152 - (0 - low) / span * 144
        step = 784 / len(values)
        shapes = "".join(
            f'<rect x="{8 + index * step:.2f}" y="{min(zero, 152 - (value - low) / span * 144):.2f}" '
            f'width="{step * .7:.2f}" height="{abs(value / span * 144):.2f}" fill="currentColor"/>'
            for index, value in enumerate(values)
        )
    label = _escape(chart.get("label") or "Cost trend")
    table = _sections({"sections": [{
        "title": str(chart.get("label") or "Cost trend"), "columns": ["Label", "Value"],
        "rows": [[label, str(value)] for label, value in zip(labels, values)],
    }]})
    return (
        f'<figure><svg role="img" aria-label="{label}" viewBox="0 0 800 160" preserveAspectRatio="none">'
        f'<title>{label}</title>{shapes}</svg></figure>'
        f'<details><summary>Chart data</summary>{table}</details>'
    )


def _dashboard(model):
    tabs = _list(model.get("tabs"), "tabs")
    if not tabs:
        raise ValueError("dashboard tabs must not be empty")
    buttons, panels, seen = [], [], set()
    for index, tab in enumerate(tabs):
        key = _required_text(tab, "id")
        if not key.isascii() or not all(c.isalnum() or c == "-" for c in key) or key in seen:
            raise ValueError("dashboard tab IDs must be unique ASCII identifiers")
        seen.add(key)
        title, status = _required_text(tab, "title"), _required_text(tab, "status")
        if status not in {"ready", "partial", "unavailable", "unsupported"}:
            raise ValueError("invalid dashboard tab status")
        buttons.append(
            f'<button type="button" role="tab" id="tab-{key}" aria-controls="panel-{key}" '
            f'aria-selected="{str(index == 0).lower()}"><span>{_escape(title)}</span>'
            f'<span class="status {status}">{status}</span></button>'
        )
        panel_warnings = str(tab.get("reason") or "")
        panels.append(
            f'<div role="tabpanel" tabindex="0" id="panel-{key}" aria-labelledby="tab-{key}">'
            f'<section><h2>{_escape(title)}</h2><p>{_escape(tab.get("description"))}</p>'
            f'<p class="provenance">{_escape(status)} | Source: {_escape(tab.get("source"))} | '
            f'As of: {_escape(tab.get("refreshedAt") or "Unknown")} | {_escape(tab.get("period"))}</p>'
            + (f'<aside class="warnings">{_escape(panel_warnings)}</aside>' if panel_warnings else "")
            + '</section>' + (_metrics(tab) if tab.get("metrics") else "")
            + _dashboard_chart(tab) + _sections(tab) + '</div>'
        )
    return (
        '<div class="toolbar"><label for="table-search">Search table rows</label>'
        '<input id="table-search" type="search" placeholder="Resource, team, service, region..." '
        'aria-describedby="search-count"><span id="search-count" role="status"></span>'
        '<button type="button" id="print-report">Print / save PDF</button></div>'
        '<div class="dashboard-layout"><nav role="tablist" aria-label="FinOps sections" '
        'aria-orientation="vertical">' + "".join(buttons) + '</nav><div>'
        + "".join(panels) + '</div></div>'
    )


def render_report(model):
    """Return deterministic self-contained report HTML for one structured model."""

    if not isinstance(model, dict):
        raise ValueError("model must be an object")
    title = _required_text(model, "title")
    description = str(model.get("description") or "")
    refreshed_at = _required_text(model, "refreshedAt")
    scopes = [str(item) for item in _list(model.get("scopeSummary"), "scopeSummary")]
    warnings = [str(item) for item in _list(model.get("warnings"), "warnings")]
    partial = bool(model.get("partial"))
    is_dashboard = "tabs" in model

    warning_items = list(warnings)
    if partial and not warning_items:
        warning_items.append("Partial data: one or more source pulls were incomplete.")
    warning_html = ""
    if warning_items:
        notices = "".join(
            f"<li>{_escape(item)}</li>" for item in warning_items
        )
        if is_dashboard:
            warning_html = (
                f'<aside class="warnings"><details><summary>Data quality: {len(warning_items)} '
                f'notices - review before acting</summary><ul>{notices}</ul></details></aside>'
            )
        else:
            warning_html = '<aside class="warnings"><strong>Data quality</strong><ul>' + notices + "</ul></aside>"

    scope_html = (
        "<ul>" + "".join(f"<li><code>{_escape(scope)}</code></li>" for scope in scopes) + "</ul>"
        if scopes
        else '<p class="empty">No managed scope summary was supplied.</p>'
    )
    chart_markup, chart_script = ("", "") if is_dashboard else _chart(model)
    content = _dashboard(model) if is_dashboard else (
        _metrics(model) + chart_markup + _sections(model)
    )
    stylesheet = Path(__file__).with_name("theme.css").read_text(encoding="utf-8")
    mode = "Everything (tenant)" if model.get("scopeMode") == "everything" else "Managed resources"
    csp = (
        "default-src 'none'; script-src 'nonce-{REPORT_NONCE}'; "
        "style-src 'nonce-{REPORT_NONCE}'; img-src data:; connect-src 'none'; "
        "base-uri 'none'; form-action 'none';"
        if is_dashboard else _CSP
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="{csp}">
<title>{_escape(title)}</title>
{_THEME}
<style nonce="{{REPORT_NONCE}}">
{stylesheet}
</style>
{_CHART_JS if chart_script else ""}
</head>
<body>
<main>
<header><p class="eyebrow">Azure FinOps / Decision workspace</p><h1>{_escape(title)}</h1><p class="subtitle">{_escape(description)}</p><p class="refreshed">Last refreshed: {_escape(refreshed_at)} | Scope: {_escape(mode)} | Scheduled snapshot, not real-time billing</p></header>
{warning_html}
<details class="scope-details"><summary>Report scope: {_escape(mode)}</summary>{scope_html}</details>
{content}
</main>
{chart_script}
{_DASHBOARD_SCRIPT if is_dashboard else ""}
</body>
</html>
"""


def write_report(model, output_path):
    """Render a report and write it to output_path, returning the path."""

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_report(model), encoding="utf-8")
    return str(path)
