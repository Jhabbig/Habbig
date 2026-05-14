/* "What is this page?" popover — small ⓘ next to every .page-title h1.
 *
 * Two ways to wire an explanation:
 *
 *   1. Inline data attributes on any element:
 *        <h1 class="page-title"
 *            data-explain="One paragraph explanation."
 *            data-explain-title="Optional headline">…</h1>
 *      Templates that already control their own h1 just opt in by
 *      adding the attributes.
 *
 *   2. URL-path lookup against the EXPLANATIONS table below. Pages that
 *      render a generic h1 (no per-page template owner) get covered
 *      without touching the HTML — the script attaches an icon to any
 *      `.page-title` whose path is in the table.
 *
 * Public hooks:
 *   window.narveExplain.mount()          — re-scan the DOM (HTMX swaps).
 *   window.narveExplain.close()          — programmatic close.
 *   window.narveExplain.set(path, …)     — register an explanation at runtime.
 *   window.narveExplain.EXPLANATIONS     — the static table (see below).
 *
 * Accessibility:
 *   * Trigger button has aria-haspopup="dialog" + aria-expanded.
 *   * Popover has role="dialog" + aria-labelledby pointing at its h4.
 *   * Escape closes; focus returns to the trigger.
 *   * Click-outside closes.
 *   * Multiple icons on a page work independently — opening one closes
 *     any other open popover first.
 */
