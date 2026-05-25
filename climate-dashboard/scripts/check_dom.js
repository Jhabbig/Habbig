/**
 * Headless-DOM smoke test for index.html.
 *
 * Loads the actual index.html into jsdom, stubs fetch() to return mocked
 * /api/* responses, runs the inline scripts, and reports any thrown errors
 * or unhandled console warnings. This catches a class of bugs the simple
 * JS-parse check misses (missing DOM IDs, race conditions, type errors
 * on null returns from getElementById, etc).
 */
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

// Resolve the dashboard HTML relative to this script's location, not an
// absolute path — so the check runs from any checkout / CI.
const HTML = fs.readFileSync(path.resolve(__dirname, "..", "static", "index.html"), "utf8");

// Stub responses for each /api/* path. Shape mirrors what the real
// endpoints would return when upstream data is present.
const MOCK_RESPONSES = {
  "/api/summary": {
    gistemp: {
      latest_annual: { year: 2024, anomaly_c: 1.29 },
      projection: {
        current_year: 2025, months_observed: 8, ytd_anomaly_c: 1.18,
        drift_to_year_end_c: 0.05, drift_std_c: 0.07,
        projected_annual_anomaly_c: 1.23,
        current_record: { year: 2024, anomaly_c: 1.29 },
        p_breaks_record: 0.21,
      },
      thresholds: { thresholds: [
        { threshold_c: 1.3, p_at_or_above: 0.18 },
        { threshold_c: 1.4, p_at_or_above: 0.01 },
      ], mu_c: 1.23, sigma_c: 0.07 },
      calibration: { n: 5, mae: 0.04, rmse: 0.05, bias: -0.01, unit: "°C" },
    },
    co2: {
      latest: { year: 2025, month: 8, ppm: 425.3 },
      projection: { projected_year_end_ppm: 427.5, ppm_per_year: 2.9, residual_std_ppm: 0.4, current_year: 2025, latest_ppm: 425.3 },
      thresholds: { thresholds: [{ threshold_ppm: 427, p_at_or_above: 0.55 }, { threshold_ppm: 428, p_at_or_above: 0.31 }], mu_ppm: 427.5, sigma_ppm: 0.4 },
      calibration: { n: 5, mae: 0.34, rmse: 0.41, bias: 0.12, unit: "ppm" },
    },
    methane: {
      latest: { year: 2025, month: 7, ppb: 1932.4 },
      projection: { projected_year_end_ppb: 1938.2, ppb_per_year: 6.5, residual_std_ppb: 2.5, current_year: 2025, latest_ppb: 1932.4 },
      thresholds: { thresholds: [{ threshold_ppb: 1940, p_at_or_above: 0.31 }], mu_ppb: 1938.2, sigma_ppb: 2.5 },
      calibration: { n: 5, mae: 1.8, rmse: 2.1, bias: 0.4, unit: "ppb" },
    },
    n2o: {
      latest: { year: 2025, month: 7, ppb: 339.2 },
      projection: { projected_year_end_ppb: 340.0, ppb_per_year: 1.0, residual_std_ppb: 0.3, current_year: 2025, latest_ppb: 339.2 },
      thresholds: { thresholds: [{ threshold_ppb: 340, p_at_or_above: 0.51 }], mu_ppb: 340.0, sigma_ppb: 0.3 },
      calibration: { n: 5, mae: 0.15, rmse: 0.18, bias: 0.02, unit: "ppb" },
    },
    sf6: {
      latest: { year: 2025, month: 7, ppt: 11.7 },
      projection: { projected_year_end_ppt: 11.85, ppt_per_year: 0.3, residual_std_ppt: 0.05, current_year: 2025, latest_ppt: 11.7 },
      thresholds: { thresholds: [{ threshold_ppt: 12.0, p_at_or_above: 0.10 }], mu_ppt: 11.85, sigma_ppt: 0.05 },
    },
    forcing: {
      co2_wm2: 2.13, ch4_wm2: 0.52, n2o_wm2: 0.20, sf6_wm2: 0.006,
      total_wm2: 2.86, effective_co2_ppm: 506,
      current_co2_ppm: 425.3, have_all_gases: true,
      method: "Myhre et al. 1998 / IPCC AR5 simplified formulas; pre-industrial reference 1750.",
    },
    sea_ice: {
      record_check: { date: "2025-08-20", extent_mkm2: 5.41, doy_min: 4.62, doy_max: 7.81, doy_mean: 6.21, rank_lowest_in_record: 8, history_years: 46 },
      arctic_projection: { projected_min_mkm2: 4.3, residual_std_mkm2: 0.4, trend_mkm2_per_year: -0.07, current_year: 2025, fit_window_years: 25, is_post_min: false },
      antarctic_projection: { projected_min_mkm2: 2.2, residual_std_mkm2: 0.3, trend_mkm2_per_year: -0.03, current_year: 2025, fit_window_years: 25, is_post_min: true },
    },
    regime: { latest: { year: 2025, month: 7, oni: 0.3 }, state: "Neutral" },
    fetched_at: new Date().toISOString(),
  },
  "/api/temperature": { source: "NASA GISTEMP v4 (GLB.Ts+dSST)", baseline: "1951-1980", units: "°C",
    monthly: Array.from({ length: 60 }, (_, i) => ({ year: 2020 + Math.floor(i / 12), month: (i % 12) + 1, anomaly_c: 0.9 + i * 0.005 })),
    annual: [{year: 2020, anomaly_c: 1.02}, {year: 2021, anomaly_c: 0.85}, {year: 2022, anomaly_c: 0.91}, {year: 2023, anomaly_c: 1.17}, {year: 2024, anomaly_c: 1.29}],
    projection: null, fetched_at: new Date().toISOString() },
  "/api/co2": { source: "NOAA GML Mauna Loa", units: "ppm",
    monthly: Array.from({ length: 60 }, (_, i) => ({ year: 2020 + Math.floor(i / 12), month: (i % 12) + 1, ppm: 415 + i * 0.2, decimal_date: 2020 + i / 12 })),
    latest: { year: 2025, month: 8, ppm: 425.3 } },
  "/api/methane": { source: "NOAA GML CH4", units: "ppb",
    monthly: Array.from({ length: 60 }, (_, i) => ({ year: 2020 + Math.floor(i / 12), month: (i % 12) + 1, ppb: 1900 + i * 0.5, decimal_date: 2020 + i / 12 })),
    latest: { year: 2025, month: 7, ppb: 1932.4 } },
  "/api/n2o": { source: "NOAA GML N2O", units: "ppb",
    monthly: Array.from({ length: 60 }, (_, i) => ({ year: 2020 + Math.floor(i / 12), month: (i % 12) + 1, ppb: 335 + i * 0.07, decimal_date: 2020 + i / 12 })),
    latest: { year: 2025, month: 7, ppb: 339.2 } },
  "/api/sf6": { source: "NOAA GML SF6", units: "ppt",
    monthly: Array.from({ length: 60 }, (_, i) => ({ year: 2020 + Math.floor(i / 12), month: (i % 12) + 1, ppt: 10.5 + i * 0.02, decimal_date: 2020 + i / 12 })),
    latest: { year: 2025, month: 7, ppt: 11.7 } },
  "/api/sea-ice": {
    source: "NSIDC", units: "million km²",
    arctic_recent: Array.from({ length: 1100 }, (_, i) => ({ year: 2023 + Math.floor(i / 365), month: 1, day: 1, extent_mkm2: 10 + Math.sin(i / 60) * 3 })),
    antarctic_recent: Array.from({ length: 1100 }, (_, i) => ({ year: 2023 + Math.floor(i / 365), month: 1, day: 1, extent_mkm2: 7 + Math.sin(i / 60 + 3) * 3 })),
    arctic_annual: Array.from({ length: 45 }, (_, i) => ({ year: 1980 + i, min_mkm2: 7 - i * 0.05, max_mkm2: 15 - i * 0.02 })),
    antarctic_annual: Array.from({ length: 45 }, (_, i) => ({ year: 1980 + i, min_mkm2: 3 + Math.sin(i) * 0.3, max_mkm2: 18 + Math.cos(i) * 0.5 })),
    record_check: { date: "2025-08-20", extent_mkm2: 5.41, doy_min: 4.62, doy_max: 7.81, doy_mean: 6.21, rank_lowest_in_record: 8, history_years: 46 },
  },
  "/api/regime": { source: "NOAA CPC ONI", state: "Neutral", latest: { year: 2025, month: 7, oni: 0.3 },
    monthly: Array.from({ length: 60 }, (_, i) => ({ year: 2020 + Math.floor(i / 12), month: (i % 12) + 1, oni: Math.sin(i / 12) * 0.8 })) },
  "/api/sst": { source: "OISST", units: "°C",
    series: [{ name: String(new Date().getUTCFullYear()), data: Array(366).fill(20.5).map((v, i) => i < 200 ? v : null) },
             { name: "1982-2011 mean", data: Array(366).fill(20.0) }] },
  "/api/markets": {
    markets: [
      { conditionId: "m1", question: "Will CO2 exceed 428 ppm in 2025?", _event_title: "Atmospheric CO2 in 2025",
        slug: "co2-428-2025", lastTradePrice: 0.25, liquidity: 5000, endDate: "2025-12-31",
        _implied_p: 0.25, _model_p: 0.31, _edge_pp: 6.0, _venue: "polymarket",
        _rationale: "N(μ=427.5 ppm, σ=0.4 ppm), +2.9/yr" },
      { conditionId: "m2", question: "Arctic minimum below 4 million km² this summer?",
        _event_title: "Arctic sea ice 2025",
        slug: "arctic-min-2025", lastTradePrice: 0.45, liquidity: 2500, endDate: "2025-09-30",
        _implied_p: 0.45, _model_p: 0.38, _edge_pp: -7.0, _venue: "polymarket",
        _rationale: "Trend → 4.3 Mkm² (σ=0.4)" },
      { conditionId: "m3", question: "2025 sets a new annual temperature record",
        _event_title: "Will 2025 be the warmest year on record?",
        slug: "WARMEST-2025-YES", lastTradePrice: 0.63, liquidity: 12000, endDate: "2025-12-31",
        _implied_p: 0.63, _model_p: 0.75, _edge_pp: 12.0, _venue: "kalshi",
        _rationale: "YTD projects to +1.29°C vs record +1.29°C (2024)" },
    ],
    count: 3,
  },
  "/api/backtest": {
    gistemp: [{ year: 2020, as_of: "Jun", projected_c: 1.05, actual_c: 1.02, error_c: 0.03 }],
    co2: [{ year: 2020, as_of: "Jun", projected_year_end_ppm: 414.0, actual_dec_ppm: 414.4, error_ppm: -0.4 }],
    methane: [], n2o: [],
    calibration: {
      gistemp: { n: 5, mae: 0.04, rmse: 0.05, bias: -0.01, unit: "°C" },
      co2: { n: 5, mae: 0.34, rmse: 0.41, bias: 0.12, unit: "ppm" },
    },
    method: { gistemp: "...", co2: "...", methane: "...", n2o: "..." },
  },
  "/api/highlights": { items: [
    { kind: "record", text: "2024 set a new annual temperature record at +1.29°C (vs 1951-1980)." },
    { kind: "trend", text: "CO₂ rose +2.34 ppm over the last 12 months (now 425.30 ppm)." },
  ] },
  "/api/ocean-heat": {
    source: "NOAA NCEI", units: "10^22 J",
    yearly: Array.from({ length: 10 }, (_, i) => ({ year: 2015 + i, ohc_1e22_J: 12 + i * 1.8 })),
    latest: { year: 2024, ohc_1e22_J: 29.2 },
  },
  "/api/sea-level": {
    source: "NOAA STAR", units: "mm",
    series: Array.from({ length: 60 }, (_, i) => ({ decimal_year: 2020 + i / 12, sea_level_mm: 84 + i * 0.4 })),
    latest: { decimal_year: 2024.99, sea_level_mm: 107.6 },
  },
  "/api/snow-cover": {
    source: "Rutgers", units: "million km²",
    monthly: Array.from({ length: 24 }, (_, i) => ({ year: 2023 + Math.floor(i / 12), month: (i % 12) + 1, extent_mkm2: 25 + Math.sin(i / 12 * Math.PI * 2) * 20 })),
    latest: { year: 2024, month: 12, extent_mkm2: 44.5 },
  },
  "/api/scenarios": {
    trajectories: {
      temperature_c_vs_1850_1900: { "SSP1-2.6": [{year:2020,value:1.10},{year:2050,value:1.70},{year:2100,value:1.80}] },
      co2_ppm: { "SSP1-2.6": [{year:2020,value:412},{year:2050,value:445},{year:2100,value:420}] },
    },
    current_match: {
      temperature: { scenario: "SSP5-8.5", distance_c: 0.20, scenario_value_c: 1.30, observed_value_c: 1.50, position: "above_all", year: 2025 },
      co2: { scenario: "SSP2-4.5", distance_ppm: 5.0, scenario_value_ppm: 430.0, observed_value_ppm: 425.0, position: "between", year: 2025 },
    },
  },
  "/api/emissions": {
    latest_year: 2022,
    top_emitters: [
      { iso: "CHN", country: "China", year: 2022, co2_mt: 11396, co2_per_capita_t: 8.0, share_global: 30.7 },
      { iso: "USA", country: "United States", year: 2022, co2_mt: 5057, co2_per_capita_t: 15.2, share_global: 13.6 },
    ],
    global: { year: 2022, global_co2_mt: 37154, decade_ago_year: 2012, decade_ago_co2_mt: 35043, decade_change_pct: 6.0 },
    source: "Our World in Data — co2-data",
  },
};

