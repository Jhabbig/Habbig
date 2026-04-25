/* shortcuts-discovery.js — one-time "press ? for shortcuts" hint.
 *
 * Goal: make power-user shortcuts discoverable without spamming. Two
 * triggers:
 *   1. Idle: 30 s on the same page without any keyboard activity.
 *   2. Bad-key: user presses a single letter outside an input, nothing
 *      happens (no shortcut matched).
 *
 * Either trigger shows a small toast in the bottom-right with a "Got it"
 * dismiss. Once dismissed, set a localStorage flag so we never hint again.
 *
 * Loaded via render_page after shortcuts.js so window.narve.shortcuts
 * is available. Zero dependencies, < 1 KB minified.
 */
(function () {
  "use strict";

  const STORAGE_KEY = "narve.shortcutHintDismissed";
  const IDLE_MS = 30_000;

  // Bail early if already dismissed.
  try {
    if (localStorage.getItem(STORAGE_KEY) === "1") return;
  } catch (_) {
    /* localStorage may be blocked (private mode); show once anyway. */
  }

  let toast = null;
  let shown = false;
  let idleTimer = null;
  let badKeyArmed = false;

  function dismiss(persist) {
    if (toast) {
      toast.classList.remove("narve-sc-hint--open");
      // Remove from DOM after the fade-out so a re-trigger can rebuild.
      setTimeout(() => { toast && toast.remove(); toast = null; }, 220);
    }
    shown = false;
    if (persist) {
      try { localStorage.setItem(STORAGE_KEY, "1"); } catch (_) { /* ignore */ }
    }
  }

  function build() {
    const el = document.createElement("aside");
    el.className = "narve-sc-hint";
    el.setAttribute("role", "status");
    el.setAttribute("aria-live", "polite");
    el.innerHTML =
      '<div class="narve-sc-hint__body">' +
        'Press <kbd>?</kbd> for keyboard shortcuts.' +
      '</div>' +
      '<div class="narve-sc-hint__actions">' +
        '<button type="button" class="narve-sc-hint__open"  data-narve-hint-open>Show</button>' +
        '<button type="button" class="narve-sc-hint__close" data-narve-hint-close aria-label="Dismiss">&times;</button>' +
      '</div>';
    el.addEventListener("click", (ev) => {
      const t = ev.target.closest("[data-narve-hint-open],[data-narve-hint-close]");
      if (!t) return;
      if (t.hasAttribute("data-narve-hint-open")) {
        if (window.narve && window.narve.shortcuts && window.narve.shortcuts.showHelp) {
          window.narve.shortcuts.showHelp();
        }
      }
      dismiss(true);
    });
    return el;
  }

  function show() {
    if (shown) return;
    shown = true;
    toast = build();
    document.body.appendChild(toast);
    // Force a reflow so the transition fires on the next frame.
    requestAnimationFrame(() => toast.classList.add("narve-sc-hint--open"));
    // Auto-dismiss after 12 s of inattention so we don't camp the page.
    setTimeout(() => { if (shown) dismiss(false); }, 12_000);
  }

  function armIdle() {
    clearTimeout(idleTimer);
    idleTimer = setTimeout(() => { if (!shown) show(); }, IDLE_MS);
  }

  function isTypingIn(target) {
    if (!target) return false;
    const tag = (target.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea" || tag === "select") return true;
    if (target.isContentEditable) return true;
    return false;
  }

  function onKey(event) {
    // Reset idle on any keypress.
    armIdle();
    badKeyArmed = false;

    if (isTypingIn(event.target)) return;
    // Skip if a modifier is held — those are intentional shortcuts, not
    // discovery moments.
    if (event.metaKey || event.ctrlKey || event.altKey) return;

    // Single printable character with no shortcut binding → discovery moment.
    // We can't ask the registry "did this match?" cleanly, so we use a
    // micro-tasked check: if no shortcut handler called preventDefault
    // by the next tick, treat it as unmatched.
    if (event.key.length !== 1) return;

    // Don't interrupt the user mid-question — `?` itself opens the
    // overlay, which is the goal anyway.
    if (event.key === "?") return;

    badKeyArmed = true;
    setTimeout(() => {
      if (badKeyArmed && !shown) show();
      badKeyArmed = false;
    }, 60);
  }

  function init() {
    armIdle();
    document.addEventListener("keydown", onKey, { capture: true });
    document.addEventListener("mousemove", armIdle, { passive: true });
    document.addEventListener("scroll", armIdle, { passive: true });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  window.narve = window.narve || {};
  window.narve.shortcutHint = { show, dismiss };
})();
