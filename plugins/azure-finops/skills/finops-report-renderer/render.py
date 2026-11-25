"""Deterministic CSP-compliant static HTML renderer for FinOps reports."""

import functools
import hashlib
import html
import json
import math
from decimal import Decimal
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parents[2]
_spec = spec_from_file_location("finops_renderer_models", Path(__file__).with_name("models.py"))
_models = module_from_spec(_spec)
_spec.loader.exec_module(_models)


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
    "unavailable": "Data not available",
    "unsupported": "Not supported",
}
_LIVE_REPORT_MAX_BYTES = 10_000_000
_NONCE_ALLOWANCE_BYTES = 4096
_TAB_ICONS = {
    "briefing": "agents",
    "overview": "home",
    "explorer": "product-cost-management",
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
  const themeButtons = [...document.querySelectorAll('[data-theme-choice]')];
  function applyTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    themeButtons.forEach(button => {
      button.disabled = false;
      button.setAttribute('aria-pressed', String(button.dataset.themeChoice === theme));
    });
  }
  themeButtons.forEach(button => button.addEventListener('click', () => applyTheme(button.dataset.themeChoice)));
  applyTheme(document.documentElement.getAttribute('data-theme') || 'light');
  const money = cents => {
    const magnitude = Math.abs(cents);
    return (cents < 0 ? '-' : '') + '$' + Math.trunc(magnitude / 100).toLocaleString('en-US') +
      '.' + String(magnitude % 100).padStart(2, '0');
  };
  const browsers = new Map();
  const chartAnimations = new Set();
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  let printing = false;
  function stopChartAnimations() {
    chartAnimations.forEach(animation => animation.cancel());
    chartAnimations.clear();
  }
  reducedMotion.addEventListener('change', event => {
    if (event.matches) stopChartAnimations();
  });
  function animateChart(chart) {
    if (reducedMotion.matches) return;
    const play = (element, frames, duration = 650, delay = 0) => {
      const animation = element.animate(frames, {
        duration, delay, easing: 'cubic-bezier(.16, 1, .3, 1)', fill: 'backwards'
      });
      chartAnimations.add(animation);
      animation.onfinish = animation.oncancel = () => chartAnimations.delete(animation);
    };
    chart.querySelectorAll('meter').forEach((meter, index) => {
      play(meter, {transform: ['scaleX(0)', 'scaleX(1)'],
        transformOrigin: ['left center', 'left center']}, 650, Math.min(index, 8) * 40);
    });
    chart.querySelectorAll('polyline').forEach(line => {
      const length = line.getTotalLength();
      if (length) play(line, {
        strokeDasharray: [String(length), String(length)], strokeDashoffset: [length, 0]
      }, 900);
    });
    const points = [...chart.querySelectorAll('figure svg circle, figure svg rect')];
    points.forEach((point, index) => {
      play(point, {opacity: [0, 1]}, 250, index / Math.max(1, points.length - 1) * 650);
    });
    chart.querySelectorAll('.column-fill').forEach((column, index) => {
      const horizontal = column.parentElement.getBoundingClientRect().height < 30;
      const origin = horizontal ? (column.dataset.negative === 'true' ? 'right center' : 'left center') :
        (column.dataset.negative === 'true' ? 'center top' : 'center bottom');
      const scale = horizontal ? 'scaleX' : 'scaleY';
      play(column, {transform: [scale + '(0)', scale + '(1)'],
        transformOrigin: [origin, origin]}, 650, Math.min(index, 10) * 35);
    });
  }
  document.querySelectorAll('.resource-browser').forEach(browser => {
    const state = {selected: new Map(), tones: new Map(), drill: new Map(), limit: 10,
      rows: [...browser.querySelectorAll('tbody tr[data-search]')]};
    browsers.set(browser, state);
    browser.querySelectorAll('[data-chart-choice]').forEach(button => {
      button.disabled = false;
      button.addEventListener('click', () => {
        browser.querySelectorAll('[data-chart-choice]').forEach(choice =>
          choice.setAttribute('aria-pressed', String(choice === button)));
        browser.querySelectorAll('[data-dimension]').forEach(chart => {
          chart.hidden = chart.dataset.dimension !== button.dataset.chartChoice;
        });
      });
    });
    browser.querySelectorAll('[data-dimension]').forEach((chart, index) => { chart.hidden = index !== 0; });
    const limit = browser.querySelector('[data-resource-limit]');
    limit.disabled = false;
    limit.addEventListener('change', () => { state.limit = Number(limit.value); filter(); });
    const download = browser.querySelector('.download-resources');
    download.disabled = false;
    download.addEventListener('click', () => {
      const headings = JSON.parse(browser.dataset.exportColumns);
      const values = state.rows.filter(row => matchesRow(state, row, search.value.toLocaleLowerCase().trim()));
      const csvCell = value => {
        let text = String(value);
        if (/^-?\\$[\\d,]+\\.\\d{2}$/.test(text)) text = text.replace(/[$,]/g, '');
        if (/^\\s*[=+@-]/.test(text) && !/^-?\\d+(\\.\\d+)?$/.test(text)) text = "'" + text;
        return '"' + text.replace(/"/g, '""') + '"';
      };
      const lines = [headings, ...values.map(row => [
        row.querySelector('.resource-identity summary').title,
        ...[...row.nextElementSibling.querySelectorAll('.resource-detail-field:not(.resource-detail-id) dd')]
          .map(cell => cell.textContent)
      ])].map(cells => cells.map(csvCell).join(','));
      const csv = '\\ufeff' + lines.join('\\r\\n') + '\\r\\n';
      const preview = browser.querySelector('.csv-preview');
      preview.hidden = false;
      preview.querySelector('textarea').value = csv;
      preview.querySelector('textarea').focus();
      if (window.self !== window.top) return;
      const blob = new Blob([csv], {type: 'text/csv;charset=utf-8'});
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url; link.download = browser.dataset.exportName; link.hidden = true;
      document.body.append(link);
      try { link.click(); }
      finally { link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000); }
    });
    browser.querySelector('.select-csv').addEventListener('click', () => {
      const text = browser.querySelector('.csv-preview textarea');
      text.focus();
      text.select();
    });
    browser.querySelector('.close-csv').addEventListener('click', () => {
      browser.querySelector('.csv-preview').hidden = true;
      download.focus();
    });
    browser.querySelectorAll('.resource-filter').forEach(dropdown => {
      dropdown.querySelectorAll('input').forEach(input => { input.disabled = false; });
      dropdown.addEventListener('change', event => {
        const input = event.target.closest('input[data-filter-option]');
        if (!input) return;
        const key = dropdown.dataset.filter;
        state.drill.delete(key);
        const values = new Set(state.selected.get(key) || []);
        if (input.checked) values.add(input.value);
        else values.delete(input.value);
        if (values.size) state.selected.set(key, values);
        else state.selected.delete(key);
        filter();
      });
      dropdown.addEventListener('keydown', event => {
        if (event.key === 'Escape') {
          event.preventDefault();
          dropdown.open = false;
          dropdown.querySelector('summary').focus();
        }
      });
    });
    browser.querySelectorAll('[data-filter-all]').forEach(button => {
      button.addEventListener('click', () => {
        state.selected.delete(button.dataset.filterAll);
        state.drill.delete(button.dataset.filterAll);
        filter();
      });
    });
    browser.querySelectorAll('[data-dimension]').forEach(chart => {
      state.tones.set(chart.dataset.dimension, new Map(Object.entries(JSON.parse(chart.dataset.toneMap))));
      chart.addEventListener('click', event => {
        if (event.target.closest('.chart-back')) {
          const stack = state.drill.get(chart.dataset.dimension) || [];
          stack.pop();
          if (stack.length) state.selected.set(chart.dataset.dimension, new Set(stack.at(-1)));
          else state.selected.delete(chart.dataset.dimension);
          filter();
          return;
        }
        const button = event.target.closest('[data-value]');
        if (!button) return;
        const key = chart.dataset.dimension;
        const selected = state.selected.get(key);
        const values = button.dataset.values ? JSON.parse(button.dataset.values) : [button.dataset.value];
        if (values.length > 1) {
          const stack = state.drill.get(key) || [];
          stack.push(values);
          state.drill.set(key, stack);
          state.selected.set(key, new Set(values));
        } else if (selectionEquals(selected, values)) {
          const parent = state.drill.get(key)?.at(-1);
          if (parent) state.selected.set(key, new Set(parent));
          else state.selected.delete(key);
        }
        else state.selected.set(key, new Set(values));
        filter();
        const target = [...chart.querySelectorAll('[data-value]')].find(item =>
          item.dataset.value === button.dataset.value) || chart.querySelector('.chart-back');
        if (!target.hidden) target.focus();
      });
    });
    browser.querySelector('.clear-breakdown').addEventListener('click', () => {
      state.selected.clear();
      state.drill.clear();
      search.value = '';
      filter();
    });
  });
  function selectionEquals(selected, values) {
    return selected?.size === values.length && values.every(value => selected.has(value));
  }
  function matchesRow(state, row, term, except) {
    return row.dataset.periodPresent !== 'false' &&
      row.dataset.search.toLocaleLowerCase().includes(term) &&
      [...state.selected].every(([key, values]) => key === except || values.has(row.dataset[key]));
  }
  function compactEntries(entries) {
    const result = entries.map(([name, group]) => [name, {...group, values: [name]}]);
    if (result.length <= 11) return result;
    const rest = result.slice(10);
    let name = 'Other (' + rest.length + ')';
    while (entries.some(([value]) => value === name)) name += ' combined';
    const group = rest.reduce((sum, [, item]) => ({
      total: sum.total + item.total, count: sum.count + item.count,
      unknown: sum.unknown + item.unknown, values: [...sum.values, ...item.values]
    }), {total: 0, count: 0, unknown: 0, values: []});
    return [...result.slice(0, 10), [name, group]];
  }
  function drawColumns(chart, entries, tones, selected, depth) {
    const compact = compactEntries(entries);
    const high = Math.max(0, ...compact.map(([, group]) => group.total));
    const low = Math.min(0, ...compact.map(([, group]) => group.total));
    const span = high - low || 1;
    const verticalZero = high / span * 160;
    const horizontalZero = -low / span * 100;
    const axis = chart.querySelector('.column-axis');
    axis.replaceChildren();
    for (let index = 0; high !== low && index < 5; index++) {
      const tick = document.createElement('span');
      tick.textContent = money(Math.round(high - span * index / 4));
      axis.append(tick);
    }
    chart.querySelector('.chart-back').hidden = !depth;
    chart.querySelector('.chart-caption').textContent = entries.length ?
      entries.length + ' categories' + (entries.length > 11 ? ' · Top 10 + Other' : '') +
        (depth ? ' · Exploring Other' : '') : 'No categories match these filters';
    const list = chart.querySelector('.breakdown-bars');
    list.className = 'breakdown-bars columns-' + Math.max(1, compact.length);
    list.replaceChildren();
    const svgNS = 'http://www.w3.org/2000/svg';
    compact.forEach(([value, group]) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'chart-tone-' + (group.values.length > 1 ? 'other' : tones.get(value));
      button.dataset.value = value;
      button.dataset.values = JSON.stringify(group.values);
      button.setAttribute('aria-pressed', String(selectionEquals(selected, group.values)));
      const amount = group.unknown === group.count ? 'Not quantified' : money(group.total);
      const description = value + ': ' + amount + ', ' + group.count + ' records' +
        (group.unknown ? ', ' + group.unknown + ' unquantified' : '');
      button.title = description;
      button.setAttribute('aria-label', description);
      const label = document.createElement('span');
      label.className = 'breakdown-name'; label.textContent = value;
      const cost = document.createElement('strong'); cost.textContent = amount;
      const vertical = document.createElementNS(svgNS, 'svg');
      vertical.setAttribute('class', 'column-track-vertical');
      vertical.setAttribute('viewBox', '0 0 100 160');
      vertical.setAttribute('preserveAspectRatio', 'none');
      vertical.setAttribute('aria-hidden', 'true');
      const baseline = document.createElementNS(svgNS, 'line');
      [['x1', 0], ['x2', 100], ['y1', verticalZero], ['y2', verticalZero], ['class', 'column-zero']]
        .forEach(([name, value]) => baseline.setAttribute(name, value));
      const bar = document.createElementNS(svgNS, 'rect');
      const barHeight = Math.abs(group.total) / span * 160;
      const barY = group.total < 0 ? verticalZero : verticalZero - barHeight;
      [['x', 22], ['width', 56], ['y', barY], ['height', barHeight],
       ['class', 'column-fill'], ['data-negative', String(group.total < 0)]]
        .forEach(([name, value]) => bar.setAttribute(name, value));
      vertical.append(baseline, bar);
      const horizontal = document.createElementNS(svgNS, 'svg');
      horizontal.setAttribute('class', 'column-track-horizontal');
      horizontal.setAttribute('viewBox', '0 0 100 14');
      horizontal.setAttribute('preserveAspectRatio', 'none');
      horizontal.setAttribute('aria-hidden', 'true');
      const zero = document.createElementNS(svgNS, 'line');
      [['x1', horizontalZero], ['x2', horizontalZero], ['y1', 0], ['y2', 14], ['class', 'column-zero']]
        .forEach(([name, value]) => zero.setAttribute(name, value));
      const horizontalBar = document.createElementNS(svgNS, 'rect');
      const barWidth = Math.abs(group.total) / span * 100;
      const barX = group.total < 0 ? (group.total - low) / span * 100 : horizontalZero;
      [['x', barX], ['width', barWidth], ['y', 2], ['height', 10],
       ['class', 'column-fill'], ['data-negative', String(group.total < 0)]]
        .forEach(([name, value]) => horizontalBar.setAttribute(name, value));
      horizontal.append(zero, horizontalBar);
      button.append(cost, vertical, horizontal, label); list.append(button);
    });
  }
  function updateBrowser(browser, term) {
    browser.querySelector('.csv-preview').hidden = true;
    const state = browsers.get(browser);
    browser.querySelectorAll('.resource-filter').forEach(dropdown => {
      const selected = state.selected.get(dropdown.dataset.filter);
      dropdown.querySelector('.filter-selection').textContent = selected ? '(' + selected.size + ' selected)' : 'All';
      dropdown.querySelector('summary').title = selected ? [...selected].join(', ') : 'All';
      dropdown.querySelectorAll('input[data-filter-option]').forEach(input => {
        input.checked = selected?.has(input.value) || false;
      });
    });
    browser.querySelectorAll('[data-filter-all]').forEach(button => {
      button.disabled = false;
      button.setAttribute('aria-pressed', String(!state.selected.has(button.dataset.filterAll)));
    });
    const matches = (row, except) => matchesRow(state, row, term, except);
    let total = 0, visible = 0, quantified = 0;
    state.rows.forEach(row => {
      const matched = matches(row);
      row.hidden = !matched || visible >= state.limit;
      row.nextElementSibling.hidden = row.hidden;
      if (matched) {
        visible++;
        if (row.dataset.amount !== '') { total += Number(row.dataset.amount); quantified++; }
      }
    });
    const totalLabel = visible && !quantified ? 'No quantified amount' : money(total);
    browser.querySelector('.breakdown-result').textContent =
      visible + ' records · ' + totalLabel + (quantified < visible ? ' · ' + (visible - quantified) + ' unquantified' : '') +
      (state.selected.size ? ' · ' + [...state.selected.values()].map(values =>
        values.size === 1 ? [...values][0] : values.size + ' selected').join(' / ') : ' · All resources');
    browser.querySelector('.resource-preview-count').textContent =
      'Showing ' + Math.min(visible, state.limit) + ' of ' + visible + ' matching records';
    browser.querySelector('.download-resources').disabled = visible === 0;
    browser.querySelector('.clear-breakdown').disabled = state.selected.size === 0 && !term;
    browser.querySelector('.breakdown-empty').hidden = visible !== 0;
    browser.querySelectorAll('[data-dimension]').forEach(chart => {
      const key = chart.dataset.dimension;
      const groups = new Map();
      state.rows.filter(row => matches(row, key)).forEach(row => {
        const value = row.dataset[key];
        const group = groups.get(value) || {total: 0, count: 0, unknown: 0};
        group.count++;
        if (row.dataset.amount !== '') group.total += Number(row.dataset.amount);
        else group.unknown++;
        groups.set(value, group);
      });
      const stack = state.drill.get(key) || [];
      const subset = stack.at(-1);
      const entries = [...groups].filter(([value]) => !subset || subset.includes(value))
        .sort((a, b) => b[1].total - a[1].total || a[0].localeCompare(b[0]));
      drawColumns(chart, entries, state.tones.get(key), state.selected.get(key), stack.length);
    });
    return visible;
  }
  function filter() {
    const term = search.value.toLocaleLowerCase().trim();
    let matches = 0;
    const panel = panels.find(p => !p.hidden);
    for (const browser of panel.querySelectorAll('.resource-browser')) matches += updateBrowser(browser, term);
    for (const row of panel.querySelectorAll('tbody tr')) {
      if (row.closest('.resource-browser')) continue;
      row.hidden = !row.textContent.toLocaleLowerCase().includes(term);
      if (!row.hidden) matches++;
    }
    for (const card of panel.querySelectorAll('.decision-card')) {
      const job = panel.dataset.jobFilter || '';
      card.closest('li').hidden = (job && card.dataset.job !== job) ||
        !card.textContent.toLocaleLowerCase().includes(term);
      if (term && !card.closest('li').hidden) matches++;
    }
    count.textContent = term ? matches + ' matching rows in this section' : '';
  }
  document.querySelectorAll('[data-job-filter]').forEach(button => {
    button.addEventListener('click', () => {
      const panel = button.closest('[role="tabpanel"]');
      panel.dataset.jobFilter = button.dataset.jobFilter;
      panel.querySelectorAll('[data-job-filter]').forEach(chip =>
        chip.setAttribute('aria-pressed', String(chip === button)));
      filter();
    });
  });
  document.querySelectorAll('.copy-prompt').forEach(button => {
    button.addEventListener('click', () => {
      const holder = button.closest('.decision-card, .rollup-card, .briefing-block');
      const fallback = holder.querySelector('.prompt-fallback');
      const text = fallback.querySelector('textarea');
      text.value = button.dataset.prompt;
      const label = button.querySelector('span');
      const original = label.textContent;
      const done = copied => {
        fallback.hidden = copied;
        if (!copied) { text.focus(); text.select(); return; }
        label.textContent = 'Copied: paste in agent chat';
        setTimeout(() => { label.textContent = original; }, 2400);
      };
      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(button.dataset.prompt).then(() => done(true), () => done(false));
      } else {
        done(false);
      }
    });
  });
  function select(index, focus) {
    stopChartAnimations();
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
  document.getElementById('clear-report-filters').addEventListener('click', () => {
    search.value = '';
    browsers.forEach(state => { state.selected.clear(); state.drill.clear(); });
    document.querySelectorAll('[role="tabpanel"]').forEach(panel => { delete panel.dataset.jobFilter; });
    document.querySelectorAll('[data-job-filter]').forEach(chip =>
      chip.setAttribute('aria-pressed', String(chip.dataset.jobFilter === '')));
    filter();
  });
  document.querySelectorAll('[data-browse-services]').forEach(button => {
    button.addEventListener('click', () => {
      search.value = '';
      select(tabs.findIndex(tab => tab.id === 'tab-explorer'), false);
      const dropdown = document.querySelector('#panel-explorer .resource-filter[data-filter="d0"]');
      if (dropdown) { dropdown.open = true; dropdown.querySelector('summary').focus(); }
    });
  });
  document.addEventListener('click', event => {
    if (event.target.closest('[data-browse-services]')) return;
    document.querySelectorAll('.resource-filter[open]').forEach(dropdown => {
      if (!dropdown.contains(event.target)) dropdown.open = false;
    });
  });
  document.getElementById('print-report').addEventListener('click', () => {
    if (window.self === window.top) { window.print(); return; }
    printing = true;
    stopChartAnimations();
    document.documentElement.classList.add('report-print-view');
    document.getElementById('print-help').hidden = false;
    document.getElementById('close-print-view').focus();
  });
  document.getElementById('close-print-view').addEventListener('click', () => {
    printing = false;
    document.documentElement.classList.remove('report-print-view');
    document.getElementById('print-help').hidden = true;
    document.getElementById('print-report').focus();
  });
  let printDetails = null;
  window.addEventListener('beforeprint', () => {
    printing = true;
    stopChartAnimations();
    if (printDetails !== null) return;
    printDetails = [...document.querySelectorAll('.resource-identity:not([open])')];
    printDetails.forEach(detail => { detail.open = true; });
  });
  window.addEventListener('afterprint', () => {
    printing = false;
    printDetails?.forEach(detail => { detail.open = false; });
    printDetails = null;
  });
  const historyData = document.getElementById('cost-history-data');
  if (historyData) {
    const range = document.getElementById('history-range');
    const custom = document.getElementById('history-custom');
    const startInput = document.getElementById('history-start');
    const endInput = document.getElementById('history-end');
    const error = document.getElementById('history-error');
    const period = document.getElementById('history-period');
    const history = JSON.parse(historyData.textContent);
    const baseTime = Date.parse(history.window[0] + 'T00:00:00Z');
    const dayMs = 86400000;
    const dayLabel = day => new Date(baseTime + day * dayMs).toISOString().slice(0, 10);
    const offset = value => {
      if (!/^[0-9]{4}-[0-9]{2}-[0-9]{2}$/.test(value)) return null;
      const time = Date.parse(value + 'T00:00:00Z');
      if (!Number.isFinite(time) || new Date(time).toISOString().slice(0, 10) !== value) return null;
      return (time - baseTime) / dayMs;
    };
    const showError = message => { error.textContent = message; error.hidden = false; };
    if (typeof BigInt !== 'function') {
      showError('This browser cannot calculate historical costs. The saved 30-day view remains available.');
    } else {
      const zero = BigInt(0), two = BigInt(2);
      const factor = BigInt(10) ** BigInt(history.scale - 2);
      const maxSafe = BigInt(Number.MAX_SAFE_INTEGER);
      const overview = history.overview.rows.map(([day, service, group, amount]) =>
        [day, service, group, BigInt(amount)]);
      const series = history.explorer.map(record => record.map(([day, amount]) => [day, BigInt(amount)]));
      const costBrowser = document.querySelector('#panel-explorer .resource-browser');
      const costState = browsers.get(costBrowser);
      let appliedOption = '30';
      function cents(amount) {
        const negative = amount < zero;
        const magnitude = negative ? -amount : amount;
        let value = magnitude / factor;
        if ((magnitude % factor) * two >= factor) value += BigInt(1);
        if (value > maxSafe) throw new RangeError('The selected cost totals exceed this browser’s exact range.');
        return Number(negative ? -value : value);
      }
      function add(map, key, amount) {
        map.set(key, (map.get(key) || zero) + amount);
      }
      function ranked(map, names) {
        return [...map].sort((a, b) => {
          if (a[1] !== b[1]) return a[1] > b[1] ? -1 : 1;
          const first = names[a[0]].toLocaleLowerCase(), second = names[b[0]].toLocaleLowerCase();
          return first < second ? -1 : first > second ? 1 : 0;
        }).slice(0, 15).map(([key, amount]) => ({key, cents: cents(amount)}));
      }
      function calculate(first, last) {
        const daily = new Map(), services = new Map(), groups = new Map();
        let total = zero;
        overview.forEach(([day, service, group, amount]) => {
          if (day < first || day > last) return;
          total += amount;
          add(daily, day, amount); add(services, service, amount); add(groups, group, amount);
        });
        const resources = series.map(record => {
          let sum = zero, present = false;
          record.forEach(([day, amount]) => {
            if (day >= first && day <= last) { sum += amount; present = true; }
          });
          return {present, cents: cents(sum)};
        });
        let magnitude = 0;
        resources.forEach(item => {
          magnitude += Math.abs(item.cents);
          if (!Number.isSafeInteger(magnitude)) {
            throw new RangeError('The selected resource totals exceed this browser’s exact range.');
          }
        });
        return {
          total: cents(total),
          daily: [...daily].sort((a, b) => a[0] - b[0]).map(([day, amount]) => ({day, cents: cents(amount)})),
          services: ranked(services, history.overview.services.map(item => item.name)),
          groups: ranked(groups, history.overview.groups),
          resources
        };
      }
      function appendRow(tbody, values) {
        const row = document.createElement('tr');
        values.forEach(value => {
          const cell = document.createElement('td');
          cell.textContent = value;
          row.append(cell);
        });
        tbody.append(row);
      }
      function empty(container, message) {
        const section = document.createElement('section');
        const paragraph = document.createElement('p');
        paragraph.className = 'empty';
        paragraph.textContent = message;
        section.append(paragraph);
        container.replaceChildren(section);
      }
      function drawTrend(daily) {
        const slot = document.getElementById('history-trend-slot');
        if (!daily.length) { empty(slot, 'No daily cost records for the selected dates.'); return; }
        const open = slot.querySelector('.chart-data')?.open || false;
        const content = document.getElementById('history-trend-template').content.cloneNode(true);
        content.querySelector('.chart-data').open = open;
        content.querySelector('.chart-count').textContent = daily.length + ' data points';
        const values = daily.map(item => item.cents / 100);
        const low = Math.min(0, ...values), high = Math.max(0, ...values), span = high - low || 1;
        const axis = value => {
          if (value === 0) return '0';
          if (Math.abs(value) >= 1) return value.toLocaleString('en-US', {maximumFractionDigits: 2});
          return Number(value.toPrecision(4)).toString();
        };
        content.querySelectorAll('.chart-ticks span').forEach((tick, index) => {
          tick.textContent = axis([high, low + (high - low) / 2, low][index]);
        });
        const labels = content.querySelector('.chart-x-labels');
        labels.replaceChildren();
        [daily[0].day, ...(daily.length > 1 ? [daily[daily.length - 1].day] : [])].forEach(day => {
          const label = document.createElement('span');
          label.textContent = dayLabel(day);
          labels.append(label);
        });
        const svg = content.querySelector('figure svg');
        svg.querySelectorAll('polyline, circle, .chart-baseline').forEach(shape => shape.remove());
        const ns = 'http://www.w3.org/2000/svg';
        if (low < 0 && high > 0) {
          const baseline = document.createElementNS(ns, 'line');
          const y = 152 - (0 - low) / span * 144;
          [['x1', 8], ['x2', 792], ['y1', y], ['y2', y], ['class', 'chart-baseline'],
           ['vector-effect', 'non-scaling-stroke']].forEach(([name, value]) => baseline.setAttribute(name, value));
          svg.append(baseline);
        }
        const coordinates = values.map((value, index) => [
          values.length === 1 ? 400 : 8 + index * 784 / (values.length - 1),
          152 - (value - low) / span * 144
        ]);
        const line = document.createElementNS(ns, 'polyline');
        line.setAttribute('points', coordinates.map(point => point.join(',')).join(' '));
        line.setAttribute('fill', 'none'); line.setAttribute('stroke', 'currentColor');
        line.setAttribute('stroke-width', '3'); line.setAttribute('vector-effect', 'non-scaling-stroke');
        svg.append(line);
        if (coordinates.length <= 31) coordinates.forEach(([x, y]) => {
          const dot = document.createElementNS(ns, 'circle');
          dot.setAttribute('cx', x); dot.setAttribute('cy', y); dot.setAttribute('r', 3);
          dot.setAttribute('fill', 'currentColor'); svg.append(dot);
        });
        const tbody = content.querySelector('.chart-data tbody');
        tbody.replaceChildren();
        daily.forEach(item => appendRow(tbody, [dayLabel(item.day), money(item.cents)]));
        slot.replaceChildren(content);
      }
      function drawServices(items) {
        const slot = document.getElementById('history-services-slot');
        const open = slot.querySelector('.chart-data')?.open || false;
        const content = document.getElementById('history-services-template').content.cloneNode(true);
        const chart = content.querySelector('.service-chart');
        const list = content.querySelector('.service-bars');
        const details = content.querySelector('.chart-data');
        details.open = open;
        const tbody = details.querySelector('tbody');
        list.replaceChildren(); tbody.replaceChildren();
        const maximum = Math.max(1, ...items.map(item => Math.abs(item.cents)));
        items.forEach(item => {
          const service = history.overview.services[item.key];
          const li = document.createElement('li');
          li.className = 'chart-tone-' + service.tone;
          const symbol = document.createElement('span'); symbol.className = 'service-symbol';
          const ns = 'http://www.w3.org/2000/svg';
          const icon = document.createElementNS(ns, 'svg');
          icon.setAttribute('class', 'icon'); icon.setAttribute('aria-hidden', 'true');
          icon.setAttribute('focusable', 'false');
          const use = document.createElementNS(ns, 'use');
          use.setAttribute('href', '#' + service.icon); icon.append(use); symbol.append(icon);
          const name = document.createElement('span');
          name.className = 'service-label'; name.textContent = service.name;
          const amount = document.createElement('strong'); amount.textContent = money(item.cents);
          const meter = document.createElement('meter');
          meter.min = 0; meter.max = maximum; meter.value = Math.abs(item.cents);
          meter.setAttribute('aria-label', service.name + ' cost magnitude in USD cents');
          li.append(symbol, name, amount, meter); list.append(li);
          appendRow(tbody, [service.name, money(item.cents)]);
        });
        if (!items.length) {
          list.hidden = true; details.hidden = true;
          const message = document.createElement('p');
          message.className = 'empty'; message.textContent = 'No service cost rows for the selected dates.';
          chart.append(message);
        }
        slot.replaceChildren(content);
      }
      function drawGroups(items) {
        const slot = document.getElementById('history-groups-slot');
        const content = document.getElementById('history-groups-template').content.cloneNode(true);
        const table = content.querySelector('.table-wrap');
        const tbody = table.querySelector('tbody');
        tbody.replaceChildren();
        items.forEach(item => appendRow(tbody, [history.overview.groups[item.key], money(item.cents)]));
        if (!items.length) {
          table.hidden = true;
          const message = document.createElement('p');
          message.className = 'empty'; message.textContent = 'No resource-group cost rows for the selected dates.';
          content.querySelector('section').append(message);
        }
        slot.replaceChildren(content);
      }
      function applyRange(first, last, option) {
        let result;
        try { result = calculate(first, last); }
        catch (failure) {
          if (!(failure instanceof RangeError)) throw failure;
          showError(failure.message);
          return false;
        }
        const start = dayLabel(first), end = dayLabel(last);
        const metrics = document.querySelectorAll('#panel-overview .metric');
        metrics[0].querySelector('.metric-label').textContent =
          option === '30' ? '30-day spend' : option === '90' ? '90-day spend' : 'Selected-period spend';
        metrics[0].querySelector('.metric-value').textContent = money(result.total);
        metrics[1].querySelector('.metric-value').textContent = String(result.daily.length);
        metrics[2].querySelector('.metric-value').textContent = String(result.services.length);
        drawTrend(result.daily); drawServices(result.services); drawGroups(result.groups);
        costState.rows.forEach(row => {
          const item = result.resources[Number(row.dataset.historyIndex)];
          const detail = row.nextElementSibling;
          row.dataset.periodPresent = String(item.present);
          row.dataset.amount = String(item.cents);
          row.dataset.search = row.dataset.searchBase + ' ' + money(item.cents);
          row.querySelector('.resource-cost').textContent = money(item.cents);
          detail.querySelector('.resource-detail-cost').textContent = money(item.cents);
          row.classList.toggle('period-inactive', !item.present);
          detail.classList.toggle('period-inactive', !item.present);
        });
        costState.rows.sort((a, b) => {
          const present = Number(b.dataset.periodPresent === 'true') - Number(a.dataset.periodPresent === 'true');
          return present || Number(b.dataset.amount) - Number(a.dataset.amount) ||
            Number(a.dataset.historyIndex) - Number(b.dataset.historyIndex);
        });
        const pairs = costState.rows.map(row => [row, row.nextElementSibling]);
        const tbody = costBrowser.querySelector('tbody');
        pairs.forEach(([row, detail]) => tbody.append(row, detail));
        updateBrowser(costBrowser, '');
        period.textContent = start + ' to ' + end + ' UTC';
        ['overview', 'explorer'].forEach(id => {
          document.querySelector('#panel-' + id + ' .source-period').textContent = start + ' to ' + end + ' UTC';
        });
        document.querySelectorAll('#coverage-source tbody tr').forEach(row => {
          if (['Overview', 'Cost explorer'].includes(row.cells[0].textContent)) {
            row.cells[4].textContent = start + ' to ' + end + ' UTC';
          }
        });
        startInput.value = start; endInput.value = end;
        error.hidden = true; appliedOption = option;
        filter();
        return true;
      }
      range.disabled = false;
      range.addEventListener('change', () => {
        error.hidden = true;
        custom.hidden = range.value !== 'custom';
        if (range.value === 'custom') { startInput.focus(); return; }
        const last = offset(history.window[1]);
        const first = range.value === '90' ? 0 : last - 29;
        if (!applyRange(first, last, range.value)) {
          range.value = appliedOption;
          custom.hidden = appliedOption !== 'custom';
        }
      });
      document.getElementById('history-apply').addEventListener('click', () => {
        const first = offset(startInput.value), last = offset(endInput.value);
        if (first === null || last === null || first < 0 || last > 89 || first > last) {
          showError('Choose valid UTC dates within the saved 90-day history, with From on or before To.');
          return;
        }
        applyRange(first, last, 'custom');
      });
    }
  }
  if ('IntersectionObserver' in window && Element.prototype.animate) {
    const observer = new IntersectionObserver(entries => {
      entries.forEach(entry => {
        if (!entry.isIntersecting || entry.target.closest('[role="tabpanel"]')?.hidden ||
            printing || window.matchMedia('print').matches) return;
        observer.unobserve(entry.target);
        animateChart(entry.target);
      });
    });
    document.querySelectorAll('.chart-panel, .service-bars, .breakdown-chart').forEach(chart => observer.observe(chart));
  }
  select(0, false);
  document.documentElement.classList.add('dashboard-enhanced');
})();
</script>"""


def _escape(value):
    return html.escape(str(value if value is not None else ""), quote=True)


def _icon(name):
    symbol = name if name.startswith("product-") else "fluent-" + name
    return (
        '<svg class="icon" aria-hidden="true" focusable="false">'
        f'<use href="#{symbol}"></use></svg>'
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


def _sections(model, heading_level=2, id_prefix="section"):
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

        resource_ids = section.get("resourceIds")
        name_column = None
        if resource_ids is not None:
            resource_ids = _list(resource_ids, f"sections[{index}].resourceIds")
            if len(resource_ids) != len(rows) or not all(isinstance(item, str) for item in resource_ids):
                raise ValueError(f"sections[{index}].resourceIds must contain one text ID per row")
            if columns.count("Name") != 1:
                raise ValueError(f"sections[{index}].resourceIds requires one Name column")
            name_column = columns.index("Name")

        body = []
        for row_index, row in enumerate(rows):
            if not isinstance(row, list) or len(row) != len(columns):
                raise ValueError(
                    f"sections[{index}].rows[{row_index}] must match columns"
                )
            cells = []
            for column_index, cell in enumerate(row):
                value = _escape(cell)
                if name_column == column_index and resource_ids[row_index]:
                    value = (
                        f'<details class="resource-identity"><summary>{value}</summary>'
                        '<span class="resource-context">Full resource ID: '
                        f'<code>{_escape(resource_ids[row_index])}</code></span></details>'
                    )
                cells.append(f"<td>{value}</td>")
            body.append("<tr>" + "".join(cells) + "</tr>")

        if section.get("breakdown") and heading_level > 2:
            rendered.append(_resource_browser(section, f"{id_prefix}-{index}"))
            continue
        if section.get("visualization") == "service-bars" and heading_level > 2 and rows:
            rendered.append(_service_bars(section))
            continue
        if body:
            dashboard_table = heading_level > 2
            table_hint = (
                '<p class="table-hint">Scroll horizontally to view more columns.</p>'
                if dashboard_table and len(columns) > 2 else ""
            )
            table_class = "table-wrap resource-identity-table" if resource_ids is not None else "table-wrap"
            table_wrap = (
                f'<div class="{table_class}" role="region" tabindex="0" aria-label="{_escape(title)} table">'
                if dashboard_table else f'<div class="{table_class}">'
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
        section_id = ' id="coverage-source"' if id_prefix == "coverage" and title == "Source coverage" else ""
        rendered.append(
            f'<section{section_id}><h{heading_level}>{_escape(title)}</h{heading_level}>'
            f'<p class="section-description">'
            f"{_escape(description)}</p>{content}</section>"
        )
    return "".join(rendered)


def _compact_breakdown(entries):
    members = {value: [value] for value, _ in entries}
    if len(entries) <= 11:
        return entries, members
    rest = entries[10:]
    label = f"Other ({len(rest)})"
    while label in members:
        label += " combined"
    members[label] = [value for value, _ in rest]
    totals = tuple(sum(group[index] for _, group in rest) for index in range(3))
    return entries[:10] + [(label, totals)], members


def _column_chart(entries, tones):
    compact, members = _compact_breakdown(entries)
    high = max([amount for _, (amount, _, _) in compact] + [0])
    low = min([amount for _, (amount, _, _) in compact] + [0])
    span = high - low or 1
    axis = "".join(f"<span>{_usd(round(Decimal(high) - Decimal(span) * index / 4))}</span>"
                   for index in range(5)) if high != low else ""
    columns = []
    def svg_line(css_class, **attrs):
        rendered_attrs = " ".join(f'{name.replace("_", "-")}="{_escape(value)}"'
                                  for name, value in attrs.items())
        return f'<line class="{css_class}" {rendered_attrs}/>'
    for value, (amount, count, missing) in compact:
        tone = tones.get(value, "other")
        label = "Not quantified" if missing == count else _usd(amount)
        description = f"{value}: {label}, {count} records"
        if missing:
            description += f", {missing} unquantified"
        vertical_zero = high / span * 160
        horizontal_zero = -low / span * 100
        bar_height = abs(amount) / span * 160
        bar_y = vertical_zero if amount < 0 else vertical_zero - bar_height
        bar_width = abs(amount) / span * 100
        bar_x = (amount - low) / span * 100 if amount < 0 else horizontal_zero
        columns.append(
            f'<button type="button" class="chart-tone-{tone}" '
            f'data-value="{_escape(value)}" data-values="{_escape(json.dumps(members[value]))}" '
            f'aria-label="{_escape(description)}" title="{_escape(description)}" aria-pressed="false" disabled="disabled">'
            f'<strong>{_escape(label)}</strong><svg class="column-track-vertical" viewBox="0 0 100 160" '
            f'preserveAspectRatio="none" aria-hidden="true">'
            f'{svg_line("column-zero", x1=0, x2=100, y1=vertical_zero, y2=vertical_zero)}'
            f'<rect class="column-fill" x="22" width="56" y="{bar_y:.6f}" height="{bar_height:.6f}" '
            f'data-negative="{str(amount < 0).lower()}"/></svg>'
            f'<svg class="column-track-horizontal" viewBox="0 0 100 14" preserveAspectRatio="none" aria-hidden="true">'
            f'{svg_line("column-zero", x1=horizontal_zero, x2=horizontal_zero, y1=0, y2=14)}'
            f'<rect class="column-fill" x="{bar_x:.6f}" width="{bar_width:.6f}" y="2" height="10" '
            f'data-negative="{str(amount < 0).lower()}"/></svg>'
            f'<span class="breakdown-name">{_escape(value)}</span></button>'
        )
    return (
        '<div class="column-plot">'
        f'<div class="column-axis" aria-hidden="true">{axis}</div>'
        f'<div class="breakdown-bars columns-{max(1, len(compact))}">{"".join(columns)}</div></div>'
    )


def _resource_browser(section, id_prefix="resource"):
    """Render compact identities and faceted, exact-cent breakdowns from adapter tables."""
    config = section["breakdown"]
    dimensions = config["dimensions"]
    savings = config["kind"] == "savings"
    groups = [{}, {}]
    all_values = [set(), set()]
    body = []
    total = 0
    unknown = 0
    visible_records = 0
    filter_values = {"subscription": set(), "type": set(), "d0": set(), "d1": set()}
    history_records = section.get("historyRecords")
    def resource_key(fields):
        return tuple(value.strip().casefold() if index in {0, 1, 2, 4} else value.strip()
                     for index, value in enumerate(fields))

    history_lookup = {
        resource_key(record["fields"]): (index, record["active"])
        for index, record in enumerate(history_records or [])
    }
    rows = section["rows"]
    rows = sorted(rows, key=lambda row: (
        row[config["amount"]] == "\u2014",
        -Decimal(row[config["amount"]].replace("$", "").replace(",", ""))
        if row[config["amount"]] != "\u2014" else Decimal(0),
    ))
    for row_index, row in enumerate(rows):
        history_record = history_lookup.get(resource_key(row[:6])) if history_records is not None else None
        if history_records is not None and history_record is None:
            raise ValueError("resource record is missing its cost history")
        active = history_record is None or history_record[1]
        amount = row[config["amount"]]
        cents = None if amount == "\u2014" else int(Decimal(amount.replace("$", "").replace(",", "")) * 100)
        if active:
            visible_records += 1
            if cents is None:
                unknown += 1
            else:
                total += cents
        attrs = []
        for index, (_, column) in enumerate(dimensions):
            value = str(row[column]).strip() or "(unknown)"
            if savings and index == 0:
                value = _resource_type_label(value)
            attrs.append(f'data-d{index}="{_escape(value)}"')
            all_values[index].add(value)
            if not savings:
                filter_values[f"d{index}"].add(value)
            if active:
                group = groups[index].setdefault(value, [0, 0, 0])
                group[0] += cents or 0
                group[1] += 1
                group[2] += cents is None
        identity = str(row[0])
        resource_type = _resource_type_from_id(identity) if not savings else str(row[1])
        if not savings:
            subscription = str(row[1]).strip() or "(unknown)"
            type_label = _resource_type_label(resource_type)
            attrs.extend([f'data-subscription="{_escape(subscription)}"',
                          f'data-type="{_escape(type_label)}"'])
            for key, value in (("subscription", subscription), ("type", type_label),
                               ("d1", str(row[4]).strip() or "(unknown)")):
                filter_values[key].add(value)
        name = identity.rstrip("/").rsplit("/", 1)[-1] or "(unknown)"
        segments = identity.split("/")
        group_name = next((segments[i + 1] for i, part in enumerate(segments[:-1])
                           if part.lower() == "resourcegroups"), "(unknown)")
        details = "".join(
            f'<div class="resource-detail-field"><dt>{_escape(label)}</dt>'
            + ('<dd class="resource-detail-cost">' if not savings and index == config["amount"] else "<dd>")
            + f'{_escape(value)}</dd></div>'
            for index, (label, value) in enumerate(zip(section["columns"], row)) if index
        )
        if not savings:
            details += f'<div class="resource-detail-field"><dt>Resource type</dt><dd>{_escape(resource_type)}</dd></div>'
        details += (
            f'<div class="resource-detail-field resource-detail-id"><dt>{_escape(section["columns"][0])}</dt>'
            f'<dd>{_escape(identity)}</dd></div>'
        )
        detail_id = f"{id_prefix}-resource-{row_index}"
        identity_html = (
            f'<details class="resource-identity"><summary title="{_escape(identity)}" aria-controls="{detail_id}">'
            f'{_icon(_service_icon(row[1] if savings else row[3]))}'
            f'<span class="resource-name">{_escape(name)}</span></summary></details>'
            f'<span class="resource-context">{_escape(group_name if savings else row[2])}</span>'
        )
        cells = (
            [identity_html, _escape(_resource_type_label(row[1])), _escape(row[4]), _escape(row[6]), _escape(row[7])]
            if savings else
            [identity_html, _escape(row[3]), _escape(row[4]), _escape(row[6])]
        )
        if history_record is not None:
            attrs.extend([f'data-history-index="{history_record[0]}"',
                          f'data-period-present="{str(active).lower()}"',
                          f'data-search-base="{_escape(" ".join(str(cell) for cell in row[:6]))}"'])
        inactive = ' class="period-inactive" hidden' if not active else ""
        body.append(
            f'<tr{inactive} {" ".join(attrs)} data-amount="{cents if cents is not None else ""}" '
            f'data-search="{_escape(" ".join(str(cell) for cell in row))}">'
            + "".join(('<td class="resource-cost">' if not savings and index == 3 else "<td>")
                      + cell + "</td>" for index, cell in enumerate(cells)) + "</tr>"
            + f'<tr class="resource-detail-row{" period-inactive" if not active else ""}"'
            f'{" hidden" if not active else ""} id="{detail_id}"><td colspan="{len(cells)}">'
            f'<div class="resource-detail-panel"><h4>Resource details</h4><dl>{details}</dl></div></td></tr>'
        )
    charts = []
    for index, (label, _) in enumerate(dimensions):
        entries = sorted(groups[index].items(), key=lambda item: (-item[1][0], item[0]))
        tones = _chart_tones(all_values[index])
        charts.append(
            f'<div class="breakdown-chart" data-dimension="d{index}"'
            f' data-tone-map="{_escape(json.dumps(tones, ensure_ascii=True))}">'
            f'<div class="chart-heading"><div><h4>{"Savings" if savings else "Spend"} by {_escape(label.lower())}</h4>'
            f'<p class="chart-caption">{len(entries)} categories'
            f'{" · Top 10 + Other" if len(entries) > 11 else ""}</p></div>'
            '<button type="button" class="chart-back" hidden>Back to broader view</button></div>'
            f'{_column_chart(entries, tones)}</div>'
        )
    chart_choices = "".join(
        f'<button type="button" data-chart-choice="d{index}" aria-pressed="{str(index == 0).lower()}" disabled>'
        f'{_escape(label)}</button>' for index, (label, _) in enumerate(dimensions)
    )
    columns = ["Resource name", "Resource type", "Recommended action", "Monthly savings", "Validation"] if savings else [
        "Resource name", "Service", "Region", "Cost (USD)",
    ]
    filter_html = ""
    if not savings:
        controls = []
        for key, label in (("d0", "Service"), ("subscription", "Subscription"), ("type", "Resource type"), ("d1", "Region")):
            options = "".join(
                f'<label><input type="checkbox" data-filter-option value="{_escape(value)}" disabled>'
                f'<span>{_escape(value)}</span></label>'
                for value in sorted(filter_values[key])
            )
            controls.append(
                f'<details class="resource-filter" data-filter="{key}"><summary>'
                f'<span class="filter-label">{label}:</span> <strong class="filter-selection">All</strong></summary>'
                f'<div class="filter-menu"><button type="button" data-filter-all="{key}" '
                f'aria-label="{label}: All" aria-pressed="true" disabled>All</button>'
                f'<fieldset><legend class="sr-only">{label}</legend>{options}</fieldset></div></details>'
            )
        filter_html = '<div class="resource-filters" role="group" aria-label="Cost explorer filters">' + "".join(controls) + "</div>"
    return (
        f'<section class="resource-browser" '
        f'data-export-columns="{_escape(json.dumps(section["columns"] + ([] if savings else ["Resource type"])))}" '
        f'data-export-name="finops-{"savings" if savings else "resource-costs"}.csv">'
        f'<h3>{_escape(section["title"])}</h3>'
        f'<p class="section-description">{_escape(section["description"])}</p>'
        f'{filter_html}'
        f'<div class="chart-choices" role="group" aria-label="Chart grouping">{chart_choices}</div>'
        '<p class="breakdown-instructions">Select a bar to explore resources. Other opens its categories in this chart. Credits retain their negative sign.</p>'
        f'<div class="breakdown-grid">{"".join(charts)}</div>'
        '<div class="breakdown-toolbar"><p class="breakdown-result" role="status">'
        f'{visible_records} records · {"No quantified amount" if visible_records and unknown == visible_records else _usd(total)}'
        + (f" · {unknown} unquantified" if unknown else "") +
        '</p><button type="button" class="clear-breakdown" disabled>Clear filters</button></div>'
        '<div class="resource-preview-toolbar"><label>Show top '
        '<select data-resource-limit disabled aria-label="Number of resource records to show">'
        '<option value="10">10</option><option value="25">25</option><option value="50">50</option>'
        '</select> records</label>'
        f'<span class="resource-preview-count" role="status">{visible_records} records in the saved table</span>'
        '<button type="button" class="download-resources" disabled>Export CSV · all matching</button></div>'
        '<p class="resource-export-note">Search and totals include every matching record, not just the preview. '
        'CSV includes full resource IDs and details; amounts use displayed cent precision. '
        'Embedded viewers show selectable CSV below because they block file downloads. '
        'Use the Live Reports toolbar to download the HTML and open it locally for file downloads.</p>'
        '<section class="csv-preview" hidden aria-label="Matching records as CSV">'
        '<h4>CSV · all matching records</h4><p>Select the text, copy it, and save it as a .csv file. '
        'A downloaded HTML report can also save the CSV directly.</p>'
        '<textarea readonly aria-label="CSV data" spellcheck="false"></textarea>'
        '<div class="csv-actions"><button type="button" class="select-csv">Select CSV text</button>'
        '<button type="button" class="close-csv">Close CSV</button></div></section>'
        f'<p class="breakdown-empty"{ " hidden" if visible_records else ""}>No records match these filters.</p>'
        '<p class="breakdown-print-note">Complete saved resource detail; interactive filters are not applied to print.</p>'
        '<div class="table-wrap" role="region" tabindex="0" aria-label="' + _escape(section["title"]) + ' table">'
        '<table class="resource-table"><thead><tr>'
        + "".join(f'<th scope="col">{label}</th>' for label in columns)
        + '</tr></thead><tbody>' + "".join(body) + '</tbody></table></div></section>'
    )


def _usd(cents):
    return ("-" if cents < 0 else "") + f"${abs(Decimal(cents)) / 100:,.2f}"


def _resource_type_label(value):
    return {
        "microsoft.compute/virtualmachines": "Virtual machines",
        "microsoft.compute/disks": "Managed disks",
        "microsoft.web/sites": "App Service apps",
        "microsoft.web/serverfarms": "App Service plans",
        "microsoft.storage/storageaccounts": "Storage accounts",
        "microsoft.sql/servers/databases": "SQL databases",
        "microsoft.containerservice/managedclusters": "Kubernetes clusters",
    }.get(value.casefold(), value or "(unknown)")


def _chart_tones(values):
    service_tones = {
        "virtual machines": 0, "storage": 1, "storage accounts": 1,
        "azure sql database": 2, "sql databases": 2, "app service": 3,
        "app service apps": 3, "app service plans": 3, "other services": 4,
    }
    return {value: service_tones.get(value.casefold(), index % 8)
            for index, value in enumerate(sorted(set(values)))}


def _resource_type_from_id(identity):
    return _models._resource_identity_parts(identity)[2].casefold()


def _service_icon(value):
    value = value.casefold()
    if "storage" in value:
        return "product-storage"
    if "disk" in value:
        return "hard-drive"
    if value in {"azure sql database", "sql database", "microsoft.sql/servers/databases"}:
        return "product-sql"
    if "sql" in value or "database" in value:
        return "database"
    if value in {"virtual machines", "virtual machine", "microsoft.compute/virtualmachines"}:
        return "product-virtual-machine"
    if "compute" in value:
        return "cloud-desktop"
    if value in {"app service", "app service apps", "app service plans"} or value.startswith("microsoft.web/"):
        return "product-app-service"
    if "web" in value:
        return "globe"
    if "container" in value or "kubernetes" in value:
        return "box"
    if "openai" in value or "cognitive" in value:
        return "brain-circuit"
    return "cloud"


def _service_bars(section):
    values = [(str(row[0]), int(Decimal(row[1].replace("$", "").replace(",", "")) * 100))
              for row in section["rows"]]
    maximum = max([abs(value) for _, value in values] + [1])
    tones = _chart_tones(section.get("toneValues") or [name for name, _ in values])
    bars = "".join(
        f'<li class="chart-tone-{tones[name]}"><span class="service-symbol">{_icon(_service_icon(name))}</span>'
        f'<span class="service-label">{_escape(name)}</span><strong>{_usd(value)}</strong>'
        f'<meter min="0" max="{maximum}" value="{abs(value)}" aria-label="{_escape(name)} cost magnitude in USD cents"></meter></li>'
        for name, value in values
    )
    table = {key: value for key, value in section.items() if key != "visualization"}
    return (
        f'<section class="service-chart"><div class="service-chart-heading"><h3>{_escape(section["title"])}</h3>'
        '<span>Cost (USD)</span></div><p class="section-description">'
        f'{_escape(section["description"])} Bars show magnitude; labels retain credits.</p>'
        f'<ol class="service-bars" aria-label="Top services by cost">{bars}</ol>'
        '<details class="chart-data"><summary>View service cost data</summary>'
        + _sections({"sections": [table]}, heading_level=4) + '</details></section>'
    )


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


def _cost_history_controls(model):
    history = model.get("costHistory")
    if history is None:
        return ""
    default_start, default_end = model["window"]
    available = history["available"]
    reason = "" if available else (
        f'<p class="history-unavailable" role="status">90-day history unavailable: {_escape(history["reason"])}</p>'
    )
    date_fields = ""
    payload = ""
    if available:
        start, end = history["window"]
        services = history["overview"]["services"]
        tones = _chart_tones(services)
        service_data = []
        for name in services:
            icon = _service_icon(name)
            service_data.append({
                "name": name, "tone": tones[name],
                "icon": icon if icon.startswith("product-") else "fluent-" + icon,
            })
        data = {
            "window": history["window"], "defaultWindow": model["window"], "scale": history["scale"],
            "overview": {**history["overview"], "services": service_data},
            "explorer": [record["series"] for record in history["explorer"]],
        }
        encoded = json.dumps(data, ensure_ascii=True, separators=(",", ":"))
        encoded = encoded.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
        date_fields = (
            '<div class="history-custom" id="history-custom" hidden>'
            f'<label for="history-start">From <input id="history-start" type="date" min="{_escape(start)}" '
            f'max="{_escape(end)}" value="{_escape(default_start)}"></label>'
            f'<label for="history-end">To <input id="history-end" type="date" min="{_escape(start)}" '
            f'max="{_escape(end)}" value="{_escape(default_end)}"></label>'
            '<button type="button" id="history-apply">Apply dates</button></div>'
            '<p class="history-error" id="history-error" role="alert" hidden></p>'
        )
        trend_template = _dashboard_chart({"chart": {
            "label": "Daily ActualCost (USD)", "type": "line",
            "labels": ["2000-01-01"], "values": [1.0],
        }})
        services_template = _service_bars({
            "title": "Top services", "description": "Service totals ranked by cost.",
            "columns": ["Service", "Cost"], "rows": [["Service", "$1.00"]],
        })
        groups_template = _sections({"sections": [{
            "title": "Top resource groups", "description": "Resource-group totals ranked by cost.",
            "columns": ["Resource group", "Cost"], "rows": [["Resource group", "$1.00"]],
        }]}, heading_level=3)
        payload = (
            f'<script type="application/json" id="cost-history-data" nonce="{{REPORT_NONCE}}">{encoded}</script>'
            f'<template id="history-trend-template">{trend_template}</template>'
            f'<template id="history-services-template">{services_template}</template>'
            f'<template id="history-groups-template">{groups_template}</template>'
        )
    return (
        '<section class="cost-history"><div class="history-heading">'
        f'{_icon("calendar-clock")}<label for="history-range">Cost history</label>'
        '<select id="history-range" disabled>'
        '<option value="30">Last 30 days</option>'
        f'<option value="90"{" disabled" if not available else ""}>Last 90 days</option>'
        f'<option value="custom"{" disabled" if not available else ""}>Custom dates</option></select>'
        f'<strong id="history-period" role="status">{_escape(default_start)} to {_escape(default_end)} UTC</strong>'
        '</div><p class="history-help">Applies to Overview and Cost explorer. Other sections retain their '
        'own source periods. Daily UTC dates; hourly cost data is not available in this snapshot.</p>'
        '<p class="history-no-js">Date selection requires JavaScript; showing the saved 30-day view.</p>'
        + date_fields + reason + payload + '</section>'
    )


def _briefing_panel(briefing):
    findings = _list(briefing.get("findings"), "findings")
    chips = ['<button type="button" data-job-filter="" aria-pressed="true">All '
             f'<span>{len(findings)}</span></button>']
    jobs = {}
    for finding in findings:
        jobs.setdefault(finding["job"], [finding["jobLabel"], 0])[1] += 1
    for job, (label, count) in jobs.items():
        chips.append(f'<button type="button" data-job-filter="{_escape(job)}" aria-pressed="false">'
                     f'{_escape(label)} <span>{count}</span></button>')
    cards = []
    for finding in findings:
        actions = "".join(
            f'<button type="button" class="copy-prompt" data-prompt="{_escape(action["prompt"])}">'
            f'<span>{_escape(action["label"])}</span><small>{_escape(action["approval"])}</small></button>'
            for action in finding["actions"]
        )
        evidence = "".join(f"<li>{_escape(item)}</li>" for item in finding["evidence"])
        confidence = _escape(finding["confidence"])
        cards.append(
            f'<article class="decision-card" data-job="{_escape(finding["job"])}" id="{_escape(finding["id"])}">'
            f'<div class="decision-impact"><strong>{_escape(finding["impactText"])}</strong>'
            f'<span>{_escape(finding["impactLabel"])}</span></div>'
            '<div class="decision-body"><div class="decision-tags">'
            f'<span class="decision-job">{_escape(finding["jobLabel"])}</span>'
            f'<span class="confidence confidence-{confidence.lower()}">{confidence} confidence</span>'
            f'<span class="decision-owner">{_icon("people-team")}<span>{_escape(finding["owner"])}</span>'
            f'<small>{_escape(finding["ownerSource"])}</small></span>'
            f'<span class="decision-id">{_escape(finding["id"])}</span></div>'
            f'<h3>{_escape(finding["title"])}</h3><p class="decision-why">{_escape(finding["why"])}</p>'
            + (f'<details class="decision-evidence"><summary>Evidence ({len(finding["evidence"])})</summary>'
               f'<ul>{evidence}</ul></details>' if evidence else "")
            + f'<div class="decision-actions">{actions}</div>'
            '<div class="prompt-fallback" hidden><label>Copy this prompt into the SRE Agent chat'
            '<textarea readonly rows="4"></textarea></label></div></div></article>'
        )
    hidden = briefing.get("hiddenFindings") or 0
    queue = ('<ol class="decision-list">' + "".join(f"<li>{card}</li>" for card in cards) + "</ol>"
             if cards else '<p class="decision-empty">Nothing needs a decision right now. '
             'The checks below show what was reviewed.</p>')
    if hidden:
        queue += f'<p class="decision-more">{hidden} smaller decision(s) are not shown. Ask the agent for the full list.</p>'
    rollups = "".join(
        f'<article class="rollup-card"><h4>{_escape(item["title"])}</h4>'
        f'<p class="rollup-value">{_escape(item["impactText"])} <span>{_escape(item["impactLabel"])}</span></p>'
        f'<p>{_escape(item["why"])}</p><div class="decision-actions">'
        + "".join(f'<button type="button" class="copy-prompt" data-prompt="{_escape(a["prompt"])}">'
                  f'<span>{_escape(a["label"])}</span><small>{_escape(a["approval"])}</small></button>'
                  for a in item["actions"])
        + '</div><div class="prompt-fallback" hidden><label>Copy this prompt into the SRE Agent chat'
        '<textarea readonly rows="4"></textarea></label></div></article>'
        for item in _list(briefing.get("rollups"), "rollups") if item["job"] == "waste"
    )
    checks = "".join(
        f'<li class="check-{_escape(check["state"].lower().replace(" ", "-"))}">'
        f'<strong>{_escape(check["label"])}</strong><span class="check-state">{_escape(check["state"])}</span>'
        f'<p>{_escape(check["detail"])}</p></li>'
        for check in _list(briefing.get("checks"), "checks")
    )
    agent = next((item for item in _list(briefing.get("rollups"), "rollups") if item["job"] == "agent-cost"), None)
    agent_html = ""
    if agent:
        runs = _list(briefing.get("agentRuns"), "agentRuns")
        agent_html = (
            '<section class="briefing-block agent-cost"><h3>What this agent cost</h3>'
            f'<p>{_escape(agent["why"])}</p><ul>' + "".join(f"<li>{_escape(e)}</li>" for e in agent["evidence"])
            + "</ul>" + (_sections({"sections": [{"title": "Recent runs", "columns": ["Run", "Started", "Cost"],
                                                   "rows": runs}]}, heading_level=4, id_prefix="agent-runs")
                         if runs else "")
            + '<div class="decision-actions">' + "".join(
                f'<button type="button" class="copy-prompt" data-prompt="{_escape(a["prompt"])}">'
                f'<span>{_escape(a["label"])}</span><small>{_escape(a["approval"])}</small></button>'
                for a in agent["actions"]) + '</div><div class="prompt-fallback" hidden><label>Copy this prompt '
            'into the SRE Agent chat<textarea readonly rows="4"></textarea></label></div></section>'
        )
    owner_rows = _list(briefing.get("ownerQueue"), "ownerQueue")
    owners = (_sections({"sections": [{
        "title": "Savings by owner", "columns": ["Owner", "Items", "Monthly saving", "No estimate"],
        "rows": owner_rows,
        "description": "One list per team. Use the ticket button to send each team its items.",
    }]}, heading_level=3, id_prefix="briefing-owners") if owner_rows else "")
    return (
        f'<section class="briefing-summary">{_icon("agents")}<div><h3>This week in one line</h3>'
        f'<p>{_escape(briefing.get("headline"))}</p></div></section>'
        '<section class="briefing-block"><div class="briefing-heading"><h3>Decisions</h3>'
        '<div class="job-filters" role="group" aria-label="Filter decisions">' + "".join(chips) + '</div></div>'
        '<p class="briefing-note">Action buttons copy a ready prompt. Paste it into the SRE Agent chat: '
        'the agent prepares the change and waits for your approval. Nothing changes from this report.</p>'
        + queue + '</section>'
        + (f'<section class="briefing-block"><h3>Weekly waste review</h3><div class="rollups">{rollups}</div>'
           f'{owners}</section>' if rollups else "")
        + agent_html
        + f'<section class="briefing-block"><h3>What the agent checked</h3>'
        f'<p class="briefing-note">{_escape(briefing.get("checksSummary"))}. Evidence sections in the menu hold '
        f'the full data.</p><ul class="check-list">{checks}</ul></section>'
    )


def _dashboard(model, intro=""):
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
        status_label = _escape(tab.get("displayStatus") or _STATUS_LABELS[status])
        icon = _TAB_ICONS.get(key, "chart-multiple")
        group = {"briefing": "Decisions", "overview": "Cost analysis", "rightsizing": "Efficiency",
                 "governance": "Operations", "coverage": "Report information"}.get(key)
        if group:
            buttons.append(f'<div class="nav-group" role="presentation">{group}</div>')
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
        next_step = str(tab.get("nextStep") or "")
        panel_body = _dashboard_chart(tab) + _sections(tab, heading_level=3, id_prefix=key)
        if key == "briefing" and isinstance(tab.get("briefing"), dict):
            panel_body = _briefing_panel(tab["briefing"])
        if key == "overview" and model.get("costHistory", {}).get("available"):
            sections = tab["sections"]
            panel_body = (
                '<div class="history-trend-slot" id="history-trend-slot">' + _dashboard_chart(tab) + '</div>'
                '<div class="history-services-slot" id="history-services-slot">'
                + _sections({"sections": sections[:1]}, heading_level=3, id_prefix=key) + '</div>'
                '<div class="history-groups-slot" id="history-groups-slot">'
                + _sections({"sections": sections[1:2]}, heading_level=3, id_prefix=key) + '</div>'
                + _sections({"sections": sections[2:]}, heading_level=3, id_prefix=key)
            )
        if key == "overview":
            panel_body += (
                '<aside class="cost-records-note"><p>Overview shows the top 15 services, not every resource. '
                'Cost explorer includes every saved cost record and a Service filter for the smaller categories. '
                'Web apps commonly share charges billed to an App Service plan. Resources without cost records '
                'may not appear: this is not a resource inventory. These are reported costs, not estimates from SKU prices.</p>'
                '<button type="button" data-browse-services>Browse every service in Cost explorer</button></aside>'
            )
        panels.append(
            f'<div role="tabpanel" tabindex="0" id="panel-{key}" aria-labelledby="heading-{key}">'
            f'<section class="panel-intro"><div class="panel-heading">'
            f'<div><h2 id="heading-{key}">{_escape(title)}</h2>'
            f'<p class="section-description">{_escape(tab.get("description"))}</p></div>'
            f'<span class="status-badge {status}">{status_label}</span></div>'
            f'<p class="provenance">Source: {_escape(tab.get("source"))} '
            f'<span aria-hidden="true"> | </span> As of: {_escape(tab.get("refreshedAt") or "Unknown")} '
            f'<span aria-hidden="true"> | </span> <span class="source-period">{_escape(tab.get("period"))}</span></p>'
            + (f'<aside class="source-state"><p>{_escape(panel_warnings)}</p>'
               + (f'<p><strong>Next step:</strong> {_escape(next_step)}</p>' if next_step else "")
               + '</aside>' if panel_warnings else "")
            + '</section>' + (_metrics(tab) if tab.get("metrics") else "")
            + panel_body + '</div>'
        )
    return (
        '<div class="dashboard-layout"><aside class="dashboard-sidebar">'
        f'<div class="workspace-identity">{_icon("product-cost-management")}<div><strong>Financial operations</strong>'
        '<span>Azure cost workspace</span></div></div>'
        '<div class="sidebar-heading"><span>Report sections</span>'
        f'<span class="section-count">{len(tabs)}</span></div>'
        '<div class="mobile-section-selector"><label for="section-select">Report section</label>'
        '<select id="section-select">' + "".join(options) + '</select></div>'
        '<nav role="tablist" aria-label="FinOps sections" aria-orientation="vertical">'
        + "".join(buttons) + '</nav></aside><div class="dashboard-content">' + intro
        + _cost_history_controls(model) +
        '<div class="toolbar"><div class="search-control">'
        f'{_icon("search")}<label class="sr-only" for="table-search">Search table rows in this section</label>'
        '<input id="table-search" type="search" placeholder="Search rows in this section" '
        'aria-describedby="search-count"></div><span id="search-count" role="status"></span>'
        '<button type="button" id="clear-report-filters">Clear filters</button>'
        f'<button type="button" id="print-report">{_icon("print")}<span>Print / save PDF</span></button>'
        '</div><aside id="print-help" hidden role="status"><p>Print view shows all saved sections and resource details. '
        'The embedded viewer blocks print dialogs. Download HTML using the Live Reports toolbar, '
        'open that file in your browser, then choose Print / save PDF.</p>'
        '<button type="button" id="close-print-view">Return to dashboard</button></aside>'
        + "".join(panels) + '</div></div>'
    )


@functools.lru_cache(maxsize=1)
def _plugin_build():
    """Installed plugin version and file fingerprint (same algorithm as finops-setup)."""
    try:
        version = json.loads((_PLUGIN_ROOT / "plugin.json").read_text(encoding="utf-8")).get("version")
    except (OSError, ValueError):
        version = None
    digest = hashlib.sha256()
    for path in sorted(_PLUGIN_ROOT.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        relative = path.relative_to(_PLUGIN_ROOT).as_posix().encode()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        content = path.read_bytes()
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return {"version": str(version or "unknown"), "revision": digest.hexdigest()}


def _refresh_card(refreshed_at, build):
    revision = build.get("revision") or ""
    build_html = (
        f'<small class="build-info" title="Plugin file fingerprint {_escape(revision)}">'
        f'Azure FinOps plugin v{_escape(build.get("version") or "unknown")}'
        + (f' · build <code>{_escape(revision[:7])}</code>' if revision else "")
        + '</small>'
    )
    return (
        f'<div class="refresh-card">{_icon("clock")}<div><span>Last refreshed</span>'
        f'<strong>{_escape(refreshed_at)}</strong><small>Scheduled snapshot, not real-time billing</small>'
        f'{build_html}</div></div>'
    )


def _dashboard_header(title, description, refreshed_at, mode, scopes, warnings="", build=None):
    return (
        '<header class="dashboard-header"><p class="workspace-breadcrumb">'
        'Live Reports <span aria-hidden="true">/</span> Financial operations</p>'
        '<div class="page-heading"><div class="page-title">'
        f'<p class="eyebrow">{_icon("product-azure")}<span>Azure Cost Management</span></p>'
        f'<h1>{_escape(title)}</h1><p class="powered-by">{_icon("product-sre-agent")}'
        '<span>Powered by Azure SRE Agent</span></p>'
        f'<p class="subtitle">{_escape(description)}</p></div>'
        f'{_refresh_card(refreshed_at, build or _plugin_build())}</div>'
        f'<div class="context-disclosures"><details class="scope-details"><summary>{_icon("product-subscriptions")}'
        f'<span>Scope: <strong>{_escape(mode)}</strong></span>'
        f'<span class="scope-action">View scopes</span></summary>{scopes}</details>{warnings}</div></header>'
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
    content = "" if is_dashboard else (
        _metrics(model) + chart_markup + _sections(model)
    )
    stylesheet = Path(__file__).with_name("theme.css").read_text(encoding="utf-8")
    scope_modes = {
        "managed": "Managed resources",
        "everything": "Everything (tenant)",
        "subscriptions": "Selected subscriptions",
    }
    scope_mode = model.get("scopeMode", "managed")
    if is_dashboard and scope_mode not in scope_modes:
        raise ValueError("unknown dashboard scopeMode")
    mode = scope_modes.get(scope_mode, "Managed resources")
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
        sprite += Path(__file__).with_name("microsoft-product-icons.svg").read_text(encoding="utf-8")
        intro = _dashboard_header(title, description, refreshed_at, mode, scope_html, warning_html,
                                  build=model.get("build"))
        header_html = (
            '<div class="appbar"><div class="appbar-brand">'
            f'<span class="brand-icon">{_icon("cloud")}</span><strong>FinOps</strong>'
            '<span class="appbar-divider" aria-hidden="true">/</span>'
            '<span>Cost intelligence</span></div>'
            f'<div class="appbar-context">{_icon("clock")}<span>Scheduled snapshot</span>'
            '<span class="snapshot-label">Saved report</span></div>'
            '<div class="theme-switch" role="group" aria-label="Color theme">'
            '<button type="button" data-theme-choice="light" aria-pressed="false" disabled>Light</button>'
            '<button type="button" data-theme-choice="dark" aria-pressed="false" disabled>Dark</button>'
            '</div></div>'
        )
        content = _dashboard(model, intro)
        warning_html = ""
        scope_details = ""
        body_class = ' class="dashboard"'
    csp = (
        "default-src 'none'; script-src 'nonce-{REPORT_NONCE}'; "
        "style-src 'nonce-{REPORT_NONCE}'; img-src data:; connect-src 'none'; "
        "base-uri 'none'; form-action 'none';"
        if is_dashboard else _CSP
    )

    report = f"""<!doctype html>
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
    if is_dashboard and len(report.encode("utf-8")) > _LIVE_REPORT_MAX_BYTES - _NONCE_ALLOWANCE_BYTES:
        raise ValueError("dashboard exceeds the Live Report 10 MB size limit; no data was saved")
    return report


def write_report(model, output_path):
    """Render a report and write it to output_path, returning the path."""

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_report(model), encoding="utf-8")
    return str(path)
