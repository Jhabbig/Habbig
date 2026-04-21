// Service worker for the narve.ai Polymarket extension.
//
// Responsibilities:
//   1. Hold the long-lived JWT issued by /extension/auth.
//   2. Answer getMarketBundle(slug) messages from content.js by calling
//      /api/extension/market/{slug} with the JWT.
//   3. Refresh the badge + last-auth timestamp the popup reads.
//
// We keep all network fan-out here (not in content.js) so the JWT
// never leaks into polymarket.com's page context.

const NARVE_API = "https://narve.ai";
const JWT_KEY = "narve_jwt";
const JWT_EXPIRES_KEY = "narve_jwt_expires_at";

async function getJwt() {
  const store = await chrome.storage.local.get([JWT_KEY, JWT_EXPIRES_KEY]);
  const jwt = store[JWT_KEY];
  const expiresAt = store[JWT_EXPIRES_KEY] || 0;
  if (!jwt) return null;
  // 60s grace period — if the token expires in the next minute, treat
  // as missing so the popup can prompt a re-auth before the server 401s.
  if (Date.now() / 1000 > expiresAt - 60) return null;
  return jwt;
}

async function setJwt(jwt, expiresAtSeconds) {
  await chrome.storage.local.set({
    [JWT_KEY]: jwt,
    [JWT_EXPIRES_KEY]: expiresAtSeconds,
  });
}

async function fetchMarketBundle(slug) {
  const jwt = await getJwt();
  if (!jwt) return { error: "not_authenticated" };
  const url = `${NARVE_API}/api/extension/market/${encodeURIComponent(slug)}`;
  try {
    const resp = await fetch(url, {
      headers: { Authorization: `Bearer ${jwt}` },
    });
    if (resp.status === 401) return { error: "not_authenticated" };
    if (resp.status === 429) return { error: "rate_limited" };
    if (!resp.ok) return { error: `http_${resp.status}` };
    return { bundle: await resp.json() };
  } catch (e) {
    return { error: `fetch_failed: ${e && e.message || e}` };
  }
}

// Content script messaging. Every slug lookup goes through here so
// retries + auth flow live in one place.
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg && msg.type === "getMarketBundle" && typeof msg.slug === "string") {
    fetchMarketBundle(msg.slug).then(sendResponse);
    return true; // keep the message channel open for the async reply
  }
  if (msg && msg.type === "setJwt" && msg.jwt && msg.expires_at) {
    setJwt(msg.jwt, msg.expires_at).then(() => sendResponse({ ok: true }));
    return true;
  }
  if (msg && msg.type === "getStatus") {
    getJwt().then((jwt) =>
      sendResponse({ authenticated: Boolean(jwt) }),
    );
    return true;
  }
  return false;
});

// /extension/auth posts the JWT via window.postMessage → content.js ×
// externallyConnectable path. We also accept it directly via
// onMessageExternal when the narve.ai tab connects after login.
chrome.runtime.onMessageExternal.addListener((msg, sender, sendResponse) => {
  const originOk = sender && sender.url &&
    sender.url.startsWith("https://narve.ai/");
  if (!originOk) return false;
  if (msg && msg.type === "setJwt" && msg.jwt && msg.expires_at) {
    setJwt(msg.jwt, msg.expires_at).then(() => sendResponse({ ok: true }));
    return true;
  }
  return false;
});
