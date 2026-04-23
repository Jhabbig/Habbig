/* Lightweight server-side analytics tracker.
 *
 * Tracks page views automatically on DOMContentLoaded, and exposes
 * window.narveTrack(event_type, properties) for explicit events.
 *
 * Uses navigator.sendBeacon when possible for fire-and-forget POSTs.
 * No cookies set client-side; we rely only on the existing session.
 */
(function () {
  var ENDPOINT = "/api/analytics/event";

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

  function track(eventType, properties) {
    try {
      var payload = {
        event_type: String(eventType || "").slice(0, 64),
        page: window.location.pathname,
        referrer: document.referrer || "",
        user_agent_category: pickUserAgentCategory(),
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
