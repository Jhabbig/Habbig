/**
 * density.js — Comfortable / Compact toggle.
 *
 * Mirrors theme.js's persistence model so the two settings live the
 * same way: localStorage for the per-browser default + a domain cookie
 * (.narve.ai) so subdomains stay in sync. The anti-FOUC inline script
 * in <head> already applies ``data-density`` before this file loads,
 * so we only handle interaction + cross-tab sync here.
 *
 * Wire-up: any element with ``data-density-value="comfortable"`` or
 * ``data-density-value="compact"`` becomes a toggle button. The
 * settings page renders a radiogroup of two such buttons; nothing else
 * needs to know about this script.
 */
(function () {
  'use strict';

  var STORAGE_KEY = 'nv-density';
  var COOKIE_KEY = 'nv-density';
  var DEFAULT = 'comfortable';
  var VALID = { comfortable: true, compact: true };

  var root = document.documentElement;

  // ── Storage helpers ──────────────────────────────────────────

  function readCookie(name) {
    var m = document.cookie.match(new RegExp('(?:^|; )' + name + '=([^;]*)'));
    return m ? decodeURIComponent(m[1]) : null;
  }

  function writeCookie(name, value) {
    try {
      var host = location.hostname;
      var domain = '';
      if (host.indexOf('narve.ai') !== -1) domain = '; Domain=.narve.ai';
      else if (host.indexOf('habbig.com') !== -1) domain = '; Domain=.habbig.com';
      document.cookie = name + '=' + encodeURIComponent(value) +
        '; Path=/; Max-Age=31536000; SameSite=Lax' + domain;
    } catch (e) { /* cookies disabled — localStorage still works */ }
  }

  function readStored() {
    try {
      var ls = localStorage.getItem(STORAGE_KEY);
      if (ls && VALID[ls]) return ls;
    } catch (e) {}
    var ck = readCookie(COOKIE_KEY);
    if (ck && VALID[ck]) return ck;
    return null;
  }

  function writeStored(value) {
    try { localStorage.setItem(STORAGE_KEY, value); } catch (e) {}
    writeCookie(COOKIE_KEY, value);
  }

  // ── Toast (graceful fallback if narveToast isn't loaded) ─────

  function toast(message) {
    if (typeof window.narveToast === 'function') {
      window.narveToast(message);
      return;
    }
    // Tiny inline fallback — same monochrome shape narveToast renders
    // so density feedback still works on pages without components.css.
    try {
      var el = document.createElement('div');
      el.textContent = message;
      el.style.cssText = (
        'position:fixed;bottom:24px;left:50%;transform:translateX(-50%);' +
        'background:var(--interactive-bg,#0d0d0d);color:var(--interactive-text,#fff);' +
        'padding:10px 18px;border-radius:9999px;font-size:12px;' +
        'letter-spacing:0.06em;text-transform:uppercase;z-index:10001;' +
        'box-shadow:0 4px 12px rgba(0,0,0,0.18);opacity:0;' +
        'transition:opacity 0.15s'
      );
      document.body.appendChild(el);
      requestAnimationFrame(function () { el.style.opacity = '1'; });
      setTimeout(function () {
        el.style.opacity = '0';
        setTimeout(function () { el.remove(); }, 200);
      }, 1400);
    } catch (e) { /* swallow — feedback is nice-to-have */ }
  }

  // ── Public setter ───────────────────────────────────────────

  function setDensity(value, opts) {
    if (!VALID[value]) value = DEFAULT;
    root.setAttribute('data-density', value);
    writeStored(value);
    // Sync any visible toggle buttons.
    document.querySelectorAll('[data-density-value]').forEach(function (b) {
      var matches = b.dataset.densityValue === value;
      b.setAttribute('aria-checked', matches ? 'true' : 'false');
      if (b.classList.contains('nv-toggle-btn')) {
        b.classList.toggle('is-active', matches);
      }
    });
    if (!opts || opts.silent !== true) {
      toast('Density: ' + value);
    }
  }

  // Expose for other code (e.g. command palette) to call.
  window.setDensity = setDensity;

  // ── Wire up clicks ──────────────────────────────────────────

  document.addEventListener('click', function (ev) {
    var btn = ev.target.closest('[data-density-value]');
    if (!btn) return;
    var v = btn.dataset.densityValue;
    if (!VALID[v]) return;
    setDensity(v);
  });

  // Reflect the storage value across other tabs without forcing a
  // toast — the originating tab already showed feedback.
  window.addEventListener('storage', function (ev) {
    if (ev.key !== STORAGE_KEY) return;
    if (!ev.newValue || !VALID[ev.newValue]) return;
    setDensity(ev.newValue, { silent: true });
  });

  // On load, sync button state to whatever the inline init script set.
  var current = root.getAttribute('data-density') || readStored() || DEFAULT;
  setDensity(current, { silent: true });
})();
