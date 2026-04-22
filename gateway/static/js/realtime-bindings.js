/* realtime-bindings.js
 *
 * Wires the shared ``window.rt`` client into the page-level UI. Each
 * page declares which channels it cares about via a data-realtime-*
 * attribute on <body> or a meta tag; this script subscribes and routes
 * messages to whatever handlers the page exposes (pushNotification,
 * prependToFeed, updatePriceChart, addPredictionToList).
 *
 * Decoupling matters: a dashboard JS bundle can be lazy-loaded without
 * this file; when it shows up, it just overrides ``window.handleRtEvent``
 * and the messages start flowing. Pages with no handlers get silent
 * delivery and zero errors.
 *
 * Expected declarations:
 *   <body data-realtime-market="poly:fed-rate-march">       — market detail
 *   <body data-realtime-feed="global">                      — feed page
 *   <body data-realtime-user="{{ user_id }}">               — notification bell
 *   <body data-realtime-admin-security="1">                 — admin security
 *   <body data-realtime-subproduct="trading-intel">         — subproduct
 */
(function () {
  "use strict";

  if (!window.rt) return;          // realtime.js didn't load — nothing to bind.

  const body = document.body;
  if (!body) return;

  // Small helpers so page code can override without knowing the channel name.
  function call(name, payload) {
    const fn = window[name];
    if (typeof fn === "function") {
      try { fn(payload); } catch (err) {
        // Log to console so a per-page handler bug doesn't tear down the tab.
        if (window.console && console.error) console.error(name, err);
      }
    }
  }

  // Normalise every envelope to {type, …payload}. Pages only care about type.
  function handle(envelope) {
    if (!envelope || typeof envelope !== "object") return;
    const type = envelope.type;
    if (!type) return;
    switch (type) {
      case "new_prediction":
        call("prependToFeed", envelope);
        call("addPredictionToList", envelope);
        break;
      case "price_tick":
        call("updatePriceChart", envelope);
        break;
      case "credibility_update":
        call("updateCredibility", envelope);
        break;
      case "notification":
        call("showNotification", envelope);
        break;
      case "capture_attempt":
        call("showCaptureAttempt", envelope);
        break;
      default:
        call("handleRtEvent", envelope);
    }
  }

  // ── Channel subscriptions from data attributes ──────────────────────

  const marketSlug = body.dataset.realtimeMarket;
  if (marketSlug) rt.subscribe("market:" + marketSlug, handle);

  const feedScope = body.dataset.realtimeFeed;
  if (feedScope === "global") rt.subscribe("feed:global", handle);

  const userId = body.dataset.realtimeUser;
  if (userId && /^\d+$/.test(userId)) rt.subscribe("user:" + userId, handle);

  if (body.dataset.realtimeAdminSecurity === "1") {
    rt.subscribe("admin:security", handle);
  }

  const subproductSlug = body.dataset.realtimeSubproduct;
  if (subproductSlug) rt.subscribe("subproduct:" + subproductSlug, handle);

  // Expose a tiny helper for per-page on-demand subscription (e.g. a
  // chart that only boots after the user clicks "expand").
  window.rtBindings = {
    subscribeMarket: (slug) => rt.subscribe("market:" + slug, handle),
    subscribeFeed: () => rt.subscribe("feed:global", handle),
    subscribeUser: (uid) => rt.subscribe("user:" + uid, handle),
    handle,
  };
})();
