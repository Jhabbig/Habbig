/**
 * Habbig Dashboard Switcher + Theme System (v2)
 * ──────────────────────────────────────────────
 * Two UI components, both Shadow DOM-isolated:
 *   1. Persistent top header bar (navigation, dashboard dropdown, themes, user)
 *   2. Floating bottom-left panel (dashboard list, theme picker, hub link)
 *
 * Injected by the gateway proxy into every dashboard.
 *
 * Expects:
 *   window.__hbSwitcher = {
 *     dashboards: [{ key, subdomain, display_name, accent }, ...],
 *     current: "crypto",
 *     domain: "habbig.com",
 *     username: "julian"   // optional, falls back to "User"
 *   }
 */
(function () {
  'use strict';
  var cfg = window.__hbSwitcher;
  if (!cfg || !cfg.dashboards) return;

  /* ================================================================
   *  CONSTANTS & THEME DEFINITIONS
   * ================================================================ */

  var HEADER_HEIGHT = 48;

  var THEMES = [
    { id: 'indigo',  label: 'Indigo',  a: '#6366f1', a2: '#8b5cf6' },
    { id: 'forest',  label: 'Forest',  a: '#79AE6F', a2: '#9FCB98' },
    { id: 'teal',    label: 'Teal',    a: '#408A71', a2: '#B0E4CC' },
    { id: 'crimson', label: 'Crimson', a: '#C3110C', a2: '#E6501B' },
    { id: 'ocean',   label: 'Ocean',   a: '#0ea5e9', a2: '#38bdf8' },
    { id: 'rose',    label: 'Rose',    a: '#e11d48', a2: '#fb7185' },
    { id: 'gold',    label: 'Gold',    a: '#d97706', a2: '#fbbf24' },
    { id: 'violet',  label: 'Violet',  a: '#7c3aed', a2: '#a78bfa' },
    { id: 'slate',   label: 'Slate',   a: '#64748b', a2: '#94a3b8' },
    { id: 'emerald', label: 'Emerald', a: '#059669', a2: '#34d399' }
  ];

  /* ================================================================
   *  UTILITY HELPERS
   * ================================================================ */

  function hexRgba(hex, alpha) {
    var r = parseInt(hex.slice(1, 3), 16);
    var g = parseInt(hex.slice(3, 5), 16);
    var b = parseInt(hex.slice(5, 7), 16);
    return 'rgba(' + r + ',' + g + ',' + b + ',' + alpha + ')';
  }

  var proto = location.protocol;
  var port = location.port ? ':' + location.port : '';
  var base = cfg.domain || 'localhost';
  var username = cfg.username || 'User';

  /** URL to a dashboard subdomain */
  function dashUrl(sub) {
    if (base === 'localhost') return proto + '//' + sub + '.localhost' + port + '/';
    return proto + '//' + sub + '.' + base + port + '/';
  }

  /** URL to the apex domain (gateway) for a given path */
  function apexUrl(path) {
    if (base === 'localhost') return proto + '//localhost' + port + path;
    return proto + '//' + base + port + path;
  }

  /** Find the current dashboard object */
  function currentDash() {
    for (var i = 0; i < cfg.dashboards.length; i++) {
      if (cfg.dashboards[i].key === cfg.current) return cfg.dashboards[i];
    }
    return null;
  }

  /** Detect Mac for keyboard shortcut label */
  var isMac = /Mac|iPod|iPhone|iPad/.test(navigator.platform || navigator.userAgent);

  /* ================================================================
   *  THEME SYSTEM (operates on the main document, outside Shadow DOM)
   * ================================================================ */

  var themeStyleId = 'hb-theme-vars';
  var activeThemeId = 'indigo';
  try { activeThemeId = localStorage.getItem('hb-theme') || 'indigo'; } catch (e) { /* noop */ }

  /** All registered theme dot elements (across both UIs) for updating selection state */
  var allThemeDotRefs = [];

  function applyTheme(id) {
    var t;
    for (var i = 0; i < THEMES.length; i++) {
      if (THEMES[i].id === id) { t = THEMES[i]; break; }
    }
    if (!t) t = THEMES[0];
    activeThemeId = t.id;
    try { localStorage.setItem('hb-theme', t.id); } catch (e) { /* noop */ }

    /* Inject/update <style> in the main document for CSS variable overrides */
    var el = document.getElementById(themeStyleId);
    if (!el) {
      el = document.createElement('style');
      el.id = themeStyleId;
      document.head.appendChild(el);
    }
    var dim = hexRgba(t.a, 0.12);
    var glow = hexRgba(t.a, 0.18);

    if (t.id === 'indigo') {
      el.textContent = '';
      document.documentElement.removeAttribute('data-hb-theme');
    } else {
      document.documentElement.setAttribute('data-hb-theme', t.id);
      el.textContent = [
        ':root, :root[data-hb-theme="' + t.id + '"] {',
        '  --accent: ' + t.a + ';',
        '  --accent-2: ' + t.a2 + ';',
        '  --accent-hover: ' + t.a2 + ';',
        '  --accent-light: ' + dim + ';',
        '  --accent-glow: ' + glow + ';',
        '  --brand: ' + t.a + ';',
        '  --blue: ' + t.a + ';',
        '}',
        '[data-hb-theme="' + t.id + '"] .auth-submit,',
        '[data-hb-theme="' + t.id + '"] .btn-primary,',
        '[data-hb-theme="' + t.id + '"] .cta-open,',
        '[data-hb-theme="' + t.id + '"] .landing-primary-cta,',
        '[data-hb-theme="' + t.id + '"] .landing-nav-cta,',
        '[data-hb-theme="' + t.id + '"] .enquire-cta,',
        '[data-hb-theme="' + t.id + '"] .settings-actions .btn-primary,',
        '[data-hb-theme="' + t.id + '"] .seg-btn.active,',
        '[data-hb-theme="' + t.id + '"] .btn-primary-gradient {',
        '  background: linear-gradient(135deg, ' + t.a + ', ' + t.a2 + ') !important;',
        '}',
        '[data-hb-theme="' + t.id + '"] a { --accent: ' + t.a + '; }',
        '[data-hb-theme="' + t.id + '"] input:focus,',
        '[data-hb-theme="' + t.id + '"] select:focus {',
        '  border-color: ' + t.a + ';',
        '  box-shadow: 0 0 0 3px ' + dim + ';',
        '}',
        '[data-hb-theme="' + t.id + '"] .gw-brand-logo,',
        '[data-hb-theme="' + t.id + '"] .auth-logo,',
        '[data-hb-theme="' + t.id + '"] .landing-pricing-num {',
        '  background: linear-gradient(135deg, ' + t.a + ', ' + t.a2 + ') !important;',
        '}'
      ].join('\n');
    }

    /* Update all theme dot highlights across header + floating panel */
    updateAllThemeDots();
    /* Update floating logo gradient */
    updateFloatingLogoColors();
  }

  function updateAllThemeDots() {
    for (var i = 0; i < allThemeDotRefs.length; i++) {
      var ref = allThemeDotRefs[i];
      if (ref.id === activeThemeId) {
        ref.el.classList.add('sel');
      } else {
        ref.el.classList.remove('sel');
      }
    }
  }

  /** Build a row of theme dot buttons and register them for syncing */
  function buildThemeDots(dotClass, dotSize) {
    var row = document.createElement('div');
    row.style.cssText = 'display:flex;gap:6px;flex-wrap:wrap;';
    for (var i = 0; i < THEMES.length; i++) {
      (function (t) {
        var btn = document.createElement('button');
        btn.className = dotClass + (t.id === activeThemeId ? ' sel' : '');
        btn.style.cssText = '--tc:' + t.a + ';width:' + dotSize + 'px;height:' + dotSize + 'px;';
        btn.setAttribute('aria-label', t.label + ' theme');
        btn.setAttribute('title', t.label);
        btn.addEventListener('click', function (e) {
          e.stopPropagation();
          applyTheme(t.id);
        });
        allThemeDotRefs.push({ el: btn, id: t.id });
        row.appendChild(btn);
      })(THEMES[i]);
    }
    return row;
  }


  /* ================================================================
   *  1. TOP HEADER BAR
   * ================================================================ */

  var headerHost = document.createElement('div');
  headerHost.id = 'hb-header-host';
  headerHost.style.cssText = 'all:initial;position:fixed;top:0;left:0;right:0;z-index:2147483600;height:' + HEADER_HEIGHT + 'px;pointer-events:auto;';
  var headerRoot = headerHost.attachShadow({ mode: 'closed' });

  /* ── Header styles ─────────────────────────────────────────── */
  var headerStyle = document.createElement('style');
  headerStyle.textContent = [
    "@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');",
    ':host { all: initial; display: block; }',
    '*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }',

    /* bar container */
    '.hb-bar {',
    '  display: flex; align-items: center; height: ' + HEADER_HEIGHT + 'px;',
    '  padding: 0 20px;',
    '  background: rgba(14,16,22,0.92);',
    '  backdrop-filter: blur(20px) saturate(180%);',
    '  -webkit-backdrop-filter: blur(20px) saturate(180%);',
    '  border-bottom: 1px solid rgba(255,255,255,0.06);',
    '  font-family: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;',
    '  font-size: 13px; color: rgba(255,255,255,0.8);',
    '  -webkit-font-smoothing: antialiased;',
    '}',

    /* logo area */
    '.hb-brand { display: flex; align-items: center; gap: 10px; text-decoration: none; color: inherit; flex-shrink: 0; }',
    '.hb-brand:hover .hb-brand-text { color: rgba(255,255,255,0.95); }',
    '.hb-brand-mark {',
    '  width: 28px; height: 28px; border-radius: 8px;',
    '  background: linear-gradient(135deg, var(--ha, #6366f1), var(--hb, #8b5cf6));',
    '  display: flex; align-items: center; justify-content: center;',
    '  font-size: 14px; font-weight: 700; color: #fff; flex-shrink: 0;',
    '}',
    '.hb-brand-text {',
    '  font-size: 14px; font-weight: 600; color: rgba(255,255,255,0.75);',
    '  letter-spacing: 0.01em; transition: color 0.14s ease;',
    '}',

    /* separator */
    '.hb-sep {',
    '  width: 1px; height: 20px; background: rgba(255,255,255,0.08);',
    '  margin: 0 14px; flex-shrink: 0;',
    '}',

    /* current dashboard trigger (dropdown trigger) */
    '.hb-dash-trigger {',
    '  display: flex; align-items: center; gap: 8px; cursor: pointer;',
    '  padding: 5px 10px; border-radius: 8px; border: none; background: none;',
    '  color: rgba(255,255,255,0.8); font-family: inherit; font-size: 13px; font-weight: 500;',
    '  transition: background 0.14s ease, color 0.14s ease;',
    '  position: relative; letter-spacing: 0.01em;',
    '}',
    '.hb-dash-trigger:hover { background: rgba(255,255,255,0.06); color: #fff; }',
    '.hb-dash-dot {',
    '  width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0;',
    '  box-shadow: 0 0 6px var(--dc);',
    '}',
    '.hb-dash-chevron {',
    '  width: 12px; height: 12px; opacity: 0.4; transition: transform 0.18s ease, opacity 0.14s ease;',
    '  flex-shrink: 0;',
    '}',
    '.hb-dash-trigger:hover .hb-dash-chevron { opacity: 0.7; }',
    '.hb-dash-trigger.open .hb-dash-chevron { transform: rotate(180deg); opacity: 0.7; }',

    /* dropdown */
    '.hb-dropdown {',
    '  position: absolute; top: ' + (HEADER_HEIGHT - 4) + 'px; left: 0;',
    '  min-width: 220px; padding: 6px;',
    '  background: rgba(14,16,22,0.96);',
    '  backdrop-filter: blur(32px); -webkit-backdrop-filter: blur(32px);',
    '  border: 1px solid rgba(255,255,255,0.08); border-radius: 12px;',
    '  box-shadow: 0 8px 32px rgba(0,0,0,0.5), 0 0 0 1px rgba(255,255,255,0.03);',
    '  opacity: 0; transform: translateY(-4px); pointer-events: none;',
    '  transition: opacity 0.14s ease, transform 0.14s ease;',
    '  z-index: 10;',
    '}',
    '.hb-dropdown.open { opacity: 1; transform: translateY(0); pointer-events: auto; }',
    '.hb-dd-item {',
    '  display: flex; align-items: center; gap: 10px; padding: 8px 10px;',
    '  border-radius: 8px; text-decoration: none; color: rgba(255,255,255,0.72);',
    '  font-size: 13px; font-weight: 450; transition: background 0.14s ease, color 0.14s ease;',
    '  cursor: pointer; letter-spacing: 0.01em;',
    '}',
    '.hb-dd-item:hover { background: rgba(255,255,255,0.06); color: #fff; }',
    '.hb-dd-item.active { background: rgba(255,255,255,0.08); color: #fff; font-weight: 520; }',
    '.hb-dd-dot {',
    '  width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0;',
    '  box-shadow: 0 0 6px var(--dc);',
    '}',
    '.hb-dd-badge { margin-left: auto; font-size: 10px; font-weight: 500; color: rgba(255,255,255,0.25); letter-spacing: 0.3px; }',

    /* spacer */
    '.hb-spacer { flex: 1 1 auto; }',

    /* nav links */
    '.hb-nav { display: flex; align-items: center; gap: 4px; flex-shrink: 0; }',
    '.hb-nav-link {',
    '  display: flex; align-items: center; gap: 6px; padding: 5px 10px;',
    '  border-radius: 7px; text-decoration: none; color: rgba(255,255,255,0.52);',
    '  font-size: 12.5px; font-weight: 500; transition: background 0.14s ease, color 0.14s ease;',
    '  letter-spacing: 0.01em; white-space: nowrap;',
    '}',
    '.hb-nav-link:hover { background: rgba(255,255,255,0.05); color: rgba(255,255,255,0.85); }',
    '.hb-nav-link svg { width: 14px; height: 14px; opacity: 0.6; flex-shrink: 0; }',
    '.hb-nav-label { /* text label, hidden on mobile */ }',

    /* header theme dots */
    '.hb-themes-row { display: flex; align-items: center; gap: 5px; flex-shrink: 0; }',
    '.hb-tdot {',
    '  width: 14px; height: 14px; border-radius: 50%; border: 2px solid transparent;',
    '  cursor: pointer; background: var(--tc); flex-shrink: 0; padding: 0;',
    '  transition: transform 0.14s ease, border-color 0.14s ease, box-shadow 0.14s ease;',
    '}',
    '.hb-tdot:hover { transform: scale(1.18); box-shadow: 0 0 6px var(--tc); }',
    '.hb-tdot.sel { border-color: rgba(255,255,255,0.65); box-shadow: 0 0 8px var(--tc); }',

    /* user pill */
    '.hb-user {',
    '  display: flex; align-items: center; gap: 6px; padding: 4px 10px;',
    '  border-radius: 20px; text-decoration: none;',
    '  background: rgba(255,255,255,0.05); color: rgba(255,255,255,0.65);',
    '  font-size: 12px; font-weight: 500; transition: background 0.14s ease, color 0.14s ease;',
    '  letter-spacing: 0.01em; flex-shrink: 0;',
    '}',
    '.hb-user:hover { background: rgba(255,255,255,0.09); color: rgba(255,255,255,0.9); }',
    '.hb-user-avatar {',
    '  width: 20px; height: 20px; border-radius: 50%;',
    '  background: linear-gradient(135deg, var(--ha, #6366f1), var(--hb, #8b5cf6));',
    '  display: flex; align-items: center; justify-content: center;',
    '  font-size: 10px; font-weight: 700; color: #fff; flex-shrink: 0;',
    '}',

    /* sign out */
    '.hb-signout {',
    '  padding: 5px 8px; border-radius: 7px; text-decoration: none;',
    '  color: rgba(255,255,255,0.32); font-size: 12px; font-weight: 500;',
    '  transition: background 0.14s ease, color 0.14s ease;',
    '  letter-spacing: 0.01em; flex-shrink: 0; white-space: nowrap;',
    '}',
    '.hb-signout:hover { background: rgba(255,255,255,0.05); color: rgba(255,255,255,0.65); }',

    /* hamburger (mobile) */
    '.hb-hamburger {',
    '  display: none; width: 32px; height: 32px; border: none; background: none;',
    '  cursor: pointer; padding: 4px; border-radius: 6px;',
    '  transition: background 0.14s ease; flex-shrink: 0;',
    '}',
    '.hb-hamburger:hover { background: rgba(255,255,255,0.06); }',
    '.hb-hamburger svg { width: 100%; height: 100%; color: rgba(255,255,255,0.6); }',

    /* mobile menu overlay */
    '.hb-mobile-menu {',
    '  display: none; position: fixed; top: ' + HEADER_HEIGHT + 'px; left: 0; right: 0;',
    '  bottom: 0; background: rgba(14,16,22,0.97);',
    '  backdrop-filter: blur(24px); -webkit-backdrop-filter: blur(24px);',
    '  padding: 16px 20px; z-index: 5;',
    '  flex-direction: column; gap: 4px;',
    '  font-family: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;',
    '  overflow-y: auto;',
    '  opacity: 0; transition: opacity 0.18s ease;',
    '}',
    '.hb-mobile-menu.open { display: flex; opacity: 1; }',
    '.hb-mob-section-label {',
    '  font-size: 10px; font-weight: 600; letter-spacing: 0.6px; text-transform: uppercase;',
    '  color: rgba(255,255,255,0.3); padding: 12px 10px 6px;',
    '}',
    '.hb-mob-link {',
    '  display: flex; align-items: center; gap: 10px; padding: 11px 10px;',
    '  border-radius: 10px; text-decoration: none; color: rgba(255,255,255,0.72);',
    '  font-size: 14px; font-weight: 450; transition: background 0.14s ease;',
    '}',
    '.hb-mob-link:hover { background: rgba(255,255,255,0.06); }',
    '.hb-mob-link.active { background: rgba(255,255,255,0.08); color: #fff; font-weight: 520; }',
    '.hb-mob-dot {',
    '  width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0;',
    '  box-shadow: 0 0 6px var(--dc);',
    '}',
    '.hb-mob-div { height: 1px; margin: 6px 10px; background: rgba(255,255,255,0.06); }',
    '.hb-mob-themes { display: flex; align-items: center; gap: 10px; padding: 10px 10px; }',
    '.hb-mob-themes-label {',
    '  font-size: 11px; font-weight: 600; letter-spacing: 0.4px; text-transform: uppercase;',
    '  color: rgba(255,255,255,0.3); flex-shrink: 0;',
    '}',

    /* responsive: hide desktop elements, show hamburger */
    '@media (max-width: 720px) {',
    '  .hb-nav { display: none; }',
    '  .hb-themes-row { display: none; }',
    '  .hb-user { display: none; }',
    '  .hb-signout { display: none; }',
    '  .hb-sep.desktop-only { display: none; }',
    '  .hb-hamburger { display: flex; align-items: center; justify-content: center; }',
    '  .hb-bar { padding: 0 14px; }',
    '}',
    '@media (min-width: 721px) {',
    '  .hb-mobile-menu { display: none !important; }',
    '}'
  ].join('\n');
  headerRoot.appendChild(headerStyle);

  /* ── Build header bar ──────────────────────────────────────── */
  var bar = document.createElement('div');
  bar.className = 'hb-bar';

  /* Brand logo + text => links to hub */
  var brand = document.createElement('a');
  brand.className = 'hb-brand';
  brand.href = apexUrl('/dashboards');
  var brandMark = document.createElement('span');
  brandMark.className = 'hb-brand-mark';
  brandMark.textContent = 'H';
  var brandText = document.createElement('span');
  brandText.className = 'hb-brand-text';
  brandText.textContent = 'Habbig';
  brand.appendChild(brandMark);
  brand.appendChild(brandText);
  bar.appendChild(brand);

  /* Separator */
  bar.appendChild(makeSep(''));

  /* Current dashboard dropdown trigger */
  var dashTriggerWrap = document.createElement('div');
  dashTriggerWrap.style.cssText = 'position:relative;';
  var dashTrigger = document.createElement('button');
  dashTrigger.className = 'hb-dash-trigger';
  var curDash = currentDash();
  var trigDot = document.createElement('span');
  trigDot.className = 'hb-dash-dot';
  trigDot.style.cssText = '--dc:' + (curDash ? curDash.accent : '#6366f1') + ';background:' + (curDash ? curDash.accent : '#6366f1') + ';';
  var trigName = document.createElement('span');
  trigName.textContent = curDash ? curDash.display_name : 'Dashboard';
  var trigChevron = document.createElement('span');
  trigChevron.className = 'hb-dash-chevron';
  trigChevron.innerHTML = '<svg viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3 4.5L6 7.5L9 4.5"/></svg>';
  dashTrigger.appendChild(trigDot);
  dashTrigger.appendChild(trigName);
  dashTrigger.appendChild(trigChevron);
  dashTriggerWrap.appendChild(dashTrigger);

  /* Dropdown panel */
  var dropdown = document.createElement('div');
  dropdown.className = 'hb-dropdown';
  var ddOpen = false;

  for (var di = 0; di < cfg.dashboards.length; di++) {
    (function (d) {
      var item = document.createElement('a');
      item.className = 'hb-dd-item' + (d.key === cfg.current ? ' active' : '');
      item.href = dashUrl(d.subdomain);
      var dot = document.createElement('span');
      dot.className = 'hb-dd-dot';
      dot.style.cssText = '--dc:' + d.accent + ';background:' + d.accent + ';';
      var name = document.createElement('span');
      name.textContent = d.display_name;
      item.appendChild(dot);
      item.appendChild(name);
      if (d.key === cfg.current) {
        var badge = document.createElement('span');
        badge.className = 'hb-dd-badge';
        badge.textContent = 'current';
        item.appendChild(badge);
        item.addEventListener('click', function (e) { e.preventDefault(); toggleDropdown(); });
      }
      dropdown.appendChild(item);
    })(cfg.dashboards[di]);
  }
  dashTriggerWrap.appendChild(dropdown);
  bar.appendChild(dashTriggerWrap);

  function toggleDropdown() {
    ddOpen = !ddOpen;
    if (ddOpen) {
      dropdown.classList.add('open');
      dashTrigger.classList.add('open');
    } else {
      dropdown.classList.remove('open');
      dashTrigger.classList.remove('open');
    }
  }

  function closeDropdown() {
    if (!ddOpen) return;
    ddOpen = false;
    dropdown.classList.remove('open');
    dashTrigger.classList.remove('open');
  }

  dashTrigger.addEventListener('click', function (e) {
    e.stopPropagation();
    toggleDropdown();
  });

  /* Spacer */
  var spacer = document.createElement('div');
  spacer.className = 'hb-spacer';
  bar.appendChild(spacer);

  /* Nav links */
  var nav = document.createElement('div');
  nav.className = 'hb-nav';

  var navItems = [
    { label: 'Hub',      path: '/dashboards', icon: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3"><rect x="2" y="2" width="5" height="5" rx="1"/><rect x="9" y="2" width="5" height="5" rx="1"/><rect x="2" y="9" width="5" height="5" rx="1"/><rect x="9" y="9" width="5" height="5" rx="1"/></svg>' },
    { label: 'Billing',  path: '/billing',    icon: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3"><rect x="1.5" y="3" width="13" height="10" rx="1.5"/><path d="M1.5 6.5h13"/></svg>' },
    { label: 'Settings', path: '/settings',   icon: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3"><circle cx="8" cy="8" r="2.5"/><path d="M8 1.5v2M8 12.5v2M1.5 8h2M12.5 8h2M3.4 3.4l1.4 1.4M11.2 11.2l1.4 1.4M3.4 12.6l1.4-1.4M11.2 4.8l1.4-1.4"/></svg>' }
  ];

  for (var ni = 0; ni < navItems.length; ni++) {
    (function (item) {
      var a = document.createElement('a');
      a.className = 'hb-nav-link';
      a.href = apexUrl(item.path);
      a.innerHTML = item.icon;
      var lbl = document.createElement('span');
      lbl.className = 'hb-nav-label';
      lbl.textContent = item.label;
      a.appendChild(lbl);
      nav.appendChild(a);
    })(navItems[ni]);
  }
  bar.appendChild(nav);

  /* Separator */
  bar.appendChild(makeSep('desktop-only'));

  /* Theme dots (small, in header) */
  var headerThemesRow = document.createElement('div');
  headerThemesRow.className = 'hb-themes-row';
  var headerDotsContainer = buildThemeDots('hb-tdot', 14);
  /* Move children from the container into the themed row */
  while (headerDotsContainer.firstChild) {
    headerThemesRow.appendChild(headerDotsContainer.firstChild);
  }
  bar.appendChild(headerThemesRow);

  /* Separator */
  bar.appendChild(makeSep('desktop-only'));

  /* User pill */
  var userPill = document.createElement('a');
  userPill.className = 'hb-user';
  userPill.href = apexUrl('/profile');
  var userAvatar = document.createElement('span');
  userAvatar.className = 'hb-user-avatar';
  userAvatar.textContent = username.charAt(0).toUpperCase();
  var userLabel = document.createElement('span');
  userLabel.textContent = username;
  userPill.appendChild(userAvatar);
  userPill.appendChild(userLabel);
  bar.appendChild(userPill);

  /* Sign out */
  var signout = document.createElement('a');
  signout.className = 'hb-signout';
  signout.href = apexUrl('/logout');
  signout.textContent = 'Sign out';
  bar.appendChild(signout);

  /* Hamburger (mobile) */
  var hamburger = document.createElement('button');
  hamburger.className = 'hb-hamburger';
  hamburger.setAttribute('aria-label', 'Menu');
  hamburger.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 7h16M4 12h16M4 17h16"/></svg>';
  bar.appendChild(hamburger);

  headerRoot.appendChild(bar);

  /* ── Mobile menu ───────────────────────────────────────────── */
  var mobileMenu = document.createElement('div');
  mobileMenu.className = 'hb-mobile-menu';
  var mobileOpen = false;

  /* Dashboards section */
  var mobSectionDash = document.createElement('div');
  mobSectionDash.className = 'hb-mob-section-label';
  mobSectionDash.textContent = 'Dashboards';
  mobileMenu.appendChild(mobSectionDash);

  for (var mi = 0; mi < cfg.dashboards.length; mi++) {
    (function (d) {
      var a = document.createElement('a');
      a.className = 'hb-mob-link' + (d.key === cfg.current ? ' active' : '');
      a.href = dashUrl(d.subdomain);
      var dot = document.createElement('span');
      dot.className = 'hb-mob-dot';
      dot.style.cssText = '--dc:' + d.accent + ';background:' + d.accent + ';';
      var name = document.createElement('span');
      name.textContent = d.display_name;
      a.appendChild(dot);
      a.appendChild(name);
      mobileMenu.appendChild(a);
    })(cfg.dashboards[mi]);
  }

  /* Divider */
  var mobDiv1 = document.createElement('div');
  mobDiv1.className = 'hb-mob-div';
  mobileMenu.appendChild(mobDiv1);

  /* Nav links section */
  var mobSectionNav = document.createElement('div');
  mobSectionNav.className = 'hb-mob-section-label';
  mobSectionNav.textContent = 'Navigation';
  mobileMenu.appendChild(mobSectionNav);

  var mobNavItems = [
    { label: 'Hub', path: '/dashboards' },
    { label: 'Billing', path: '/billing' },
    { label: 'Settings', path: '/settings' },
    { label: 'Profile', path: '/profile' }
  ];
  for (var mni = 0; mni < mobNavItems.length; mni++) {
    (function (item) {
      var a = document.createElement('a');
      a.className = 'hb-mob-link';
      a.href = apexUrl(item.path);
      var name = document.createElement('span');
      name.textContent = item.label;
      a.appendChild(name);
      mobileMenu.appendChild(a);
    })(mobNavItems[mni]);
  }

  /* Divider */
  var mobDiv2 = document.createElement('div');
  mobDiv2.className = 'hb-mob-div';
  mobileMenu.appendChild(mobDiv2);

  /* Theme section */
  var mobThemes = document.createElement('div');
  mobThemes.className = 'hb-mob-themes';
  var mobThemesLabel = document.createElement('span');
  mobThemesLabel.className = 'hb-mob-themes-label';
  mobThemesLabel.textContent = 'Theme';
  mobThemes.appendChild(mobThemesLabel);
  mobThemes.appendChild(buildThemeDots('hb-tdot', 18));
  mobileMenu.appendChild(mobThemes);

  /* Divider */
  var mobDiv3 = document.createElement('div');
  mobDiv3.className = 'hb-mob-div';
  mobileMenu.appendChild(mobDiv3);

  /* Sign out (mobile) */
  var mobSignout = document.createElement('a');
  mobSignout.className = 'hb-mob-link';
  mobSignout.href = apexUrl('/logout');
  mobSignout.style.color = 'rgba(255,255,255,0.4)';
  var mobSignoutText = document.createElement('span');
  mobSignoutText.textContent = 'Sign out';
  mobSignout.appendChild(mobSignoutText);
  mobileMenu.appendChild(mobSignout);

  headerRoot.appendChild(mobileMenu);

  /* Hamburger toggle */
  hamburger.addEventListener('click', function (e) {
    e.stopPropagation();
    mobileOpen = !mobileOpen;
    if (mobileOpen) {
      mobileMenu.classList.add('open');
      /* Animate in after display:flex is applied */
      requestAnimationFrame(function () {
        mobileMenu.style.opacity = '1';
      });
      hamburger.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 6l12 12M6 18L18 6"/></svg>';
    } else {
      closeMobileMenu();
    }
  });

  function closeMobileMenu() {
    mobileOpen = false;
    mobileMenu.style.opacity = '0';
    mobileMenu.classList.remove('open');
    hamburger.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 7h16M4 12h16M4 17h16"/></svg>';
  }

  /** Helper to create separator elements */
  function makeSep(extraClass) {
    var sep = document.createElement('div');
    sep.className = 'hb-sep' + (extraClass ? ' ' + extraClass : '');
    return sep;
  }


  /* ================================================================
   *  2. FLOATING PANEL (bottom-left logo + popup)
   * ================================================================ */

  var floatHost = document.createElement('div');
  floatHost.id = 'hb-float-host';
  floatHost.style.cssText = 'all:initial;position:fixed;z-index:2147483647;bottom:0;left:0;width:0;height:0;pointer-events:none;';
  var floatRoot = floatHost.attachShadow({ mode: 'closed' });

  /* ── Floating panel styles ─────────────────────────────────── */
  var floatStyle = document.createElement('style');
  floatStyle.textContent = [
    "@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');",
    ':host { all: initial; }',
    '*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }',

    /* logo trigger */
    '.hb-logo {',
    '  pointer-events: auto; cursor: pointer; position: fixed; bottom: 24px; left: 24px;',
    '  width: 44px; height: 44px; border-radius: 14px; border: none;',
    '  background: linear-gradient(135deg, var(--logo-a, #6366f1), var(--logo-b, #8b5cf6));',
    '  display: flex; align-items: center; justify-content: center;',
    '  font-family: "Inter", -apple-system, sans-serif; font-size: 18px; font-weight: 700;',
    '  color: #fff;',
    '  transition: transform 0.18s ease, box-shadow 0.18s ease;',
    '  box-shadow: 0 2px 12px rgba(0,0,0,0.35);',
    '}',
    '.hb-logo:hover { transform: scale(1.08); box-shadow: 0 4px 20px var(--logo-glow, rgba(99,102,241,0.4)); }',

    /* panel */
    '.sw-panel {',
    '  pointer-events: auto; position: fixed; bottom: 78px; left: 24px; width: 272px;',
    '  background: rgba(14,16,22,0.92); backdrop-filter: blur(32px); -webkit-backdrop-filter: blur(32px);',
    '  border: 1px solid rgba(255,255,255,0.07); border-radius: 16px;',
    '  box-shadow: 0 8px 40px rgba(0,0,0,0.55), 0 0 0 1px rgba(255,255,255,0.03);',
    '  font-family: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;',
    '  overflow: hidden; transform-origin: bottom left;',
    '  transition: opacity 0.18s ease, transform 0.18s ease;',
    '}',
    '.sw-panel[aria-hidden="true"] { opacity: 0; transform: translateY(8px) scale(0.97); pointer-events: none; }',
    '.sw-panel[aria-hidden="false"] { opacity: 1; transform: translateY(0) scale(1); }',

    '.sw-head { padding: 14px 16px 8px; display: flex; align-items: center; justify-content: space-between; }',
    '.sw-title { font-size: 11px; font-weight: 600; letter-spacing: 0.6px; text-transform: uppercase; color: rgba(255,255,255,0.38); }',
    '.sw-kbd {',
    '  font-size: 10px; font-weight: 500; color: rgba(255,255,255,0.22);',
    '  border: 1px solid rgba(255,255,255,0.08); border-radius: 5px; padding: 2px 6px;',
    '  font-family: "Inter", sans-serif; letter-spacing: 0.3px;',
    '}',

    '.sw-list { padding: 4px 8px 10px; display: flex; flex-direction: column; gap: 2px; }',

    '.sw-item {',
    '  display: flex; align-items: center; gap: 10px; padding: 9px 10px; border-radius: 10px;',
    '  text-decoration: none; color: rgba(255,255,255,0.72); font-size: 13px; font-weight: 450;',
    '  transition: background 0.14s ease, color 0.14s ease; cursor: pointer; letter-spacing: 0.01em;',
    '}',
    '.sw-item:hover { background: rgba(255,255,255,0.06); color: rgba(255,255,255,0.95); }',
    '.sw-item.active { background: rgba(255,255,255,0.08); color: #fff; font-weight: 520; }',

    '.sw-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; box-shadow: 0 0 6px var(--c); }',
    '.sw-badge { margin-left: auto; font-size: 10px; font-weight: 500; color: rgba(255,255,255,0.25); letter-spacing: 0.3px; }',
    '.sw-div { height: 1px; margin: 4px 12px; background: rgba(255,255,255,0.06); }',

    /* theme picker in floating panel */
    '.sw-themes { padding: 6px 16px 10px; display: flex; align-items: center; gap: 10px; }',
    '.sw-themes-label { font-size: 10px; font-weight: 600; letter-spacing: 0.5px; text-transform: uppercase; color: rgba(255,255,255,0.28); flex-shrink: 0; }',
    '.sw-tdot {',
    '  width: 18px; height: 18px; border-radius: 50%; border: 2px solid transparent; cursor: pointer;',
    '  transition: transform 0.14s ease, border-color 0.14s ease, box-shadow 0.14s ease;',
    '  background: var(--tc); flex-shrink: 0; padding: 0;',
    '}',
    '.sw-tdot:hover { transform: scale(1.15); box-shadow: 0 0 8px var(--tc); }',
    '.sw-tdot.sel { border-color: rgba(255,255,255,0.7); box-shadow: 0 0 10px var(--tc); }',

    /* footer hub link */
    '.sw-hub {',
    '  display: flex; align-items: center; gap: 8px; padding: 8px 16px 12px;',
    '  text-decoration: none; color: rgba(255,255,255,0.36); font-size: 11px; font-weight: 500;',
    '  transition: color 0.14s ease; letter-spacing: 0.2px;',
    '}',
    '.sw-hub:hover { color: rgba(255,255,255,0.6); }',
    '.sw-hub svg { width: 14px; height: 14px; }',

    '@media (max-width: 500px) {',
    '  .hb-logo { bottom: 16px; left: 16px; width: 40px; height: 40px; border-radius: 12px; font-size: 16px; }',
    '  .sw-panel { left: 12px; right: 12px; bottom: 68px; width: auto; }',
    '}'
  ].join('\n');
  floatRoot.appendChild(floatStyle);

  /* ── Build floating UI ─────────────────────────────────────── */
  var panelOpen = false;

  /* Logo trigger */
  var floatLogo = document.createElement('button');
  floatLogo.className = 'hb-logo';
  floatLogo.setAttribute('aria-label', 'Switch dashboard');
  floatLogo.textContent = 'H';

  /* Panel */
  var floatPanel = document.createElement('div');
  floatPanel.className = 'sw-panel';
  floatPanel.setAttribute('aria-hidden', 'true');

  /* Panel header */
  var panelHead = document.createElement('div');
  panelHead.className = 'sw-head';
  var panelTitle = document.createElement('span');
  panelTitle.className = 'sw-title';
  panelTitle.textContent = 'Dashboards';
  var panelKbd = document.createElement('span');
  panelKbd.className = 'sw-kbd';
  panelKbd.textContent = isMac ? '\u2318 J' : 'Ctrl J';
  panelHead.appendChild(panelTitle);
  panelHead.appendChild(panelKbd);
  floatPanel.appendChild(panelHead);

  /* Dashboard list */
  var panelList = document.createElement('div');
  panelList.className = 'sw-list';
  for (var pi = 0; pi < cfg.dashboards.length; pi++) {
    (function (d) {
      var a = document.createElement('a');
      a.className = 'sw-item' + (d.key === cfg.current ? ' active' : '');
      a.href = dashUrl(d.subdomain);
      var dot = document.createElement('span');
      dot.className = 'sw-dot';
      dot.style.cssText = '--c:' + d.accent + ';background:' + d.accent + ';';
      var name = document.createElement('span');
      name.textContent = d.display_name;
      a.appendChild(dot);
      a.appendChild(name);
      if (d.key === cfg.current) {
        var badge = document.createElement('span');
        badge.className = 'sw-badge';
        badge.textContent = 'here';
        a.appendChild(badge);
        a.addEventListener('click', function (e) { e.preventDefault(); togglePanel(); });
      }
      panelList.appendChild(a);
    })(cfg.dashboards[pi]);
  }
  floatPanel.appendChild(panelList);

  /* Divider */
  floatPanel.appendChild(makeFloatDiv());

  /* Theme picker */
  var panelThemes = document.createElement('div');
  panelThemes.className = 'sw-themes';
  var panelThemesLabel = document.createElement('span');
  panelThemesLabel.className = 'sw-themes-label';
  panelThemesLabel.textContent = 'Theme';
  panelThemes.appendChild(panelThemesLabel);
  panelThemes.appendChild(buildThemeDots('sw-tdot', 18));
  floatPanel.appendChild(panelThemes);

  /* Divider */
  floatPanel.appendChild(makeFloatDiv());

  /* Hub link */
  var panelHub = document.createElement('a');
  panelHub.className = 'sw-hub';
  panelHub.href = apexUrl('/dashboards');
  panelHub.innerHTML = '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="10" cy="10" r="7"/><path d="M10 6v4l2.5 1.5"/></svg>';
  var panelHubText = document.createElement('span');
  panelHubText.textContent = 'All dashboards';
  panelHub.appendChild(panelHubText);
  floatPanel.appendChild(panelHub);

  floatRoot.appendChild(floatLogo);
  floatRoot.appendChild(floatPanel);

  function makeFloatDiv() {
    var d = document.createElement('div');
    d.className = 'sw-div';
    return d;
  }

  /* ── Floating logo color sync ──────────────────────────────── */
  function updateFloatingLogoColors() {
    var t;
    for (var i = 0; i < THEMES.length; i++) {
      if (THEMES[i].id === activeThemeId) { t = THEMES[i]; break; }
    }
    if (!t) t = THEMES[0];
    floatLogo.style.setProperty('--logo-a', t.a);
    floatLogo.style.setProperty('--logo-b', t.a2);
    floatLogo.style.setProperty('--logo-glow', hexRgba(t.a, 0.4));
    /* Also update header brand logo gradient */
    brandMark.style.setProperty('--ha', t.a);
    brandMark.style.setProperty('--hb', t.a2);
    userAvatar.style.setProperty('--ha', t.a);
    userAvatar.style.setProperty('--hb', t.a2);
  }

  /* ── Floating panel toggle ─────────────────────────────────── */
  function togglePanel() {
    panelOpen = !panelOpen;
    floatPanel.setAttribute('aria-hidden', panelOpen ? 'false' : 'true');
  }

  function closePanel() {
    if (!panelOpen) return;
    panelOpen = false;
    floatPanel.setAttribute('aria-hidden', 'true');
  }

  floatLogo.addEventListener('click', function (e) { e.stopPropagation(); togglePanel(); });
  floatPanel.addEventListener('click', function (e) { e.stopPropagation(); });


  /* ================================================================
   *  GLOBAL EVENT HANDLERS
   * ================================================================ */

  /* Click outside => close dropdown, floating panel, and mobile menu */
  document.addEventListener('click', function () {
    closePanel();
    closeDropdown();
    if (mobileOpen) closeMobileMenu();
  });

  /* Prevent clicks inside header/floating from closing everything */
  headerHost.addEventListener('click', function (e) {
    /* Only close floating panel if clicking in header area */
    closePanel();
  });

  /* Stop dropdown clicks from closing the dropdown */
  dropdown.addEventListener('click', function (e) { e.stopPropagation(); });

  /* Keyboard shortcuts */
  document.addEventListener('keydown', function (e) {
    /* Ctrl/Cmd + J => toggle floating panel */
    if ((e.metaKey || e.ctrlKey) && e.key === 'j') {
      e.preventDefault();
      togglePanel();
    }
    /* Escape => close everything */
    if (e.key === 'Escape') {
      closePanel();
      closeDropdown();
      if (mobileOpen) closeMobileMenu();
    }
  });


  /* ================================================================
   *  MOUNT & INITIALIZE
   * ================================================================ */

  /* Apply saved theme */
  applyTheme(activeThemeId);
  updateFloatingLogoColors();

  /* Push content down to make room for the header bar */
  function pushBodyDown() {
    var current = parseInt(document.body.style.paddingTop, 10) || 0;
    /* Only add padding if we haven't already (avoid double-add on re-init) */
    if (!document.body.getAttribute('data-hb-header-offset')) {
      document.body.style.paddingTop = (current + HEADER_HEIGHT) + 'px';
      document.body.setAttribute('data-hb-header-offset', String(HEADER_HEIGHT));
    }
  }

  /* Mount both Shadow DOM hosts */
  document.body.appendChild(headerHost);
  document.body.appendChild(floatHost);
  pushBodyDown();

})();
