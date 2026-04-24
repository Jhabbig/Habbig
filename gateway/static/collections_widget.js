/* collections_widget.js — shared "+ Collection" picker.
 *
 * Exposes a single global:
 *
 *   window.hbAddToCollection(itemType, itemRef, anchorEl)
 *
 * Call it from a detail page's "+" button. It fetches the user's own
 * collections via /api/collections/me, renders an anchored popover
 * with a row per mutable collection (system "Saved"/"Watchlist" rows
 * are filtered out — they auto-populate from the source tables), plus
 * a "New collection…" affordance that lets the user type a title and
 * add in one step.
 *
 * No framework dependency. Works on every page that loads this script.
 * Loader integration: include once in the dashboard shell
 *   <script src="/_gateway_static/collections_widget.js" defer></script>
 *
 * Auth model: relies on the session cookie. If the user is not signed
 * in, the popover renders a single "Sign in to save" link.
 *
 * Styling: scoped to the .hbc-* class prefix so it cannot leak into
 * any surrounding stylesheet. Follows the monochrome dashboard theme
 * (CSS custom properties) with an inline fallback for pages that don't
 * define --ink / --bg.
 */
(function () {
  'use strict';

  if (window.hbAddToCollection) return;

  // ── Styles (injected once) ────────────────────────────────────────────────
  var CSS = (
    '.hbc-pop{position:absolute;z-index:9999;min-width:260px;max-width:320px;' +
    'background:var(--bg,#fff);color:var(--ink,#0d0d0d);' +
    'border:1px solid var(--border,#e5e5e5);border-radius:10px;' +
    'box-shadow:0 10px 30px rgba(0,0,0,0.12);padding:8px;font-family:inherit;' +
    'font-size:13px;max-height:360px;overflow:auto}' +
    '.hbc-pop-title{font-size:10px;text-transform:uppercase;letter-spacing:0.1em;' +
    'color:var(--muted,#666);padding:6px 10px}' +
    '.hbc-row{display:flex;justify-content:space-between;align-items:center;' +
    'gap:10px;padding:8px 10px;border-radius:6px;cursor:pointer;' +
    'background:transparent;border:0;width:100%;text-align:left;' +
    'color:inherit;font:inherit}' +
    '.hbc-row:hover{background:var(--soft,#fafafa)}' +
    '.hbc-row-title{font-weight:500;flex:1;white-space:nowrap;overflow:hidden;' +
    'text-overflow:ellipsis}' +
    '.hbc-row-meta{font-size:11px;color:var(--muted,#666);white-space:nowrap}' +
    '.hbc-sep{height:1px;background:var(--border,#e5e5e5);margin:6px 0}' +
    '.hbc-new-form{padding:6px 10px;display:flex;gap:6px}' +
    '.hbc-new-form input{flex:1;padding:6px 8px;border:1px solid var(--border,#e5e5e5);' +
    'border-radius:5px;background:var(--bg,#fff);color:var(--ink,#0d0d0d);font:inherit}' +
    '.hbc-new-form button{padding:6px 12px;border:0;border-radius:5px;' +
    'background:var(--ink,#0d0d0d);color:var(--bg,#fff);cursor:pointer;' +
    'font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:0.06em}' +
    '.hbc-empty{padding:14px 10px;color:var(--muted,#666);font-size:12px;text-align:center}' +
    '.hbc-toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%);' +
    'background:var(--ink,#0d0d0d);color:var(--bg,#fff);padding:10px 18px;' +
    'border-radius:999px;font-size:12px;letter-spacing:0.06em;text-transform:uppercase;' +
    'z-index:10000;opacity:0;transition:opacity 0.15s}' +
    '.hbc-toast.show{opacity:1}'
  );
  var styleEl = document.createElement('style');
  styleEl.setAttribute('data-hbc', '1');
  styleEl.textContent = CSS;
  document.head.appendChild(styleEl);

  // ── Helpers ──────────────────────────────────────────────────────────────

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c];
    });
  }

  function csrf() {
    var m = document.cookie.match(/(?:^|;\s*)_csrf=([^;]*)/);
    return m ? decodeURIComponent(m[1]) : '';
  }

  function toast(text) {
    var el = document.createElement('div');
    el.className = 'hbc-toast';
    el.textContent = text;
    document.body.appendChild(el);
    requestAnimationFrame(function () { el.classList.add('show'); });
    setTimeout(function () {
      el.classList.remove('show');
      setTimeout(function () { el.remove(); }, 200);
    }, 1600);
  }

  async function api(method, path, body) {
    var opts = {
      method: method,
      headers: { 'Content-Type': 'application/json', 'x-csrf-token': csrf() },
      credentials: 'same-origin'
    };
    if (body !== undefined) opts.body = JSON.stringify(body);
    var r = await fetch(path, opts);
    var data = {};
    try { data = await r.json(); } catch (e) { /* ignore */ }
    if (!r.ok) {
      var msg = (data && data.detail) || ('HTTP ' + r.status);
      var err = new Error(msg);
      err.status = r.status;
      throw err;
    }
    return data;
  }

  // ── Popover ──────────────────────────────────────────────────────────────

  function closePop() {
    var existing = document.querySelector('.hbc-pop');
    if (existing) existing.remove();
    document.removeEventListener('click', _onDocClick, true);
    document.removeEventListener('keydown', _onKey, true);
  }

  function _onDocClick(ev) {
    var pop = document.querySelector('.hbc-pop');
    if (!pop) return;
    if (!pop.contains(ev.target)) closePop();
  }

  function _onKey(ev) {
    if (ev.key === 'Escape') closePop();
  }

  function placePop(pop, anchor) {
    if (!anchor) {
      pop.style.top = '80px';
      pop.style.left = '50%';
      pop.style.transform = 'translateX(-50%)';
      return;
    }
    var r = anchor.getBoundingClientRect();
    pop.style.top = (window.scrollY + r.bottom + 6) + 'px';
    var left = window.scrollX + r.left;
    var maxLeft = window.scrollX + window.innerWidth - 340;
    if (left > maxLeft) left = maxLeft;
    pop.style.left = Math.max(window.scrollX + 8, left) + 'px';
  }

  async function renderChoices(pop, itemType, itemRef) {
    pop.innerHTML = '<div class="hbc-pop-title">Loading\u2026</div>';
    var data;
    try {
      data = await api('GET', '/api/collections/me');
    } catch (e) {
      if (e.status === 401) {
        pop.innerHTML =
          '<div class="hbc-empty">' +
          '<a href="/login" style="color:inherit">Sign in</a> to save to a collection.' +
          '</div>';
      } else {
        pop.innerHTML = '<div class="hbc-empty">Couldn\'t load collections.</div>';
      }
      return;
    }
    var own = (data && data.own) || [];
    // System boards (Saved/Watchlist) are read-only — filter them out.
    var pickable = own.filter(function (c) { return !c.is_system; });

    var html = '<div class="hbc-pop-title">Add to collection</div>';
    if (pickable.length) {
      pickable.forEach(function (c) {
        html += (
          '<button class="hbc-row" data-id="' + c.id + '" type="button">' +
          '<span class="hbc-row-title">' + esc(c.title) + '</span>' +
          '<span class="hbc-row-meta">' + (c.item_count || 0) +
          ' \u00b7 ' + esc(c.visibility) + '</span>' +
          '</button>'
        );
      });
    } else {
      html += '<div class="hbc-empty">No collections yet — create your first below.</div>';
    }
    html += '<div class="hbc-sep"></div>';
    html += (
      '<div class="hbc-pop-title">New collection</div>' +
      '<form class="hbc-new-form" data-new="1">' +
      '<input name="title" placeholder="e.g. Fed meetings Q2" maxlength="80" required>' +
      '<button type="submit">Create + add</button>' +
      '</form>'
    );
    pop.innerHTML = html;

    pop.querySelectorAll('.hbc-row[data-id]').forEach(function (btn) {
      btn.addEventListener('click', async function () {
        var cid = parseInt(btn.dataset.id, 10);
        btn.disabled = true;
        try {
          await api('POST', '/api/collections/' + cid + '/items',
                    { item_type: itemType, item_ref: itemRef });
          closePop();
          toast('Added');
        } catch (e) {
          btn.disabled = false;
          (window.narveToastError || window.alert)(e.message);
        }
      });
    });

    var form = pop.querySelector('form[data-new]');
    if (form) {
      form.addEventListener('submit', async function (ev) {
        ev.preventDefault();
        var title = form.querySelector('input[name="title"]').value.trim();
        if (!title) return;
        form.querySelector('button').disabled = true;
        try {
          var created = await api('POST', '/api/collections',
                                  { title: title, visibility: 'private' });
          await api('POST', '/api/collections/' + created.id + '/items',
                    { item_type: itemType, item_ref: itemRef });
          closePop();
          toast('Added to “' + title + '”');
        } catch (e) {
          form.querySelector('button').disabled = false;
          (window.narveToastError || window.alert)(e.message);
        }
      });
    }
  }

  // ── Public entry point ──────────────────────────────────────────────────

  window.hbAddToCollection = function (itemType, itemRef, anchorEl) {
    if (!itemType || !itemRef) {
      console.warn('hbAddToCollection: itemType + itemRef required');
      return;
    }
    var existing = document.querySelector('.hbc-pop');
    if (existing) {
      // Toggle — a second click on the same anchor closes the popover.
      closePop();
      return;
    }
    var pop = document.createElement('div');
    pop.className = 'hbc-pop';
    pop.setAttribute('role', 'dialog');
    pop.setAttribute('aria-label', 'Add to collection');
    document.body.appendChild(pop);
    placePop(pop, anchorEl || null);
    // Wire dismiss handlers on next tick so the click that opened the
    // popover doesn't immediately close it.
    setTimeout(function () {
      document.addEventListener('click', _onDocClick, true);
      document.addEventListener('keydown', _onKey, true);
    }, 0);
    renderChoices(pop, itemType, itemRef);
  };

  // ── Auto-wire any [data-add-to-collection] buttons on the page ──────────
  //
  // Detail pages opt in by rendering a button like:
  //   <button data-add-to-collection
  //           data-item-type="market"
  //           data-item-ref="poly:fed-hold">+</button>
  //
  // The widget discovers them on DOMContentLoaded + whenever new ones are
  // appended (MutationObserver) so SPA-style content swaps keep working.

  function wireButtons(root) {
    (root || document).querySelectorAll('[data-add-to-collection]')
      .forEach(function (btn) {
        if (btn.dataset.hbcWired === '1') return;
        btn.dataset.hbcWired = '1';
        btn.addEventListener('click', function (ev) {
          ev.stopPropagation();
          window.hbAddToCollection(
            btn.dataset.itemType, btn.dataset.itemRef, btn
          );
        });
      });
  }

  if (document.readyState !== 'loading') {
    wireButtons(document);
  } else {
    document.addEventListener('DOMContentLoaded', function () {
      wireButtons(document);
    });
  }

  try {
    var obs = new MutationObserver(function (mutations) {
      for (var i = 0; i < mutations.length; i++) {
        if (mutations[i].addedNodes && mutations[i].addedNodes.length) {
          wireButtons(document);
          return;
        }
      }
    });
    obs.observe(document.documentElement, { childList: true, subtree: true });
  } catch (e) { /* MutationObserver unavailable — first-render wiring is enough */ }
})();
