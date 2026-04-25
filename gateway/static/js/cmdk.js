/**
 * narveCmdK — global ⌘K / Ctrl+K command palette.
 *
 *   - Search across markets, sources, predictions via the existing
 *     /api/search FTS5 endpoint (response shape: { results: { markets:
 *     [], sources: [], predictions: [] }, total, took_ms }).
 *   - Type "/" to switch to command mode — quick navigation to the
 *     dashboard pages everyone learns the keyboard-shortcut for first.
 *   - Recent searches persist in localStorage as a Set of unique
 *     queries (last 10) so a frequent search reappears instantly.
 *   - Self-mounting: dropping a single <script> tag on every authed
 *     page is enough; no init function callers need to wire.
 *
 * Design constraints driving the implementation:
 *   - The palette must keep working on pages that haven't migrated to
 *     _base.html yet, so we inject our own root + ARIA live region
 *     defensively.
 *   - Keyboard nav has to land on every list item, including command-
 *     mode rows; the loop indexes ALL [data-idx] elements after a
 *     render so re-ordering doesn't desync the cursor.
 *   - We never trust /api/search content for direct innerHTML insert —
 *     the FTS snippet() field already wraps matches in <mark>, which
 *     we DO want to render, but we run every other field through
 *     `escape()` and only trust the highlight token through a narrow
 *     allowlist that re-emits the <mark> wrapper.
 *   - Failure modes: a /api/search 401/403 (anonymous visitor on a
 *     non-authed page) renders a soft "Sign in to search" hint
 *     rather than a stack trace. Network errors → toast.
 */
