/**
 * narveToast — the single surface for ephemeral feedback.
 *
 * Why a dedicated module:
 *   - Replaces alert() (blocks the main thread, breaks mobile UX, can't be
 *     styled to match the monochrome chrome) everywhere in the client JS.
 *   - Replaces the zoo of bespoke `.flash-success` / `.banner-error` /
 *     "errorMsg.textContent = …" patterns each page rolled its own
 *     version of. One function, one visual grammar.
 *   - Respects `aria-live` so screen readers announce the change without
 *     stealing focus. Errors use role="alert" for assertive announcement;
 *     info + success use role="status" (polite).
 *
 * API:
 *   narveToast("Saved.");
 *   narveToast("Couldn't save.", { type: "error" });
 *   narveToast("Prediction deleted.", { action: { label: "Undo", onClick: () => … } });
 *   narveToast("Payment processing…", { duration: 8000, type: "info" });
 *
 * Design invariants (don't relax without updating CSS too):
 *   - Region is position: fixed so toasts sit above sidebars and modals.
 *   - Bottom-centre on desktop, top-centre on narrow screens (thumbs reach
 *     the top of the screen on phones; bottom conflicts with the status bar).
 *   - Max 480 px wide so long messages wrap at a readable measure.
 *   - Click anywhere on a toast to dismiss it early — useful on mobile
 *     when the auto-dismiss feels slow.
 *   - `data-testid="nv-toast"` on every toast so integration tests can
 *     assert presence without coupling to the class name.
 */
(() => {
  "use strict";

  const REGION_ID = "nv-toast-region";
  const DEFAULTS = { type: "info", duration: 2500 };

  function region() {
    let el = document.getElementById(REGION_ID);
    if (!el) {
      // Page didn't pre-render the region (older templates). Inject it so
      // narveToast works even on non-migrated pages; later sessions can
      // rely on the in-base version.
      el = document.createElement("div");
      el.id = REGION_ID;
      el.setAttribute("aria-live", "polite");
      el.setAttribute("aria-atomic", "true");
      document.body.appendChild(el);
    }
    return el;
  }

  function escape(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (m) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[m]));
  }

  window.narveToast = (message, opts = {}) => {
    const { type, duration, action } = { ...DEFAULTS, ...opts };
    const el = document.createElement("div");
    el.className = `nv-toast nv-toast--${type}`;
    el.setAttribute("data-testid", "nv-toast");
    // role=alert for errors to force screen-reader announcement; status
    // for the info/success path so we don't interrupt anything they're
    // reading.
    el.setAttribute("role", type === "error" ? "alert" : "status");

    const msg = document.createElement("span");
    msg.className = "nv-toast__msg";
    msg.textContent = message;
    el.appendChild(msg);

    let timeoutHandle = null;
    const dismiss = () => {
      if (timeoutHandle) clearTimeout(timeoutHandle);
      el.classList.add("nv-toast--exit");
      // Match the CSS transition duration so removal happens after the
      // exit animation lands; 240 ms is the upper bound of the transition.
      setTimeout(() => el.remove(), 240);
    };

    if (action && typeof action === "object" && action.label) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "nv-toast__action";
      btn.textContent = action.label;
      btn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        try {
          action.onClick && action.onClick();
        } finally {
          dismiss();
        }
      });
      el.appendChild(btn);
    }

    region().appendChild(el);
    // Force a reflow so the enter transition animates from the initial
    // opacity/transform state rather than jumping straight to the final
    // values in the same frame.
    // eslint-disable-next-line no-unused-expressions
    el.offsetHeight;
    requestAnimationFrame(() => el.classList.add("nv-toast--enter"));

    timeoutHandle = setTimeout(dismiss, duration);

    // Click-to-dismiss on mobile + as a secondary affordance on desktop.
    el.addEventListener("click", (ev) => {
      if (ev.target && ev.target.classList &&
          ev.target.classList.contains("nv-toast__action")) return;
      dismiss();
    });

    return el;
  };

  // Back-compat shortcut for the common "X failed, try again" pattern —
  // saves call-sites from passing { type: "error" } every time.
  window.narveToastError = (message, opts = {}) =>
    window.narveToast(message, { ...opts, type: "error" });

  // Useful hook so tests / e2e can wait for the first toast without
  // polling the DOM.
  window.narveToast._ready = true;
})();
