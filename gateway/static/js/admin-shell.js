/* admin-shell.js — left-rail active-state highlight + mobile drawer glue.
 *
 * Zero-dependency, <1 KB. Reads ``data-active-route`` on the shell root
 * OR matches ``location.pathname`` against each anchor's ``href`` +
 * ``data-route``, whichever comes first.
 *
 * The mobile hamburger itself is inlined in admin_shell.html so the
 * initial click lands before this script finishes loading.
 */
(function () {
  "use strict";

  function highlight() {
    var shell = document.querySelector(".admin-shell");
    if (!shell) return;

    var explicitRoute = shell.getAttribute("data-active-route") || "";
    var path = location.pathname.replace(/\/+$/, "");

    document.querySelectorAll(".admin-nav a").forEach(function (anchor) {
      var href = anchor.getAttribute("href") || "";
      var route = anchor.getAttribute("data-route") || "";

      var matches =
        (explicitRoute && explicitRoute === route) ||
        href === path ||
        (route && path.indexOf("/admin/" + route) === 0);

      if (matches) {
        anchor.setAttribute("aria-current", "page");
      } else {
        anchor.removeAttribute("aria-current");
      }
    });
  }

  function closeDrawerOnNav() {
    // After a link is clicked in the mobile drawer, close it so the new
    // page shows full-width. No animation on close — it's about to be
    // replaced by a page transition anyway.
    document.querySelectorAll(".admin-nav a").forEach(function (anchor) {
      anchor.addEventListener("click", function () {
        var rail = document.getElementById("admin-rail");
        var toggle = document.querySelector(".admin-rail-toggle");
        if (rail) rail.classList.remove("admin-rail--open");
        if (toggle) toggle.setAttribute("aria-expanded", "false");
      });
    });
  }

  function escapeToCloseDrawer() {
    // Esc collapses the mobile rail and returns focus to the toggle.
    document.addEventListener("keydown", function (event) {
      if (event.key !== "Escape") return;
      var rail = document.getElementById("admin-rail");
      if (!rail || !rail.classList.contains("admin-rail--open")) return;
      rail.classList.remove("admin-rail--open");
      var toggle = document.querySelector(".admin-rail-toggle");
      if (toggle) {
        toggle.setAttribute("aria-expanded", "false");
        toggle.focus();
      }
    });
  }

  function init() {
    highlight();
    closeDrawerOnNav();
    escapeToCloseDrawer();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
