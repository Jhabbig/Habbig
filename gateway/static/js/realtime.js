/* narve.ai realtime client.
 *
 * Single shared WebSocket connection for every live-update subscription
 * on the page. Auto-reconnects with exponential backoff (1s → 30s cap).
 * Heartbeat every 30s so proxies don't idle-kill the connection.
 *
 * Usage:
 *   window.rt.subscribe("market:poly:fed-rate-march", (msg) => { ... });
 *   const unsub = window.rt.subscribe("feed:global", fn);
 *   unsub();                       // remove this one listener
 *
 * The client resubscribes to every channel in `this.subscriptions` on
 * reconnect, so listeners don't need to re-register after a drop.
 */
(function () {
  "use strict";

  const INITIAL_BACKOFF_MS = 1000;
  const MAX_BACKOFF_MS = 30000;
  const HEARTBEAT_MS = 30000;
  const READY_OPEN = 1;

  class RealtimeClient {
    constructor() {
      this.ws = null;
      this.subscriptions = new Set();             // channels we've asked to subscribe to
      this.listeners = new Map();                 // channel -> Set<fn>
      this.globalListeners = new Set();           // called on every envelope (debug)
      this.reconnectDelay = INITIAL_BACKOFF_MS;
      this.backoffTimer = null;
      this.heartbeatTimer = null;
      this.isConnecting = false;
      this.serverLimits = null;                   // populated from hello envelope
      this.lastEventAt = 0;
    }

    connect() {
      if (this.isConnecting || (this.ws && this.ws.readyState <= READY_OPEN)) return;
      this.isConnecting = true;
      const proto = location.protocol === "https:" ? "wss:" : "ws:";
      const url = `${proto}//${location.host}/ws`;
      try {
        this.ws = new WebSocket(url);
      } catch (err) {
        this.isConnecting = false;
        this._scheduleReconnect();
        return;
      }

      this.ws.addEventListener("open", () => {
        this.isConnecting = false;
        this.reconnectDelay = INITIAL_BACKOFF_MS;
        // Resubscribe to every channel we know about — handles the case
        // where we're reconnecting after a drop.
        this.subscriptions.forEach((ch) => {
          try {
            this.ws.send(JSON.stringify({ op: "subscribe", channel: ch }));
          } catch (_) { /* will resurface on next reconnect */ }
        });
        this._startHeartbeat();
      });

      this.ws.addEventListener("message", (event) => {
        let envelope;
        try {
          envelope = JSON.parse(event.data);
        } catch (_) { return; }
        this.lastEventAt = Date.now();
        this._dispatch(envelope);
      });

      this.ws.addEventListener("close", (event) => {
        this.isConnecting = false;
        this._stopHeartbeat();
        // 4401 is our auth-required close; no point reconnecting without
        // new credentials.
        if (event.code === 4401) return;
        this._scheduleReconnect();
      });

      this.ws.addEventListener("error", () => {
        // The close handler will run right after; let it schedule the retry.
        this.isConnecting = false;
      });
    }

    /**
     * Subscribe a listener to ``channel``. Returns an unsubscribe function.
     * If the channel already has other listeners, this call is additive —
     * it doesn't send a fresh subscribe frame to the server.
     */
    subscribe(channel, listener) {
      if (typeof listener !== "function") {
        throw new TypeError("subscribe requires a listener function");
      }
      if (!this.listeners.has(channel)) this.listeners.set(channel, new Set());
      this.listeners.get(channel).add(listener);

      const wasNew = !this.subscriptions.has(channel);
      this.subscriptions.add(channel);
      if (wasNew && this.ws && this.ws.readyState === READY_OPEN) {
        try {
          this.ws.send(JSON.stringify({ op: "subscribe", channel }));
        } catch (_) { /* will resubscribe on reconnect */ }
      }
      return () => this._removeListener(channel, listener);
    }

    onMessage(listener) {
      this.globalListeners.add(listener);
      return () => this.globalListeners.delete(listener);
    }

    unsubscribe(channel) {
      this.subscriptions.delete(channel);
      this.listeners.delete(channel);
      if (this.ws && this.ws.readyState === READY_OPEN) {
        try {
          this.ws.send(JSON.stringify({ op: "unsubscribe", channel }));
        } catch (_) { /* best-effort */ }
      }
    }

    // ── internal ─────────────────────────────────────────────────────

    _removeListener(channel, listener) {
      const set = this.listeners.get(channel);
      if (!set) return;
      set.delete(listener);
      if (set.size === 0) {
        this.listeners.delete(channel);
        this.subscriptions.delete(channel);
        if (this.ws && this.ws.readyState === READY_OPEN) {
          try {
            this.ws.send(JSON.stringify({ op: "unsubscribe", channel }));
          } catch (_) { /* best-effort */ }
        }
      }
    }

    _dispatch(envelope) {
      // Capture server-provided limits on the hello message so UIs can
      // warn before clamping.
      if (envelope && envelope.op === "hello" && envelope.limits) {
        this.serverLimits = envelope.limits;
      }
      this.globalListeners.forEach((fn) => {
        try { fn(envelope); } catch (_) { /* listener bugs shouldn't kill dispatch */ }
      });
      const channel = envelope && envelope.channel;
      if (!channel) return;
      const set = this.listeners.get(channel);
      if (!set) return;
      set.forEach((fn) => {
        try { fn(envelope); } catch (_) { /* ditto */ }
      });
    }

    _scheduleReconnect() {
      clearTimeout(this.backoffTimer);
      const delay = this.reconnectDelay;
      this.backoffTimer = setTimeout(() => this.connect(), delay);
      this.reconnectDelay = Math.min(this.reconnectDelay * 2, MAX_BACKOFF_MS);
    }

    _startHeartbeat() {
      this._stopHeartbeat();
      this.heartbeatTimer = setInterval(() => {
        if (this.ws && this.ws.readyState === READY_OPEN) {
          try { this.ws.send('{"op":"ping"}'); } catch (_) { /* socket will close */ }
        }
      }, HEARTBEAT_MS);
    }

    _stopHeartbeat() {
      if (this.heartbeatTimer) {
        clearInterval(this.heartbeatTimer);
        this.heartbeatTimer = null;
      }
    }
  }

  const rt = new RealtimeClient();
  window.rt = rt;

  // Auto-connect on first script load unless the document opts out with
  // <meta name="realtime-autoconnect" content="off"> — useful for the
  // realtime-admin page which wants to control connect timing itself.
  const optOut = document.querySelector('meta[name="realtime-autoconnect"][content="off"]');
  if (!optOut) {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", () => rt.connect());
    } else {
      rt.connect();
    }
  }
})();
