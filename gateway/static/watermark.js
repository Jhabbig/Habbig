/*
 * narve.ai watermark + capture-attempt bundle
 *
 * This script runs on every authenticated page. It:
 *
 *   1. Draws an invisible canvas pattern over the viewport seeded with a
 *      per-session 32-bit value (server-injected via data-seed). The
 *      pattern survives JPEG compression and partial cropping because
 *      it's woven across the full viewport at a very low contrast delta.
 *
 *   2. Listens for capture-attempt signals (getDisplayMedia override,
 *      PrintScreen / Cmd+Shift+4 shortcut, large clipboard copy,
 *      visibilitychange, and a devtools-timing heuristic) and POSTs
 *      summaries to /api/security/capture-attempt.
 *
 *   3. Applies the "nv-privacy-blur" class to <body> when the tab loses
 *      focus for >3s OR when devtools are detected, unless the user has
 *      turned those off in /settings/privacy (flagged via
 *      window.__NARVE_WATERMARK_PREFS__.inactive_blur / .devtools_blur).
 *
 * Pointer-events on the visible overlay + canvas are off in CSS, so this
 * script never blocks interactions.
 *
 * Fail-open: every block is wrapped so a single exception in, say, a
 * pre-2021 browser without ``navigator.mediaDevices`` can't brick the
 * dashboard for that user.
 */