(() => {
  "use strict";

  if (window.narveCmdK && window.narveCmdK._installed) return;

  const RECENT_KEY = "nv-cmdk-recent";
  const MAX_RECENT = 10;
  const SEARCH_DEBOUNCE_MS = 150;
  const MIN_QUERY = 2;

  const STATE = {
    open: false,
    query: "",
    results: [],          // flattened [{ type, title, subtitle, url, highlight? }]
    selectedIdx: 0,
    mode: "search",       // "search" | "command" | "recent"
  };

  // Static commands surfaced when the user types "/" — same routes
  // that show up in the sidebar, plus a couple of common actions.
  const COMMANDS = [
    { label: "Go to Dashboards",       url: "/dashboards" },
    { label: "Go to Saved",            url: "/saved" },
    { label: "Go to Your predictions", url: "/predictions" },
    { label: "Go to Notifications",    url: "/notifications" },
    { label: "Go to Settings",         url: "/settings" },
    { label: "Go to Billing",          url: "/billing" },
    { label: "Go to Pricing",          url: "/pricing" },
    { label: "Go to Leaderboard",      url: "/leaderboard" },
    { label: "Go to Methodology",      url: "/methodology" },
    { label: "Go to How it works",     url: "/how-it-works" },
    { label: "Toggle theme",           action: toggleTheme },
    { label: "Sign out",               url: "/logout" },
  ];

  let modal = null;
  let inputEl = null;
  let resultsEl = null;
  let footerEl = null;

  // ── Boot ────────────────────────────────────────────────────────────

  function mount() {
    if (modal) return;
    modal = document.createElement("div");
    modal.className = "nv-cmdk";
    modal.setAttribute("role", "dialog");
    modal.setAttribute("aria-modal", "true");
    modal.setAttribute("aria-label", "Command palette");
    modal.innerHTML = `
      <div class="nv-cmdk__backdrop" data-cmdk-close></div>
      <div class="nv-cmdk__panel" role="combobox" aria-haspopup="listbox" aria-expanded="true">
        <input class="nv-cmdk__input"
               type="text"
               placeholder="Search markets, sources, predictions  ·  type / for commands"
               aria-label="Search or type a command"
               aria-controls="nv-cmdk-list"
               autocomplete="off"
               spellcheck="false">
        <div id="nv-cmdk-list" class="nv-cmdk__results" role="listbox"></div>
        <div class="nv-cmdk__footer">
          <span><kbd>↑</kbd><kbd>↓</kbd> navigate</span>
          <span><kbd>↵</kbd> open</span>
          <span><kbd>/</kbd> commands</span>
          <span><kbd>esc</kbd> close</span>
        </div>
      </div>
    `;
    document.body.appendChild(modal);
    inputEl = modal.querySelector(".nv-cmdk__input");
    resultsEl = modal.querySelector(".nv-cmdk__results");
    footerEl = modal.querySelector(".nv-cmdk__footer");

    modal.querySelector("[data-cmdk-close]").addEventListener("click", close);
    inputEl.addEventListener("input", onInput);
    inputEl.addEventListener("keydown", onKey);
    // Ensure clicks on rows close + navigate even when the input loses focus.
    resultsEl.addEventListener("click", onRowClick);
  }

  function open() {
    mount();
    STATE.open = true;
    STATE.selectedIdx = 0;
    modal.classList.add("nv-cmdk--open");
    inputEl.value = "";
    STATE.query = "";
    inputEl.focus();
    renderRecent();
  }

  function close() {
    if (!modal) return;
    STATE.open = false;
    modal.classList.remove("nv-cmdk--open");
    if (document.activeElement === inputEl) inputEl.blur();
  }

  // ── Input + render ──────────────────────────────────────────────────

  const onInput = debounce(async (e) => {
    const q = e.target.value;
    STATE.query = q;
    STATE.selectedIdx = 0;

    if (q.startsWith("/")) {
      STATE.mode = "command";
      renderCommands(q.slice(1).trim());
      return;
    }

    if (q.length < MIN_QUERY) {
      STATE.mode = "recent";
      renderRecent();
      return;
    }

    STATE.mode = "search";
    try {
      const r = await fetch(
        "/api/search?q=" + encodeURIComponent(q) + "&limit=10",
        { credentials: "same-origin" },
      );
      if (r.status === 401 || r.status === 403) {
        renderHint(
          "Sign in to search markets, sources, and predictions.",
          { href: "/login", label: "Sign in" },
        );
        return;
      }
      if (!r.ok) throw new Error("search failed: " + r.status);
      const data = await r.json();
      STATE.results = flatten(data.results || {});
      render();
    } catch (err) {
      // Toast first if available; fall back to in-panel error so the
      // user still sees something even on a half-loaded page.
      if (window.narveToastError) {
        window.narveToastError("Search failed. Try again.");
      }
      renderHint("Couldn't reach search.");
    }
  }, SEARCH_DEBOUNCE_MS);

  // Flatten the per-type response into a single list with rendering
  // metadata. The order — markets, sources, predictions — is the
  // attention order: a query usually targets a market first, then
  // the source behind a hot take, then individual predictions.
  function flatten(grouped) {
    const out = [];
    for (const m of grouped.markets || []) {
      out.push({
        type: "Markets",
        title: m.market_question || m.market_slug,
        subtitle: m.category || "",
        url: "/market/" + encodeURIComponent(m.market_slug),
        highlight: m.highlight || null,
      });
    }
    for (const s of grouped.sources || []) {
      const cred = (typeof s.global_credibility === "number")
        ? s.global_credibility.toFixed(2)
        : null;
      out.push({
        type: "Sources",
        title: "@" + s.handle,
        subtitle: cred ? ("credibility " + cred) : "",
        url: "/sources/" + encodeURIComponent(s.handle),
      });
    }
    for (const p of grouped.predictions || []) {
      out.push({
        type: "Predictions",
        title: stripTags(p.content || ""),
        subtitle: "@" + (p.source_handle || ""),
        url: "/predictions/" + p.id,
        highlight: p.highlight || null,
      });
    }
    return out;
  }

  function render() {
    const grouped = {};
    for (const r of STATE.results) {
      (grouped[r.type] = grouped[r.type] || []).push(r);
    }
    let html = "";
    let idx = 0;
    for (const type of Object.keys(grouped)) {
      html += '<div class="nv-cmdk__group-header">' + escape(type) + "</div>";
      for (const item of grouped[type]) {
        html += rowHtml(item, idx++);
      }
    }
    if (!STATE.results.length && STATE.query.length >= MIN_QUERY) {
      html =
        '<div class="nv-cmdk__no-results">No matches for &ldquo;'
        + escape(STATE.query) + "&rdquo;</div>";
    }
    resultsEl.innerHTML = html;
    syncSelection();
  }

  function renderCommands(filter) {
    const matches = filter
      ? COMMANDS.filter((c) => c.label.toLowerCase().includes(filter.toLowerCase()))
      : COMMANDS;
    let html = '<div class="nv-cmdk__group-header">Commands</div>';
    matches.forEach((cmd, i) => {
      html += rowHtml({
        title: cmd.label,
        subtitle: cmd.url || "",
        url: cmd.url,
        action: cmd.action,
      }, i);
    });
    if (!matches.length) {
      html += '<div class="nv-cmdk__no-results">No commands match.</div>';
    }
    STATE.results = matches.map((c) => ({
      title: c.label, url: c.url, action: c.action,
    }));
    resultsEl.innerHTML = html;
    syncSelection();
  }

  function renderRecent() {
    const recent = readRecent();
    if (!recent.length) {
      resultsEl.innerHTML = `
        <div class="nv-cmdk__hint">
          Start typing to search markets, sources, predictions.<br>
          Type <kbd>/</kbd> for commands.
        </div>`;
      STATE.results = [];
      return;
    }
    let html = '<div class="nv-cmdk__group-header">Recent</div>';
    recent.forEach((q, i) => {
      html += rowHtml({
        title: q,
        subtitle: "",
        action: () => { inputEl.value = q; STATE.query = q; onInput({ target: inputEl }); },
      }, i);
    });
    STATE.results = recent.map((q) => ({
      title: q,
      action: () => { inputEl.value = q; STATE.query = q; onInput({ target: inputEl }); },
    }));
    resultsEl.innerHTML = html;
    syncSelection();
  }

  function renderHint(message, action) {
    let html = '<div class="nv-cmdk__hint">' + escape(message) + "</div>";
    if (action) {
      html +=
        '<a class="nv-cmdk__hint-action" href="' + escapeAttr(action.href) + '">'
        + escape(action.label) + "</a>";
    }
    resultsEl.innerHTML = html;
    STATE.results = [];
  }

  function rowHtml(item, idx) {
    const titleHtml = item.highlight
      ? safeHighlight(item.highlight)
      : escape(item.title || "");
    const subtitle = item.subtitle
      ? '<span class="nv-cmdk__row-subtitle">' + escape(item.subtitle) + "</span>"
      : "";
    return (
      '<a class="nv-cmdk__row" role="option" data-idx="' + idx + '" '
      + (item.url ? 'href="' + escapeAttr(item.url) + '"' : 'href="#"')
      + ">"
      + '<span class="nv-cmdk__row-title">' + titleHtml + "</span>"
      + subtitle
      + "</a>"
    );
  }

  function onRowClick(e) {
    const row = e.target.closest("[data-idx]");
    if (!row) return;
    e.preventDefault();
    const idx = parseInt(row.dataset.idx, 10);
    activate(STATE.results[idx]);
  }

  function syncSelection() {
    const rows = resultsEl.querySelectorAll("[data-idx]");
    if (!rows.length) {
      footerEl && footerEl.removeAttribute("data-has-results");
      return;
    }
    if (STATE.selectedIdx >= rows.length) STATE.selectedIdx = 0;
    rows.forEach((r, i) => {
      const sel = i === STATE.selectedIdx;
      r.setAttribute("aria-selected", sel ? "true" : "false");
      r.classList.toggle("nv-cmdk__row--active", sel);
    });
    footerEl && footerEl.setAttribute("data-has-results", "true");
  }

  // ── Keyboard ────────────────────────────────────────────────────────

  function onKey(e) {
    if (e.key === "Escape") {
      e.preventDefault();
      close();
      return;
    }
    if (e.key === "ArrowDown") { e.preventDefault(); move(1); return; }
    if (e.key === "ArrowUp")   { e.preventDefault(); move(-1); return; }
    if (e.key === "Enter") {
      e.preventDefault();
      const item = STATE.results[STATE.selectedIdx];
      if (item) activate(item);
    }
  }

  function move(delta) {
    const rows = resultsEl.querySelectorAll("[data-idx]");
    if (!rows.length) return;
    STATE.selectedIdx = (STATE.selectedIdx + delta + rows.length) % rows.length;
    syncSelection();
    rows[STATE.selectedIdx].scrollIntoView({ block: "nearest" });
  }

  function activate(item) {
    if (!item) return;
    if (STATE.mode === "search" && STATE.query.length >= MIN_QUERY) {
      pushRecent(STATE.query);
    }
    close();
    if (typeof item.action === "function") {
      try { item.action(); } catch (e) { /* swallow — palette closes either way */ }
      return;
    }
    if (item.url) {
      // Match the dashboard nav convention: same-tab navigation.
      window.location.href = item.url;
    }
  }

  // ── Recent (localStorage) ───────────────────────────────────────────

  function readRecent() {
    try {
      const raw = localStorage.getItem(RECENT_KEY);
      if (!raw) return [];
      const arr = JSON.parse(raw);
      return Array.isArray(arr) ? arr.slice(0, MAX_RECENT) : [];
    } catch (e) { return []; }
  }

  function pushRecent(q) {
    const cleaned = (q || "").trim();
    if (!cleaned) return;
    try {
      const cur = readRecent().filter((x) => x !== cleaned);
      cur.unshift(cleaned);
      localStorage.setItem(
        RECENT_KEY, JSON.stringify(cur.slice(0, MAX_RECENT)),
      );
    } catch (e) { /* private mode or quota — silent */ }
  }

  // ── Helpers ─────────────────────────────────────────────────────────

  function debounce(fn, wait) {
    let t = null;
    return function (...args) {
      clearTimeout(t);
      t = setTimeout(() => fn.apply(this, args), wait);
    };
  }

  function escape(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (m) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[m]));
  }

  function escapeAttr(s) { return escape(s); }

  function stripTags(s) {
    return String(s == null ? "" : s).replace(/<[^>]+>/g, "").slice(0, 200);
  }

  /**
   * The /api/search snippet() output already wraps matched substrings
   * in <mark>…</mark>. We HTML-escape everything else, then re-emit
   * the <mark>…</mark> wrapping by replacing the escaped angle
   * brackets — narrow allowlist, no other tags survive.
   */
  function safeHighlight(snippet) {
    const escaped = escape(snippet);
    return escaped
      .replace(/&lt;mark&gt;/g, "<mark>")
      .replace(/&lt;\/mark&gt;/g, "</mark>");
  }

  function toggleTheme() {
    try {
      const cur = document.documentElement.getAttribute("data-theme") || "light";
      const next = cur === "dark" ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", next);
      localStorage.setItem("narve-theme", next);
      // Some pages still read the legacy cookie name — keep them in sync.
      document.cookie = "narve-theme=" + next + ";path=/;max-age=" + (60 * 60 * 24 * 365);
    } catch (e) { /* no-op */ }
  }

  // ── Global hotkey ───────────────────────────────────────────────────

  document.addEventListener("keydown", (e) => {
    // ⌘K (mac) / Ctrl+K (everywhere else). Stop the browser from
    // claiming the key (Chrome's "search the URL bar" binding sits
    // on Ctrl+K too on some platforms).
    if ((e.metaKey || e.ctrlKey) && (e.key === "k" || e.key === "K")) {
      e.preventDefault();
      STATE.open ? close() : open();
    }
    // "/" opens the palette in command mode when the user isn't
    // typing into another input.
    if (
      !STATE.open && e.key === "/" && !isEditableTarget(e.target)
    ) {
      e.preventDefault();
      open();
      // After the palette mounts the input gets focus; pre-fill
      // with "/" so the command-mode UI fires on the next keystroke
      // without forcing the user to retype the slash.
      setTimeout(() => {
        if (inputEl) {
          inputEl.value = "/";
          STATE.query = "/";
          STATE.mode = "command";
          renderCommands("");
        }
      }, 0);
    }
  });

  function isEditableTarget(el) {
    if (!el) return false;
    const tag = el.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return true;
    if (el.isContentEditable) return true;
    return false;
  }

  // ── Public surface ──────────────────────────────────────────────────

  window.narveCmdK = {
    open,
    close,
    _installed: true,
  };
})();