(function () {
  "use strict";

  /** Path → {title, body, link?}. Match against window.location.pathname.
   *  Trailing slashes are stripped at lookup time. */
  var EXPLANATIONS = {
    // ── Dashboard ────────────────────────────────────────────────────
    "/dashboard/feed": {
      title: "Feed",
      body: "Predictions from the sources you follow, in real time. Each prediction has a credibility score from 0–1 — higher means the source has been right more often, weighted by how confidently they called it and how early.",
    },
    "/dashboard/best-bets": {
      title: "Best bets",
      body: "Markets where narve's consensus probability disagrees with the market price by enough to matter. Edge is the percentage-point gap. Sorted by expected value after credibility-weighting.",
    },
    "/dashboard/markets": {
      title: "Markets",
      body: "Every Polymarket and Kalshi market we track. Filter by category, time-to-close, edge, source count. Save filters as views from the panel on the left.",
    },
    "/dashboard/sources": {
      title: "Sources",
      body: "The people whose predictions feed our consensus. Each has a credibility score derived from resolved predictions: accuracy + calibration + timing + network-adjusted novelty.",
    },
    "/dashboard/intelligence": {
      title: "Intelligence",
      body: "Ask questions about markets, sources, your own predictions, or the underlying data. Backed by Claude with access to current market state and your history.",
    },
    "/dashboard/predictions": {
      title: "Your predictions",
      body: "Calls you've made. Track accuracy over time. Brier score measures probabilistic accuracy — lower is better, 0.25 is a coin flip.",
    },
    "/dashboard/watchlist": {
      title: "Watchlist",
      body: "Markets and sources you're tracking. Get notifications when prices move significantly or new predictions are made.",
    },
    "/dashboard/portfolio": {
      title: "Portfolio",
      body: "Your live positions on connected platforms. P&L computed against entry. Risk metrics include Kelly-suggested sizing.",
    },

    // Top-level analogues for routes that exist outside /dashboard/* in
    // this build. Path tries the longer form first, then falls through.
    "/predictions": {
      title: "Your predictions",
      body: "Calls you've made. Track accuracy over time. Brier score measures probabilistic accuracy — lower is better, 0.25 is a coin flip.",
    },
    "/saved": {
      title: "Watchlist",
      body: "Markets and sources you're tracking. Get notifications when prices move significantly or new predictions are made.",
    },
    "/dashboards": {
      title: "Dashboards",
      body: "Your hub. Each card opens a sibling dashboard in its own tab. Locked tiles need an active subscription — manage that under Billing.",
    },
    "/intelligence": {
      title: "Intelligence",
      body: "Ask questions about markets, sources, your own predictions, or the underlying data. Backed by Claude with access to current market state and your history.",
    },

    // ── Settings ─────────────────────────────────────────────────────
    "/settings": {
      title: "Settings",
      body: "Account, language, theme, email preferences, privacy. Settings save immediately — no Apply button.",
    },
    "/settings/billing": {
      title: "Billing",
      body: "Your current plan, payment method, and invoice history. Cancel or upgrade anytime — changes take effect immediately.",
    },
    "/billing": {
      title: "Billing",
      body: "Your current plan, payment method, and invoice history. Cancel or upgrade anytime — changes take effect immediately.",
    },
    "/settings/api-keys": {
      title: "API keys",
      body: "API keys let you query narve programmatically. Each key has a rate limit per hour. Keys are shown only once at creation.",
    },
    "/settings/webhooks": {
      title: "Webhooks",
      body: "Webhooks deliver events (best bets, market resolutions, signals) to your URL. Each delivery is HMAC-signed using your secret.",
    },

    // ── Admin ────────────────────────────────────────────────────────
    "/admin": {
      title: "Admin",
      body: "Operations dashboard. User counts, MRR, open incidents, AI spend. Click any tile to drill in.",
    },
    "/admin/users": {
      title: "Users",
      body: "Search and inspect users. Click a row for detail + impersonation.",
    },
    "/admin/impersonations": {
      title: "Impersonations",
      body: "Audit log of every admin impersonation. Reason required at start. Destructive paths blocked while impersonating.",
    },
    "/admin/flags": {
      title: "Feature flags",
      body: "Globally disable, enable per user, or roll out by percentage. Evaluation order: globally disabled → user disabled → user enabled → tier → rollout %.",
    },
    "/admin/emails": {
      title: "Outbound emails",
      body: "Recent sends, queued deliveries, and failed jobs. Filter by template / status / recipient. Inspect rendered payload + retry failed deliveries.",
    },
    "/admin/email-templates": {
      title: "Email templates",
      body: "Override default email templates. DB override wins over file fallback. Broken overrides fall through silently — no email is dropped.",
    },
    "/admin/incidents": {
      title: "Incidents",
      body: "Status page incidents. Active incidents are visible publicly at /status.",
    },
    "/admin/security/forensics": {
      title: "Forensics",
      body: "Identify which user a leaked screenshot or data dump came from. Watermark extraction + data-perturbation reverse lookup.",
    },
    "/admin/cache": {
      title: "Cache",
      body: "In-memory TTL cache state. Hit rate, items, prefix breakdown. Clear specific keys for testing.",
    },
    "/admin/ai-usage": {
      title: "AI usage",
      body: "Claude API spend by feature. Daily total, per-feature breakdown, kill-switch for emergency cost containment.",
    },
    "/admin/audit-log": {
      title: "Audit log",
      body: "Append-only log of admin actions and security-relevant events. Filter by action, user, time range.",
    },
    "/admin/moderation": {
      title: "Moderation",
      body: "Reported content waiting for review. Resolve by deleting, dismissing, or warning the user.",
    },
    "/admin/feedback": {
      title: "Feedback",
      body: "User-submitted product feedback and roadmap votes. Triage, merge duplicates, mark shipped.",
    },
    "/admin/sharing": {
      title: "Sharing & referrals",
      body: "Share-loop metrics — shares created, click-throughs, signups attributed, top referrers, country breakdown.",
    },
    "/admin/churn": {
      title: "Churn",
      body: "Cancellation reasons, retention paths, and pause-vs-cancel ratios. Click a reason to filter.",
    },

    // ── Marketing / public ───────────────────────────────────────────
    "/pricing": {
      title: "Pricing",
      body: "Plans and what's included. Pro bundles all six subproducts plus the main platform.",
    },
    "/methodology": {
      title: "Methodology",
      body: "How credibility is computed: accuracy, calibration, timing, network independence, weighted across resolved predictions.",
    },
    "/changelog": {
      title: "Changelog",
      body: "Every user-visible change to narve.ai, newest first. Subscribe via the RSS link at the foot of the page.",
    },
    "/explore": {
      title: "Explore",
      body: "Curated collections, featured sources, and recently-updated playlists from the community.",
    },
    "/leaderboard": {
      title: "Leaderboard",
      body: "Top-ranked subscribers and sources by prediction accuracy. Switch the period to see different windows.",
    },
    "/saved": {
      title: "Saved predictions",
      body: "Bookmark a prediction and we'll show its outcome here when the market resolves.",
    },
    "/notifications": {
      title: "Notifications",
      body: "Market resolutions, signal alerts, and anything else you've opted in for. Tap to jump to the source — it'll be marked read automatically.",
    },
    "/calendar": {
      title: "Resolution calendar",
      body: "Markets sorted by closing date. Use the filter chips to scope by category or expected close window.",
    },
    "/signal-search": {
      title: "Signal Search",
      body: "Topic-based monitoring across all tracked sources. Pull fresh signals into a topic to see what's moving.",
    },
    "/predictions": {
      title: "Your predictions",
      body: "Calls you've recorded against open markets. Brier score is lower-is-better; 0.0 is perfect, 0.25 is a coin flip.",
    },
    "/profile": {
      title: "Your profile",
      body: "Account details, subscription status, and the public-profile opt-in. Toggle that on to claim a /u/{handle} page.",
    },
    "/settings/saved-views": {
      title: "Saved views",
      body: "Named filter sets scoped to a tab. Pin to the sidebar for one-click access; share read-only with anyone via a token URL.",
    },
    "/settings/embeds": {
      title: "Embed widgets",
      body: "Generate token-gated, domain-locked iframes that render narve.ai data on your own site.",
    },
    "/settings/profile": {
      title: "Public profile",
      body: "Opt in to a /u/{handle} page. Pick a handle, choose what's visible, and your top public predictions show up there.",
    },
    "/settings/appearance": {
      title: "Appearance",
      body: "Theme + density. Compact tightens row padding and section gaps without changing the layout.",
    },
    "/collections": {
      title: "Collections",
      body: "Spotify-style playlists for markets, sources, and predictions. Make them private, share-by-link, or fully public.",
    },
    "/feedback": {
      title: "Feedback",
      body: "What other subscribers want, ranked by upvote. Add yours, comment on theirs.",
    },
  };

  // ── DOM helpers ─────────────────────────────────────────────────────

  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function normalisePath(path) {
    if (!path) return "/";
    var stripped = path.replace(/\/+$/, "");
    return stripped || "/";
  }

  function lookupForPath(path) {
    return EXPLANATIONS[normalisePath(path)] || null;
  }

  // ── Popover lifecycle ───────────────────────────────────────────────

  var activePopover = null;
  var activeTrigger = null;
  var popoverIdSeq = 0;

  function close() {
    if (activePopover && activePopover.parentNode) {
      activePopover.parentNode.removeChild(activePopover);
    }
    if (activeTrigger) {
      activeTrigger.setAttribute("aria-expanded", "false");
      // Return focus only if the user didn't move it elsewhere meanwhile.
      try { activeTrigger.focus({ preventScroll: true }); } catch (e) {}
    }
    activePopover = null;
    activeTrigger = null;
    document.removeEventListener("click", outsideClickHandler, true);
    document.removeEventListener("keydown", escHandler, true);
    window.removeEventListener("resize", close);
    window.removeEventListener("scroll", close, true);
  }

  function outsideClickHandler(e) {
    if (!activePopover) return;
    if (activePopover.contains(e.target)) return;
    if (activeTrigger && activeTrigger.contains(e.target)) return;
    close();
  }

  function escHandler(e) {
    if (e.key === "Escape" || e.key === "Esc") {
      e.stopPropagation();
      close();
    }
  }

  function show(trigger, body, title, link) {
    if (activeTrigger === trigger) { close(); return; }
    if (activePopover) close();

    var popoverId = "nv-explain-" + (++popoverIdSeq);
    var headingId = popoverId + "-h";

    var pop = document.createElement("div");
    pop.className = "nv-explain__popover";
    pop.id = popoverId;
    pop.setAttribute("role", "dialog");
    if (title) pop.setAttribute("aria-labelledby", headingId);

    var html = "";
    if (title) {
      html += '<h4 id="' + headingId + '">' + escapeHtml(title) + "</h4>";
    }
    html += "<p>" + escapeHtml(body) + "</p>";
    if (link && link.href && link.label) {
      html += '<a href="' + escapeHtml(link.href) + '">' +
        escapeHtml(link.label) + "</a>";
    }
    pop.innerHTML = html;

    document.body.appendChild(pop);

    // Position below the trigger. We measure after insertion so the
    // popover's own size is available.
    var triggerRect = trigger.getBoundingClientRect();
    var popRect = pop.getBoundingClientRect();
    var top = triggerRect.bottom + window.scrollY + 8;
    var left = triggerRect.left + window.scrollX;
    // Right-edge guard — don't paint past the viewport.
    var maxLeft = window.scrollX + window.innerWidth - popRect.width - 12;
    if (left > maxLeft) left = maxLeft;
    if (left < 8) left = 8;
    pop.style.top = top + "px";
    pop.style.left = left + "px";

    trigger.setAttribute("aria-expanded", "true");
    trigger.setAttribute("aria-controls", popoverId);

    activePopover = pop;
    activeTrigger = trigger;

    // Defer the listener attachment so the click that opened the
    // popover doesn't immediately close it.
    setTimeout(function () {
      document.addEventListener("click", outsideClickHandler, true);
      document.addEventListener("keydown", escHandler, true);
      window.addEventListener("resize", close);
      window.addEventListener("scroll", close, true);
    }, 0);
  }

  // ── Trigger creation ────────────────────────────────────────────────

  var ICON_SVG =
    '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" ' +
    'stroke="currentColor" stroke-width="2" aria-hidden="true">' +
    '<circle cx="12" cy="12" r="10"/>' +
    '<line x1="12" y1="11" x2="12" y2="17"/>' +
    '<circle cx="12" cy="7.5" r="0.9" fill="currentColor"/>' +
    "</svg>";

  function createTrigger(body, title, link) {
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "nv-explain__trigger";
    btn.setAttribute("aria-label", "Explain this page");
    btn.setAttribute("aria-haspopup", "dialog");
    btn.setAttribute("aria-expanded", "false");
    btn.dataset.explainMounted = "1";
    btn.innerHTML = ICON_SVG;
    btn.addEventListener("click", function (e) {
      e.preventDefault();
      e.stopPropagation();
      show(btn, body, title, link);
    });
    return btn;
  }

  function attachToElement(el, body, title, link) {
    if (!el || !body) return;
    if (el.dataset.explainMounted === "1") return;
    el.dataset.explainMounted = "1";
    var trigger = createTrigger(body, title, link);
    el.appendChild(trigger);
  }

  // ── Mount / re-mount ───────────────────────────────────────────────

  function mount() {
    // 1. Inline opt-in: any [data-explain] without an attached trigger.
    var explicit = document.querySelectorAll(
      "[data-explain]:not([data-explain-mounted='1'])"
    );
    explicit.forEach(function (el) {
      var body = el.dataset.explain || "";
      var title = el.dataset.explainTitle || "";
      attachToElement(el, body, title, null);
    });

    // 2. Path-based fallback: every .page-title under the current path.
    var entry = lookupForPath(window.location.pathname);
    if (!entry) return;
    var titles = document.querySelectorAll(
      ".page-title:not([data-explain-mounted='1'])"
    );
    if (!titles.length) {
      // Some templates use h1 alone without .page-title; cover the
      // first <h1> on the page only — never touch arbitrary h1s like
      // "Sign in" inside auth cards.
      var firstH1 = document.querySelector("main h1, .main-content h1");
      if (firstH1 && !firstH1.dataset.explainMounted) {
        attachToElement(firstH1, entry.body, entry.title, entry.link);
      }
      return;
    }
    titles.forEach(function (el) {
      attachToElement(el, entry.body, entry.title, entry.link);
    });
  }

  // ── Public API ─────────────────────────────────────────────────────

  window.narveExplain = {
    mount: mount,
    close: close,
    set: function (path, entry) {
      EXPLANATIONS[normalisePath(path)] = entry;
    },
    EXPLANATIONS: EXPLANATIONS,
    _show: show,           // exported for tests
    _lookup: lookupForPath,
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", mount);
  } else {
    mount();
  }
  // Re-run after HTMX swaps and after our own dynamic insertions
  // (e.g. command-palette injecting a page header).
  document.addEventListener("htmx:afterSwap", mount);
})();
