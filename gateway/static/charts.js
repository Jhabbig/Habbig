/* charts.js — Historical odds chart renderer for /api/markets/{slug}/chart.
 *
 * Usage:
 *   <canvas id="odds-chart" data-market-slug="btc-100k-q2"></canvas>
 *   <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
 *   <script src="/_gateway_static/charts.js" defer></script>
 *
 * Or programmatically:
 *   window.narveCharts.renderOddsChart('odds-chart', 'btc-100k-q2');
 *
 * Features:
 * - Line chart of Polymarket YES price over time (odds_history)
 * - Dashed vertical markers at each prediction timestamp (prediction_markers)
 * - Tooltips showing source handle, credibility, direction, predicted prob,
 *   and market price at the time of each prediction
 * - Theme-aware colours read from CSS variables (light + dark)
 * - Auto-registers with window.narveCharts.charts so theme changes refresh
 *   every rendered instance in place.
 */

(function () {
  'use strict';

  if (!window.narveCharts) {
    window.narveCharts = { charts: [], renderOddsChart: null, getColors: null, _theme: null };
  }

  // ── Theme colours (read from :root CSS variables) ────────────────────
  function cssVar(name, fallback) {
    var v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  }

  function getColors() {
    // These names hit both the reverted Notion palette (--surface, --accent,
    // --text-primary, --text-muted, --border) AND the monochrome palette if
    // it's ever restored. Unknown names fall back to sensible greys.
    return {
      line: cssVar('--accent', '#f0f0f0'),
      fill: 'rgba(99, 102, 241, 0.08)',
      tick: cssVar('--text-muted', '#6b7280'),
      grid: cssVar('--border-light', 'rgba(255,255,255,0.04)'),
      background: cssVar('--surface', '#1a1d27'),
      markerLine: cssVar('--text-muted', '#6b7280'),
      markerDot: cssVar('--accent', '#f0f0f0'),
      tooltipBg: cssVar('--surface', '#1a1d27'),
      tooltipText: cssVar('--text-primary', '#f0f0f0'),
      tooltipBorder: cssVar('--border', '#2a2d3a'),
    };
  }
  window.narveCharts.getColors = getColors;

  // ── Custom plugin: render prediction markers as dashed vertical lines ─
  var predictionMarkerPlugin = {
    id: 'predictionMarkers',
    afterDraw: function (chart, _args, options) {
      if (!options || !options.markers || !options.markers.length) return;
      var ctx = chart.ctx;
      var chartArea = chart.chartArea;
      var xScale = chart.scales.x;
      var colors = (window.narveCharts.getColors || getColors)();
      ctx.save();
      options.markers.forEach(function (marker) {
        var x = xScale.getPixelForValue(marker.timestamp * 1000);
        if (x < chartArea.left || x > chartArea.right) return;
        // Dashed vertical line at prediction time
        ctx.strokeStyle = colors.markerLine;
        ctx.setLineDash([3, 3]);
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(x, chartArea.top);
        ctx.lineTo(x, chartArea.bottom);
        ctx.stroke();
        // Small dot at x-axis
        ctx.setLineDash([]);
        ctx.fillStyle = colors.markerDot;
        ctx.beginPath();
        ctx.arc(x, chartArea.bottom, 3, 0, Math.PI * 2);
        ctx.fill();
      });
      ctx.restore();
    },
  };

  function registerOnce() {
    if (typeof Chart === 'undefined') return false;
    if (registerOnce._done) return true;
    Chart.register(predictionMarkerPlugin);
    // Time-axis adapter support: we use linear timestamps * 1000 and format
    // ticks ourselves to avoid the date-fns dependency.
    registerOnce._done = true;
    return true;
  }

  // ── Chart config ─────────────────────────────────────────────────────
  function buildConfig(data) {
    var colors = getColors();
    var points = data.odds_history.map(function (d) {
      return { x: d.timestamp * 1000, y: Math.round(d.yes_price * 100) };
    });
    // A map of timestamp -> marker so tooltip can look up hover targets
    var markerMap = {};
    (data.prediction_markers || []).forEach(function (m) {
      markerMap[m.timestamp] = m;
    });

    return {
      type: 'line',
      data: {
        datasets: [{
          label: 'Market odds (YES %)',
          data: points,
          borderColor: colors.line,
          backgroundColor: colors.fill,
          borderWidth: 1.5,
          pointRadius: 0,
          pointHitRadius: 10,
          tension: 0.3,
          fill: true,
        }],
      },
      options: {
        parsing: false,
        normalized: true,
        animation: { duration: 400 },
        maintainAspectRatio: false,
        responsive: true,
        interaction: { mode: 'nearest', intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: colors.tooltipBg,
            titleColor: colors.tooltipText,
            bodyColor: colors.tooltipText,
            borderColor: colors.tooltipBorder,
            borderWidth: 1,
            padding: 10,
            callbacks: {
              title: function (items) {
                if (!items.length) return '';
                var d = new Date(items[0].parsed.x);
                return d.toLocaleDateString() + ' ' + d.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
              },
              label: function (item) {
                return 'Market: ' + item.parsed.y + '% YES';
              },
              afterBody: function (items) {
                if (!items.length) return '';
                // Find the closest marker (within 1h) to the hovered point
                var hoverTs = Math.round(items[0].parsed.x / 1000);
                var closest = null;
                var bestDiff = 3600;
                Object.keys(markerMap).forEach(function (k) {
                  var diff = Math.abs(parseInt(k, 10) - hoverTs);
                  if (diff < bestDiff) { bestDiff = diff; closest = markerMap[k]; }
                });
                if (!closest) return '';
                var lines = ['', '@' + closest.source_handle];
                if (closest.credibility !== null && closest.credibility !== undefined) {
                  lines.push('Credibility: ' + Number(closest.credibility).toFixed(2));
                }
                if (closest.direction) {
                  var pct = closest.predicted_probability != null
                    ? ' (' + Math.round(closest.predicted_probability * 100) + '%)'
                    : '';
                  lines.push('Predicted: ' + closest.direction + pct);
                }
                if (closest.market_yes_price_at_time != null) {
                  lines.push('Market at time: ' + Math.round(closest.market_yes_price_at_time * 100) + '%');
                  if (closest.predicted_probability != null) {
                    var edge = closest.predicted_probability - closest.market_yes_price_at_time;
                    var sign = edge >= 0 ? '+' : '';
                    lines.push('Edge: ' + sign + Math.round(edge * 100) + '%');
                  }
                }
                return lines;
              },
            },
          },
          predictionMarkers: {
            markers: data.prediction_markers || [],
          },
        },
        scales: {
          x: {
            type: 'linear',
            ticks: {
              color: colors.tick,
              callback: function (value) {
                var d = new Date(value);
                return d.toLocaleDateString(undefined, {month: 'short', day: 'numeric'});
              },
              maxRotation: 0,
              autoSkipPadding: 20,
            },
            grid: { color: colors.grid, drawBorder: false },
          },
          y: {
            min: 0,
            max: 100,
            ticks: {
              color: colors.tick,
              callback: function (v) { return v + '%'; },
              stepSize: 25,
            },
            grid: { color: colors.grid, drawBorder: false },
          },
        },
      },
    };
  }

  // ── Public API ───────────────────────────────────────────────────────
  async function renderOddsChart(canvasId, marketSlug) {
    if (!registerOnce()) {
      console.warn('[narveCharts] Chart.js not loaded yet');
      return null;
    }
    var canvas = typeof canvasId === 'string' ? document.getElementById(canvasId) : canvasId;
    if (!canvas) {
      console.warn('[narveCharts] canvas not found:', canvasId);
      return null;
    }
    var resp;
    try {
      resp = await fetch('/api/markets/' + encodeURIComponent(marketSlug) + '/chart');
    } catch (e) {
      console.warn('[narveCharts] fetch failed', e);
      return null;
    }
    if (!resp.ok) {
      console.warn('[narveCharts] fetch non-ok:', resp.status);
      return null;
    }
    var data = await resp.json();
    if (!data || !data.odds_history || !data.odds_history.length) {
      // No history yet — render an empty-state message instead of an empty canvas
      var ctx = canvas.getContext('2d');
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      ctx.fillStyle = getColors().tick;
      ctx.font = '14px Inter, -apple-system, sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText('No odds data yet for this market.', canvas.width / 2, canvas.height / 2);
      return null;
    }
    // Destroy any prior instance on this canvas
    var existing = window.narveCharts.charts.find(function (c) { return c.canvas === canvas; });
    if (existing) {
      existing.destroy();
      window.narveCharts.charts = window.narveCharts.charts.filter(function (c) { return c !== existing; });
    }
    var chart = new Chart(canvas, buildConfig(data));
    window.narveCharts.charts.push(chart);
    return chart;
  }
  window.narveCharts.renderOddsChart = renderOddsChart;

  // ── Theme change: refresh all charts when prefers-color-scheme flips ─
  if (window.matchMedia) {
    var mql = window.matchMedia('(prefers-color-scheme: dark)');
    var refresh = function () {
      window.narveCharts.charts.forEach(function (chart) {
        var colors = getColors();
        var ds = chart.data.datasets[0];
        ds.borderColor = colors.line;
        ds.backgroundColor = colors.fill;
        chart.options.scales.x.ticks.color = colors.tick;
        chart.options.scales.y.ticks.color = colors.tick;
        chart.options.scales.x.grid.color = colors.grid;
        chart.options.scales.y.grid.color = colors.grid;
        chart.update('none');
      });
    };
    if (mql.addEventListener) mql.addEventListener('change', refresh);
    else if (mql.addListener) mql.addListener(refresh);
  }

  // ── Auto-render any canvases with data-market-slug on DOM ready ─────
  function autoRender() {
    document.querySelectorAll('canvas[data-market-slug]').forEach(function (canvas) {
      var slug = canvas.getAttribute('data-market-slug');
      if (slug) renderOddsChart(canvas, slug);
    });
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', autoRender);
  } else {
    autoRender();
  }
})();
