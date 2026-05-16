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
 *
 * Consent gate (audit #22 MED #1, strict-ePrivacy reading):
 *   Reads the ``narve_consent`` cookie before firing anything. If the
 *   value is unset or "decline" the auto page_view is suppressed and
 *   window.narveTrack(...) is a no-op. On "accept" everything fires
 *   normally. Newsletter form-submits are exempt — they're an explicit
 *   first-party action initiated by the user, not passive tracking.
 *   cookie_consent.js currently does window.location.reload() on the
 *   Accept click — the reloaded page mints the visitor cookie + fires
 *   the first page_view through the normal auto-track path. The
 *   window.narveTrackPostConsent() helper below is exported for any
 *   future SPA-style consent flow that wants to avoid the reload, but
 *   it isn't called today. Either path is ePrivacy-compliant: no page
 *   view is recorded until consent is explicit. (Doc accuracy nit from
 *   audit #23 — fixed 2026-05-16.)
 */
(function () {
  var ENDPOINT = "/api/analytics/event";
  var VISITOR_COOKIE = "narve_visitor";
  var CONSENT_COOKIE = "narve_consent";

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

  function readCookie(name) {
    // document.cookie is the canonical source; the middleware sets
    // HttpOnly=false specifically so this read works.
    try {
      var raw = document.cookie || "";
      var prefix = name + "=";
      var parts = raw.split(";");
      for (var i = 0; i < parts.length; i++) {
        var p = parts[i].trim();
        if (p.indexOf(prefix) === 0) {
          return p.slice(prefix.length);
        }
      }
    } catch (e) {
      /* swallow — fall through to caller default */
    }
    return "";
  }

  function readVisitorCookie() {
    return readCookie(VISITOR_COOKIE);
  }

  function consentState() {
    // "" (unset) | "accept" | "decline". Anything else treated as unset.
    var v = (readCookie(CONSENT_COOKIE) || "").toLowerCase();
    if (v === "accept" || v === "decline") return v;
    return "";
  }

  function hasConsent() {
    return consentState() === "accept";
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

  function sendEvent(eventType, properties) {
    // Internal — does the actual network send with no consent check.
    // Callers must gate on hasConsent() unless the event is exempt
    // (first-party user-initiated action, e.g. newsletter_signup).
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

  function track(eventType, properties) {
    // Public tracker — respects consent gate. Unset/decline → no-op.
    if (!hasConsent()) return;
    sendEvent(eventType, properties);
  }

  window.narveTrack = track;

  // Deferred page_view helper for cookie_consent.js to invoke after the
  // user clicks Accept, so the current visit isn't entirely untracked.
  window.narveTrackPostConsent = function () {
    if (!hasConsent()) return;
    sendEvent("page_view");
  };

  // Auto-track page view — gated on consent==accept.
  if (hasConsent()) {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", function () { sendEvent("page_view"); });
    } else {
      sendEvent("page_view");
    }
  }

  // Auto-hook newsletter forms (selector .newsletter-form or #prerelease-form).
  // Exempt from the consent gate: newsletter signup is an explicit
  // first-party action the user initiated, not passive tracking.
  document.addEventListener("submit", function (ev) {
    var f = ev.target;
    if (!f || f.nodeName !== "FORM") return;
    if (f.classList && f.classList.contains("newsletter-form")) {
      sendEvent("newsletter_signup");
    } else if (f.id === "prerelease-form" || f.id === "newsletter-form") {
      sendEvent("newsletter_signup");
    }
  }, true);
})();
