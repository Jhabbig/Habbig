/**
 * SSE client for real-time dashboard updates.
 *
 * Injected by the gateway into every proxied HTML response.
 * Connects to /api/stream and refreshes dashboard data when the
 * backend publishes a data_updated event through Redis.
 *
 * How it works:
 *   1. Opens an EventSource to the gateway SSE endpoint.
 *   2. On "data_updated" → finds all elements with [data-sse-refresh]
 *      and re-fetches their data-src URL, or simply re-fetches the
 *      page's main /api/* endpoint and dispatches a custom event.
 *   3. Falls back to periodic polling if SSE is unavailable.
 */
(function () {
  "use strict";

  // Which dashboard are we on? Extracted from the subdomain.
  var host = window.location.hostname;
  var parts = host.split(".");
  var dashboard = parts.length >= 3 ? parts[0] : (parts.length === 2 && parts[1] === 'localhost' ? parts[0] : null);
  if (!dashboard) return; // apex domain, no SSE needed

  var STREAM_URL = "/api/stream?dashboards=" + encodeURIComponent(dashboard);
  var RECONNECT_MS = 3000;
  var POLL_FALLBACK_MS = 30000;

  var source = null;
  var pollTimer = null;
  var connected = false;

  // ── Indicator badge (top-right corner) ──────────────────────────────
  var badge = document.createElement("div");
  badge.id = "sse-badge";
  badge.style.cssText =
    "position:fixed;top:8px;right:8px;z-index:99999;" +
    "padding:4px 10px;border-radius:12px;font-size:11px;" +
    "font-family:system-ui,sans-serif;color:#fff;" +
    "pointer-events:none;transition:opacity .3s;opacity:0.85;";
  setBadge("connecting", "#666");
  document.addEventListener("DOMContentLoaded", function () {
    document.body.appendChild(badge);
  });

  function setBadge(text, color) {
    badge.textContent = "\u26a1 " + text;
    badge.style.background = color;
  }

  // ── Connect to SSE ──────────────────────────────────────────────────
  function connect() {
    if (source) {
      try { source.close(); } catch (_) {}
    }

    source = new EventSource(STREAM_URL);

    source.addEventListener("connected", function () {
      connected = true;
      setBadge("live", "#22c55e");
      clearFallbackPoll();
    });

    source.addEventListener("data_updated", function (e) {
      var payload;
      try { payload = JSON.parse(e.data); } catch (_) { return; }

      // Flash the badge
      setBadge("updating\u2026", "#3b82f6");
      setTimeout(function () { setBadge("live", "#22c55e"); }, 800);

      // Dispatch a custom event so dashboard JS can react.
      window.dispatchEvent(
        new CustomEvent("dashboard:data_updated", { detail: payload })
      );

      // Auto-refresh: re-fetch any element with data-sse-refresh attribute.
      refreshTaggedElements();
    });

    source.addEventListener("cache_warm", function (e) {
      // Cache was just warmed by the background poller — same as data_updated.
      window.dispatchEvent(
        new CustomEvent("dashboard:data_updated", {
          detail: JSON.parse(e.data || "{}"),
        })
      );
      refreshTaggedElements();
    });

    source.addEventListener("heartbeat", function () {
      // Connection still alive, nothing to do.
    });

    source.addEventListener("error", function () {
      connected = false;
      setBadge("reconnecting\u2026", "#ef4444");
      source.close();
      startFallbackPoll();
      setTimeout(connect, RECONNECT_MS);
    });
  }

  // ── Auto-refresh logic ──────────────────────────────────────────────

  function refreshTaggedElements() {
    // Elements like <div data-sse-refresh="/api/data"> get their
    // innerHTML replaced with a fresh fetch.
    var els = document.querySelectorAll("[data-sse-refresh]");
    els.forEach(function (el) {
      var url = el.getAttribute("data-sse-refresh");
      if (!url) return;
      fetch(url, { credentials: "same-origin" })
        .then(function (r) { return r.text(); })
        .then(function (html) { el.innerHTML = html; })
        .catch(function () {});
    });

    // For SPAs / JS-heavy dashboards: just refetch the main API and let
    // the dashboard's own JS handle the update via the custom event.
    // This is the most common pattern — dashboards listen to
    // window.addEventListener("dashboard:data_updated", handler).
  }

  // ── Fallback polling (if SSE dies) ──────────────────────────────────

  function startFallbackPoll() {
    if (pollTimer) return;
    pollTimer = setInterval(function () {
      window.dispatchEvent(
        new CustomEvent("dashboard:data_updated", {
          detail: { event: "poll_fallback", dashboard: dashboard },
        })
      );
      refreshTaggedElements();
    }, POLL_FALLBACK_MS);
  }

  function clearFallbackPoll() {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  // ── Start ───────────────────────────────────────────────────────────
  connect();
})();
