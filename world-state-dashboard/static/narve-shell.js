/*!
 * narve.ai — unified dashboard shell.
 *
 * Self-injecting top bar that gives every dashboard the same brand + switcher.
 * Vendored identically into each dashboard's /static/ directory.
 * Adding a new dashboard = one entry in DASHBOARDS below + redeploy the file.
 *
 * Conventions:
 *   - Production: each dashboard lives at <subdomain>.narve.ai
 *   - Local dev:  each dashboard binds a fixed port on localhost
 * The shell auto-detects which mode it's in from window.location.
 */
(function () {
  'use strict';

  // Single source of truth for the suite.
  const DASHBOARDS = [
    { id: 'world',    label: 'World',    subdomain: 'world',    port: 7050 },
    { id: 'climate',  label: 'Climate',  subdomain: 'climate',  port: 7052 },
    { id: 'weather',  label: 'Weather',  subdomain: 'weather',  port: 5050 },
    { id: 'crypto',   label: 'Crypto',   subdomain: 'crypto',   port: 8000 },
    { id: 'midterm',  label: 'Midterm',  subdomain: 'midterm',  port: 8051 },
    { id: 'voters',   label: 'Voters',   subdomain: 'voters',   port: 8052 },
    { id: 'stock',    label: 'Stocks',   subdomain: 'stocks',   port: 8053 },
    { id: 'centralbank', label: 'Banks', subdomain: 'banks',    port: 8054 },
    { id: 'sports',   label: 'Sports',   subdomain: 'sports',   port: 8055 },
    { id: 'top-traders', label: 'Traders', subdomain: 'traders', port: 8056 },
  ];

  function isLocalhost() {
    const h = location.hostname;
    return h === 'localhost' || h === '127.0.0.1' || h.endsWith('.local');
  }

  function urlFor(d) {
    if (isLocalhost()) {
      return `${location.protocol}//${location.hostname}:${d.port}/`;
    }
    return `https://${d.subdomain}.narve.ai/`;
  }

  function detectCurrent() {
    const host = location.hostname;
    const port = location.port;
    for (const d of DASHBOARDS) {
      if (host === d.subdomain + '.narve.ai') return d.id;
      if (host.startsWith(d.subdomain + '.')) return d.id;
      if (isLocalhost() && String(port) === String(d.port)) return d.id;
    }
    return null;
  }

  // CSS — kept in JS so deployers don't have to ship a separate CSS file.
  const SHELL_CSS = `
    :root { --narve-shell-h: 32px; }
    body { padding-top: var(--narve-shell-h) !important; }
    /* Fixed-position headers used by the dashboards' own UIs need to be
       pushed below the shell. Targeting common selectors. */
    body > .header,
    body > .topbar,
    body > header.header,
    body > header.topbar { top: var(--narve-shell-h) !important; }
    /* Cesium fills the viewport — push its container down. */
    body > #cesiumContainer { top: calc(40px + var(--narve-shell-h)) !important; }
    /* Loading overlays should also clear the shell. */
    body > #loading { top: var(--narve-shell-h) !important; }

    .narve-shell {
      position: fixed; top: 0; left: 0; right: 0; height: var(--narve-shell-h);
      background: rgba(5,5,5,0.95);
      border-bottom: 1px solid #1f1f1f;
      display: flex; align-items: center; padding: 0 12px; gap: 14px;
      z-index: 10000;
      font-family: 'SF Mono','Cascadia Code','Fira Code','JetBrains Mono',Monaco,Consolas,monospace;
      font-size: 11px;
      color: #e6e6e6;
      backdrop-filter: blur(8px);
      -webkit-backdrop-filter: blur(8px);
      letter-spacing: 0.02em;
    }
    .narve-shell .brand {
      font-weight: 600;
      letter-spacing: 0.08em;
      text-transform: lowercase;
      color: #4ade80;
      flex-shrink: 0;
      cursor: default;
      user-select: none;
    }
    .narve-shell .brand .dot {
      display: inline-block; width: 5px; height: 5px; border-radius: 50%;
      background: #4ade80; margin-right: 6px; box-shadow: 0 0 4px #4ade80;
    }
    .narve-shell .switcher {
      display: flex; gap: 2px; flex: 1; min-width: 0;
      overflow-x: auto; scrollbar-width: none;
    }
    .narve-shell .switcher::-webkit-scrollbar { display: none; }
    .narve-shell .switcher a {
      color: #888; text-decoration: none;
      padding: 2px 9px;
      border: 1px solid transparent;
      white-space: nowrap;
      transition: color .12s, border-color .12s, background .12s;
    }
    .narve-shell .switcher a:hover {
      color: #e6e6e6; border-color: #2a2a2a;
    }
    .narve-shell .switcher a.active {
      color: #4ade80; border-color: #1f1f1f; background: #111;
    }
    .narve-shell .search {
      background: #111; border: 1px solid #1f1f1f; color: #e6e6e6;
      padding: 2px 10px; min-width: 160px; outline: none;
      font-family: inherit; font-size: 11px;
      flex-shrink: 0;
    }
    .narve-shell .search::placeholder { color: #555; }
    .narve-shell .search:focus { border-color: #4ade80; }
    .narve-shell .search:disabled { cursor: not-allowed; }
    .narve-shell .right-pad { color: #555; font-size: 10px; flex-shrink: 0; }

    @media (max-width: 700px) {
      .narve-shell .search { display: none; }
      .narve-shell .right-pad { display: none; }
      .narve-shell .switcher a { padding: 2px 7px; }
    }
  `;

  function inject() {
    if (document.querySelector('.narve-shell')) return;  // idempotent

    // Inject CSS
    if (!document.getElementById('narve-shell-css')) {
      const style = document.createElement('style');
      style.id = 'narve-shell-css';
      style.textContent = SHELL_CSS;
      document.head.appendChild(style);
    }

    const current = detectCurrent();
    const switcherHTML = DASHBOARDS.map(d => {
      const cls = d.id === current ? 'active' : '';
      return `<a href="${urlFor(d)}" class="${cls}" data-dash="${d.id}">${d.label}</a>`;
    }).join('');

    const shell = document.createElement('div');
    shell.className = 'narve-shell';
    shell.innerHTML = `
      <span class="brand"><span class="dot"></span>narve.ai</span>
      <nav class="switcher" aria-label="Dashboards">${switcherHTML}</nav>
      <input type="text" class="search" placeholder="search the suite (soon)…" disabled aria-label="Search (coming soon)">
      <span class="right-pad">${isLocalhost() ? 'local' : 'prod'}</span>
    `;

    // Insert at the very top of body so existing dashboard layout sits below.
    if (document.body.firstChild) {
      document.body.insertBefore(shell, document.body.firstChild);
    } else {
      document.body.appendChild(shell);
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', inject);
  } else {
    inject();
  }
})();
