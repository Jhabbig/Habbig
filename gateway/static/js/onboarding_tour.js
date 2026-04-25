/* Onboarding overlay tour — 5-step spotlight on the dashboard nav.
 *
 * Auto-starts on the first dashboard visit AFTER the user has completed
 * the /onboarding flow. Gating lives server-side at
 * /api/onboarding/tour-state; this script only renders + dispatches.
 *
 * Design constraints (matches gateway/static/gateway.css):
 *   - Monochrome only, no colour beyond the existing semantic palette.
 *   - prefers-reduced-motion → instant transitions, no smooth-scroll.
 *   - Keyboard: Esc skips; Enter / Space advances; Tab loops within the
 *     popover so the user can't tab off into the dimmed page beneath.
 *
 * Public surface:
 *   window.NarveTour.start({force: true})  — manual replay (admin debug).
 *   window.NarveTour.skip()                 — programmatic skip.
 */
(function () {
  "use strict";

  if (window.NarveTour) return;  // double-load guard

  var STEPS = [
    {
      target: "[data-tour='feed']",
      title: "Your feed",
      body: "Predictions from sources you follow land here in real time.",
    },
    {
      target: "[data-tour='best-bets']",
      title: "Best bets",
      body: "Where narve's consensus disagrees with the market — ranked by edge.",
    },
    {
      target: "[data-tour='intelligence']",
      title: "Intelligence",
      body: "Ask anything about markets, sources, or your own performance.",
    },
    {
      target: "[data-tour='watchlist']",
      title: "Watchlist",
      body: "Save markets and sources to track here.",
    },
    {
      target: "[data-tour='predictions']",
      title: "Your predictions",
      body: "Record your own calls. Track your accuracy over time.",
    },
  ];

  var REDUCED_MOTION = window.matchMedia &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function escapeHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"})[c];
    });
  }

  function csrf() {
    var m = document.cookie.match(/(?:^|;\s*)_csrf=([^;]*)/);
    return m ? decodeURIComponent(m[1]) : "";
  }

  function postSilent(url) {
    // Sentinels (skip / complete) are best-effort: a failed request must
    // never re-trigger the tour. The server treats both endpoints as
    // idempotent so a retry is harmless.
    return fetch(url, {
      method: "POST",
      headers: {"Content-Type": "application/json", "x-csrf-token": csrf()},
      body: "{}",
      keepalive: true,
    }).catch(function () { /* swallow */ });
  }

  function findTarget(selector) {
    try {
      return document.querySelector(selector);
    } catch (_) {
      return null;
    }
  }

  function clearOverlay(overlay) {
    if (!overlay) return;
    document.removeEventListener("keydown", overlay.__keyHandler, true);
    if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
  }

  function renderOverlay(target, step, idx, total, handlers) {
    var rect = target.getBoundingClientRect();
    var pad = 8;
    var spotlightTop = rect.top - pad;
    var spotlightLeft = rect.left - pad;
    var spotlightW = rect.width + pad * 2;
    var spotlightH = rect.height + pad * 2;

    // Position the popover BELOW the spotlight by default; if there
    // isn't 220px of vertical room, flip above. Same for left/right
    // bias to keep it inside the viewport.
    var roomBelow = window.innerHeight - rect.bottom;
    var popoverTop = roomBelow >= 220
      ? rect.bottom + 12
      : Math.max(8, rect.top - 220);
    var popoverLeft = Math.min(
      Math.max(8, rect.left),
      window.innerWidth - 340  // popover max-width 320 + 20 cushion
    );

    var overlay = document.createElement("div");
    overlay.className = "nv-tour";
    overlay.setAttribute("role", "dialog");
    overlay.setAttribute("aria-modal", "true");
    overlay.setAttribute("aria-labelledby", "nv-tour-title-" + idx);
    overlay.setAttribute("aria-describedby", "nv-tour-body-" + idx);

    overlay.innerHTML = (
      '<div class="nv-tour__backdrop"></div>' +
      '<div class="nv-tour__spotlight" style="' +
        "top:" + spotlightTop + "px;" +
        "left:" + spotlightLeft + "px;" +
        "width:" + spotlightW + "px;" +
        "height:" + spotlightH + "px;" +
      '"></div>' +
      '<div class="nv-tour__popover" style="' +
        "top:" + popoverTop + "px;" +
        "left:" + popoverLeft + "px;" +
      '">' +
        '<div class="nv-tour__progress" aria-label="Step ' +
          (idx + 1) + ' of ' + total + '">' +
          '<span class="nv-tour__step">' + (idx + 1) + ' / ' + total + '</span>' +
        '</div>' +
        '<h3 id="nv-tour-title-' + idx + '">' + escapeHtml(step.title) + '</h3>' +
        '<p id="nv-tour-body-' + idx + '">' + escapeHtml(step.body) + '</p>' +
        '<div class="nv-tour__actions">' +
          '<button class="nv-tour__skip" type="button">Skip tour</button>' +
          '<button class="nv-tour__next" type="button">' +
            (idx === total - 1 ? "Done" : "Next") +
          '</button>' +
        '</div>' +
      '</div>'
    );
    document.body.appendChild(overlay);

    var nextBtn = overlay.querySelector(".nv-tour__next");
    var skipBtn = overlay.querySelector(".nv-tour__skip");
    nextBtn.addEventListener("click", handlers.onNext);
    skipBtn.addEventListener("click", handlers.onSkip);
    // Backdrop click also skips — common dialog convention, but only if
    // they click the dim part, not the popover or spotlight.
    overlay.querySelector(".nv-tour__backdrop")
      .addEventListener("click", handlers.onSkip);

    // Move keyboard focus to Next so Enter advances; trap Tab inside.
    nextBtn.focus();
    function onKey(e) {
      if (e.key === "Escape") {
        e.preventDefault();
        handlers.onSkip();
        return;
      }
      if (e.key === "Tab") {
        // Tiny focus trap — only two buttons, alternate them.
        e.preventDefault();
        if (document.activeElement === nextBtn) skipBtn.focus();
        else nextBtn.focus();
      }
    }
    overlay.__keyHandler = onKey;
    document.addEventListener("keydown", onKey, true);

    return overlay;
  }

  function start(opts) {
    opts = opts || {};
    var idx = 0;
    var current = null;

    function show(i) {
      clearOverlay(current);
      current = null;

      // Skip steps whose target isn't in the DOM (e.g. user is on a
      // dashboard layout that omits Watchlist). Don't fail the tour.
      while (i < STEPS.length && !findTarget(STEPS[i].target)) {
        i += 1;
      }
      if (i >= STEPS.length) {
        finish();
        return;
      }

      var target = findTarget(STEPS[i].target);
      if (!REDUCED_MOTION) {
        target.scrollIntoView({behavior: "smooth", block: "center", inline: "nearest"});
      } else {
        target.scrollIntoView({block: "center"});
      }

      // Wait one frame so the scrollIntoView lands before we measure.
      requestAnimationFrame(function () {
        current = renderOverlay(target, STEPS[i], i, STEPS.length, {
          onNext: function () { idx += 1; show(idx); },
          onSkip: function () { skip(); },
        });
      });
    }

    function finish() {
      clearOverlay(current);
      current = null;
      postSilent("/api/onboarding/tour-complete");
    }

    function skip() {
      clearOverlay(current);
      current = null;
      postSilent("/api/onboarding/tour-skip");
    }

    // Re-position on viewport resize so the popover doesn't drift off-screen.
    window.addEventListener("resize", function () {
      if (current) show(idx);
    }, {passive: true});

    show(idx);
    return {skip: skip, finish: finish};
  }

  async function maybeStart() {
    try {
      var r = await fetch("/api/onboarding/tour-state", {credentials: "same-origin"});
      if (!r.ok) return;
      var data = await r.json();
      if (data && data.should_show) start();
    } catch (_) {
      // Silent — the tour is non-essential UX. Don't break the dashboard
      // boot sequence over a network blip.
    }
  }

  // Expose a programmatic surface for admin debug + tests.
  window.NarveTour = {
    start: function (opts) { return start(opts || {}); },
    skip: function () { postSilent("/api/onboarding/tour-skip"); },
  };

  // Auto-start ~1.2 s after load — gives the dashboard's own JS time to
  // populate `data-tour` slots that are server-injected.
  if (document.readyState === "complete") {
    setTimeout(maybeStart, 1200);
  } else {
    window.addEventListener("load", function () {
      setTimeout(maybeStart, 1200);
    });
  }
})();
