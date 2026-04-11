/**
 * Narve.ai monochrome light/dark theme toggle
 *
 * - Default: light on first visit. Return visit uses last chosen.
 * - Persists in BOTH localStorage AND a Domain=.narve.ai cookie so
 *   the state propagates across subdomains (gateway + dashboards).
 * - Keyboard shortcut: Cmd/Ctrl + Shift + L
 * - Auto-injects a floating toggle button into the body.
 *
 * The inline anti-flash <head> script sets data-theme before this
 * script loads, so we don't need to apply the attribute here — just
 * handle user interaction and icon state.
 */
(function () {
  'use strict';

  var STORAGE_KEY = 'narve-theme';
  var COOKIE_KEY  = 'narve-theme';
  // Legacy keys — read for backward-compat so pre-rebrand users don't lose
  // their saved theme on first visit after the rename. Never written to.
  var LEGACY_STORAGE_KEY = 'betyc-theme';
  var LEGACY_COOKIE_KEY  = 'betyc-theme';
  var DEFAULT     = 'light';
  var root        = document.documentElement;

  /* ── Storage helpers ──────────────────────────────────────────── */

  function readCookie(name) {
    var m = document.cookie.match(new RegExp('(?:^|; )' + name + '=([^;]*)'));
    return m ? decodeURIComponent(m[1]) : null;
  }

  function writeCookie(name, value) {
    // Cross-subdomain: Domain=.narve.ai so sports.narve.ai etc. see it too.
    // 1-year lifetime. SameSite=Lax so it survives navigation.
    try {
      var host = location.hostname;
      var domain = '';
      if (host.indexOf('narve.ai') !== -1) domain = '; Domain=.narve.ai';
      else if (host.indexOf('habbig.com') !== -1) domain = '; Domain=.habbig.com';
      document.cookie = name + '=' + encodeURIComponent(value) +
        '; Path=/; Max-Age=31536000; SameSite=Lax' + domain;
    } catch (e) { /* cookies disabled — fall back to localStorage only */ }
  }

  function readStorage() {
    try {
      return localStorage.getItem(STORAGE_KEY)
          || localStorage.getItem(LEGACY_STORAGE_KEY);
    } catch (e) { return null; }
  }

  function writeStorage(value) {
    try { localStorage.setItem(STORAGE_KEY, value); } catch (e) { /* ignored */ }
  }

  function getSavedTheme() {
    // Cookie wins if present (cross-subdomain source of truth).
    // Read both new (narve-theme) and legacy (betyc-theme) so the rename
    // is invisible to returning users.
    return readCookie(COOKIE_KEY)
        || readCookie(LEGACY_COOKIE_KEY)
        || readStorage()
        || DEFAULT;
  }

  function saveTheme(theme) {
    writeStorage(theme);
    writeCookie(COOKIE_KEY, theme);
  }

  /* ── Toggle button (auto-injected on first run) ──────────────── */

  function makeButton() {
    if (document.getElementById('theme-toggle')) return;
    var btn = document.createElement('button');
    btn.id = 'theme-toggle';
    btn.type = 'button';
    btn.setAttribute('aria-label', 'Toggle light/dark theme');
    btn.title = 'Toggle theme (Cmd+Shift+L)';
    btn.innerHTML =
      '<svg id="theme-toggle-icon" width="16" height="16" viewBox="0 0 24 24" ' +
      'fill="none" stroke="currentColor" stroke-width="2" ' +
      'stroke-linecap="round" stroke-linejoin="round">' +
      '<circle cx="12" cy="12" r="9"></circle>' +
      '<path d="M12 3 A 9 9 0 0 1 12 21 Z" fill="currentColor"></path>' +
      '</svg>';
    btn.addEventListener('click', toggle);
    document.body.appendChild(btn);
  }

  /* ── Public API ──────────────────────────────────────────────── */

  function apply(theme) {
    if (theme !== 'light' && theme !== 'dark') theme = DEFAULT;
    root.setAttribute('data-theme', theme);
    // Fire a custom event so particle canvas / charts / etc. can react.
    try {
      document.dispatchEvent(new CustomEvent('themechange', { detail: { theme: theme } }));
    } catch (e) { /* old browsers */ }
  }

  function get() {
    return root.getAttribute('data-theme') || DEFAULT;
  }

  function set(theme) {
    apply(theme);
    saveTheme(theme);
  }

  function toggle() {
    set(get() === 'light' ? 'dark' : 'light');
  }

  /* ── Init ─────────────────────────────────────────────────────── */

  function init() {
    // The inline <head> anti-flash script already applied the attribute.
    // Re-apply from the same source of truth in case nothing ran.
    if (!root.getAttribute('data-theme')) {
      apply(getSavedTheme());
    }

    makeButton();

    // Cross-tab sync via storage events. Accept updates from either the
    // new or legacy key so an older tab can still broadcast to a newer one.
    window.addEventListener('storage', function (e) {
      if ((e.key === STORAGE_KEY || e.key === LEGACY_STORAGE_KEY) && e.newValue) {
        apply(e.newValue);
      }
    });

    // Keyboard shortcut: Cmd/Ctrl + Shift + L
    document.addEventListener('keydown', function (e) {
      if ((e.metaKey || e.ctrlKey) && e.shiftKey &&
          (e.key === 'L' || e.key === 'l')) {
        e.preventDefault();
        toggle();
      }
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  // Expose a tiny public API on window.narve (canonical) and keep
  // window.betyc as an alias so any in-flight code still works.
  window.narve = window.narve || {};
  window.narve.theme = { get: get, set: set, toggle: toggle };
  window.betyc = window.narve;
})();
