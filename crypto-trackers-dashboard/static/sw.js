// Crypto Trackers service worker (minimal).
// Cache-then-network for the shell HTML, network-first for API calls.

const CACHE_NAME = "ct-v1";
const PRECACHE = ["/", "/coin"];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(PRECACHE).catch(() => {}))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  // Network-first for API + websocket-ish; cache HTML shell as fallback.
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/ws/")) {
    return;
  }
  event.respondWith(
    fetch(req).then((res) => {
      const copy = res.clone();
      caches.open(CACHE_NAME).then((c) => c.put(req, copy).catch(() => {}));
      return res;
    }).catch(() => caches.match(req).then((r) => r || new Response("offline", { status: 503 })))
  );
});

// Push notification support — payload is JSON {title, body, url?}
self.addEventListener("push", (event) => {
  if (!event.data) return;
  let payload;
  try { payload = event.data.json(); }
  catch { payload = { title: "Crypto Trackers", body: event.data.text() }; }
  event.waitUntil(
    self.registration.showNotification(payload.title || "Crypto Trackers", {
      body: payload.body || "",
      icon: "/static/icon.svg",
      badge: "/static/icon.svg",
      data: { url: payload.url || "/" },
    })
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(self.clients.openWindow(event.notification.data?.url || "/"));
});
