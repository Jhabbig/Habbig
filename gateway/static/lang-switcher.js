/* Language switcher client — reads window.LANG (set by render_page) and
 * POSTs /api/set-language when the user picks a new one. Full-page
 * reload on success so server-rendered templates pick up the fresh
 * locale immediately.
 *
 * The widget is injected by render_page when window.SUPPORTED_LANGS is
 * populated; this script just wires up the behaviour.
 */
(function () {
  "use strict";

  var FLAGS = {
    "en":    "🇺🇸",
    "es":    "🇪🇸",
    "de":    "🇩🇪",
    "pt-br": "🇧🇷",
  };
  var NAMES = {
    "en":    "English",
    "es":    "Español",
    "de":    "Deutsch",
    "pt-br": "Português (BR)",
  };

  function getCsrf() {
    var m = document.cookie.match(/(?:^|;\s*)_csrf=([^;]*)/);
    return m ? decodeURIComponent(m[1]) : "";
  }

  function switchLang(lang) {
    if (!lang) return Promise.resolve();
    return fetch("/api/set-language", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "x-csrf-token": getCsrf(),
      },
      credentials: "same-origin",
      body: JSON.stringify({lang: lang}),
    }).then(function (r) {
      if (!r.ok) throw new Error("set-language failed: " + r.status);
      return r.json();
    });
  }

  function render(container, current, supported) {
    // Skip rendering if only one language supported (single-locale install).
    if (!supported || supported.length < 2) {
      container.style.display = "none";
      return;
    }

    var flag = FLAGS[current] || FLAGS.en;
    var name = NAMES[current] || NAMES.en;

    var menuItems = supported.map(function (lang) {
      var selected = lang === current;
      return '<li><button type="button" data-lang="' + lang + '"' +
        (selected ? ' aria-current="true"' : '') + '>' +
        '<span class="lang-switcher__flag">' + (FLAGS[lang] || "🌐") + '</span>' +
        '<span>' + (NAMES[lang] || lang) + '</span>' +
        '</button></li>';
    }).join("");

    container.innerHTML =
      '<button type="button" class="lang-switcher"' +
        ' aria-haspopup="listbox" aria-expanded="false"' +
        ' title="Change language">' +
        '<span class="lang-switcher__flag">' + flag + '</span>' +
        '<span class="lang-switcher__name">' + name + '</span>' +
        '<svg class="lang-switcher__chevron" viewBox="0 0 12 12" aria-hidden="true">' +
          '<path d="M2.5 4.5 L6 8 L9.5 4.5"/>' +
        '</svg>' +
      '</button>' +
      '<ul class="lang-switcher__menu" role="listbox">' + menuItems + '</ul>';

    var trigger = container.querySelector(".lang-switcher");
    var menu = container.querySelector(".lang-switcher__menu");

    // Menu option buttons — referenced by the keyboard handlers below so
    // up/down/home/end can walk the list without re-querying each tick.
    var options = Array.prototype.slice.call(
      menu.querySelectorAll("button[data-lang]")
    );

    function setOpen(open) {
      menu.dataset.open = open ? "true" : "false";
      trigger.setAttribute("aria-expanded", open ? "true" : "false");
      if (open) {
        // Focus the current selection if we have one, otherwise the first.
        var target =
          options.filter(function (o) { return o.getAttribute("aria-current") === "true"; })[0] ||
          options[0];
        if (target) target.focus();
      } else {
        // Return focus to the trigger so screen readers announce the close.
        trigger.focus();
      }
    }

    function focusOption(idx) {
      if (!options.length) return;
      var clamped = ((idx % options.length) + options.length) % options.length;
      options[clamped].focus();
    }

    function currentOptionIndex() {
      return options.indexOf(document.activeElement);
    }

    trigger.addEventListener("click", function (e) {
      e.stopPropagation();
      setOpen(menu.dataset.open !== "true");
    });

    // Trigger-level keys: Enter / Space / ArrowDown / ArrowUp open the menu;
    // Escape closes it.
    trigger.addEventListener("keydown", function (e) {
      if (e.key === "Escape") { setOpen(false); return; }
      if (e.key === "Enter" || e.key === " " || e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        setOpen(true);
      }
    });

    // Menu-level keys: Arrow walk, Home/End jump, Enter/Space select, Escape
    // close, Tab closes and lets focus move out of the widget.
    menu.addEventListener("keydown", function (e) {
      var i = currentOptionIndex();
      if (e.key === "Escape") { e.preventDefault(); setOpen(false); return; }
      if (e.key === "Tab") { setOpen(false); return; }  // don't preventDefault — Tab should move on
      if (e.key === "ArrowDown") { e.preventDefault(); focusOption(i + 1); return; }
      if (e.key === "ArrowUp")   { e.preventDefault(); focusOption(i - 1); return; }
      if (e.key === "Home")      { e.preventDefault(); focusOption(0); return; }
      if (e.key === "End")       { e.preventDefault(); focusOption(options.length - 1); return; }
      if (e.key === "Enter" || e.key === " ") {
        if (document.activeElement && document.activeElement.matches("button[data-lang]")) {
          e.preventDefault();
          document.activeElement.click();
        }
      }
    });

    menu.addEventListener("click", function (e) {
      var btn = e.target.closest("button[data-lang]");
      if (!btn) return;
      e.preventDefault();
      var lang = btn.dataset.lang;
      if (lang === current) { setOpen(false); return; }
      btn.disabled = true;
      btn.setAttribute("aria-busy", "true");
      btn.textContent = "…";
      switchLang(lang).then(function () {
        // Persist the new lang into the URL so server-side detection
        // picks it up on the next request even if the cookie hasn't
        // propagated yet. `?lang=` also makes the change survivable
        // across a share-link copy.
        var url = new URL(location.href);
        url.searchParams.set("lang", lang);
        location.href = url.toString();
      }).catch(function (err) {
        console.warn("lang-switcher:", err);
        btn.disabled = false;
        btn.removeAttribute("aria-busy");
      });
    });

    // Click-outside to close.
    document.addEventListener("click", function (e) {
      if (!container.contains(e.target)) setOpen(false);
    });
  }

  function init() {
    var mount = document.getElementById("lang-switcher-mount");
    if (!mount) return;
    var current = (window.LANG || "en").toLowerCase();
    var supported = window.SUPPORTED_LANGS || ["en"];
    render(mount, current, supported);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
