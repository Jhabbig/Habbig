/* "What's new" widget — fetches recent changelog entries, renders the
 * top 3, marks them seen after 5 s of visibility, persists collapse
 * state per-user in localStorage.
 *
 * The widget self-installs on any page that contains a
 * `[data-changelog]` element. Pages that don't ship the partial just
 * never see it; the script's load-listener returns immediately.
 *
 * Backend contract:
 *   GET  /api/changelog?limit=3   → {entries: [...], unseen_count: N}
 *   POST /api/changelog/seen      → {persisted: bool, marked: N}
 */
(function () {
  "use strict";

  var SEEN_DELAY_MS = 5000;
  var COLLAPSE_KEY = "narve_changelog_collapsed";

  function $(sel, root) { return (root || document).querySelector(sel); }

  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function formatDate(iso) {
    if (!iso) return "Unreleased";
    // Render in the user's locale if Intl is available; otherwise the
    // raw "YYYY-MM-DD" string is already legible.
    try {
      var lang = window.LANG || "en";
      var d = new Date(iso + "T00:00:00Z");
      if (isNaN(d.getTime())) return iso;
      return new Intl.DateTimeFormat(lang, {
        year: "numeric", month: "short", day: "numeric",
      }).format(d);
    } catch (e) {
      return iso;
    }
  }

  function getCsrf() {
    // The server uses a double-submit cookie pattern with cookie name
    // _csrf; the matching header is x-csrf-token. Fall back to
    // window.NV_CSRF if a build is exposing it that way.
    var m = document.cookie.match(/(?:^|;\s*)_csrf=([^;]*)/);
    if (m) return decodeURIComponent(m[1]);
    return window.NV_CSRF || "";
  }

  function renderSkeleton(listEl) {
    var rows = "";
    for (var i = 0; i < 3; i++) {
      rows +=
        '<li class="nv-changelog-item">' +
        '<div class="nv-changelog-item__date">' +
        '<span class="skeleton skeleton-text-sm" style="width:90px;display:inline-block"></span></div>' +
        '<div class="nv-changelog-item__title">' +
        '<span class="skeleton skeleton-text" style="width:70%;display:inline-block"></span></div>' +
        '<div class="nv-changelog-item__body">' +
        '<span class="skeleton skeleton-text-sm" style="width:55%;display:inline-block"></span></div>' +
        "</li>";
    }
    listEl.innerHTML = rows;
  }

  function renderError(listEl, message) {
    listEl.innerHTML =
      '<li class="nv-changelog-item">' +
      '<div class="nv-changelog-item__title">' + escapeHtml(message) + "</div>" +
      '<div class="nv-changelog-item__body">' +
      '<button type="button" data-changelog-retry style="background:none;border:none;color:var(--text-secondary);text-decoration:underline;cursor:pointer;padding:0;font:inherit">' +
      "Retry</button></div></li>";
  }

  function renderEntries(listEl, entries) {
    if (!entries || !entries.length) {
      listEl.innerHTML =
        '<li class="nv-changelog-item">' +
        '<div class="nv-changelog-item__body">No updates yet.</div></li>';
      return;
    }
    var html = entries.map(function (e) {
      var unseen = e && e.seen === false ? " nv-changelog-item--unseen" : "";
      var date = formatDate(e.date) || escapeHtml(e.version || "");
      var title = escapeHtml(e.title || e.version || "");
      var body = escapeHtml(e.summary || "");
      return (
        '<li class="nv-changelog-item' + unseen + '" data-changelog-key="' + escapeHtml(e.key || "") + '">' +
        '<div class="nv-changelog-item__date">' + escapeHtml(date) + "</div>" +
        '<div class="nv-changelog-item__title">' + title + "</div>" +
        (body
          ? '<div class="nv-changelog-item__body">' + body + "</div>"
          : "") +
        "</li>"
      );
    }).join("");
    listEl.innerHTML = html;
  }

  function updateBadge(badgeEl, count) {
    if (!badgeEl) return;
    if (count && count > 0) {
      badgeEl.textContent = String(count);
      badgeEl.hidden = false;
    } else {
      badgeEl.hidden = true;
    }
  }

  function applyCollapsed(widget, collapsed) {
    widget.dataset.collapsed = collapsed ? "true" : "false";
    var btn = $("[data-changelog-collapse]", widget);
    if (btn) {
      btn.setAttribute("aria-expanded", collapsed ? "false" : "true");
      btn.setAttribute(
        "aria-label",
        collapsed ? "Expand What's new" : "Collapse What's new"
      );
    }
  }

  function readCollapsed() {
    try { return localStorage.getItem(COLLAPSE_KEY) === "true"; }
    catch (e) { return false; }
  }

  function writeCollapsed(value) {
    try { localStorage.setItem(COLLAPSE_KEY, value ? "true" : "false"); }
    catch (e) { /* private mode / quota — ignore */ }
  }

  function markSeen(keys) {
    if (!keys || !keys.length) return Promise.resolve();
    return fetch("/api/changelog/seen", {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "x-csrf-token": getCsrf(),
      },
      body: JSON.stringify({ keys: keys }),
    }).catch(function (e) {
      // Best-effort — failure here just means the user might see the
      // dot one more time. Never surface to the UI.
      if (window.console) console.warn("changelog: mark_seen failed", e);
    });
  }

  function load(widget) {
    var listEl = $("[data-changelog-list]", widget);
    var badgeEl = $("[data-changelog-badge]", widget);
    if (!listEl) return;

    renderSkeleton(listEl);
    return fetch("/api/changelog?limit=3", {
      credentials: "same-origin",
      headers: { "Accept": "application/json" },
    })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (data) {
        var entries = (data && data.entries) || [];
        renderEntries(listEl, entries);
        updateBadge(badgeEl, data && data.unseen_count);
        widget.hidden = false;

        // Schedule the seen-update only for entries the server says
        // are still unseen. Empty list → no fetch.
        var unseenKeys = entries
          .filter(function (e) { return e && e.seen === false && e.key; })
          .map(function (e) { return e.key; });
        if (unseenKeys.length) {
          setTimeout(function () {
            markSeen(unseenKeys).then(function () {
              // Local UI: drop the dot + zero the badge. Saves a
              // re-fetch and feels instant.
              unseenKeys.forEach(function (key) {
                var li = listEl.querySelector(
                  '[data-changelog-key="' + key.replace(/"/g, '\\"') + '"]'
                );
                if (li) li.classList.remove("nv-changelog-item--unseen");
              });
              updateBadge(badgeEl, 0);
            });
          }, SEEN_DELAY_MS);
        }
      })
      .catch(function (e) {
        renderError(listEl, "Couldn't load updates.");
        widget.hidden = false;
        if (window.console) console.warn("changelog widget:", e);
      });
  }

  function bindCollapse(widget) {
    var btn = $("[data-changelog-collapse]", widget);
    if (!btn) return;
    btn.addEventListener("click", function () {
      var newState = widget.dataset.collapsed !== "true";
      applyCollapsed(widget, newState);
      writeCollapsed(newState);
    });
  }

  function bindRetry(widget) {
    widget.addEventListener("click", function (ev) {
      var btn = ev.target && ev.target.closest("[data-changelog-retry]");
      if (btn) {
        ev.preventDefault();
        load(widget);
      }
    });
  }

  function init() {
    var widget = document.querySelector("[data-changelog]");
    if (!widget) return;
    applyCollapsed(widget, readCollapsed());
    bindCollapse(widget);
    bindRetry(widget);
    load(widget);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  // Public hook for tests / re-init after locale change.
  window.narveChangelog = { reload: function () {
    var w = document.querySelector("[data-changelog]");
    if (w) load(w);
  }};
})();
