/**
 * narveShareMenu — generic share-button dropdown.
 *
 * Mounts a shared "Share ▾" button + menu into any element carrying a
 * data-share attribute. Four actions: copy link, copy as markdown,
 * share on X, preview OG card.
 *
 * Why this lives alongside the existing share-button.js (mints
 * signed share TOKENS via POST /api/share/{kind}, single-action,
 * already on prediction_detail / market_detail / source.html):
 *
 *   - The token-mint flow is high-friction: one click → server
 *     round-trip → copy a /s/m/{token} URL. Right for sharing
 *     resolved predictions where the link is canonical.
 *   - This menu is low-friction: copy the current canonical URL
 *     instantly, no server round-trip. Right for the "I want to
 *     send this market to a friend" path that's most of the share
 *     volume.
 *
 * Both surfaces can coexist on the same page — the legacy button
 * keeps minting tokens, and this menu offers the lighter copies.
 *
 * DOM API:
 *
 *   <span data-share
 *         data-share-url="https://narve.ai/market/foo"
 *         data-share-title="Will the Fed hold rates?"
 *         data-share-markdown="**Will the Fed hold rates?** — narve …"
 *         data-share-og="/og/market/foo"></span>
 *
 *   - data-share-url       defaults to location.href
 *   - data-share-title     defaults to document.title
 *   - data-share-markdown  defaults to `[title](url)`
 *   - data-share-og        the public OG image route (optional)
 *
 * Mounts on DOMContentLoaded + on htmx:afterSwap so async-rendered
 * cards pick the menu up automatically. Idempotent — re-mounting
 * the same element is a no-op.
 *
 * Keyboard:
 *   Tab   move into trigger
 *   ↵     toggle the menu
 *   ↑/↓   move within the menu
 *   Esc   close
 *
 * Hard requirement: window.narveToast must exist for copy feedback.
 * Defensive fallback: alert() so a half-loaded page still gives the
 * user feedback.
 */
