"""Canonical user-facing sidebar — single source of truth.

Every user-facing page (NOT admin — admin uses ``admin_shell.py``) renders
its sidebar by calling ``render_sidebar(active=<route>, …)`` and dropping
the result into the template via ``{{ raw_sidebar }}``.

Why this module exists
----------------------
Before this, each page hand-rolled its own ``<aside class="sidebar">``
markup. Templates drifted: billing.html had only Dashboards/Billing/
Settings, settings.html had a slightly different set, profile.html
showed the same name as ``{{ username }}`` but used a different avatar
attribute, etc. Every nav-item change had to be applied in 12 places.

This module is the single place to:
  * add or rename a nav item
  * change the avatar / username footer
  * add the collapse-toggle button
  * swap the brand logo

How to use
----------
    from sidebar import render_sidebar
    return render_page(
        "billing",
        request=request,
        username=user["username"],
        raw_sidebar=render_sidebar(
            request, active="billing",
            username=user["username"],
            raw_admin_link=admin_link,
            raw_intelligence_link=intel_link,
            raw_signal_search_link=signal_link,
            raw_nav_role=nav_role,
        ),
        ...
    )

Active-state is computed by matching ``active`` against each item's
``key``. Active items get ``class="nav-item active"``.

The collapsed state (icon-only, 56 px wide) is controlled by the
``data-sidebar="collapsed"`` attribute on ``<html>``. ``sidebar.js``
(loaded inline at the bottom of this partial) hooks the toggle button
and persists state in ``localStorage`` under ``narve-sidebar``.
"""

from __future__ import annotations

import html
from typing import Optional


# ── Nav item definitions ───────────────────────────────────────────────
#
# (key, href, label, svg_path_d). The SVG has consistent stroke / size /
# viewBox; only the path commands change. Keeping the icons inline (not
# referencing an external sprite) avoids a network round-trip and keeps
# the sidebar self-contained.

_NAV: list[tuple[str, str, str, str]] = [
    ("dashboards", "/dashboards", "Dashboards",
     '<rect x="1.5" y="1.5" width="5" height="5" rx="1"/>'
     '<rect x="9.5" y="1.5" width="5" height="5" rx="1"/>'
     '<rect x="1.5" y="9.5" width="5" height="5" rx="1"/>'
     '<rect x="9.5" y="9.5" width="5" height="5" rx="1"/>'),
    ("collections", "/collections", "Collections",
     '<path d="M3 4h10M3 8h10M3 12h6"/>'),
    ("predictions", "/predictions", "Your predictions",
     '<path d="M2.5 4h11M2.5 8h11M2.5 12h7"/>'
     '<circle cx="12.5" cy="12" r="1.4"/>'),
    ("explore", "/explore", "Explore",
     '<circle cx="8" cy="8" r="6.5"/>'
     '<path d="M10.5 5.5L9 9l-3.5 1.5L7 7z"/>'),
    ("billing", "/billing", "Billing",
     '<rect x="1" y="3" width="14" height="10" rx="2"/>'
     '<line x1="1" y1="7" x2="15" y2="7"/>'),
    ("settings", "/settings", "Settings",
     '<circle cx="8" cy="8" r="2.5"/>'
     '<path d="M8 1.5v1.5M8 13v1.5M1.5 8H3M13 8h1.5'
     'M3.1 3.1l1.1 1.1M11.8 11.8l1.1 1.1'
     'M3.1 12.9l1.1-1.1M11.8 4.2l1.1-1.1"/>'),
]


def _icon_svg(path_d: str) -> str:
    return (
        '<svg class="nav-item-icon" viewBox="0 0 16 16" fill="none" '
        'stroke="currentColor" stroke-width="1.5" stroke-linecap="round" '
        'stroke-linejoin="round" aria-hidden="true">'
        f'{path_d}'
        '</svg>'
    )


def _nav_item(key: str, href: str, label: str, svg_d: str, *, active: str) -> str:
    cls = "nav-item active" if key == active else "nav-item"
    aria_current = ' aria-current="page"' if key == active else ""
    icon = _icon_svg(svg_d)
    label_safe = html.escape(label)
    return (
        f'<a href="{html.escape(href)}" class="{cls}" data-nav-key="{key}"{aria_current} '
        f'title="{label_safe}">'
        f'{icon}'
        f'<span class="nav-item-label">{label_safe}</span>'
        f'</a>'
    )


