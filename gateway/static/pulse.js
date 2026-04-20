/* narve Pulse — front-end controller
 * Renders SVG line charts, computes the Pulse Index composite, and drives
 * the Time Machine year scrubber that re-snaps every chart in lockstep.
 *
 * Single-file vanilla JS, no dependencies. SVG-only rendering for crisp
 * monochrome charts that scale with the gateway theme.
 */
(function () {
  'use strict';

  // ─────────────────────────────────────────────────────────────
  // Constants
  // ─────────────────────────────────────────────────────────────
  var DATA_URL = '/_gateway_static/pulse_data/pulse_metrics.json';
  var FORECAST_HORIZON = 2030;
  var HISTORICAL_END_YEAR = 2024; // years > this are "forecast mode"

  var SVG_NS = 'http://www.w3.org/2000/svg';

  var CATEGORY_LABEL = {
    happiness:         'Happiness',
    connection:        'Connection',
    mental_health:     'Mental Health',
    meaning:           'Meaning & Trust',
    material_security: 'Material Security',
    time_attention:    'Time & Attention',
    daily_friction:    'Daily Friction'
  };

  var MAGNITUDE_LABEL = { high: 'high', medium: 'med', low: 'low' };

  // Module state
  var state = {
    data: null,                    // raw JSON
    densified: {},                 // metric_id -> { year: value } map (interpolated)
    pulseIndex: {},                // year -> score 0..100
    pulseHistoryYears: [],         // sorted year list
    currentYear: HISTORICAL_END_YEAR
  };

  // ─────────────────────────────────────────────────────────────
  // Utility
  // ─────────────────────────────────────────────────────────────
  function el(tag, attrs, children) {
    var node = document.createElementNS(SVG_NS, tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        node.setAttribute(k, attrs[k]);
      });
    }
    if (children) {
      (Array.isArray(children) ? children : [children]).forEach(function (c) {
        if (c) node.appendChild(c);
      });
    }
    return node;
  }

  function htmlEl(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  function fmt(value, unit, decimals) {
    if (value == null || isNaN(value)) return '—';
    var d = decimals != null ? decimals : (Math.abs(value) < 10 ? 2 : 1);
    var n = Number(value).toFixed(d);
    if (unit === '%') return n + '%';
    if (unit === 'per 1k') return n;
    if (unit === 'per 100k') return n;
    if (unit === 'years') return n + ' yr';
    if (unit === 'hours') return n + ' h';
    if (unit === 'births') return n;
    if (unit === 'score') return n;
    if (unit === 'calls/mo') return Math.round(value) + '/mo';
    return n;
  }

  function fmtUnit(unit) {
    if (unit === '%')         return 'percent';
    if (unit === 'per 1k')    return 'per 1,000';
    if (unit === 'per 100k')  return 'per 100,000';
    if (unit === 'years')     return 'years';
    if (unit === 'hours')     return 'hours/day';
    if (unit === 'births')    return 'births / woman';
    if (unit === 'score')     return '0–10 ladder';
    if (unit === 'calls/mo')  return 'calls / month';
    return unit;
  }

  // Linear interpolation between two known data points
  function lerp(x, x1, y1, x2, y2) {
    if (x2 === x1) return y1;
    return y1 + (y2 - y1) * (x - x1) / (x2 - x1);
  }

  // Build a year -> value map for a metric using linear interpolation
  // between sparse data points (history + forecast).
  function densify(metric) {
    var combined = []
      .concat(metric.history.map(function (d) { return { year: d.year, value: d.value }; }))
      .concat(metric.forecast.map(function (d) { return { year: d.year, value: d.value }; }))
      .sort(function (a, b) { return a.year - b.year; });
    var out = {};
    if (combined.length === 0) return out;
    for (var year = combined[0].year; year <= combined[combined.length - 1].year; year++) {
      // Find the bracketing points
      var prev = combined[0], next = combined[combined.length - 1];
      for (var i = 0; i < combined.length; i++) {
        if (combined[i].year <= year) prev = combined[i];
        if (combined[i].year >= year) { next = combined[i]; break; }
      }
      out[year] = lerp(year, prev.year, prev.value, next.year, next.value);
    }
    return out;
  }

  // Min/max over a metric's HISTORICAL data only (used for normalization)
  function historyRange(metric) {
    var values = metric.history.map(function (d) { return d.value; });
    return { min: Math.min.apply(null, values), max: Math.max.apply(null, values) };
  }

  // Normalize a value to 0..100 using a metric's historical range.
  // Negative-polarity metrics are inverted so 100 always = "good".
  function normalize(value, metric) {
    var r = historyRange(metric);
    if (r.max === r.min) return 50;
    var pct = (value - r.min) / (r.max - r.min);
    pct = Math.max(0, Math.min(1, pct));
    if (metric.polarity === 'negative') pct = 1 - pct;
    return pct * 100;
  }

  // ─────────────────────────────────────────────────────────────
  // Pulse Index computation
  // ─────────────────────────────────────────────────────────────
  function computePulseIndex() {
    var metrics = state.data.metrics;
    // Densify every metric once
    metrics.forEach(function (m) {
      state.densified[m.id] = densify(m);
    });
    // Compute index for years where at least 60% of weight is available
    var totalWeight = metrics.reduce(function (s, m) { return s + (m.pulse_weight || 0); }, 0);
    var minYear = 1972; // earliest year where most modern series start
    var maxYear = FORECAST_HORIZON;

    var index = {};
    for (var y = minYear; y <= maxYear; y++) {
      var weighted = 0, w = 0;
      metrics.forEach(function (m) {
        var v = state.densified[m.id][y];
        if (v == null) return;
        var n = normalize(v, m);
        weighted += n * m.pulse_weight;
        w += m.pulse_weight;
      });
      if (w / totalWeight >= 0.55) {
        index[y] = weighted / w;
      }
    }
    state.pulseIndex = index;
    state.pulseHistoryYears = Object.keys(index).map(Number).sort(function (a, b) { return a - b; });
  }

  // ─────────────────────────────────────────────────────────────
  // SVG chart rendering
  // ─────────────────────────────────────────────────────────────

  // Build a polyline path string from an array of {x, y} points
  function pointsToPath(pts) {
    if (!pts.length) return '';
    return 'M' + pts.map(function (p) { return p.x.toFixed(1) + ',' + p.y.toFixed(1); }).join(' L');
  }

  // Render any line chart inside an SVG element.
  // opts:
  //   svg            : the <svg> element to fill
  //   history        : [{year, value}]
  //   forecast       : [{year, value, ci_low, ci_high}]
  //   yMin, yMax     : optional y-axis bounds (else inferred)
  //   xMin, xMax     : optional x-axis bounds (else inferred)
  //   width, height  : viewBox dimensions
  //   showAxes       : true to draw axis labels
  //   playheadYear   : optional current year for vertical scrubber line
  //   events         : [{year, label}] optional event annotations
  //   showEvents     : true to draw event dashed lines (only on hero)
  function renderLineChart(opts) {
    var svg = opts.svg;
    while (svg.firstChild) svg.removeChild(svg.firstChild);

    var W = opts.width  || 800;
    var H = opts.height || 280;
    svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
    svg.setAttribute('preserveAspectRatio', 'none');

    var pad = opts.showAxes
      ? { top: 16, right: 18, bottom: 24, left: 36 }
      : { top: 8,  right: 8,  bottom: 18, left: 8 };

    var innerW = W - pad.left - pad.right;
    var innerH = H - pad.top  - pad.bottom;

    // x-range
    var allYears = []
      .concat(opts.history.map(function (d) { return d.year; }))
      .concat(opts.forecast.map(function (d) { return d.year; }));
    var xMin = opts.xMin != null ? opts.xMin : Math.min.apply(null, allYears);
    var xMax = opts.xMax != null ? opts.xMax : Math.max.apply(null, allYears);

    // y-range
    var allValues = []
      .concat(opts.history.map(function (d) { return d.value; }))
      .concat(opts.forecast.map(function (d) { return d.value; }))
      .concat(opts.forecast.map(function (d) { return d.ci_low;  }))
      .concat(opts.forecast.map(function (d) { return d.ci_high; }));
    allValues = allValues.filter(function (v) { return v != null && !isNaN(v); });
    var yMin = opts.yMin != null ? opts.yMin : Math.min.apply(null, allValues);
    var yMax = opts.yMax != null ? opts.yMax : Math.max.apply(null, allValues);
    // pad y-range slightly
    var yPad = (yMax - yMin) * 0.08;
    yMin -= yPad; yMax += yPad;
    if (yMin === yMax) { yMin -= 1; yMax += 1; }

    function xOf(year)  { return pad.left + ((year  - xMin) / (xMax - xMin)) * innerW; }
    function yOf(value) { return pad.top  + (1 - (value - yMin) / (yMax - yMin)) * innerH; }

    // ── Gridlines (horizontal, 4 lines) ──
    if (opts.showAxes) {
      for (var g = 0; g <= 4; g++) {
        var gy = pad.top + (innerH * g) / 4;
        svg.appendChild(el('line', {
          x1: pad.left, x2: W - pad.right,
          y1: gy, y2: gy,
          class: 'grid-line'
        }));
      }
    }

    // ── Forecast confidence band (polygon) ──
    if (opts.forecast && opts.forecast.length) {
      // Connect history end → forecast start so the band starts cleanly.
      var lastHist = opts.history[opts.history.length - 1];
      var bandTop = [], bandBot = [];
      if (lastHist) {
        bandTop.push({ x: xOf(lastHist.year), y: yOf(lastHist.value) });
        bandBot.push({ x: xOf(lastHist.year), y: yOf(lastHist.value) });
      }
      opts.forecast.forEach(function (d) {
        if (d.ci_high != null) bandTop.push({ x: xOf(d.year), y: yOf(d.ci_high) });
        if (d.ci_low  != null) bandBot.push({ x: xOf(d.year), y: yOf(d.ci_low)  });
      });
      if (bandTop.length > 1) {
        var poly = bandTop.concat(bandBot.slice().reverse());
        svg.appendChild(el('path', {
          d: pointsToPath(poly) + ' Z',
          class: 'forecast-band'
        }));
      }
    }

    // ── Event markers ──
    if (opts.showEvents && opts.events) {
      opts.events.forEach(function (ev) {
        if (ev.year < xMin || ev.year > xMax) return;
        var ex = xOf(ev.year);
        svg.appendChild(el('line', {
          x1: ex, x2: ex,
          y1: pad.top, y2: pad.top + innerH,
          class: 'event-marker'
        }));
        var lbl = el('text', {
          x: ex + 3,
          y: pad.top + 9,
          class: 'event-label'
        });
        lbl.textContent = ev.label;
        svg.appendChild(lbl);
      });
    }

    // ── History line ──
    if (opts.history && opts.history.length) {
      var histPts = opts.history.map(function (d) {
        return { x: xOf(d.year), y: yOf(d.value) };
      });
      svg.appendChild(el('path', {
        d: pointsToPath(histPts),
        class: 'history-line'
      }));
    }

    // ── Forecast line (dashed) ──
    if (opts.forecast && opts.forecast.length) {
      var lastH = opts.history[opts.history.length - 1];
      var fcPts = [];
      if (lastH) fcPts.push({ x: xOf(lastH.year), y: yOf(lastH.value) });
      opts.forecast.forEach(function (d) {
        fcPts.push({ x: xOf(d.year), y: yOf(d.value) });
      });
      svg.appendChild(el('path', {
        d: pointsToPath(fcPts),
        class: 'forecast-line'
      }));
    }

    // ── Axis tick labels (only when showAxes) ──
    if (opts.showAxes) {
      // X axis: 4 labels evenly spaced
      var xTicks = 4;
      for (var t = 0; t <= xTicks; t++) {
        var year = Math.round(xMin + (xMax - xMin) * (t / xTicks));
        var tx = xOf(year);
        var txLabel = el('text', {
          x: tx, y: H - 6,
          class: 'axis-label',
          'text-anchor': 'middle'
        });
        txLabel.textContent = year;
        svg.appendChild(txLabel);
      }
      // Y axis: 5 labels
      for (var s = 0; s <= 4; s++) {
        var val = yMin + (yMax - yMin) * (1 - s / 4);
        var ty = pad.top + (innerH * s) / 4;
        var lbl2 = el('text', {
          x: pad.left - 6, y: ty + 3,
          class: 'axis-label',
          'text-anchor': 'end'
        });
        lbl2.textContent = Math.round(val * 10) / 10;
        svg.appendChild(lbl2);
      }
    }

    // ── Playhead (Time Machine vertical line) ──
    if (opts.playheadYear != null && opts.playheadYear >= xMin && opts.playheadYear <= xMax) {
      var px = xOf(opts.playheadYear);
      svg.appendChild(el('line', {
        x1: px, x2: px,
        y1: pad.top, y2: pad.top + innerH,
        class: 'playhead'
      }));
      // Find or interpolate the value at the playhead year
      var allPts = []
        .concat(opts.history)
        .concat(opts.forecast)
        .sort(function (a, b) { return a.year - b.year; });
      var py = null;
      for (var k = 0; k < allPts.length - 1; k++) {
        if (allPts[k].year <= opts.playheadYear && allPts[k + 1].year >= opts.playheadYear) {
          py = lerp(opts.playheadYear, allPts[k].year, allPts[k].value, allPts[k + 1].year, allPts[k + 1].value);
          break;
        }
      }
      if (py == null && allPts.length) py = allPts[allPts.length - 1].value;
      if (py != null) {
        svg.appendChild(el('circle', {
          cx: px, cy: yOf(py),
          r: 4,
          class: 'playhead-dot'
        }));
      }
    }
  }

  // ─────────────────────────────────────────────────────────────
  // Hero render — the Pulse Index headline + big chart
  // ─────────────────────────────────────────────────────────────
  function renderHero() {
    var year = state.currentYear;
    var indexValue = state.pulseIndex[year];
    var historyMidpoint = state.pulseIndex[1990];

    document.getElementById('pulse-index-value').textContent =
      indexValue != null ? indexValue.toFixed(1) : '—';
    document.getElementById('pulse-index-year').textContent = year;

    var deltaEl = document.getElementById('pulse-index-delta');
    if (indexValue != null && historyMidpoint != null) {
      var delta = indexValue - historyMidpoint;
      var sign = delta >= 0 ? '+' : '−';
      deltaEl.textContent = sign + Math.abs(delta).toFixed(1) + ' vs 1990';
    } else {
      deltaEl.textContent = '—';
    }

    // Build hero chart series from the index
    var historyPts = [], forecastPts = [];
    state.pulseHistoryYears.forEach(function (y) {
      var pt = { year: y, value: state.pulseIndex[y] };
      if (y <= HISTORICAL_END_YEAR) {
        historyPts.push(pt);
      } else {
        // Add a synthetic confidence band around forecast (widening)
        var horizon = y - HISTORICAL_END_YEAR;
        var band = horizon * 1.5;
        forecastPts.push({
          year: y,
          value: pt.value,
          ci_low:  pt.value - band,
          ci_high: pt.value + band
        });
      }
    });

    renderLineChart({
      svg: document.getElementById('pulse-index-chart'),
      history: historyPts,
      forecast: forecastPts,
      width: 800,
      height: 280,
      showAxes: true,
      yMin: 30,
      yMax: 80,
      events: state.data.moments,
      showEvents: true,
      playheadYear: year
    });
  }

  // ─────────────────────────────────────────────────────────────
  // Chart card builder — one per metric
  // ─────────────────────────────────────────────────────────────
  function valueAtYear(metric, year) {
    var d = state.densified[metric.id];
    if (!d) return null;
    if (d[year] != null) return d[year];
    // Find nearest year
    var years = Object.keys(d).map(Number);
    if (!years.length) return null;
    var nearest = years.reduce(function (a, b) {
      return Math.abs(b - year) < Math.abs(a - year) ? b : a;
    });
    return d[nearest];
  }

  function buildChartCard(metric) {
    var card = htmlEl('div', 'pulse-chart-card');
    card.setAttribute('data-metric-id', metric.id);

    // Head: title + current value
    var head = htmlEl('div', 'pulse-chart-card-head');
    var titles = htmlEl('div', 'pulse-chart-card-titles');
    titles.appendChild(htmlEl('h3', 'pulse-chart-card-title', metric.title));
    titles.appendChild(htmlEl('div', 'pulse-chart-card-sub', metric.subtitle));
    head.appendChild(titles);

    var valueWrap = htmlEl('div', 'pulse-chart-card-value');
    var currentEl = htmlEl('div', 'pulse-chart-card-current', '—');
    currentEl.setAttribute('data-current-for', metric.id);
    valueWrap.appendChild(currentEl);
    valueWrap.appendChild(htmlEl('div', 'pulse-chart-card-unit', fmtUnit(metric.unit)));
    head.appendChild(valueWrap);
    card.appendChild(head);

    // SVG chart
    var svgWrap = htmlEl('div', 'pulse-chart-card-svg-wrap');
    var svg = document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('class', 'pulse-chart pulse-chart-card-svg');
    svg.setAttribute('viewBox', '0 0 400 160');
    svg.setAttribute('preserveAspectRatio', 'none');
    svg.setAttribute('data-chart-for', metric.id);
    svgWrap.appendChild(svg);
    card.appendChild(svgWrap);

    // Delta row (1960 → today, or earliest → today)
    var firstHistYear = metric.history[0] ? metric.history[0].year : null;
    var deltaRow = htmlEl('div', 'pulse-chart-card-delta');
    var earlyVal = metric.history[0] ? metric.history[0].value : null;
    var lastVal = metric.history[metric.history.length - 1].value;
    var delta = lastVal - earlyVal;
    var deltaPct = earlyVal !== 0 ? (delta / earlyVal) * 100 : 0;
    var deltaText = (delta >= 0 ? '+' : '−') + Math.abs(delta).toFixed(1)
      + ' (' + (deltaPct >= 0 ? '+' : '−') + Math.abs(deltaPct).toFixed(0) + '%)';
    deltaRow.appendChild(htmlEl('span', null, firstHistYear + ' → today'));
    deltaRow.appendChild(htmlEl('span', 'pulse-chart-card-delta-value', deltaText));
    card.appendChild(deltaRow);

    // Consequences ("What this moves")
    if (metric.moves && metric.moves.length) {
      var movesWrap = htmlEl('div', 'pulse-chart-card-moves');
      movesWrap.appendChild(htmlEl('div', 'pulse-chart-card-moves-label', 'What this moves'));
      metric.moves.forEach(function (m) {
        var row = htmlEl('div', 'pulse-move');
        row.appendChild(htmlEl('span', 'pulse-move-arrow ' + m.direction));
        row.appendChild(htmlEl('span', 'pulse-move-label', m.label));
        row.appendChild(htmlEl('span', 'pulse-move-mag', MAGNITUDE_LABEL[m.magnitude] || m.magnitude));
        movesWrap.appendChild(row);
      });
      card.appendChild(movesWrap);

      // narve read (the actionable line)
      var lastForecast = metric.forecast[metric.forecast.length - 1];
      if (lastForecast) {
        var read = htmlEl('div', 'pulse-narve-read');
        var direction = lastForecast.value > lastVal ? 'rising' : 'falling';
        var changePct = Math.abs((lastForecast.value - lastVal) / (lastVal || 1) * 100).toFixed(0);
        var topMove = metric.moves[0];
        var moveDir = topMove.direction === 'up' ? 'up' : 'down';
        read.innerHTML = '<strong>narve read:</strong> '
          + metric.title.toLowerCase() + ' '
          + direction + ' to ' + fmt(lastForecast.value, metric.unit)
          + ' by ' + lastForecast.year + ' '
          + '(' + (lastForecast.value > lastVal ? '+' : '−') + changePct + '%). '
          + 'Watch ' + topMove.label.toLowerCase() + ' ' + moveDir + '.';
        card.appendChild(read);
      }
    }

    // Source footer
    var srcRow = htmlEl('div', 'pulse-chart-card-source');
    var srcLink = document.createElement('a');
    srcLink.href = metric.source.url;
    srcLink.target = '_blank';
    srcLink.rel = 'noopener noreferrer';
    srcLink.textContent = metric.source.name;
    var srcSpan = htmlEl('span', null, 'Source: ');
    srcSpan.appendChild(srcLink);
    srcRow.appendChild(srcSpan);
    srcRow.appendChild(htmlEl('span', null, 'Latest: ' + metric.source.latest_year));
    srcRow.appendChild(htmlEl('span', 'pulse-chart-card-source-method', metric.forecast_method));
    card.appendChild(srcRow);

    return card;
  }

  // ─────────────────────────────────────────────────────────────
  // Render every chart card and section
  // ─────────────────────────────────────────────────────────────
  function renderAllSections() {
    var grids = document.querySelectorAll('[data-charts-for]');
    grids.forEach(function (grid) {
      var category = grid.getAttribute('data-charts-for');
      var metrics = state.data.metrics.filter(function (m) { return m.category === category; });
      grid.innerHTML = '';
      metrics.forEach(function (m) {
        grid.appendChild(buildChartCard(m));
      });
    });
  }

  // ─────────────────────────────────────────────────────────────
  // What's Moving Now — top consequence cards across the board
  // ─────────────────────────────────────────────────────────────
  function renderWhatsMovingNow() {
    var grid = document.getElementById('pulse-moving-now');
    grid.innerHTML = '';
    // Pull the top "high"-magnitude move from each metric, prioritizing those
    // where the forecast direction actively pushes the consequence further.
    var picks = [];
    state.data.metrics.forEach(function (metric) {
      if (!metric.moves || !metric.moves.length) return;
      var top = metric.moves.filter(function (m) { return m.magnitude === 'high'; })[0];
      if (!top) return;
      picks.push({
        driver: metric.title,
        category: metric.category,
        label: top.label,
        direction: top.direction,
        magnitude: top.magnitude,
        lag: top.lag_months,
        weight: metric.pulse_weight
      });
    });
    // Sort by weight (most-impactful drivers first), take top 8
    picks.sort(function (a, b) { return b.weight - a.weight; });
    picks.slice(0, 8).forEach(function (p) {
      var card = htmlEl('div', 'pulse-moving-card');
      card.appendChild(htmlEl('div', 'pulse-moving-card-driver', CATEGORY_LABEL[p.category] + ' · ' + p.driver));
      card.appendChild(htmlEl('div', 'pulse-moving-card-label', p.label));
      var meta = htmlEl('div', 'pulse-moving-card-meta');
      meta.appendChild(htmlEl('span', 'pulse-moving-card-arrow ' + p.direction));
      meta.appendChild(htmlEl('span', 'pulse-moving-card-mag', MAGNITUDE_LABEL[p.magnitude]));
      meta.appendChild(htmlEl('span', 'pulse-moving-card-lag',
        p.lag === 0 ? 'concurrent' : ('lag ' + Math.abs(p.lag) + ' mo')));
      card.appendChild(meta);
      grid.appendChild(card);
    });
  }

  // ─────────────────────────────────────────────────────────────
  // Historical Misery Playbook — 4 past crisis episodes with receipts
  // ─────────────────────────────────────────────────────────────
  function renderMiseryPlaybook() {
    var grid = document.getElementById('pulse-playbook');
    if (!grid) return;
    grid.innerHTML = '';
    var episodes = state.data.historical_misery_playbook || [];
    episodes.forEach(function (ep) {
      var card = htmlEl('div', 'pulse-playbook-card');
      card.setAttribute('data-episode-id', ep.id);

      // Head — name, years, pulse delta chip
      var head = htmlEl('div', 'pulse-playbook-head');
      var titles = htmlEl('div', 'pulse-playbook-titles');
      titles.appendChild(htmlEl('div', 'pulse-playbook-years', ep.years));
      titles.appendChild(htmlEl('h3', 'pulse-playbook-name', ep.name));
      head.appendChild(titles);

      var deltaChip = htmlEl('div', 'pulse-playbook-delta');
      deltaChip.appendChild(htmlEl('span', 'pulse-playbook-delta-label', 'Pulse Index'));
      var deltaVal = htmlEl('span', 'pulse-playbook-delta-value',
        (ep.pulse_index_delta_est >= 0 ? '+' : '−') + Math.abs(ep.pulse_index_delta_est));
      deltaChip.appendChild(deltaVal);
      head.appendChild(deltaChip);
      card.appendChild(head);

      // Headline
      if (ep.headline) {
        card.appendChild(htmlEl('p', 'pulse-playbook-headline', ep.headline));
      }

      // Dropped column
      if (ep.dropped && ep.dropped.length) {
        var droppedWrap = htmlEl('div', 'pulse-playbook-col pulse-playbook-col-dropped');
        droppedWrap.appendChild(htmlEl('div', 'pulse-playbook-col-label', 'What dropped'));
        ep.dropped.forEach(function (item) {
          var row = htmlEl('div', 'pulse-playbook-item');
          row.appendChild(htmlEl('span', 'pulse-playbook-item-arrow down'));
          var txt = htmlEl('div', 'pulse-playbook-item-text');
          txt.appendChild(htmlEl('div', 'pulse-playbook-item-label', item.label));
          if (item.detail) txt.appendChild(htmlEl('div', 'pulse-playbook-item-detail', item.detail));
          row.appendChild(txt);
          row.appendChild(htmlEl('span', 'pulse-playbook-item-value', item.value));
          droppedWrap.appendChild(row);
        });
        card.appendChild(droppedWrap);
      }

      // Rose column
      if (ep.rose && ep.rose.length) {
        var roseWrap = htmlEl('div', 'pulse-playbook-col pulse-playbook-col-rose');
        roseWrap.appendChild(htmlEl('div', 'pulse-playbook-col-label', 'What rose'));
        ep.rose.forEach(function (item) {
          var row = htmlEl('div', 'pulse-playbook-item');
          row.appendChild(htmlEl('span', 'pulse-playbook-item-arrow up'));
          var txt = htmlEl('div', 'pulse-playbook-item-text');
          txt.appendChild(htmlEl('div', 'pulse-playbook-item-label', item.label));
          if (item.detail) txt.appendChild(htmlEl('div', 'pulse-playbook-item-detail', item.detail));
          row.appendChild(txt);
          row.appendChild(htmlEl('span', 'pulse-playbook-item-value', item.value));
          roseWrap.appendChild(row);
        });
        card.appendChild(roseWrap);
      }

      // Surprises
      if (ep.surprised && ep.surprised.length) {
        var surpWrap = htmlEl('div', 'pulse-playbook-surprises');
        surpWrap.appendChild(htmlEl('div', 'pulse-playbook-col-label', 'What surprised'));
        ep.surprised.forEach(function (note) {
          surpWrap.appendChild(htmlEl('div', 'pulse-playbook-surprise', note));
        });
        card.appendChild(surpWrap);
      }

      grid.appendChild(card);
    });
  }

  // ─────────────────────────────────────────────────────────────
  // Re-render every chart at the current year (Time Machine update)
  // ─────────────────────────────────────────────────────────────
  function rerenderChartsForYear(year) {
    state.currentYear = year;
    // Hero
    renderHero();

    // Per-metric chart SVGs + their current-value labels
    state.data.metrics.forEach(function (metric) {
      var svg = document.querySelector('svg[data-chart-for="' + metric.id + '"]');
      if (svg) {
        renderLineChart({
          svg: svg,
          history: metric.history,
          forecast: metric.forecast,
          width: 400,
          height: 160,
          showAxes: false,
          playheadYear: year
        });
      }
      var cur = document.querySelector('[data-current-for="' + metric.id + '"]');
      if (cur) {
        var v = valueAtYear(metric, year);
        cur.textContent = fmt(v, metric.unit);
      }
    });

    // Time Machine label + mode chip
    document.getElementById('pulse-tm-year-label').textContent = year;
    var modeChip = document.getElementById('pulse-tm-mode');
    if (year > HISTORICAL_END_YEAR) {
      modeChip.textContent = 'forecast';
      modeChip.className = 'pulse-tm-mode forecast';
    } else {
      modeChip.textContent = 'historical';
      modeChip.className = 'pulse-tm-mode';
    }
  }

  // ─────────────────────────────────────────────────────────────
  // Time Machine controller
  // ─────────────────────────────────────────────────────────────
  function initTimeMachine() {
    var slider = document.getElementById('pulse-tm-slider');
    if (!slider) return;
    slider.addEventListener('input', function () {
      var year = parseInt(slider.value, 10);
      rerenderChartsForYear(year);
    });
    document.querySelectorAll('.pulse-tm-btn').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var year = parseInt(btn.getAttribute('data-jump'), 10);
        slider.value = year;
        rerenderChartsForYear(year);
      });
    });
  }

  // ─────────────────────────────────────────────────────────────
  // Status footer (last updated, data quality)
  // ─────────────────────────────────────────────────────────────
  function renderStatus() {
    var q = document.getElementById('pulse-status-quality');
    if (q && state.data.data_quality) {
      q.textContent = state.data.data_quality.replace(/_/g, ' ');
    }
    var u = document.getElementById('pulse-status-updated');
    if (u && state.data.last_updated) {
      u.textContent = 'Updated ' + state.data.last_updated;
    }
  }

  // ─────────────────────────────────────────────────────────────
  // Boot
  // ─────────────────────────────────────────────────────────────
  function boot() {
    fetch(DATA_URL, { cache: 'no-cache' })
      .then(function (r) {
        if (!r.ok) throw new Error('pulse data fetch failed: ' + r.status);
        return r.json();
      })
      .then(function (data) {
        state.data = data;
        computePulseIndex();
        renderAllSections();
        renderWhatsMovingNow();
        renderMiseryPlaybook();
        rerenderChartsForYear(HISTORICAL_END_YEAR);
        initTimeMachine();
        renderStatus();
      })
      .catch(function (err) {
        console.error('[narve Pulse]', err);
        var hero = document.getElementById('pulse-index-value');
        if (hero) hero.textContent = '!';
      });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
