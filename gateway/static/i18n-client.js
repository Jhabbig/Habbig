/* Client-side translator — mirror of gateway/i18n/translator.py::t for JS.
 *
 * render_page() injects a <script type="application/json" id="__NARVE_I18N__">
 * element holding the current locale as a flat {key: "string"} map. This
 * module reads that blob once at load, then exposes:
 *
 *   window.t("nav.billing")                       → "Billing" / "Facturación"
 *   window.t("billing.access.trader", {remaining: 3, total: 5})
 *                                                 → "You have 3 of 5…"
 *
 * Missing keys fall through to the raw key. Missing placeholders leave the
 * template with its `{name}` intact rather than throwing — UI never blanks
 * out because of a translator miss.
 *
 * Caveats:
 *   * This only knows about the CURRENT locale. We don't ship every locale
 *     to every page — that would be a 4× payload. Switching language
 *     requires a reload anyway (see lang-switcher.js).
 *   * Values containing HTML are rendered as plain text by callers — the
 *     translator doesn't escape. Caller is responsible.
 */
(function () {
  "use strict";

  var locale = {};

  function readBlob() {
    try {
      var el = document.getElementById("__NARVE_I18N__");
      if (!el || !el.textContent) return {};
      var parsed = JSON.parse(el.textContent);
      return (parsed && typeof parsed === "object") ? parsed : {};
    } catch (e) {
      console.warn("i18n-client: locale blob unreadable:", e);
      return {};
    }
  }

  function interpolate(template, vars) {
    if (!vars) return template;
    return template.replace(/\{(\w+)\}/g, function (match, key) {
      return (key in vars) ? String(vars[key]) : match;
    });
  }

  /**
   * Translate `key` into the current locale. `vars` is optional.
   * Returns the key itself if no translation is known.
   */
  function t(key, vars) {
    if (!key || typeof key !== "string") return "";
    var template = (key in locale) ? locale[key] : key;
    return interpolate(template, vars);
  }

  /**
   * Locale-aware number formatter. Passes through to Intl.NumberFormat
   * using window.LANG (server-resolved). Safe fallback to plain
   * Number.toString() if Intl is unavailable.
   */
  function formatNumber(n, opts) {
    if (n === null || n === undefined || n === "" || isNaN(n)) return "";
    try {
      return new Intl.NumberFormat(window.LANG || "en", opts || {}).format(n);
    } catch (e) {
      return String(n);
    }
  }

  /**
   * Format a probability-like value (0..1) as a percentage in the
   * current locale. `precision` defaults to 0 decimals.
   */
  function formatPercent(n, precision) {
    if (n === null || n === undefined || n === "" || isNaN(n)) return "";
    return formatNumber(n, {
      style: "percent",
      maximumFractionDigits: (precision === undefined) ? 0 : precision,
    });
  }

  /**
   * Locale-aware date formatter. `date` can be a Date, timestamp (ms or
   * s), or ISO string. opts passed to Intl.DateTimeFormat.
   */
  function formatDate(date, opts) {
    if (date === null || date === undefined || date === "") return "";
    var d;
    if (date instanceof Date) {
      d = date;
    } else if (typeof date === "number") {
      // Heuristic: unix seconds → ms.
      d = new Date(date < 1e12 ? date * 1000 : date);
    } else {
      d = new Date(date);
    }
    if (isNaN(d.getTime())) return "";
    try {
      return new Intl.DateTimeFormat(window.LANG || "en", opts || {}).format(d);
    } catch (e) {
      return d.toLocaleDateString();
    }
  }

  function init() {
    locale = readBlob();
  }

  // Read once at script load. If we're parsing the head before the blob
  // is injected, DOMContentLoaded will re-read.
  init();
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  }

  window.t = t;
  window.narveI18n = {
    t: t,
    formatNumber: formatNumber,
    formatPercent: formatPercent,
    formatDate: formatDate,
    reload: init,  // tests can re-read after mutating the DOM blob
  };
})();
