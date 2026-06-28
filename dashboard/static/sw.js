// GDELT Monitor service worker — instant app-shell open via stale-while-revalidate.
// The shell (HTML + static assets) is served from cache immediately and refreshed
// in the background. API/data requests always go to the network (never stale).
const CACHE = 'gdelt-shell-v2';
const SHELL = ['/', '/static/favicon.svg', '/static/icon-192.png', '/static/icon-512.png'];

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

  // Navigations: serve the cached shell instantly, revalidate in the background.
  if (req.mode === 'navigate') {
    e.respondWith(
      caches.open(CACHE).then(async (cache) => {
        const cached = await cache.match('/');
        const network = fetch(req)
          .then((res) => { if (res && res.status === 200) cache.put('/', res.clone()); return res; })
          .catch(() => cached);
        return cached || network;
      })
    );
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
