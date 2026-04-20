/**
 * background.js — Service Worker for narve.ai Chrome extension.
 *
 * Responsibilities:
 * - Handle API calls to narve.ai on behalf of content scripts (cross-origin)
 * - Store and manage extension JWT in chrome.storage.local
 * - Relay auth tokens from the /extension/auth page via message passing
 */

const NARVE_API = 'https://narve.ai';

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type === 'FETCH_MARKET_DATA') {
    fetchMarketData(message.marketSlug).then(sendResponse);
    return true; // keep sendResponse channel open for async
  }

  if (message.type === 'SAVE_AUTH') {
    chrome.storage.local.set({
      narve_jwt: message.jwt,
      narve_display_name: message.display_name || '',
      narve_tier: message.tier || '',
    });
    sendResponse({ saved: true });
    return false;
  }

  if (message.type === 'LOGOUT') {
    chrome.storage.local.remove(['narve_jwt', 'narve_display_name', 'narve_tier']);
    sendResponse({ done: true });
    return false;
  }

  if (message.type === 'GET_STATUS') {
    chrome.storage.local.get(['narve_jwt', 'narve_display_name', 'narve_tier'], (data) => {
      sendResponse({
        authenticated: !!data.narve_jwt,
        display_name: data.narve_display_name || '',
        tier: data.narve_tier || '',
      });
    });
    return true;
  }

  return false;
});


async function fetchMarketData(marketSlug) {
  const data = await chrome.storage.local.get('narve_jwt');
  const jwt = data.narve_jwt;

  if (!jwt) {
    return { error: 'not_authenticated' };
  }

  try {
    const resp = await fetch(
      `${NARVE_API}/api/extension/market/${encodeURIComponent(marketSlug)}`,
      {
        headers: { 'Authorization': `Bearer ${jwt}` },
      }
    );

    if (resp.status === 401) {
      // JWT expired or invalid — clear stored credentials so popup shows login
      await chrome.storage.local.remove(['narve_jwt', 'narve_display_name', 'narve_tier']);
      return { error: 'not_authenticated' };
    }

    if (resp.status === 403) {
      return { error: 'tier_required' };
    }

    if (resp.status === 404) {
      return { error: 'no_data' };
    }

    if (resp.status === 429) {
      return { error: 'rate_limited' };
    }

    if (!resp.ok) {
      return { error: 'api_error', status: resp.status };
    }

    return await resp.json();

  } catch (err) {
    return { error: 'network_error', message: err.message };
  }
}
