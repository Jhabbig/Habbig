/* sample-feed.js — empty-dashboard seed for first-visit users.
 *
 * When a dashboard's feed container is empty AND the user hasn't opted
 * out, we pull /api/feed/sample (5 starter predictions) and render them
 * behind a "This is a sample view." banner. Disappears once the user
 * has ≥ 10 real items OR explicitly dismisses.
 *
 * Mount points:
 *   <div data-narve-sample-feed></div>          standalone container
 *   <div data-narve-sample-feed data-real-selector=".feed-item"></div>
 *                                                counts real items to
 *                                                decide whether to show
 *
 * Both attributes are honoured in order — if data-real-selector is
 * present we count matching elements first and only inject if the count
 * is below 10. Callers wanting pure "empty means show sample" behaviour
 * omit the attribute.
 *
 * Dismissal persists in localStorage under narve:sample-feed-dismissed.
 */
(function () {
  'use strict';

  const DISMISS_KEY = 'narve:sample-feed-dismissed';
  const REAL_ITEM_THRESHOLD = 10;

  function isDismissed() {
    try { return localStorage.getItem(DISMISS_KEY) === '1'; } catch { return false; }
  }

  function markDismissed() {
    try { localStorage.setItem(DISMISS_KEY, '1'); } catch {}
  }

  function countRealItems(target) {
    const sel = target.getAttribute('data-real-selector');
    if (!sel) return 0;
    try {
      return document.querySelectorAll(sel).length;
    } catch {
      return 0;
    }
  }

  function escapeHtml(s) {
    return (s == null ? '' : String(s)).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  function renderRow(p) {
    const dir = (p.direction || '').toUpperCase() === 'NO' ? 'NO' : 'YES';
    const edge = p.edge != null ? Math.round(p.edge * 100) + '% edge' : '';
    return [
      '<li class="narve-sample-row">',
        '<div class="narve-sample-row__main">',
          '<span class="narve-sample-row__source">@', escapeHtml(p.source_handle || 'source'), '</span>',
          ' · ',
          '<span class="narve-sample-row__claim">', escapeHtml(p.content || ''), '</span>',
        '</div>',
        '<div class="narve-sample-row__meta">',
          '<span class="narve-sample-row__dir">', dir, '</span>',
          edge ? ' · <span class="narve-sample-row__edge">' + escapeHtml(edge) + '</span>' : '',
          ' · <span class="narve-sample-row__cat">', escapeHtml(p.category || 'other'), '</span>',
        '</div>',
      '</li>',
    ].join('');
  }

  async function fetchSample() {
    try {
      const res = await fetch('/api/feed/sample', { credentials: 'same-origin' });
      if (!res.ok) return null;
      return await res.json();
    } catch {
      return null;
    }
  }

  function inject(target, payload) {
    if (!payload || !Array.isArray(payload.predictions) || !payload.predictions.length) return;
    const banner = document.createElement('aside');
    banner.className = 'narve-sample-feed';
    banner.setAttribute('role', 'region');
    banner.setAttribute('aria-label', 'Sample feed — fills in as you follow sources');
    banner.innerHTML = [
      '<div class="narve-sample-feed__head">',
        '<strong class="narve-sample-feed__title">Sample view</strong>',
        '<span class="narve-sample-feed__note">', escapeHtml(payload.note || 'This is a sample view. Your feed fills in as you follow sources.'), '</span>',
        '<button type="button" class="narve-sample-feed__close" aria-label="Dismiss sample view">×</button>',
      '</div>',
      '<ul class="narve-sample-feed__list">',
        payload.predictions.slice(0, 5).map(renderRow).join(''),
      '</ul>',
      '<div class="narve-sample-feed__cta">',
        '<a href="/onboarding" class="narve-sample-feed__link">Take the 90-second tour →</a>',
      '</div>',
    ].join('');
    target.appendChild(banner);
    const close = banner.querySelector('.narve-sample-feed__close');
    if (close) close.addEventListener('click', () => {
      markDismissed();
      banner.remove();
    });
  }

  async function hydrate() {
    if (isDismissed()) return;
    const targets = document.querySelectorAll('[data-narve-sample-feed]');
    for (const target of targets) {
      if (countRealItems(target) >= REAL_ITEM_THRESHOLD) continue;
      const payload = await fetchSample();
      inject(target, payload);
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', hydrate, { once: true });
  } else {
    hydrate();
  }

  // Expose for programmatic triggering (e.g., after the real feed
  // renders and the caller wants to conditionally show or hide).
  window.narveSampleFeed = {
    hydrate,
    dismiss: () => {
      markDismissed();
      document.querySelectorAll('.narve-sample-feed').forEach((el) => el.remove());
    },
    isDismissed,
  };
})();