// Strip out the uPlot CDN <script> tags so jsdom doesn't try to fetch the network
const cleanedHtml = HTML
  .replace(/<script[^>]*src="https?:[^"]*"[^>]*><\/script>/g, "")
  .replace(/<link[^>]*href="https?:[^"]*"[^>]*\/?>/g, "");

const errors = [];
const warnings = [];

const dom = new JSDOM(cleanedHtml, {
  url: "http://localhost:7052/",
  runScripts: "dangerously",
  pretendToBeVisual: true,
  beforeParse(window) {
    window.fetch = (url) => {
      const path = url.startsWith("http") ? new URL(url).pathname : url;
      if (path in MOCK_RESPONSES) {
        return Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve(MOCK_RESPONSES[path]),
          text: () => Promise.resolve(JSON.stringify(MOCK_RESPONSES[path])),
        });
      }
      return Promise.resolve({ ok: false, status: 404, json: () => Promise.resolve(null) });
    };
    window.addEventListener("error", e => errors.push(e.message + " — " + (e.error?.stack || "no stack")));
    window.addEventListener("unhandledrejection", e => errors.push("unhandled rejection: " + (e.reason?.stack || e.reason)));
    const origConsole = window.console;
    window.console = {
      ...origConsole,
      error: (...args) => { errors.push("console.error: " + args.join(" ")); origConsole.error(...args); },
      warn: (...args) => { warnings.push("console.warn: " + args.join(" ")); origConsole.warn(...args); },
    };
  },
});

