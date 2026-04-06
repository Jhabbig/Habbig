/**
 * Habbig Dashboard Switcher
 * The H logo opens a panel to switch between dashboards.
 * Injected by the gateway proxy into every dashboard.
 *
 * Expects window.__hbSwitcher = { dashboards: [...], current: "key", domain: "..." }
 */
(function () {
  'use strict';
  var cfg = window.__hbSwitcher;
  if (!cfg || !cfg.dashboards) return;

  var proto = location.protocol;
  var port = location.port ? ':' + location.port : '';
  var base = cfg.domain || 'localhost';

  function dashUrl(sub) {
    if (base === 'localhost') return proto + '//' + sub + '.localhost' + port + '/';
    return proto + '//' + sub + '.' + base + port + '/';
  }
  function hubUrl() {
    if (base === 'localhost') return proto + '//localhost' + port + '/dashboards';
    return proto + '//' + base + port + '/dashboards';
  }

  var host = document.createElement('div');
  host.style.cssText = 'all:initial;position:fixed;z-index:2147483647;bottom:0;left:0;width:0;height:0;pointer-events:none;';
  var root = host.attachShadow({ mode: 'closed' });

  var style = document.createElement('style');
  style.textContent = [
    "@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');",
    ':host{all:initial;}',
    '*{box-sizing:border-box;margin:0;padding:0;}',

    '.hb-logo{',
    '  pointer-events:auto;cursor:pointer;position:fixed;bottom:24px;left:24px;',
    '  width:44px;height:44px;border-radius:14px;border:none;',
    '  background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);',
    '  display:flex;align-items:center;justify-content:center;',
    '  font-family:"Inter",-apple-system,sans-serif;font-size:18px;font-weight:700;',
    '  color:#fff;',
    '  transition:transform .18s ease,box-shadow .18s ease;',
    '  box-shadow:0 2px 12px rgba(0,0,0,0.35);',
    '}',
    '.hb-logo:hover{',
    '  transform:scale(1.08);',
    '  box-shadow:0 4px 20px rgba(102,126,234,0.45);',
    '}',

    '.sw-panel{',
    '  pointer-events:auto;position:fixed;bottom:78px;left:24px;width:260px;',
    '  background:rgba(14,16,22,0.92);backdrop-filter:blur(32px);-webkit-backdrop-filter:blur(32px);',
    '  border:1px solid rgba(255,255,255,0.07);border-radius:16px;',
    '  box-shadow:0 8px 40px rgba(0,0,0,0.55),0 0 0 1px rgba(255,255,255,0.03);',
    '  font-family:"Inter",-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;',
    '  overflow:hidden;transform-origin:bottom left;',
    '  transition:opacity .18s ease,transform .18s ease;',
    '}',
    '.sw-panel[aria-hidden="true"]{opacity:0;transform:translateY(8px) scale(0.97);pointer-events:none;}',
    '.sw-panel[aria-hidden="false"]{opacity:1;transform:translateY(0) scale(1);}',

    '.sw-head{padding:14px 16px 8px;display:flex;align-items:center;justify-content:space-between;}',
    '.sw-title{font-size:11px;font-weight:600;letter-spacing:0.6px;text-transform:uppercase;color:rgba(255,255,255,0.38);}',
    '.sw-kbd{font-size:10px;font-weight:500;color:rgba(255,255,255,0.22);',
    '  border:1px solid rgba(255,255,255,0.08);border-radius:5px;padding:2px 6px;',
    '  font-family:"Inter",sans-serif;letter-spacing:0.3px;}',

    '.sw-list{padding:4px 8px 10px;display:flex;flex-direction:column;gap:2px;}',

    '.sw-item{',
    '  display:flex;align-items:center;gap:10px;padding:9px 10px;border-radius:10px;',
    '  text-decoration:none;color:rgba(255,255,255,0.72);font-size:13px;font-weight:450;',
    '  transition:background .14s ease,color .14s ease;cursor:pointer;letter-spacing:0.01em;',
    '}',
    '.sw-item:hover{background:rgba(255,255,255,0.06);color:rgba(255,255,255,0.95);}',
    '.sw-item.active{background:rgba(255,255,255,0.08);color:#fff;font-weight:520;}',

    '.sw-dot{width:8px;height:8px;border-radius:50%;flex-shrink:0;box-shadow:0 0 6px var(--c);}',
    '.sw-badge{margin-left:auto;font-size:10px;font-weight:500;color:rgba(255,255,255,0.25);letter-spacing:0.3px;}',
    '.sw-div{height:1px;margin:4px 12px;background:rgba(255,255,255,0.06);}',

    '.sw-hub{',
    '  display:flex;align-items:center;gap:8px;padding:8px 16px 12px;',
    '  text-decoration:none;color:rgba(255,255,255,0.36);font-size:11px;font-weight:500;',
    '  transition:color .14s ease;letter-spacing:0.2px;',
    '}',
    '.sw-hub:hover{color:rgba(255,255,255,0.6);}',
    '.sw-hub svg{width:14px;height:14px;}',

    '@media(max-width:500px){',
    '  .hb-logo{bottom:16px;left:16px;width:40px;height:40px;border-radius:12px;font-size:16px;}',
    '  .sw-panel{left:12px;right:12px;bottom:68px;width:auto;}',
    '}',
  ].join('\n');
  root.appendChild(style);

  var open = false;

  // H logo = trigger
  var logo = document.createElement('button');
  logo.className = 'hb-logo';
  logo.setAttribute('aria-label', 'Switch dashboard');
  logo.textContent = 'H';

  // Panel
  var panel = document.createElement('div');
  panel.className = 'sw-panel';
  panel.setAttribute('aria-hidden', 'true');

  var head = document.createElement('div');
  head.className = 'sw-head';
  var title = document.createElement('span');
  title.className = 'sw-title';
  title.textContent = 'Dashboards';
  var kbd = document.createElement('span');
  kbd.className = 'sw-kbd';
  kbd.textContent = /Mac|iPod|iPhone|iPad/.test(navigator.platform || navigator.userAgent) ? '\u2318 J' : 'Ctrl J';
  head.appendChild(title);
  head.appendChild(kbd);
  panel.appendChild(head);

  var list = document.createElement('div');
  list.className = 'sw-list';

  (cfg.dashboards || []).forEach(function (d) {
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
      a.addEventListener('click', function (e) { e.preventDefault(); toggle(); });
    }

    list.appendChild(a);
  });
  panel.appendChild(list);

  var div = document.createElement('div');
  div.className = 'sw-div';
  panel.appendChild(div);

  var hub = document.createElement('a');
  hub.className = 'sw-hub';
  hub.href = hubUrl();
  hub.innerHTML = '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="10" cy="10" r="7"/><path d="M10 6v4l2.5 1.5"/></svg>';
  var hubText = document.createElement('span');
  hubText.textContent = 'All dashboards';
  hub.appendChild(hubText);
  panel.appendChild(hub);

  root.appendChild(logo);
  root.appendChild(panel);

  function toggle() {
    open = !open;
    panel.setAttribute('aria-hidden', open ? 'false' : 'true');
  }

  logo.addEventListener('click', function (e) { e.stopPropagation(); toggle(); });
  document.addEventListener('click', function () { if (open) toggle(); });
  panel.addEventListener('click', function (e) { e.stopPropagation(); });

  document.addEventListener('keydown', function (e) {
    if ((e.metaKey || e.ctrlKey) && e.key === 'j') { e.preventDefault(); toggle(); }
    if (e.key === 'Escape' && open) toggle();
  });

  document.body.appendChild(host);
})();
