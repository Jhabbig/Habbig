/* engagement_banner.js — dashboard-top re-engagement prompt.
 *
 * Flow:
 *   1. On DOMContentLoaded, hit GET /api/engagement/prompt.
 *   2. If prompt === null, do nothing.
 *   3. Otherwise render a dismissible banner at the top of <main>
 *      (or <body> if no <main>), with a CTA linking to prompt.cta_url.
 *   4. On dismiss, POST /api/engagement/prompt/dismiss with the tier.
 *      The server stores a 7-day cooldown row so we don't re-nag.
 *
 * Kept deliberately small + framework-free. Renders with plain DOM ops
 * so it drops into any page without waiting for a bundler. Safe to load
 * twice — the top-of-file idempotency guard prevents double-render.
 */
(function () {
  "use strict";

  if (window.__narveEngagementBannerLoaded) return;
  window.__narveEngagementBannerLoaded = true;

  // Pages where we deliberately suppress the banner. /settings/billing
  // especially — the user is already managing their subscription there
  // and a "you're churning" nag on top of that is obnoxious.
  var SUPPRESS_PATHS = [
    "/settings/billing",
    "/settings/billing/cancel-flow",
    "/token",
    "/login",
    "/admin",
  ];

  function shouldSuppress() {
    var p = (window.location.pathname || "").toLowerCase();
    for (var i = 0; i < SUPPRESS_PATHS.length; i++) {
      if (p === SUPPRESS_PATHS[i] || p.indexOf(SUPPRESS_PATHS[i] + "/") === 0) {
        return true;
      }
    }
    return false;
  }

  function getCsrf() {
    var m = document.cookie.match(/(?:^|; )_csrf=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : "";
  }

  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    attrs = attrs || {};
    Object.keys(attrs).forEach(function (k) {
      if (k === "style") {
        node.setAttribute("style", attrs[k]);
      } else if (k === "onclick") {
        node.addEventListener("click", attrs[k]);
      } else if (k === "html") {
        node.innerHTML = attrs[k];
      } else {
        node.setAttribute(k, attrs[k]);
      }
    });
    (children || []).forEach(function (c) {
      if (typeof c === "string") node.appendChild(document.createTextNode(c));
      else if (c) node.appendChild(c);
    });
    return node;
  }

  function dismiss(tier, bannerEl) {
    // Hide immediately — don't wait for the server to confirm. The POST
    // is idempotent and a failure just means the banner comes back next
    // reload, which is harmless.
    bannerEl.parentNode && bannerEl.parentNode.removeChild(bannerEl);
    var csrf = getCsrf();
    var opts = {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": csrf,
      },
      body: JSON.stringify({ tier: tier }),
    };
    fetch("/api/engagement/prompt/dismiss", opts).catch(function () {});
  }

  function render(prompt) {
    // Visual style matches the sb-notice pattern used on /settings pages:
    // rounded card, accent-tinted border, compact padding.
    var bg = prompt.tier === "critical"
      ? "rgba(239,68,68,0.08)"
      : "rgba(59,130,246,0.08)";
    var border = prompt.tier === "critical"
      ? "rgba(239,68,68,0.3)"
      : "rgba(59,130,246,0.3)";
    var style =
      "display:flex;align-items:center;gap:16px;" +
      "max-width:1080px;margin:16px auto;padding:12px 16px;" +
      "background:" + bg + ";border:1px solid " + border + ";" +
      "border-radius:8px;font-size:13px;color:var(--text-primary);";

    var banner = el("div", {
      style: style,
      id: "narve-engagement-banner",
      role: "status",
      "data-tier": prompt.tier,
    }, [
      el("div", { style: "flex:1;min-width:0" }, [prompt.message]),
      el("a", {
        href: prompt.cta_url,
        class: "sb-btn sb-btn-primary",
        style: "font-size:12px;padding:6px 12px;flex-shrink:0",
      }, [prompt.cta_label || "Open"]),
      el("button", {
        type: "button",
        "aria-label": "Dismiss",
        style:
          "background:none;border:0;color:var(--text-muted);cursor:pointer;" +
          "font-size:18px;line-height:1;padding:4px 8px;flex-shrink:0",
        onclick: function () { dismiss(prompt.tier, banner); },
      }, ["×"]),
    ]);

    // Insert at the top of <main> if it exists; otherwise right after
    // the opening <body>. Either way the banner reads before the
    // dashboard grid.
    var host = document.querySelector("main") || document.body;
    if (host.firstChild) {
      host.insertBefore(banner, host.firstChild);
    } else {
      host.appendChild(banner);
    }
  }

  function load() {
    if (shouldSuppress()) return;
    fetch("/api/engagement/prompt", {
      credentials: "same-origin",
      headers: { "Accept": "application/json" },
    })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (body) {
        if (!body || !body.prompt) return;
        render(body.prompt);
      })
      .catch(function () {});
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", load);
  } else {
    load();
  }
})();
