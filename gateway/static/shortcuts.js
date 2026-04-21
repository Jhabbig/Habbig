/* shortcuts.js — keyboard shortcut registry + help overlay.
 *
 * Pages can register additional shortcuts via
 *     window.narve.shortcuts.register({...})
 * before or after this script loads. Everything is a no-op until the
 * user actually presses a key, so adding hooks is cheap.
 *
 * Design notes:
 *   - Shortcuts never fire while the user is typing in an editable field.
 *   - Conflicts are avoided by a single document-level listener that
 *     dispatches based on canonical key strings ("cmd+k", "g f", etc.).
 *   - G-then-X (Gmail-style) is supported with a 1s trailing timeout.
 *   - ⌘/ or ? opens the help overlay which lists every registered shortcut.
 */
(function () {
  'use strict';

  const narve = (window.narve = window.narve || {});

  const registry = []; // each: { id, keys, description, group, scope, handler }
  const byCanonicalKey = new Map(); // "cmd+k" → shortcut record

  function normaliseKey(key) {
    if (!key) return '';
    const parts = key
      .toLowerCase()
      .split('+')
      .map((p) => p.trim())
      .filter(Boolean);
    // Canonical modifier order: ctrl, alt, shift, cmd — then plain key last.
    const mods = { ctrl: false, alt: false, shift: false, cmd: false };
    let plain = '';
    for (const p of parts) {
      if (p === 'ctrl' || p === 'control') mods.ctrl = true;
      else if (p === 'alt' || p === 'option') mods.alt = true;
      else if (p === 'shift') mods.shift = true;
      else if (p === 'cmd' || p === 'meta' || p === 'command') mods.cmd = true;
      else plain = p;
    }
    return (
      (mods.ctrl ? 'ctrl+' : '') +
      (mods.alt ? 'alt+' : '') +
      (mods.shift ? 'shift+' : '') +
      (mods.cmd ? 'cmd+' : '') +
      plain
    );
  }

  function eventToKeyString(e) {
    const mods = [];
    if (e.ctrlKey) mods.push('ctrl');
    if (e.altKey) mods.push('alt');
    if (e.shiftKey) mods.push('shift');
    if (e.metaKey) mods.push('cmd');
    let k = e.key;
    if (k === ' ') k = 'space';
    else if (k === 'Escape') k = 'esc';
    else if (k === 'ArrowUp') k = 'up';
    else if (k === 'ArrowDown') k = 'down';
    else if (k === 'ArrowLeft') k = 'left';
    else if (k === 'ArrowRight') k = 'right';
    else if (k === 'Enter') k = 'enter';
    else k = k.length === 1 ? k.toLowerCase() : k.toLowerCase();
    return (mods.length ? mods.join('+') + '+' : '') + k;
  }

  function isTypingContext(target) {
    if (!target) return false;
    const tag = target.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return true;
    if (target.isContentEditable) return true;
    // Explicit opt-out: let pages mark containers so shortcuts still fire.
    const optOut = target.closest('[data-shortcuts-allow-typing]');
    return !optOut;
  }

  function register(spec) {
    // Spec: { id, keys (string or string[]), description, group?, scope?, handler, sequence?: string[][] }
    // sequence lets us express g-then-X: [['g','f'], ['g','b']] → ... not used; we take `keys: "g f"` instead.
    const rec = {
      id: spec.id || spec.keys + '-' + Math.random().toString(36).slice(2, 8),
      keys: Array.isArray(spec.keys) ? spec.keys : [spec.keys],
      description: spec.description || '',
      group: spec.group || 'General',
      scope: spec.scope || 'global',
      handler: spec.handler,
      sequence: null,
    };
    // Sequences are expressed as "g f" (space-separated). Single-shot
    // combos like "cmd+k" don't contain a space.
    for (const k of rec.keys) {
      if (k.includes(' ')) {
        rec.sequence = k.split(' ').map(normaliseKey);
      } else {
        byCanonicalKey.set(normaliseKey(k), rec);
      }
    }
    registry.push(rec);
    return () => {
      // Unregister
      const idx = registry.indexOf(rec);
      if (idx !== -1) registry.splice(idx, 1);
      for (const k of rec.keys) {
        if (!k.includes(' ')) byCanonicalKey.delete(normaliseKey(k));
      }
    };
  }

  // Sequence state: if we recently saw "g", we remember it and interpret
  // the next plain key as the second half of a g-then-X shortcut.
  let pendingPrefix = null;
  let pendingTimer = null;
  function armPrefix(key) {
    pendingPrefix = key;
    if (pendingTimer) clearTimeout(pendingTimer);
    pendingTimer = setTimeout(() => { pendingPrefix = null; }, 1000);
  }
  function clearPrefix() {
    pendingPrefix = null;
    if (pendingTimer) clearTimeout(pendingTimer);
    pendingTimer = null;
  }

  function findSequenceMatch(first, second) {
    for (const rec of registry) {
      if (rec.sequence && rec.sequence.length === 2 &&
          rec.sequence[0] === first && rec.sequence[1] === second) {
        return rec;
      }
    }
    return null;
  }

  function isAnyPrefix(key) {
    for (const rec of registry) {
      if (rec.sequence && rec.sequence[0] === key) return true;
    }
    return false;
  }

  document.addEventListener('keydown', (e) => {
    // Don't swallow keystrokes while the user is typing — but still let
    // Esc work inside inputs (close modal, blur) and ⌘/Ctrl combinations
    // (these are user-opt-in — modifier keys are rarely collisions).
    const typing = isTypingContext(e.target);
    const hasCmd = e.metaKey || e.ctrlKey;
    const canonical = eventToKeyString(e);
    const isEsc = canonical === 'esc';

    // Allow ⌘/ (help) and Esc even while typing.
    if (typing && !hasCmd && !isEsc) return;

    // Single-shot combo?
    const combo = byCanonicalKey.get(canonical);
    if (combo) {
      e.preventDefault();
      clearPrefix();
      try { combo.handler(e); } catch (err) { if (console) console.error(err); }
      return;
    }

    // Sequence in flight?
    if (pendingPrefix && !hasCmd) {
      const match = findSequenceMatch(pendingPrefix, canonical);
      clearPrefix();
      if (match) {
        e.preventDefault();
        try { match.handler(e); } catch (err) { if (console) console.error(err); }
      }
      return;
    }

    // New prefix?
    if (!hasCmd && isAnyPrefix(canonical)) {
      armPrefix(canonical);
    }
  });

  // ── Built-in shortcuts ─────────────────────────────────────────────

  // Help overlay
  const HELP_ID = 'narve-shortcut-help';
  function toggleHelp() {
    const existing = document.getElementById(HELP_ID);
    if (existing) { closeHelp(existing); return; }
    openHelp();
  }
  function groupEntries() {
    const by = new Map();
    for (const rec of registry) {
      if (!rec.description) continue;
      if (!by.has(rec.group)) by.set(rec.group, []);
      by.get(rec.group).push(rec);
    }
    return by;
  }
  function keysToHTML(keys) {
    return keys.map((k) => {
      const parts = k.split(' '); // sequence → e.g. ["g", "f"]
      return parts.map((p) =>
        '<kbd>' + p.split('+').map(prettify).join('</kbd><kbd>') + '</kbd>'
      ).join(' <span class="narve-sc-then">then</span> ');
    }).join('<span class="narve-sc-or"> or </span>');
  }
  function prettify(part) {
    const map = {
      cmd: '⌘', meta: '⌘', command: '⌘',
      ctrl: 'Ctrl', control: 'Ctrl',
      alt: 'Alt', option: '⌥',
      shift: '⇧',
      esc: 'Esc', enter: 'Enter', space: 'Space',
      up: '↑', down: '↓', left: '←', right: '→',
    };
    return map[part.toLowerCase()] || part.toUpperCase();
  }
  function openHelp() {
    const overlay = document.createElement('div');
    overlay.id = HELP_ID;
    overlay.className = 'narve-sc-overlay';
    overlay.setAttribute('role', 'dialog');
    overlay.setAttribute('aria-modal', 'true');
    overlay.setAttribute('aria-labelledby', 'narve-sc-title');
    let body = '';
    for (const [group, entries] of groupEntries()) {
      body += '<section class="narve-sc-section"><h3 class="narve-sc-heading">' +
        escapeHTML(group) + '</h3><dl class="narve-sc-list">';
      for (const rec of entries) {
        body += '<div class="narve-sc-row"><dt class="narve-sc-keys">' +
          keysToHTML(rec.keys) + '</dt><dd class="narve-sc-desc">' +
          escapeHTML(rec.description) + '</dd></div>';
      }
      body += '</dl></section>';
    }
    overlay.innerHTML =
      '<div class="narve-sc-backdrop" data-narve-sc-close></div>' +
      '<div class="narve-sc-panel" tabindex="-1">' +
        '<header class="narve-sc-header">' +
          '<h2 id="narve-sc-title" class="narve-sc-title">Keyboard shortcuts</h2>' +
          '<button type="button" class="narve-sc-close" data-narve-sc-close aria-label="Close">&times;</button>' +
        '</header>' +
        '<div class="narve-sc-body">' + body + '</div>' +
        '<footer class="narve-sc-footer">Press <kbd>Esc</kbd> to close</footer>' +
      '</div>';
    document.body.appendChild(overlay);
    overlay.addEventListener('click', (ev) => {
      if (ev.target.hasAttribute('data-narve-sc-close')) closeHelp(overlay);
    });
    const dispose = narve.trapFocus
      ? narve.trapFocus(overlay.querySelector('.narve-sc-panel'))
      : null;
    overlay._disposeTrap = dispose;
  }
  function closeHelp(overlay) {
    if (overlay._disposeTrap) overlay._disposeTrap();
    overlay.remove();
  }
  function escapeHTML(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  register({
    id: 'help',
    keys: ['cmd+/', '?'],
    description: 'Show keyboard shortcuts',
    group: 'General',
    handler: toggleHelp,
  });

  register({
    id: 'esc-help',
    keys: 'esc',
    description: '',
    group: 'General',
    handler: (e) => {
      const existing = document.getElementById(HELP_ID);
      if (existing) { closeHelp(existing); e.stopPropagation(); return; }
      // Fall through — other Esc handlers may still act.
    },
  });

  // Navigation shortcuts (g-then-X). Pages silently 404 if a route
  // doesn't exist for this user — better than a JS error.
  function go(path) { return () => { window.location.href = path; }; }
  register({ id: 'go-dashboards',   keys: 'g d', description: 'Go to Dashboards',    group: 'Navigation', handler: go('/dashboards') });
  register({ id: 'go-intelligence', keys: 'g i', description: 'Go to Intelligence',  group: 'Navigation', handler: go('/intelligence') });
  register({ id: 'go-search',       keys: 'g s', description: 'Go to Signal Search', group: 'Navigation', handler: go('/signal-search') });
  register({ id: 'go-billing',      keys: 'g b', description: 'Go to Billing',       group: 'Navigation', handler: go('/billing') });
  register({ id: 'go-settings',     keys: 'g t', description: 'Go to Settings',      group: 'Navigation', handler: go('/settings') });
  register({ id: 'go-profile',      keys: 'g p', description: 'Go to Profile',       group: 'Navigation', handler: go('/profile') });
  register({ id: 'go-saved',        keys: 'g v', description: 'Go to Saved',         group: 'Navigation', handler: go('/saved') });
  register({ id: 'go-home',         keys: 'g h', description: 'Go to Home',          group: 'Navigation', handler: go('/') });

  register({
    id: 'settings-comma',
    keys: 'cmd+,',
    description: 'Open settings',
    group: 'General',
    handler: go('/settings'),
  });

  // List navigation (j/k). Pages opt into this by setting
  // `data-shortcut-list="true"` on a container; items become
  // elements with `[data-shortcut-item]` inside. The currently
  // "selected" item gets `data-shortcut-selected`.
  function currentList() {
    return document.querySelector('[data-shortcut-list="true"]');
  }
  function currentItems(list) {
    return list ? Array.from(list.querySelectorAll('[data-shortcut-item]')) : [];
  }
  function currentIndex(items) {
    return items.findIndex((el) => el.hasAttribute('data-shortcut-selected'));
  }
  function selectIndex(items, i) {
    items.forEach((el) => el.removeAttribute('data-shortcut-selected'));
    const clamped = Math.max(0, Math.min(items.length - 1, i));
    const el = items[clamped];
    if (!el) return;
    el.setAttribute('data-shortcut-selected', '');
    if (typeof el.scrollIntoView === 'function') {
      el.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    }
    // Move keyboard focus too so screen readers announce it.
    if (el.tabIndex < 0) el.setAttribute('tabindex', '-1');
    el.focus({ preventScroll: true });
  }

  register({
    id: 'list-next', keys: 'j', description: 'Next item', group: 'List',
    handler: () => {
      const list = currentList(); if (!list) return;
      const items = currentItems(list); if (!items.length) return;
      const i = currentIndex(items);
      selectIndex(items, i < 0 ? 0 : i + 1);
    },
  });
  register({
    id: 'list-prev', keys: 'k', description: 'Previous item', group: 'List',
    handler: () => {
      const list = currentList(); if (!list) return;
      const items = currentItems(list); if (!items.length) return;
      const i = currentIndex(items);
      selectIndex(items, i < 0 ? 0 : i - 1);
    },
  });
  register({
    id: 'list-open', keys: ['enter', 'o'], description: 'Open selected', group: 'List',
    handler: () => {
      const list = currentList(); if (!list) return;
      const items = currentItems(list);
      const i = currentIndex(items);
      if (i >= 0) items[i].click();
    },
  });
  register({
    id: 'list-save', keys: 's', description: 'Save selected', group: 'List',
    handler: () => {
      const list = currentList(); if (!list) return;
      const items = currentItems(list);
      const i = currentIndex(items);
      if (i < 0) return;
      const saveBtn = items[i].querySelector('[data-shortcut-save]');
      if (saveBtn) saveBtn.click();
    },
  });

  // Focus the page's primary search input. Pages mark it with
  // `data-shortcut-search` (falls back to any <input type="search">).
  register({
    id: 'focus-search', keys: '/', description: 'Focus search', group: 'General',
    handler: (e) => {
      const el = document.querySelector('[data-shortcut-search]') ||
                 document.querySelector('input[type="search"]');
      if (el) { e.preventDefault(); el.focus(); el.select && el.select(); }
    },
  });

  // Command palette: opt-in — pages that have a palette hook it up
  // by setting window.narve.openCommandPalette = fn. Fallback: focus
  // the search input if nothing else claims ⌘K.
  register({
    id: 'cmd-palette', keys: ['cmd+k', 'ctrl+k'], description: 'Command palette / search',
    group: 'General',
    handler: (e) => {
      if (typeof narve.openCommandPalette === 'function') {
        narve.openCommandPalette(e);
        return;
      }
      const el = document.querySelector('[data-shortcut-search]') ||
                 document.querySelector('input[type="search"]');
      if (el) { el.focus(); el.select && el.select(); }
    },
  });

  // ── Expose API ─────────────────────────────────────────────────────
  narve.shortcuts = {
    register,
    list: () => registry.slice(),
    showHelp: () => { if (!document.getElementById(HELP_ID)) openHelp(); },
  };
})();