(() => {
  "use strict";

  if (window.narveShareMenu && window.narveShareMenu._installed) return;

  const SVG_SHARE = `
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none"
         stroke="currentColor" stroke-width="2"
         stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <path d="M4 12v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8"></path>
      <polyline points="16 6 12 2 8 6"></polyline>
      <line x1="12" y1="2" x2="12" y2="15"></line>
    </svg>`;

  const SVG_CARET = `
    <svg width="10" height="10" viewBox="0 0 24 24" fill="none"
         stroke="currentColor" stroke-width="2.4"
         stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <polyline points="6 9 12 15 18 9"></polyline>
    </svg>`;

  function escAttr(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (m) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[m]));
  }

  function toast(msg, opts) {
    const fn = window.narveToast || ((m) => alert(m));
    try { fn(msg, opts || {}); } catch (e) { /* swallow */ }
  }

  function toastError(msg) {
    const fn = window.narveToastError || ((m) => alert(m));
    try { fn(msg); } catch (e) { /* swallow */ }
  }

  // ── Clipboard helpers ────────────────────────────────────────────
  //
  // navigator.clipboard.writeText is the canonical API but it's
  // gated on (a) HTTPS / localhost and (b) a user gesture, neither
  // of which is reliable in dev or in iframe / embed contexts. The
  // legacy execCommand fallback handles those edges.

  async function copyToClipboard(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      try {
        await navigator.clipboard.writeText(text);
        return true;
      } catch (e) { /* fall through */ }
    }
    try {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.style.position = "absolute";
      ta.style.left = "-9999px";
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand("copy");
      document.body.removeChild(ta);
      return ok;
    } catch (e) {
      return false;
    }
  }

  // ── Single-menu invariant ────────────────────────────────────────
  // Only one menu is open at a time. We track the open trigger so
  // outside-click + Escape can close it without re-querying the DOM.

  let openTrigger = null;

  function closeOpen() {
    if (!openTrigger) return;
    const root = openTrigger.closest(".nv-share");
    if (root) {
      root.classList.remove("nv-share--open");
      const menu = root.querySelector(".nv-share__menu");
      if (menu) menu.hidden = true;
    }
    openTrigger.setAttribute("aria-expanded", "false");
    openTrigger = null;
  }

  document.addEventListener("click", (ev) => {
    if (!openTrigger) return;
    const root = openTrigger.closest(".nv-share");
    if (root && root.contains(ev.target)) return;
    closeOpen();
  });

  document.addEventListener("keydown", (ev) => {
    if (!openTrigger) return;
    if (ev.key === "Escape") {
      closeOpen();
      openTrigger.focus();
    }
  });

  // ── Build + behaviour ────────────────────────────────────────────

  function build(opts) {
    const wrap = document.createElement("span");
    wrap.className = "nv-share";

    const trigger = document.createElement("button");
    trigger.type = "button";
    trigger.className = "nv-share__trigger";
    trigger.setAttribute("aria-haspopup", "menu");
    trigger.setAttribute("aria-expanded", "false");
    trigger.setAttribute("aria-label", "Share " + (opts.title || "this page"));
    trigger.innerHTML =
      `<span class="nv-share__icon">${SVG_SHARE}</span>` +
      `<span class="nv-share__label">Share</span>` +
      `<span class="nv-share__caret">${SVG_CARET}</span>`;

    const menu = document.createElement("div");
    menu.className = "nv-share__menu";
    menu.setAttribute("role", "menu");
    menu.hidden = true;

    const xUrl =
      "https://x.com/intent/tweet?text=" +
      encodeURIComponent(opts.title || "") +
      "&url=" + encodeURIComponent(opts.url || "");

    menu.innerHTML = `
      <button type="button" role="menuitem" class="nv-share__item"
              data-share-action="copy-link">Copy link</button>
      <button type="button" role="menuitem" class="nv-share__item"
              data-share-action="copy-markdown">Copy as markdown</button>
      <a role="menuitem" class="nv-share__item"
         data-share-action="share-x"
         href="${escAttr(xUrl)}"
         target="_blank" rel="noopener noreferrer">Share on X</a>
      ${opts.ogUrl ? `
        <a role="menuitem" class="nv-share__item"
           data-share-action="og-preview"
           href="${escAttr(opts.ogUrl)}"
           target="_blank" rel="noopener noreferrer">Preview card</a>
      ` : ""}
    `;

    wrap.appendChild(trigger);
    wrap.appendChild(menu);

    function open() {
      if (openTrigger && openTrigger !== trigger) closeOpen();
      menu.hidden = false;
      wrap.classList.add("nv-share--open");
      trigger.setAttribute("aria-expanded", "true");
      openTrigger = trigger;
      // Focus the first menu item so keyboard users land in the menu.
      const first = menu.querySelector("[role='menuitem']");
      if (first) first.focus();
    }

    function close() {
      menu.hidden = true;
      wrap.classList.remove("nv-share--open");
      trigger.setAttribute("aria-expanded", "false");
      if (openTrigger === trigger) openTrigger = null;
    }

    trigger.addEventListener("click", (e) => {
      e.preventDefault();
      const isOpen = !menu.hidden;
      if (isOpen) close();
      else open();
    });

    trigger.addEventListener("keydown", (e) => {
      if (e.key === "ArrowDown" || e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        open();
      }
    });

    menu.addEventListener("keydown", (e) => {
      const items = Array.from(menu.querySelectorAll("[role='menuitem']"));
      const idx = items.indexOf(document.activeElement);
      if (e.key === "ArrowDown") {
        e.preventDefault();
        const next = items[Math.min(idx + 1, items.length - 1)];
        if (next) next.focus();
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        const prev = items[Math.max(idx - 1, 0)];
        if (prev) prev.focus();
      } else if (e.key === "Home") {
        e.preventDefault(); items[0] && items[0].focus();
      } else if (e.key === "End") {
        e.preventDefault(); items[items.length - 1] && items[items.length - 1].focus();
      } else if (e.key === "Tab") {
        // Let Tab close the menu the way OS-native menus do — moves
        // focus on out, no need to trap.
        close();
      }
    });

    menu.addEventListener("click", async (e) => {
      const item = e.target.closest("[data-share-action]");
      if (!item) return;
      const action = item.dataset.shareAction;

      // Anchors (share-x, og-preview) handle their own navigation —
      // close the menu but let the browser open the new tab.
      if (item.tagName === "A") {
        close();
        return;
      }

      e.preventDefault();
      close();

      if (action === "copy-link") {
        const ok = await copyToClipboard(opts.url || location.href);
        ok ? toast("Link copied") : toastError("Couldn't copy link.");
      } else if (action === "copy-markdown") {
        const md = opts.markdown ||
          `[${opts.title || "narve.ai"}](${opts.url || location.href})`;
        const ok = await copyToClipboard(md);
        ok ? toast("Markdown copied") : toastError("Couldn't copy markdown.");
      }
    });

    return wrap;
  }

  // ── Mount ────────────────────────────────────────────────────────

  function mount(root) {
    const scope = root || document;
    const els = scope.querySelectorAll("[data-share]");
    let mounted = 0;
    els.forEach((el) => {
      if (el.dataset.shareMounted === "1") return;
      el.dataset.shareMounted = "1";
      const opts = {
        url: el.dataset.shareUrl || location.href,
        title: el.dataset.shareTitle || document.title,
        markdown: el.dataset.shareMarkdown || "",
        ogUrl: el.dataset.shareOg || "",
      };
      el.appendChild(build(opts));
      mounted++;
    });
    return mounted;
  }

  function init() {
    mount(document);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init, { once: true });
  } else {
    init();
  }

  // Re-scan when async fragments land. We listen on htmx:afterSwap
  // (htmx is in the page already — see existing share-button.js
  // pattern) AND on a custom narve:rescan-share-buttons event so
  // pages that don't use htmx can still trigger a remount.
  document.addEventListener("htmx:afterSwap", () => mount(document));
  document.addEventListener("narve:rescan-share-menus", () => mount(document));

  window.narveShareMenu = {
    mount,
    _installed: true,
  };
})();