(function () {
  "use strict";

  // Prefs injected by render_page as a JSON blob; default everything ON.
  var prefs = (window.__NARVE_WATERMARK_PREFS__ || {
    inactive_blur: true,
    devtools_blur: true,
  });

  var POST_URL = "/api/security/capture-attempt";

  function post(type, extra) {
    try {
      var payload = { type: type };
      if (extra && typeof extra === "object") {
        Object.keys(extra).forEach(function (k) { payload[k] = extra[k]; });
      }
      // keepalive so the request survives tab unload in most browsers.
      fetch(POST_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
        credentials: "same-origin",
        keepalive: true,
      }).catch(function () { /* swallow — telemetry is best-effort */ });
    } catch (_) { /* no-op */ }
  }

  // ── 1. Steganographic canvas ─────────────────────────────────────────

  function drawCanvas() {
    try {
      var canvas = document.getElementById("nv-watermark-canvas");
      if (!canvas) return;
      var seed = parseInt(canvas.getAttribute("data-seed") || "0", 10) | 0;
      if (!seed) return;
      var dpr = window.devicePixelRatio || 1;
      // Match viewport; ignore resize (a new page load re-runs this).
      canvas.width = Math.floor(window.innerWidth * dpr);
      canvas.height = Math.floor(window.innerHeight * dpr);
      canvas.style.width = window.innerWidth + "px";
      canvas.style.height = window.innerHeight + "px";
      var ctx = canvas.getContext("2d");
      if (!ctx) return;
      var w = canvas.width, h = canvas.height;
      var img = ctx.createImageData(w, h);
      var data = img.data;
      // xorshift32 seeded with user/session-derived value.
      var state = seed >>> 0 || 0xdeadbeef;
      function rnd() {
        state ^= state << 13; state >>>= 0;
        state ^= state >> 17;
        state ^= state << 5; state >>>= 0;
        return state >>> 0;
      }
      // Paint a sparse noise pattern at delta=2 contrast — invisible on
      // a rendered page but present in any screenshot's pixels.
      for (var p = 0; p < w * h; p++) {
        var r = rnd();
        var on = (r & 0xff) < 8; // ~3% coverage
        var idx = p * 4;
        if (on) {
          data[idx] = 128 + 2;     // R
          data[idx + 1] = 128 + 2; // G
          data[idx + 2] = 128 + 2; // B
          data[idx + 3] = 2;       // α — sub-visible
        } else {
          data[idx + 3] = 0;
        }
      }
      ctx.putImageData(img, 0, 0);
    } catch (_) { /* no-op */ }
  }

  // ── 2. Capture detection ─────────────────────────────────────────────

  function hookDisplayMedia() {
    try {
      if (!navigator.mediaDevices || !navigator.mediaDevices.getDisplayMedia) return;
      var original = navigator.mediaDevices.getDisplayMedia.bind(navigator.mediaDevices);
      navigator.mediaDevices.getDisplayMedia = function () {
        var args;
        try { args = JSON.stringify(arguments[0] || {}); } catch (_) { args = ""; }
        post("getDisplayMedia", { args: args });
        showCaptureWarning();
        return original.apply(navigator.mediaDevices, arguments);
      };
    } catch (_) { /* no-op */ }
  }

  function showCaptureWarning() {
    try {
      if (document.getElementById("nv-capture-warning")) return;
      var el = document.createElement("div");
      el.id = "nv-capture-warning";
      el.setAttribute("role", "status");
      el.textContent =
        "narve.ai — all sessions are watermarked for leak attribution.";
      document.body.appendChild(el);
      setTimeout(function () { try { el.remove(); } catch (_) {} }, 9000);
    } catch (_) { /* no-op */ }
  }

  function hookShortcuts() {
    try {
      document.addEventListener("keydown", function (e) {
        var isPrintScreen = e.key === "PrintScreen";
        var isMacCapture = e.metaKey && e.shiftKey &&
          (e.key === "3" || e.key === "4" || e.key === "5");
        var isWinCapture = isPrintScreen && (e.ctrlKey || e.altKey || e.shiftKey || e.metaKey);
        if (isPrintScreen || isMacCapture || isWinCapture) {
          post("shortcut", { key: e.key, mac: !!isMacCapture, win: !!isWinCapture });
          // DON'T preventDefault — PrintScreen bypasses JS anyway, and
          // blocking Cmd+Shift+4 on mac is just user-hostile for the
          // honest case (pasting into a chat when reporting a bug).
        }
      }, { capture: true });
    } catch (_) { /* no-op */ }
  }

  function hookClipboard() {
    try {
      document.addEventListener("copy", function () {
        try {
          var sel = (window.getSelection && window.getSelection().toString()) || "";
          if (sel.length > 500) post("bulk_copy", { length: sel.length });
        } catch (_) { /* no-op */ }
      });
    } catch (_) { /* no-op */ }
  }

  // ── 3. Focus / visibility blur ───────────────────────────────────────

  var blurTimer = null;
  function hookVisibility() {
    try {
      document.addEventListener("visibilitychange", function () {
        if (!prefs.inactive_blur) return;
        if (document.hidden) {
          blurTimer = setTimeout(function () {
            document.body.classList.add("nv-privacy-blur");
          }, 3000);
        } else {
          if (blurTimer) clearTimeout(blurTimer);
          document.body.classList.remove("nv-privacy-blur");
        }
      });
    } catch (_) { /* no-op */ }
  }

  // ── 4. Devtools detection ────────────────────────────────────────────

  var devtoolsOpen = false;
  function hookDevtools() {
    if (!prefs.devtools_blur) return;
    // Only on dashboard / admin paths. Skip auth pages so users can debug
    // a stuck login without the page blurring under them.
    var p = window.location.pathname || "";
    var armed = (
      p.indexOf("/dashboard") === 0 ||
      p.indexOf("/admin") === 0 ||
      p.indexOf("/predictions") === 0 ||
      p.indexOf("/markets") === 0 ||
      p.indexOf("/sources") === 0
    );
    if (!armed) return;
    try {
      setInterval(function () {
        var start = performance.now();
        // eslint-disable-next-line no-debugger
        debugger;
        var elapsed = performance.now() - start;
        if (elapsed > 100) {
          if (!devtoolsOpen) {
            devtoolsOpen = true;
            document.body.classList.add("nv-privacy-blur");
            post("devtools_opened", { elapsed_ms: elapsed });
          }
        } else if (devtoolsOpen) {
          devtoolsOpen = false;
          document.body.classList.remove("nv-privacy-blur");
        }
      }, 1000);
    } catch (_) { /* no-op */ }
  }

  // ── 5. UX deterrents ─────────────────────────────────────────────────

  function hookDeterrents() {
    try {
      // Disable right-click on data surfaces only — not site-wide.
      var sel = ".data-table, .source-card, .market-panel, .signal-card, .nv-prediction-card";
      document.addEventListener("contextmenu", function (e) {
        var t = e.target;
        while (t && t !== document.body) {
          if (t.matches && t.matches(sel)) {
            e.preventDefault();
            return;
          }
          t = t.parentNode;
        }
      });
      // Block drag of data tiles.
      document.querySelectorAll(sel).forEach(function (el) {
        el.setAttribute("draggable", "false");
      });
    } catch (_) { /* no-op */ }
  }

  // ── Bootstrap ────────────────────────────────────────────────────────

  function start() {
    drawCanvas();
    hookDisplayMedia();
    hookShortcuts();
    hookClipboard();
    hookVisibility();
    hookDevtools();
    hookDeterrents();
    // Re-paint the canvas on resize so tall monitors don't leave a
    // watermark-less strip along the bottom.
    var resizeTimer = null;
    window.addEventListener("resize", function () {
      if (resizeTimer) clearTimeout(resizeTimer);
      resizeTimer = setTimeout(drawCanvas, 200);
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start, { once: true });
  } else {
    start();
  }
})();
