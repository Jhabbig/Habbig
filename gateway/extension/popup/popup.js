/**
 * popup.js — Extension popup UI logic.
 *
 * States:
 *   1. Not authenticated → "Connect to narve.ai" button
 *   2. Authenticated → show display name, tier, settings, disconnect
 */

const content = document.getElementById('content');

function esc(s) {
  if (s == null) return '';
  const d = document.createElement('div');
  d.textContent = String(s);
  return d.innerHTML;
}

function renderLoading() {
  content.innerHTML = '<p class="status">Checking connection…</p>';
}

function renderUnauthenticated() {
  content.innerHTML = `
    <p class="status">Not connected to narve.ai</p>
    <button class="btn" id="connect-btn">Connect to narve.ai</button>
    <p class="hint">You'll be asked to sign in on narve.ai, then the extension will connect automatically.</p>
  `;
  document.getElementById('connect-btn').addEventListener('click', () => {
    chrome.tabs.create({ url: 'https://narve.ai/extension/auth' });
  });
}

function renderAuthenticated(displayName, tier) {
  const tierLabel = tier === 'admin' ? 'Admin' :
                    tier === 'pro' ? 'Pro' :
                    tier === 'trader' ? 'Trader' : 'Free';
  content.innerHTML = `
    <div class="user-info">
      <div class="user-name">${esc(displayName)}</div>
      <div class="user-tier">${esc(tierLabel)}</div>
    </div>
    <div class="settings">
      <div class="setting-row">
        <span>Show overlay on Polymarket</span>
        <input type="checkbox" id="overlay-toggle" checked>
      </div>
    </div>
    <button class="btn btn-secondary" id="disconnect-btn">Disconnect</button>
  `;
  document.getElementById('disconnect-btn').addEventListener('click', () => {
    chrome.runtime.sendMessage({ type: 'LOGOUT' }, () => {
      renderUnauthenticated();
    });
  });

  // Overlay toggle persistence
  chrome.storage.local.get('narve_overlay_enabled', (data) => {
    const toggle = document.getElementById('overlay-toggle');
    if (toggle) toggle.checked = data.narve_overlay_enabled !== false;
  });
  document.getElementById('overlay-toggle')?.addEventListener('change', (e) => {
    chrome.storage.local.set({ narve_overlay_enabled: e.target.checked });
  });
}

// Check auth status on popup open
renderLoading();
chrome.runtime.sendMessage({ type: 'GET_STATUS' }, (status) => {
  if (status && status.authenticated) {
    renderAuthenticated(status.display_name, status.tier);
  } else {
    renderUnauthenticated();
  }
});
