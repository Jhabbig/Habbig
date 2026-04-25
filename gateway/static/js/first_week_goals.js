/* First-week goals widget — sidebar checklist, auto-hides after 14 days
 * or when all goals complete.
 *
 * Wire-up: a host element <div id="first-week-goals-mount"></div> sits
 * in the dashboard sidebar; this script fetches /api/first-week/goals,
 * renders into the mount, refreshes every 60 s, and listens for a
 * `narve:goal-completed` CustomEvent so other widgets can prompt an
 * immediate refresh after a user action.
 */
(function () {
  "use strict";

  var MOUNT_ID = "first-week-goals-mount";
  var POLL_MS = 60 * 1000;

  function escapeHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"})[c];
    });
  }

  function csrf() {
    var m = document.cookie.match(/(?:^|;\s*)_csrf=([^;]*)/);
    return m ? decodeURIComponent(m[1]) : "";
  }

  function render(host, data) {
    if (!data || data.hide_widget) {
      host.innerHTML = "";
      host.style.display = "none";
      return;
    }
    host.style.display = "";

    var done = data.completed_count || 0;
    var total = data.total || (data.goals || []).length;

    var items = (data.goals || []).map(function (g) {
      return (
        '<li class="nv-fwg__item' + (g.completed ? " nv-fwg__item--done" : "") + '">' +
          '<span class="nv-fwg__check" aria-hidden="true">' +
            (g.completed ? "✓" : "") +
          '</span>' +
          '<span class="nv-fwg__label">' + escapeHtml(g.label) + '</span>' +
        '</li>'
      );
    }).join("");

    host.innerHTML = (
      '<aside class="nv-fwg" data-goals aria-label="First-week goals">' +
        '<header class="nv-fwg__head">' +
          '<h3 class="nv-fwg__title">Getting started</h3>' +
          '<span class="nv-fwg__count" aria-live="polite">' +
            done + '/' + total +
          '</span>' +
          '<button class="nv-fwg__dismiss" type="button" aria-label="Dismiss this widget" title="Dismiss">×</button>' +
        '</header>' +
        '<ul class="nv-fwg__list">' + items + '</ul>' +
      '</aside>'
    );

    var dismiss = host.querySelector(".nv-fwg__dismiss");
    if (dismiss) {
      dismiss.addEventListener("click", function () {
        fetch("/api/first-week/widget/dismiss", {
          method: "POST",
          headers: {"Content-Type": "application/json", "x-csrf-token": csrf()},
          body: "{}",
          keepalive: true,
        }).then(function () {
          host.innerHTML = "";
          host.style.display = "none";
        }).catch(function () { /* silent */ });
      });
    }
  }

  function refresh() {
    var host = document.getElementById(MOUNT_ID);
    if (!host) return;
    fetch("/api/first-week/goals", {credentials: "same-origin"})
      .then(function (r) {
        if (!r.ok) return null;
        return r.json();
      })
      .then(function (data) { render(host, data); })
      .catch(function () { /* silent */ });
  }

  // Poll lightly so newly-completed goals tick on for the user without
  // forcing a hard reload. Other widgets dispatch
  // `narve:goal-completed` for immediate refresh.
  function start() {
    refresh();
    setInterval(refresh, POLL_MS);
    document.addEventListener("narve:goal-completed", function () {
      refresh();
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
