/* narve.ai toast system — single global function.
 *
 * Usage (anywhere, no import):
 *
 *   toast("Saved")
 *   toast("Copied to clipboard")
 *   toast("Couldn't save", "error")
 *   toast("Your session expired", "error", 5000)
 *   toast("Processing…", "info", 0)   // 0 = sticky, dismiss with hide()
 *   const id = toast("Long op started"); setTimeout(() => toast.hide(id), 3000);
 *
 * Types:
 *   "info"    (default) — neutral, 2.5s
 *   "success"           — same visual as info, 2.5s, with a subtle check
 *   "error"             — border-emphasised, 4s, dismissable
 *   "loading"           — with a tiny spinner, sticky until explicitly hidden
 *
 * Placement:
 *   - Desktop (>640px): bottom-centre, 24px from bottom edge.
 *   - Mobile:           top-centre, clear of the fixed app bar.
 *
 * Stacking:
 *   Multiple concurrent toasts stack vertically, newest closest to the edge.
 *   Max 5 visible; older ones auto-dismiss when a 6th is queued.
 *
 * All monochrome — no coloured backgrounds, consistent with the rest of the
 * design system. Contrast comes from weight/opacity + border treatment.
 *
 * The function is attached to `window` (no ES-module export) so existing
 * inline-script pages can call it without a <script type="module"> dance.
 */

(function (window, document) {
  "use strict";

  var MAX_STACK = 5;
  var DEFAULT_DURATIONS = {
    info:    2500,
    success: 2500,
    error:   4000,
    loading:  0,    // sticky
  };

  var host = null;
  var items = [];
  var nextId = 1;

  function ensureHost() {
    if (host && host.isConnected) return host;
    host = document.createElement("div");
    host.className = "nv-toast-host";
    host.setAttribute("role", "region");
    host.setAttribute("aria-label", "Notifications");
    host.setAttribute("aria-live", "polite");
    document.body.appendChild(host);
    return host;
  }

  function iconFor(type) {
    if (type === "success") {
      return '<svg class="nv-toast-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 8.5L6.5 12 13 4.5"/></svg>';
    }
    if (type === "error") {
      return '<svg class="nv-toast-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" aria-hidden="true"><circle cx="8" cy="8" r="6.5"/><line x1="8" y1="5" x2="8" y2="9"/><circle cx="8" cy="11.5" r="0.5" fill="currentColor"/></svg>';
    }
    if (type === "loading") {
      return '<span class="nv-toast-spinner" aria-hidden="true"></span>';
    }
    return "";
  }

  function hide(id) {
    var idx = -1;
    for (var i = 0; i < items.length; i++) {
      if (items[i].id === id) { idx = i; break; }
    }
    if (idx < 0) return;
    var it = items[idx];
    if (it.timer) clearTimeout(it.timer);
    items.splice(idx, 1);
    it.node.classList.add("nv-toast--exit");
    setTimeout(function () {
      if (it.node.parentNode) it.node.parentNode.removeChild(it.node);
    }, 220);
  }

  function show(message, type, duration) {
    type = type || "info";
    if (duration === undefined) duration = DEFAULT_DURATIONS[type] || DEFAULT_DURATIONS.info;

    // Trim stack — drop oldest to keep MAX_STACK
    while (items.length >= MAX_STACK) {
      hide(items[0].id);
    }

    var id = nextId++;
    var node = document.createElement("div");
    node.className = "nv-toast nv-toast--" + type;
    node.setAttribute("role", type === "error" ? "alert" : "status");

    var icon = iconFor(type);
    var body = document.createElement("div");
    body.className = "nv-toast-body";
    body.textContent = message;

    if (icon) {
      var iconWrap = document.createElement("span");
      iconWrap.className = "nv-toast-icon-wrap";
      iconWrap.innerHTML = icon;
      node.appendChild(iconWrap);
    }
    node.appendChild(body);

    if (type === "error" || duration === 0) {
      var close = document.createElement("button");
      close.type = "button";
      close.className = "nv-toast-close";
      close.setAttribute("aria-label", "Dismiss");
      close.innerHTML = "\u00d7";
      close.addEventListener("click", function () { hide(id); });
      node.appendChild(close);
    }

    ensureHost().appendChild(node);
    // Reflow to allow the enter animation.
    // eslint-disable-next-line no-unused-expressions
    node.offsetHeight;
    node.classList.add("nv-toast--enter");

    var timer = null;
    if (duration > 0) {
      timer = setTimeout(function () { hide(id); }, duration);
    }
    items.push({ id: id, node: node, timer: timer });
    return id;
  }

  // Public API
  function toast(message, type, duration) {
    return show(message, type, duration);
  }
  toast.hide = hide;
  toast.success = function (msg, duration) { return show(msg, "success", duration); };
  toast.error   = function (msg, duration) { return show(msg, "error",   duration); };
  toast.info    = function (msg, duration) { return show(msg, "info",    duration); };
  toast.loading = function (msg) { return show(msg, "loading", 0); };

  window.toast = toast;
})(window, document);
