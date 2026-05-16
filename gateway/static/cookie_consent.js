/* Cookie consent banner — client-side handler.
 *
 * Pairs with gateway/static/_partials/cookie_consent.html (markup) and
 * gateway/static/pages/cookie_consent.css (styling). The middleware
 * skips injecting both the partial and this script once narve_consent
 * is set, so this script's main job is to handle the very first visit
 * where the cookie is unset.
 *
 * Behaviour:
 *   - On DOMContentLoaded: read narve_consent. If set -> bail.
 *   - If DNT: 1 -> bail (the middleware also skips, but defence in depth
 *     so a re-proxied response with stale markup still respects DNT).
 *   - Otherwise unhide the banner.
 *   - "Accept"  -> set narve_consent=accept (1y, SameSite=Lax, Secure on
 *                  https). Reload so the middleware mints narve_visitor
 *                  on the next request (analytics joins begin from this
 *                  point — no retroactive event re-attribution).
 *   - "Decline" -> set narve_consent=decline (1y). Hide the banner. The
 *                  analytics tracker keeps firing page-view pings, but
 *                  visitor_id stays NULL server-side so no cookie-based
 *                  visitor grouping happens.
 *
 * The cookie is HttpOnly=false (we set it from JS), Secure on https,
 * SameSite=Lax, 1 year max-age. No Domain attribute — leaving it host-
 * scoped is fine: subdomain hits will independently prompt + record,
 * which is the more conservative consent posture.
 */
(function () {
  "use strict";

  var CONSENT_COOKIE = "narve_consent";
  var ONE_YEAR_SEC = 365 * 24 * 60 * 60;

  function readConsentCookie() {
    try {
      var raw = document.cookie || "";
      var prefix = CONSENT_COOKIE + "=";
      var parts = raw.split(";");
      for (var i = 0; i < parts.length; i++) {
        var p = parts[i].trim();
        if (p.indexOf(prefix) === 0) {
          return p.slice(prefix.length);
        }
      }
    } catch (e) {
      /* swallow */
    }
    return "";
  }

  function setConsentCookie(value) {
    // Always Lax + 1y. Secure flag only on https (so localhost dev still
    // round-trips the cookie). HttpOnly is implicit-false from JS — the
    // attribute is not settable via document.cookie regardless.
    var parts = [
      CONSENT_COOKIE + "=" + encodeURIComponent(value),
      "Max-Age=" + ONE_YEAR_SEC,
      "Path=/",
      "SameSite=Lax",
    ];
    try {
      if (window.location && window.location.protocol === "https:") {
        parts.push("Secure");
      }
    } catch (e) {
      /* fall through — leave Secure off in non-browser contexts */
    }
    try {
      document.cookie = parts.join("; ");
    } catch (e) {
      /* swallow — banner just won't dismiss; refresh recovers */
    }
  }

  function dntActive() {
    try {
      if (navigator.doNotTrack === "1") return true;
      if (window.doNotTrack === "1") return true;
      if (navigator.msDoNotTrack === "1") return true;
    } catch (e) {
      /* ignore */
    }
    return false;
  }

  function showBanner(el) {
    el.hidden = false;
    el.setAttribute("data-shown", "1");
  }

  function hideBanner(el) {
    el.hidden = true;
    el.removeAttribute("data-shown");
  }

  function init() {
    var banner = document.querySelector("[data-cookie-consent]");
    if (!banner) return;

    // Already chose: never re-show (cookie is the source of truth).
    if (readConsentCookie()) {
      hideBanner(banner);
      return;
    }
    // DNT short-circuit (defence in depth — middleware also skips).
    if (dntActive()) {
      hideBanner(banner);
      return;
    }

    showBanner(banner);

    var acceptBtn = banner.querySelector("[data-cookie-consent-accept]");
    var declineBtn = banner.querySelector("[data-cookie-consent-decline]");

    if (acceptBtn) {
      acceptBtn.addEventListener("click", function () {
        setConsentCookie("accept");
        // Reload so the middleware sees narve_consent=accept on the next
        // request and mints narve_visitor before the next page paints.
        try {
          window.location.reload();
        } catch (e) {
          hideBanner(banner);
        }
      });
    }

    if (declineBtn) {
      declineBtn.addEventListener("click", function () {
        setConsentCookie("decline");
        hideBanner(banner);
      });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init, { once: true });
  } else {
    init();
  }
})();