# Inline script wires:
#   1. The collapse toggle + persistence under ``narve-sidebar`` localStorage.
#   2. The avatar-initial fill (so .sidebar-user-avatar[data-username]
#      shows the first letter of the username).
#   3. The sidebar-injected upgrade — turns server-rendered <span>-wrapped
#      <a href="…">label</a> shells into proper .nav-item entries with
#      icons. Mirrors the per-template inline upgrade that dashboards.html
#      already does, lifted up here so every host page gets it for free.
# Kept inline so:
#   - Pre-paint: the collapsed-state attribute is applied before first
#     render (no flash of expanded sidebar on reload).
#   - The host template doesn't have to remember to load a side script.
_INLINE_TOGGLE_JS = """
<script>
(function(){
  try {
    var saved = localStorage.getItem('narve-sidebar') || 'expanded';
    if (saved === 'collapsed') {
      document.documentElement.setAttribute('data-sidebar','collapsed');
    }
  } catch(_) {}
  var ICONS = {
    shield:'<svg class="nav-item-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8 1.5L2.5 4v4c0 3.5 2.3 5.7 5.5 6.5 3.2-.8 5.5-3 5.5-6.5V4L8 1.5z"/></svg>',
    search:'<svg class="nav-item-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="7" cy="7" r="4.5"/><line x1="10.2" y1="10.2" x2="14" y2="14"/></svg>',
    bolt:'<svg class="nav-item-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 1L3 9h5l-1 6 6-8H8z"/></svg>'
  };
  function upgradeInjected(){
    document.querySelectorAll('.sidebar-injected').forEach(function(wrap){
      var a = wrap.querySelector('a');
      if (!a) { wrap.style.display = 'none'; return; }
      var iconKey = wrap.getAttribute('data-icon') || '';
      var label = (a.textContent || '').trim();
      a.classList.add('nav-item');
      if (label) {
        if (!a.querySelector('.nav-item-label')) {
          /* Wrap the label so the collapsed state can hide it cleanly. */
          var span = document.createElement('span');
          span.className = 'nav-item-label';
          span.textContent = label;
          a.textContent = '';
          if (ICONS[iconKey]) a.insertAdjacentHTML('beforeend', ICONS[iconKey]);
          a.appendChild(span);
        } else if (ICONS[iconKey] && !a.querySelector('.nav-item-icon')) {
          a.insertAdjacentHTML('afterbegin', ICONS[iconKey]);
        }
        a.setAttribute('title', label);
      }
      wrap.replaceWith(a);
    });
  }
  function fillAvatarInitial(){
    var av = document.querySelector('.sidebar-user-avatar[data-username]');
    if (!av) return;
    var u = av.getAttribute('data-username') || '';
    av.textContent = u ? u.charAt(0).toUpperCase() : '?';
  }
  function bind(){
    upgradeInjected();
    fillAvatarInitial();
    var btn = document.getElementById('sidebar-toggle');
    if (!btn) return;
    btn.addEventListener('click', function(){
      var doc = document.documentElement;
      var next = doc.getAttribute('data-sidebar') === 'collapsed' ? 'expanded' : 'collapsed';
      if (next === 'collapsed') doc.setAttribute('data-sidebar','collapsed');
      else doc.removeAttribute('data-sidebar');
      try { localStorage.setItem('narve-sidebar', next); } catch(_) {}
      btn.setAttribute('aria-expanded', next === 'expanded' ? 'true' : 'false');
    });
    btn.setAttribute('aria-expanded',
      document.documentElement.getAttribute('data-sidebar') === 'collapsed' ? 'false' : 'true');
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bind);
  } else {
    bind();
  }
})();
</script>
"""


def render_sidebar(
    request=None,
    *,
    active: str = "",
    username: str = "",
    raw_admin_link: str = "",
    raw_intelligence_link: str = "",
    raw_signal_search_link: str = "",
    raw_nav_role: str = "",
    show_pinned_views: bool = True,
    show_first_week_goals: bool = True,
) -> str:
    """Return the canonical sidebar HTML for a logged-in user-facing page.

    Required: nothing — every kwarg has a sensible empty-string default
    so transitional callers can wire one piece at a time.

    ``active`` matches one of the keys in ``_NAV`` (e.g. ``"billing"``,
    ``"dashboards"``). Anything else just leaves no item highlighted.
    """
    nav_items = "\n".join(
        _nav_item(key, href, label, svg_d, active=active)
        for key, href, label, svg_d in _NAV[:1]  # Dashboards
    )

    # Server-injected links (signal search, intelligence) sit between
    # Dashboards and Collections, just like the canonical layout in
    # dashboards.html. They're optional — empty strings collapse cleanly.
    if raw_signal_search_link:
        nav_items += (
            f'\n<span class="sidebar-injected" data-icon="search">'
            f'{raw_signal_search_link}</span>'
        )
    if raw_intelligence_link:
        nav_items += (
            f'\n<span class="sidebar-injected" data-icon="bolt">'
            f'{raw_intelligence_link}</span>'
        )

    # Remaining items in the order: Collections, Predictions, Explore,
    # Billing, Settings.
    for key, href, label, svg_d in _NAV[1:]:
        nav_items += "\n" + _nav_item(key, href, label, svg_d, active=active)

    if raw_admin_link:
        nav_items += (
            f'\n<span class="sidebar-injected" data-icon="shield">'
            f'{raw_admin_link}</span>'
        )

    pinned_html = '<div id="pinned-views"></div>' if show_pinned_views else ""
    fwg_html = (
        '<div id="first-week-goals-mount" style="display:none"></div>'
        if show_first_week_goals else ""
    )

    username_safe = html.escape(username or "")
    role_html = raw_nav_role or ""

    return f"""
<aside class="sidebar" aria-label="Primary">
  <div class="sidebar-logo">
    <a href="/dashboards" class="sidebar-logo-link" aria-label="narve.ai home">
      <img src="/_gateway_static/img/logo.png" alt="" class="sidebar-logo-img"
           width="22" height="22">
      <span class="sidebar-logo-text">narve.ai</span>
    </a>
    <button type="button"
            id="sidebar-toggle"
            class="sidebar-toggle"
            aria-label="Toggle sidebar"
            aria-expanded="true">
      <svg viewBox="0 0 16 16" width="14" height="14" fill="none"
           stroke="currentColor" stroke-width="1.6" stroke-linecap="round"
           stroke-linejoin="round" aria-hidden="true">
        <path d="M10 3 5 8l5 5"/>
      </svg>
    </button>
  </div>

  <nav class="sidebar-nav" aria-label="Main">
    <div class="nav-section-header">Navigation</div>
    {nav_items}
  </nav>

  {pinned_html}
  {fwg_html}

  <a href="/profile" class="sidebar-user" style="text-decoration:none">
    <div class="sidebar-user-avatar" data-username="{username_safe}"></div>
    <span class="sidebar-user-name">{username_safe}</span>
    <span class="sidebar-user-tier">{role_html}</span>
  </a>
</aside>
{_INLINE_TOGGLE_JS}
"""
