/* Lightweight server-side analytics tracker.
 *
 * Tracks page views automatically on DOMContentLoaded, and exposes
 * window.narveTrack(event_type, properties) for explicit events.
 *
 * Uses navigator.sendBeacon when possible for fire-and-forget POSTs.
 * Reads the ``narve_visitor`` opaque ID cookie minted by the gateway
 * middleware and echoes it back as ``session_id`` so server-side
 * analytics can join page-views to a single visitor across visits.
 * Falls back to crypto.randomUUID() if the cookie is missing (e.g. on
 * the very first request before the Set-Cookie has taken effect).
 */
(function () {
  var ENDPOINT = "/api/analytics/event";
  var VISITOR_COOKIE = "narve_visitor";

  function pickUserAgentCategory() {
    // UA-allowlist: device-class bucketing for analytics ONLY. Keeps
    // daily/weekly reports honest about the mobile share without
    // influencing rendering. See BROWSER_COMPAT.md §8.
    try {
      var ua = (navigator.userAgent || "").toLowerCase();
      if (/ipad|tablet/.test(ua)) return "tablet";
      if (/mobile|android|iphone/.test(ua)) return "mobile";
      return "desktop";
    } catch (e) {
      return "desktop";
    }
  }

  function readVisitorCookie() {
    // document.cookie is the canonical source; the middleware sets
    // HttpOnly=false specifically so this read works.
    try {
      var raw = document.cookie || "";
      var prefix = VISITOR_COOKIE + "=";
      var parts = raw.split(";");
      for (var i = 0; i < parts.length; i++) {
        var p = parts[i].trim();
        if (p.indexOf(prefix) === 0) {
          return p.slice(prefix.length);
        }
      }
    } catch (e) {
      /* swallow — fall through to UUID fallback */
    }
    return "";
  }

  function visitorId() {
    var v = readVisitorCookie();
    if (v) return v;
    // Cookie absent (first hit before Set-Cookie applies, or a privacy
    // tool stripped it). Mint a one-shot UUID so the event still has a
    // correlator — won't survive across pages, but better than null.
    try {
      if (window.crypto && typeof window.crypto.randomUUID === "function") {
        return window.crypto.randomUUID();
      }
    } catch (e) {
      /* swallow */
    }
    return "";
  }

  function track(eventType, properties) {
    try {
      var payload = {
        event_type: String(eventType || "").slice(0, 64),
        page: window.location.pathname,
        referrer: document.referrer || "",
        user_agent_category: pickUserAgentCategory(),
        session_id: visitorId(),
        properties: properties || {},
      };
      var body = JSON.stringify(payload);
      if (navigator.sendBeacon) {
        var blob = new Blob([body], { type: "application/json" });
        navigator.sendBeacon(ENDPOINT, blob);
      } else {
        fetch(ENDPOINT, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: body,
          keepalive: true,
        }).catch(function () {});
      }
    } catch (e) {
      /* swallow */
    }
  }

  window.narveTrack = track;

  // Auto-track page view.
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { track("page_view"); });
  } else {
    track("page_view");
  }

  // Auto-hook newsletter forms (selector .newsletter-form or #prerelease-form).
  document.addEventListener("submit", function (ev) {
    var f = ev.target;
    if (!f || f.nodeName !== "FORM") return;
    if (f.classList && f.classList.contains("newsletter-form")) {
      track("newsletter_signup");
    } else if (f.id === "prerelease-form" || f.id === "newsletter-form") {
      track("newsletter_signup");
    }
  }, true);
})();
