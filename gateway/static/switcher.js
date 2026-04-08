/**
 * betyc Dashboard Switcher + Theme System (v3 — Tab Bar)
 * ──────────────────────────────────────────────────────
 * Single UI component (Shadow DOM-isolated):
 *   Persistent top header bar with dashboard tabs, nav links, themes, user pill.
 *
 * Injected by the gateway proxy into every dashboard.
 *
 * Expects:
 *   window.__hbSwitcher = {
 *     dashboards: [{ key, subdomain, display_name, accent }, ...],
 *     current: "crypto",
 *     domain: "narve.ai",
 *     username: "julian"
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

  function dashUrl(sub) {
    if (base === 'localhost') return proto + '//' + sub + '.localhost' + port + '/';
    return proto + '//' + sub + '.' + base + port + '/';
  }

  function apexUrl(path) {
    if (base === 'localhost') return proto + '//localhost' + port + path;
    return proto + '//' + base + port + path;
  }

  function currentDash() {
    for (var i = 0; i < cfg.dashboards.length; i++) {
      if (cfg.dashboards[i].key === cfg.current) return cfg.dashboards[i];
    }
    return null;
  }

  /* ================================================================
   *  THEME SYSTEM (operates on the main document, outside Shadow DOM)
   * ================================================================ */

  var themeStyleId = 'hb-theme-vars';
  var activeThemeId = 'indigo';
  try { activeThemeId = localStorage.getItem('hb-theme') || 'indigo'; } catch (e) {}

  var allThemeDotRefs = [];

  function applyTheme(id) {
    var t;
    for (var i = 0; i < THEMES.length; i++) {
      if (THEMES[i].id === id) { t = THEMES[i]; break; }
    }
    if (!t) t = THEMES[0];
    activeThemeId = t.id;
    try { localStorage.setItem('hb-theme', t.id); } catch (e) {}

    var el = document.getElementById(themeStyleId);
    if (!el) { el = document.createElement('style'); el.id = themeStyleId; document.head.appendChild(el); }
    var dim = hexRgba(t.a, 0.12);
    var glow = hexRgba(t.a, 0.18);

    if (t.id === 'indigo') {
      el.textContent = '';
      document.documentElement.removeAttribute('data-hb-theme');
    } else {
      document.documentElement.setAttribute('data-hb-theme', t.id);
      el.textContent = [
        ':root, :root[data-hb-theme="' + t.id + '"] {',
        '  --accent: ' + t.a + '; --accent-2: ' + t.a2 + '; --accent-hover: ' + t.a2 + ';',
        '  --accent-light: ' + dim + '; --accent-glow: ' + glow + ';',
        '  --brand: ' + t.a + '; --blue: ' + t.a + ';',
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
        '  border-color: ' + t.a + '; box-shadow: 0 0 0 3px ' + dim + ';',
        '}',
        '[data-hb-theme="' + t.id + '"] .gw-brand-logo,',
        '[data-hb-theme="' + t.id + '"] .auth-logo,',
        '[data-hb-theme="' + t.id + '"] .landing-pricing-num {',
        '  background: linear-gradient(135deg, ' + t.a + ', ' + t.a2 + ') !important;',
        '}'
      ].join('\n');
    }
    updateAllThemeDots();
    updateBrandColors();
  }

  function updateAllThemeDots() {
    for (var i = 0; i < allThemeDotRefs.length; i++) {
      var ref = allThemeDotRefs[i];
      ref.el.classList[ref.id === activeThemeId ? 'add' : 'remove']('sel');
    }
  }

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
        btn.addEventListener('click', function (e) { e.stopPropagation(); applyTheme(t.id); });
        allThemeDotRefs.push({ el: btn, id: t.id });
        row.appendChild(btn);
      })(THEMES[i]);
    }
    return row;
  }


  /* ================================================================
   *  TOP HEADER BAR (Shadow DOM)
   * ================================================================ */

  var headerHost = document.createElement('div');
  headerHost.id = 'hb-header-host';
  headerHost.style.cssText = 'all:initial;position:fixed;top:0;left:0;right:0;z-index:2147483600;height:' + HEADER_HEIGHT + 'px;pointer-events:auto;';
  var headerRoot = headerHost.attachShadow({ mode: 'closed' });

  var headerStyle = document.createElement('style');
  headerStyle.textContent = [
    "@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');",
    ':host { all: initial; display: block; }',
    '*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }',

    '.hb-bar {',
    '  display: flex; align-items: center; height: ' + HEADER_HEIGHT + 'px; padding: 0 20px;',
    '  background: rgba(14,16,22,0.92);',
    '  backdrop-filter: blur(20px) saturate(180%); -webkit-backdrop-filter: blur(20px) saturate(180%);',
    '  border-bottom: 1px solid rgba(255,255,255,0.06);',
    '  font-family: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;',
    '  font-size: 13px; color: rgba(255,255,255,0.8); -webkit-font-smoothing: antialiased;',
    '}',

    '.hb-brand { display: flex; align-items: center; gap: 10px; text-decoration: none; color: inherit; flex-shrink: 0; }',
    '.hb-brand:hover .hb-brand-text { color: rgba(255,255,255,0.95); }',
    '.hb-brand-mark { width: 28px; height: 28px; border-radius: 8px; background: linear-gradient(135deg, var(--ha, #6366f1), var(--hb, #8b5cf6)); display: flex; align-items: center; justify-content: center; font-size: 14px; font-weight: 700; color: #fff; flex-shrink: 0; }',
    '.hb-brand-text { font-size: 14px; font-weight: 600; color: rgba(255,255,255,0.75); letter-spacing: 0.01em; transition: color 0.14s ease; }',

    '.hb-sep { width: 1px; height: 20px; background: rgba(255,255,255,0.08); margin: 0 14px; flex-shrink: 0; }',

    /* ── Tab bar ── */
    '.hb-tabs { display: flex; align-items: center; gap: 2px; flex-shrink: 0; }',
    '.hb-tab { display: flex; align-items: center; gap: 7px; padding: 6px 12px; border-radius: 8px; text-decoration: none; color: rgba(255,255,255,0.45); font-size: 13px; font-weight: 500; transition: background 0.14s ease, color 0.14s ease; letter-spacing: 0.01em; white-space: nowrap; position: relative; cursor: pointer; }',
    '.hb-tab:hover { background: rgba(255,255,255,0.05); color: rgba(255,255,255,0.85); }',
    '.hb-tab.active { color: #fff; font-weight: 520; }',
    '.hb-tab.active::after { content: ""; position: absolute; bottom: -8px; left: 8px; right: 8px; height: 2px; border-radius: 1px; background: var(--tab-accent); }',
    '.hb-tab-dot { width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0; }',
    '.hb-tab.active .hb-tab-dot { box-shadow: 0 0 6px var(--tab-accent); }',

    '.hb-spacer { flex: 1 1 auto; }',

    '.hb-nav { display: flex; align-items: center; gap: 4px; flex-shrink: 0; }',
    '.hb-nav-link { display: flex; align-items: center; gap: 6px; padding: 5px 10px; border-radius: 7px; text-decoration: none; color: rgba(255,255,255,0.52); font-size: 12.5px; font-weight: 500; transition: background 0.14s ease, color 0.14s ease; letter-spacing: 0.01em; white-space: nowrap; }',
    '.hb-nav-link:hover { background: rgba(255,255,255,0.05); color: rgba(255,255,255,0.85); }',
    '.hb-nav-link svg { width: 14px; height: 14px; opacity: 0.6; flex-shrink: 0; }',

    '.hb-themes-row { display: flex; align-items: center; gap: 5px; flex-shrink: 0; }',
    '.hb-tdot { width: 14px; height: 14px; border-radius: 50%; border: 2px solid transparent; cursor: pointer; background: var(--tc); flex-shrink: 0; padding: 0; transition: transform 0.14s ease, border-color 0.14s ease, box-shadow 0.14s ease; }',
    '.hb-tdot:hover { transform: scale(1.18); box-shadow: 0 0 6px var(--tc); }',
    '.hb-tdot.sel { border-color: rgba(255,255,255,0.65); box-shadow: 0 0 8px var(--tc); }',

    '.hb-user { display: flex; align-items: center; gap: 6px; padding: 4px 10px; border-radius: 20px; text-decoration: none; background: rgba(255,255,255,0.05); color: rgba(255,255,255,0.65); font-size: 12px; font-weight: 500; transition: background 0.14s ease, color 0.14s ease; letter-spacing: 0.01em; flex-shrink: 0; }',
    '.hb-user:hover { background: rgba(255,255,255,0.09); color: rgba(255,255,255,0.9); }',
    '.hb-user-avatar { width: 20px; height: 20px; border-radius: 50%; background: linear-gradient(135deg, var(--ha, #6366f1), var(--hb, #8b5cf6)); display: flex; align-items: center; justify-content: center; font-size: 10px; font-weight: 700; color: #fff; flex-shrink: 0; }',

    '.hb-signout { padding: 5px 8px; border-radius: 7px; text-decoration: none; color: rgba(255,255,255,0.32); font-size: 12px; font-weight: 500; transition: background 0.14s ease, color 0.14s ease; letter-spacing: 0.01em; flex-shrink: 0; white-space: nowrap; }',
    '.hb-signout:hover { background: rgba(255,255,255,0.05); color: rgba(255,255,255,0.65); }',

    '.hb-hamburger { display: none; width: 32px; height: 32px; border: none; background: none; cursor: pointer; padding: 4px; border-radius: 6px; transition: background 0.14s ease; flex-shrink: 0; }',
    '.hb-hamburger:hover { background: rgba(255,255,255,0.06); }',
    '.hb-hamburger svg { width: 100%; height: 100%; color: rgba(255,255,255,0.6); }',

    '.hb-mobile-menu { display: none; position: fixed; top: ' + HEADER_HEIGHT + 'px; left: 0; right: 0; bottom: 0; background: rgba(14,16,22,0.97); backdrop-filter: blur(24px); -webkit-backdrop-filter: blur(24px); padding: 16px 20px; z-index: 5; flex-direction: column; gap: 4px; font-family: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; overflow-y: auto; opacity: 0; transition: opacity 0.18s ease; }',
    '.hb-mobile-menu.open { display: flex; opacity: 1; }',
    '.hb-mob-section-label { font-size: 10px; font-weight: 600; letter-spacing: 0.6px; text-transform: uppercase; color: rgba(255,255,255,0.3); padding: 12px 10px 6px; }',
    '.hb-mob-link { display: flex; align-items: center; gap: 10px; padding: 11px 10px; border-radius: 10px; text-decoration: none; color: rgba(255,255,255,0.72); font-size: 14px; font-weight: 450; transition: background 0.14s ease; }',
    '.hb-mob-link:hover { background: rgba(255,255,255,0.06); }',
    '.hb-mob-link.active { background: rgba(255,255,255,0.08); color: #fff; font-weight: 520; }',
    '.hb-mob-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; box-shadow: 0 0 6px var(--dc); }',
    '.hb-mob-div { height: 1px; margin: 6px 10px; background: rgba(255,255,255,0.06); }',
    '.hb-mob-themes { display: flex; align-items: center; gap: 10px; padding: 10px 10px; }',
    '.hb-mob-themes-label { font-size: 11px; font-weight: 600; letter-spacing: 0.4px; text-transform: uppercase; color: rgba(255,255,255,0.3); flex-shrink: 0; }',

    '@media (max-width: 720px) {',
    '  .hb-tabs { display: none; }',
    '  .hb-nav { display: none; }',
    '  .hb-themes-row { display: none; }',
    '  .hb-user { display: none; }',
    '  .hb-signout { display: none; }',
    '  .hb-sep.desktop-only { display: none; }',
    '  .hb-hamburger { display: flex; align-items: center; justify-content: center; }',
    '  .hb-bar { padding: 0 14px; }',
    '}',
    '@media (max-width: 1024px) and (min-width: 721px) {',
    '  .hb-tab { font-size: 12px; padding: 6px 8px; gap: 5px; }',
    '  .hb-tab-dot { width: 6px; height: 6px; }',
    '}',
    '@media (min-width: 721px) { .hb-mobile-menu { display: none !important; } }'
  ].join('\n');
  headerRoot.appendChild(headerStyle);

  /* ── Build header bar ──────────────────────────────────────── */
  var bar = document.createElement('div');
  bar.className = 'hb-bar';

  var brand = document.createElement('a');
  brand.className = 'hb-brand';
  brand.href = apexUrl('/dashboards');
  var brandMark = document.createElement('span');
  brandMark.className = 'hb-brand-mark';
  brandMark.innerHTML = '<svg viewBox="0 0 24 24" width="14" height="14" fill="#e2e2e8"><polygon points="8,5 18,12 8,19"/></svg>';
  var brandText = document.createElement('span');
  brandText.className = 'hb-brand-text';
  brandText.textContent = 'betyc';
  brand.appendChild(brandMark);
  brand.appendChild(brandText);
  bar.appendChild(brand);

  bar.appendChild(makeSep(''));

  /* ── Dashboard tabs ── */
  var tabsEl = document.createElement('div');
  tabsEl.className = 'hb-tabs';
  for (var ti = 0; ti < cfg.dashboards.length; ti++) {
    (function (d) {
      var tab = document.createElement('a');
      tab.className = 'hb-tab' + (d.key === cfg.current ? ' active' : '');
      tab.href = dashUrl(d.subdomain);
      tab.style.cssText = '--tab-accent:' + d.accent + ';';
      var dot = document.createElement('span');
      dot.className = 'hb-tab-dot';
      dot.style.cssText = 'background:' + d.accent + ';';
      var label = document.createElement('span');
      label.textContent = d.display_name;
      tab.appendChild(dot);
      tab.appendChild(label);
      if (d.key === cfg.current) tab.addEventListener('click', function (e) { e.preventDefault(); });
      tabsEl.appendChild(tab);
    })(cfg.dashboards[ti]);
  }
  bar.appendChild(tabsEl);

  var spacer = document.createElement('div');
  spacer.className = 'hb-spacer';
  bar.appendChild(spacer);

  /* Nav links (no Hub — tabs replace it) */
  var nav = document.createElement('div');
  nav.className = 'hb-nav';
  var navItems = [
    { label: 'Billing',  path: '/billing',  icon: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3"><rect x="1.5" y="3" width="13" height="10" rx="1.5"/><path d="M1.5 6.5h13"/></svg>' },
    { label: 'Settings', path: '/settings', icon: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3"><circle cx="8" cy="8" r="2.5"/><path d="M8 1.5v2M8 12.5v2M1.5 8h2M12.5 8h2M3.4 3.4l1.4 1.4M11.2 11.2l1.4 1.4M3.4 12.6l1.4-1.4M11.2 4.8l1.4-1.4"/></svg>' }
  ];
  for (var ni = 0; ni < navItems.length; ni++) {
    (function (item) {
      var a = document.createElement('a');
      a.className = 'hb-nav-link';
      a.href = apexUrl(item.path);
      a.innerHTML = item.icon;
      var lbl = document.createElement('span');
      lbl.textContent = item.label;
      a.appendChild(lbl);
      nav.appendChild(a);
    })(navItems[ni]);
  }
  bar.appendChild(nav);

  bar.appendChild(makeSep('desktop-only'));

  var headerThemesRow = document.createElement('div');
  headerThemesRow.className = 'hb-themes-row';
  var hdc = buildThemeDots('hb-tdot', 14);
  while (hdc.firstChild) headerThemesRow.appendChild(hdc.firstChild);
  bar.appendChild(headerThemesRow);

  bar.appendChild(makeSep('desktop-only'));

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

  var signout = document.createElement('a');
  signout.className = 'hb-signout';
  signout.href = apexUrl('/logout');
  signout.textContent = 'Sign out';
  bar.appendChild(signout);

  var hamburger = document.createElement('button');
  hamburger.className = 'hb-hamburger';
  hamburger.setAttribute('aria-label', 'Menu');
  hamburger.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 7h16M4 12h16M4 17h16"/></svg>';
  bar.appendChild(hamburger);

  headerRoot.appendChild(bar);

  /* ── Mobile menu ── */
  var mobileMenu = document.createElement('div');
  mobileMenu.className = 'hb-mobile-menu';
  var mobileOpen = false;

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

  mobileMenu.appendChild(makeMobDiv());

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
      a.appendChild(document.createTextNode(item.label));
      mobileMenu.appendChild(a);
    })(mobNavItems[mni]);
  }

  mobileMenu.appendChild(makeMobDiv());

  var mobThemes = document.createElement('div');
  mobThemes.className = 'hb-mob-themes';
  var mobThemesLabel = document.createElement('span');
  mobThemesLabel.className = 'hb-mob-themes-label';
  mobThemesLabel.textContent = 'Theme';
  mobThemes.appendChild(mobThemesLabel);
  mobThemes.appendChild(buildThemeDots('hb-tdot', 18));
  mobileMenu.appendChild(mobThemes);

  mobileMenu.appendChild(makeMobDiv());

  var mobSignout = document.createElement('a');
  mobSignout.className = 'hb-mob-link';
  mobSignout.href = apexUrl('/logout');
  mobSignout.style.color = 'rgba(255,255,255,0.4)';
  mobSignout.appendChild(document.createTextNode('Sign out'));
  mobileMenu.appendChild(mobSignout);

  headerRoot.appendChild(mobileMenu);

  hamburger.addEventListener('click', function (e) {
    e.stopPropagation();
    mobileOpen = !mobileOpen;
    if (mobileOpen) {
      mobileMenu.classList.add('open');
      requestAnimationFrame(function () { mobileMenu.style.opacity = '1'; });
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

  function makeSep(extraClass) {
    var s = document.createElement('div');
    s.className = 'hb-sep' + (extraClass ? ' ' + extraClass : '');
    return s;
  }

  function makeMobDiv() {
    var d = document.createElement('div');
    d.className = 'hb-mob-div';
    return d;
  }

  function updateBrandColors() {
    var t;
    for (var i = 0; i < THEMES.length; i++) {
      if (THEMES[i].id === activeThemeId) { t = THEMES[i]; break; }
    }
    if (!t) t = THEMES[0];
    brandMark.style.setProperty('--ha', t.a);
    brandMark.style.setProperty('--hb', t.a2);
    userAvatar.style.setProperty('--ha', t.a);
    userAvatar.style.setProperty('--hb', t.a2);
  }

  /* ================================================================
   *  GLOBAL EVENT HANDLERS
   * ================================================================ */

  document.addEventListener('click', function () {
    if (mobileOpen) closeMobileMenu();
  });

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && mobileOpen) closeMobileMenu();
  });

  /* ================================================================
   *  MOUNT & INITIALIZE
   * ================================================================ */

  applyTheme(activeThemeId);

  function pushBodyDown() {
    var current = parseInt(document.body.style.paddingTop, 10) || 0;
    if (!document.body.getAttribute('data-hb-header-offset')) {
      document.body.style.paddingTop = (current + HEADER_HEIGHT) + 'px';
      document.body.setAttribute('data-hb-header-offset', String(HEADER_HEIGHT));
    }
  }

  document.body.appendChild(headerHost);
  pushBodyDown();

})();
