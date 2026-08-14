// Lightweight service worker for the midterm dashboard.
//
// Strategy:
//   * Static assets under /assets are cache-first (Vite hashes filenames so
//     they're immutable — caching them indefinitely is safe).
//   * Same-origin /data/* JSON requests are network-first with a stale-while-
//     revalidate fallback so race-night users see fresh odds when online but
//     keep working on flaky connections.
//   * Everything else goes straight to the network.
//
// The cache is versioned via CACHE_VERSION; bumping it invalidates everything.
const CACHE_VERSION = 'v1'
const STATIC_CACHE = `midterm-static-${CACHE_VERSION}`
const DATA_CACHE = `midterm-data-${CACHE_VERSION}`

self.addEventListener('install', (event) => {
  self.skipWaiting()
})

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((k) => k !== STATIC_CACHE && k !== DATA_CACHE)
          .map((k) => caches.delete(k))
      )
    )
  )
  self.clients.claim()
})

self.addEventListener('fetch', (event) => {
  const req = event.request
  if (req.method !== 'GET') return
  const url = new URL(req.url)
  if (url.origin !== self.location.origin) return

  // Skip the SSE stream — must always hit the network.
  if (url.pathname.startsWith('/data/stream')) return

  if (url.pathname.startsWith('/assets/')) {
    event.respondWith(cacheFirst(req, STATIC_CACHE))
    return
  }

  if (url.pathname.startsWith('/data/')) {
    event.respondWith(networkFirst(req, DATA_CACHE))
    return
  }
})

async function cacheFirst(req, cacheName) {
  const cache = await caches.open(cacheName)
  const cached = await cache.match(req)
  if (cached) return cached
  try {
    const resp = await fetch(req)
    if (resp.ok) cache.put(req, resp.clone())
    return resp
  } catch (e) {
    return cached || Response.error()
  }
}

async function networkFirst(req, cacheName) {
  const cache = await caches.open(cacheName)
  try {
    const resp = await fetch(req)
    if (resp.ok) cache.put(req, resp.clone())
    return resp
  } catch (e) {
    const cached = await cache.match(req)
    if (cached) return cached
    throw e
  }
}
