// GDELT Monitor service worker — instant app-shell open via stale-while-revalidate.
// The shell (HTML + static assets) is served from cache immediately and refreshed
// in the background. API/data requests always go to the network (never stale).
const CACHE = 'gdelt-shell-v24';
const SHELL = [
  '/',
  '/static/favicon.svg', '/static/icon-192.png', '/static/icon-512.png',
  '/static/css/base.css', '/static/css/dashboard.css',
  '/static/js/rum.js', '/static/js/dashboard.js', '/static/js/markdown.js',
];

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);

  // Data + the SW itself: always network (fresh).
  if (url.pathname.startsWith('/api/') || url.pathname === '/sw.js') return;

  // Navigations: ONLY the root is cache-assisted; all other pages (/login,
  // /about, /portal, /event/...) go straight to the network — serving the
  // shell for every navigation once rendered the dashboard at /login AND
  // poisoned the '/' cache slot with whatever page came back.
  //
  // Root strategy: NETWORK-FIRST with a 1.5s cached fallback. The shell HTML
  // is ~100-300ms via Cloudflare, so this stays imperceptibly fast while
  // guaranteeing a stale/poisoned cached shell can never be shown when the
  // network is healthy. Cache is only a slow-network/offline fallback.
  if (req.mode === 'navigate') {
    if (url.pathname === '/') {
      e.respondWith((async () => {
        const cache = await caches.open(CACHE);
        const network = fetch(req).then((res) => {
          if (res && res.status === 200) cache.put('/', res.clone());
          return res;
        });
        try {
          return await Promise.race([
            network,
            new Promise((_, rej) => setTimeout(() => rej(new Error('shell-timeout')), 1500)),
          ]);
        } catch (_) {
          // Slow or offline: cached shell (background fetch keeps revalidating).
          const cached = await cache.match('/');
          return cached || network;
        }
      })());
    }
    return;
  }

  // Static assets: stale-while-revalidate.
  e.respondWith(
    caches.open(CACHE).then(async (cache) => {
      const cached = await cache.match(req);
      const network = fetch(req)
        .then((res) => { if (res && res.status === 200 && res.type === 'basic') cache.put(req, res.clone()); return res; })
        .catch(() => cached);
      return cached || network;
    })
  );
});
