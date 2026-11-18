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
_STATUS_LABELS = {
    "ready": "Ready",
    "partial": "Partial",
    "unavailable": "Unavailable",
    "unsupported": "Not supported",
}
_TAB_ICONS = {
    "overview": "home",
    "explorer": "data-histogram",
    "allocation": "people-team",
    "billing": "receipt",
    "budgets": "wallet",
    "anomalies": "alert",
    "rightsizing": "arrow-trending",
    "commitments": "calendar-clock",
    "containers": "box",
    "ai": "brain-circuit",
    "unit-economics": "calculator",
    "sustainability": "leaf-one",
    "planning": "document-bullet-list",
    "governance": "shield-checkmark",
    "reliability": "pulse",
    "optimization": "checkmark-circle",
    "coverage": "database",
}
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
  const sectionSelect = document.getElementById('section-select');
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
    sectionSelect.value = String(index);
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
  sectionSelect.addEventListener('change', () => select(Number(sectionSelect.value), false));
  search.addEventListener('input', filter);
  document.getElementById('print-report').addEventListener('click', () => window.print());
  select(0, false);
  document.documentElement.classList.add('dashboard-enhanced');
})();
</script>"""


def _escape(value):
    return html.escape(str(value if value is not None else ""), quote=True)


def _icon(name):
    return (
        '<svg class="icon" aria-hidden="true" focusable="false">'
        f'<use href="#fluent-{name}"></use></svg>'
    )


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


def _sections(model, heading_level=2):
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
            dashboard_table = heading_level > 2
            table_hint = (
                '<p class="table-hint">Scroll horizontally to view more columns.</p>'
                if dashboard_table and len(columns) > 2 else ""
            )
            table_wrap = (
                f'<div class="table-wrap" role="region" tabindex="0" aria-label="{_escape(title)} table">'
                if dashboard_table else '<div class="table-wrap">'
            )
            column_scope = ' scope="col"' if dashboard_table else ""
            content = (
                table_hint + table_wrap + "<table><thead><tr>"
                + "".join(f"<th{column_scope}>{_escape(column)}</th>" for column in columns)
                + "</tr></thead><tbody>"
                + "".join(body)
                + "</tbody></table></div>"
            )
        else:
            content = f'<p class="empty">{_escape(empty_message)}</p>'
        rendered.append(
            f'<section><h{heading_level}>{_escape(title)}</h{heading_level}>'
            f'<p class="section-description">'
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
    if not isinstance(chart, dict):
        raise ValueError("chart must be an object")
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
    zero = 152 - (0 - low) / span * 144
    if chart_type == "line":
        coordinates = [
            (
                400 if len(values) == 1 else 8 + index * 784 / (len(values) - 1),
                152 - (value - low) / span * 144,
            )
            for index, value in enumerate(values)
        ]
        points = " ".join(f"{x:.2f},{y:.2f}" for x, y in coordinates)
        shapes = (f'<polyline points="{points}" fill="none" stroke="currentColor" '
                  'stroke-width="3" vector-effect="non-scaling-stroke"/>')
        if len(coordinates) <= 31:
            shapes += "".join(
                f'<circle cx="{x:.2f}" cy="{y:.2f}" r="3" fill="currentColor"/>'
                for x, y in coordinates
            )
    else:
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
    }]}, heading_level=4)

    def axis_label(value):
        if value == 0:
            return "0"
        if abs(value) >= 1:
            return f"{value:,.2f}".rstrip("0").rstrip(".")
        return f"{value:.4g}"

    ticks = "".join(
        f"<span>{_escape(axis_label(value))}</span>"
        for value in (high, low + (high - low) / 2, low)
    )
    x_labels = f'<span>{_escape(labels[0])}</span>'
    if len(labels) > 1:
        x_labels += f'<span>{_escape(labels[-1])}</span>'
    grid = "".join(
        f'<line x1="8" y1="{y}" x2="792" y2="{y}" class="chart-grid" '
        'vector-effect="non-scaling-stroke"/>'
        for y in (8, 80, 152)
    )
    if low < 0 < high:
        grid += (
            f'<line x1="8" y1="{zero:.2f}" x2="792" y2="{zero:.2f}" '
            'class="chart-baseline" vector-effect="non-scaling-stroke"/>'
        )
    return (
        f'<section class="chart-panel"><div class="chart-heading">'
        f'<div class="chart-heading-title">{_icon("chart-multiple")}<h3>{label}</h3></div>'
        f'<span class="chart-count">{len(values)} data points</span></div>'
        f'<div class="chart-plot"><div class="chart-ticks" aria-hidden="true">{ticks}</div>'
        f'<div class="chart-body"><figure><svg role="img" aria-label="{label}" '
        f'viewBox="0 0 800 160" preserveAspectRatio="none">'
        f'<title>{label}</title>{grid}{shapes}</svg></figure>'
        f'<div class="chart-x-labels" aria-hidden="true">{x_labels}</div></div></div>'
        f'<details class="chart-data"><summary>View chart data</summary>{table}</details></section>'
    )


def _dashboard(model):
    tabs = _list(model.get("tabs"), "tabs")
    if not tabs:
        raise ValueError("dashboard tabs must not be empty")
    buttons, options, panels, seen = [], [], [], set()
    for index, tab in enumerate(tabs):
        if not isinstance(tab, dict):
            raise ValueError(f"tabs[{index}] must be an object")
        key = _required_text(tab, "id")
        if not key.isascii() or not all(c.isalnum() or c == "-" for c in key) or key in seen:
            raise ValueError("dashboard tab IDs must be unique ASCII identifiers")
        seen.add(key)
        title, status = _required_text(tab, "title"), _required_text(tab, "status")
        if status not in _STATUS_LABELS:
            raise ValueError("invalid dashboard tab status")
        status_label = _STATUS_LABELS[status]
        icon = _TAB_ICONS.get(key, "chart-multiple")
        buttons.append(
            f'<button type="button" role="tab" id="tab-{key}" aria-controls="panel-{key}" '
            f'aria-selected="{str(index == 0).lower()}">'
            f'<span class="tab-title">{_icon(icon)}<span>{_escape(title)}</span></span>'
            f'<span class="tab-status {status}">{status_label}</span></button>'
        )
        options.append(
            f'<option value="{index}">{_escape(title)} - {status_label}</option>'
        )
        panel_warnings = str(tab.get("reason") or "")
        panels.append(
            f'<div role="tabpanel" tabindex="0" id="panel-{key}" aria-labelledby="heading-{key}">'
            f'<section class="panel-intro"><div class="panel-heading">'
            f'<div><h2 id="heading-{key}">{_escape(title)}</h2>'
            f'<p class="section-description">{_escape(tab.get("description"))}</p></div>'
            f'<span class="status-badge {status}">{status_label}</span></div>'
            f'<p class="provenance">Source: {_escape(tab.get("source"))} '
            f'<span aria-hidden="true"> | </span> As of: {_escape(tab.get("refreshedAt") or "Unknown")} '
            f'<span aria-hidden="true"> | </span> {_escape(tab.get("period"))}</p>'
            + (f'<aside class="warnings">{_escape(panel_warnings)}</aside>' if panel_warnings else "")
            + '</section>' + (_metrics(tab) if tab.get("metrics") else "")
            + _dashboard_chart(tab) + _sections(tab, heading_level=3) + '</div>'
        )
    return (
        '<div class="dashboard-layout"><aside class="dashboard-sidebar">'
        '<div class="sidebar-heading"><span>Report sections</span>'
        f'<span class="section-count">{len(tabs)}</span></div>'
        '<div class="mobile-section-selector"><label for="section-select">Report section</label>'
        '<select id="section-select">' + "".join(options) + '</select></div>'
        '<nav role="tablist" aria-label="FinOps sections" aria-orientation="vertical">'
        + "".join(buttons) + '</nav></aside><div class="dashboard-content">'
        '<div class="toolbar"><div class="search-control">'
        f'{_icon("search")}<label class="sr-only" for="table-search">Search table rows in this section</label>'
        '<input id="table-search" type="search" placeholder="Search rows in this section" '
        'aria-describedby="search-count"></div><span id="search-count" role="status"></span>'
        f'<button type="button" id="print-report">{_icon("print")}<span>Print / save PDF</span></button>'
        '</div>' + "".join(panels) + '</div></div>'
    )


def _dashboard_header(title, description, refreshed_at, mode, scopes):
    return (
        '<header class="dashboard-header"><div class="appbar">'
        f'<div class="appbar-brand"><span class="brand-icon">{_icon("cloud")}</span>'
        '<div><strong>FinOps workspace</strong><span>Azure cost visibility</span></div></div>'
        f'<div class="appbar-context">{_icon("agents")}<span>For Azure SRE Agent</span></div>'
        '</div><div class="page-heading"><div class="page-title">'
        f'<p class="eyebrow">{_icon("money-calculator")}<span>Azure Cost Management</span></p>'
        f'<h1>{_escape(title)}</h1><p class="subtitle">{_escape(description)}</p></div>'
        f'<div class="refresh-card">{_icon("clock")}<div><span>Last refreshed</span>'
        f'<strong>{_escape(refreshed_at)}</strong><small>Scheduled snapshot, not real-time billing</small>'
        '</div></div></div>'
        f'<details class="scope-details"><summary>{_icon("cloud")}'
        f'<span>Scope: <strong>{_escape(mode)}</strong></span>'
        f'<span class="scope-action">View scopes</span></summary>{scopes}</details></header>'
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
                f'<aside class="warnings quality-warning"><details><summary>{_icon("warning")}'
                f'<span><strong>Data quality</strong> · {len(warning_items)} '
                f'notice{"s" if len(warning_items) != 1 else ""} - review before acting</span>'
                f'<span class="notice-action">Review</span></summary><ul>{notices}</ul></details></aside>'
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
    header_html = (
        f'<header><p class="eyebrow">Azure FinOps / Decision workspace</p><h1>{_escape(title)}</h1>'
        f'<p class="subtitle">{_escape(description)}</p><p class="refreshed">Last refreshed: '
        f'{_escape(refreshed_at)} | Scope: {_escape(mode)} | Scheduled snapshot, not real-time billing</p></header>'
    )
    scope_details = f'<details class="scope-details"><summary>Report scope: {_escape(mode)}</summary>{scope_html}</details>'
    body_class, sprite = "", ""
    if is_dashboard:
        stylesheet += "\n" + Path(__file__).with_name("dashboard.css").read_text(encoding="utf-8")
        sprite = Path(__file__).with_name("fluent-icons.svg").read_text(encoding="utf-8")
        header_html = _dashboard_header(title, description, refreshed_at, mode, scope_html)
        scope_details = ""
        body_class = ' class="dashboard"'
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
<body{body_class}>
{sprite}<main>
{header_html}
{warning_html}
{scope_details}
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