// Give async loaders a moment to settle, then exit explicitly because the
// dashboard's setInterval(loadMarkets, 5*60*1000) keeps the event loop alive.
setTimeout(() => {
  try {
  const cardsHTML = dom.window.document.getElementById("cards").innerHTML;
  const marketsHTML = dom.window.document.getElementById("markets").innerHTML;
  const oppsHTML = dom.window.document.getElementById("opps").innerHTML;
  const highlightsHTML = dom.window.document.getElementById("highlights").innerHTML;
  const emittersHTML = dom.window.document.getElementById("emitters-card").innerHTML;

  console.log("Errors:    " + errors.length);
  errors.forEach(e => console.log("  - " + e));
  console.log("Warnings:  " + warnings.length);
  // Filter out expected non-issues from console.warn (fetch failed messages
  // for endpoints we didn't mock, etc).
  const realWarnings = warnings.filter(w => !w.includes("fetch failed"));
  realWarnings.forEach(w => console.log("  - " + w));

  console.log("");
  console.log("Render lengths:");
  console.log("  cards:      " + cardsHTML.length + (cardsHTML.length > 100 ? " ✓" : " ✗ EMPTY"));
  console.log("  markets:    " + marketsHTML.length + (marketsHTML.length > 100 ? " ✓" : " ✗ EMPTY"));
  console.log("  opps:       " + oppsHTML.length + (oppsHTML.length > 100 ? " ✓" : " ✗ EMPTY"));
  console.log("  highlights: " + highlightsHTML.length + (highlightsHTML.length > 50 ? " ✓" : " ✗ EMPTY"));
  console.log("  emitters:   " + emittersHTML.length + (emittersHTML.length > 100 ? " ✓" : " ✗ EMPTY"));

  // The card grid should have at least 10 cards (temp, co2, ch4, n2o, sf6,
  // forcing, ocean heat, snow cover, arctic, antarctic, sst). Cards that
  // gracefully degrade to "data unavailable" still match the <div class="card">
  // pattern, so a fall in this count means we lost a card module entirely.
  const cardCount = (cardsHTML.match(/<div class="card"/g) || []).length;
  console.log("  card count: " + cardCount + (cardCount >= 10 ? " ✓" : " ✗ TOO FEW"));

  if (errors.length > 0) {
    console.log("\nFAILED — " + errors.length + " error(s)");
    process.exit(1);
  }
  if (cardsHTML.length < 100 || marketsHTML.length < 100 || emittersHTML.length < 100) {
    console.log("\nFAILED — empty render");
    process.exit(1);
  }
    console.log("\nOK");
    process.exit(0);
  } catch (e) {
    console.error("checker threw:", e);
    process.exit(2);
  }
}, 2000);
