/* First-week goals widget.
 *
 * Mounts itself into `[data-fw-goals-widget]` on the dashboard shell and
 * polls /api/first-week/goals on load (+ listens for the custom event
 * `narve:goal-completed` which other features emit when a goal ships).
 *
 * Auto-hides when any of:
 *   - completed_count === total
 *   - dismissed === true
 *   - days_since_signup >= 14
 *
 * Styling is entirely inline so the widget slots into any dashboard
 * without requiring a stylesheet update. Monochrome — no accent colours.
 */

(function () {
  'use strict';

  const ENDPOINT = '/api/first-week/goals';
  const DISMISS_ENDPOINT = '/api/first-week/widget/dismiss';

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    if (attrs) {
      for (const k in attrs) {
        if (k === 'style') Object.assign(node.style, attrs[k]);
        else if (k.startsWith('on') && typeof attrs[k] === 'function') {
          node.addEventListener(k.slice(2).toLowerCase(), attrs[k]);
        } else if (k === 'class') {
          node.className = attrs[k];
        } else {
          node.setAttribute(k, attrs[k]);
        }
      }
    }
    (children || []).forEach(c => {
      if (c == null) return;
      node.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
    });
    return node;
  }

  function render(target, payload) {
    target.innerHTML = '';
    if (payload.hide_widget) {
      target.style.display = 'none';
      return;
    }
    target.style.display = '';

    const { goals, completed_count, total } = payload;

    const wrap = el('div', {
      style: {
        background: 'var(--bg-surface)',
        border: '1px solid var(--border-default)',
        borderRadius: '10px',
        padding: '16px 18px',
        marginBottom: '16px',
        fontFamily: 'var(--font-ui)',
        color: 'var(--text-primary)',
        fontSize: '13px',
      },
    });

    const header = el('div', {
      style: {
        display: 'flex',
        justifyContent: 'space-between',
        alignItems: 'center',
        marginBottom: '12px',
      },
    }, [
      el('strong', {
        style: {
          fontFamily: 'var(--font-display)',
          fontSize: '15px',
          fontWeight: '500',
        },
      }, [`Getting started (${completed_count}/${total})`]),
      el('button', {
        class: 'fw-dismiss',
        title: 'Dismiss',
        style: {
          background: 'none',
          border: 'none',
          color: 'var(--text-tertiary)',
          cursor: 'pointer',
          fontSize: '18px',
          padding: '0 4px',
          lineHeight: '1',
        },
        onclick: dismiss,
      }, ['×']),
    ]);
    wrap.appendChild(header);

    // Toggle button for collapse/expand.
    const list = el('ul', {
      style: {
        listStyle: 'none',
        padding: '0',
        margin: '0',
      },
    });
    (goals || []).forEach(g => {
      const item = el('li', {
        style: {
          display: 'flex',
          alignItems: 'center',
          gap: '8px',
          padding: '6px 0',
          color: g.completed ? 'var(--text-primary)' : 'var(--text-secondary)',
        },
      }, [
        el('span', {
          style: {
            display: 'inline-block',
            width: '16px',
            fontFamily: 'var(--font-mono)',
            fontSize: '13px',
          },
        }, [g.completed ? '✓' : '·']),
        el('span', {
          style: {
            textDecoration: g.completed ? 'line-through' : 'none',
            opacity: g.completed ? '0.7' : '1',
          },
        }, [g.label]),
      ]);
      list.appendChild(item);
    });
    wrap.appendChild(list);
    target.appendChild(wrap);
  }

  async function load() {
    const target = document.querySelector('[data-fw-goals-widget]');
    if (!target) return;
    try {
      const res = await fetch(ENDPOINT, { credentials: 'same-origin' });
      if (!res.ok) {
        target.style.display = 'none';
        return;
      }
      const payload = await res.json();
      render(target, payload);
    } catch (err) {
      target.style.display = 'none';
    }
  }

  async function dismiss() {
    const target = document.querySelector('[data-fw-goals-widget]');
    if (!target) return;
    try {
      // Include CSRF token if the page provides one.
      const csrf = document.querySelector('meta[name="csrf-token"]');
      await fetch(DISMISS_ENDPOINT, {
        method: 'POST',
        credentials: 'same-origin',
        headers: csrf ? { 'X-CSRF-Token': csrf.getAttribute('content') } : {},
      });
    } catch (_) { /* silent — cosmetic feature */ }
    target.style.display = 'none';
  }

  document.addEventListener('DOMContentLoaded', load);
  window.addEventListener('narve:goal-completed', load);

  // Expose a tiny API for other features to mark a goal.
  window.narveFirstWeek = {
    markGoal: async function (key) {
      try {
        const csrf = document.querySelector('meta[name="csrf-token"]');
        await fetch(`/api/first-week/goals/${encodeURIComponent(key)}`, {
          method: 'POST',
          credentials: 'same-origin',
          headers: csrf ? { 'X-CSRF-Token': csrf.getAttribute('content') } : {},
        });
        window.dispatchEvent(new CustomEvent('narve:goal-completed', { detail: { key } }));
      } catch (_) { /* silent */ }
    },
    refresh: load,
  };
})();
