// AKDIVEX Trading Journal — Service Worker
// v7: درخواست‌های غیرهم‌مبدأ (TradingView، APIهای قیمت و …) دیگر از SW عبور نمی‌کنند تا سریع‌تر بیایند؛
//     فقط فونت‌ها و فایل‌های خودِ برنامه کش می‌شوند.
const CACHE_NAME = 'akdivex-cache-v7';
const APP_SHELL = [
  './',
  './index.html',
  './manifest.json',
  './icon-192.png',
  './icon-512.png',
  './logo.png'
];

self.addEventListener('install', (event) => {
  // هر فایل جداگانه کش می‌شود تا نبودِ یک آیکون کل نصب را خراب نکند
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) =>
      Promise.all(APP_SHELL.map((url) => cache.add(url).catch(() => {})))
    )
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

const FONT_HOSTS = ['fonts.googleapis.com', 'fonts.gstatic.com'];

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);

  // درخواست‌های بیرونی به‌جز فونت‌ها (ویجت TradingView، Binance، CoinGecko، …) را به مرورگر می‌سپاریم:
  // هم واسطه‌ی SW حذف می‌شود و هم پاسخ‌های زنده (قیمت/اخبار) هیچ‌وقت قدیمی از کش برنگردانده می‌شوند.
  if (url.origin !== self.location.origin && !FONT_HOSTS.includes(url.hostname)) return;

  // Network-first for the HTML shell so users get the latest version when online,
  // falling back to cache when offline.
  if (req.mode === 'navigate' || (req.headers.get('accept') || '').includes('text/html')) {
    event.respondWith(
      fetch(req)
        .then((res) => {
          const copy = res.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(req, copy));
          return res;
        })
        .catch(() => caches.match(req).then((res) => res || caches.match('./index.html')))
    );
    return;
  }

  // Fonts: serve from cache instantly, refresh in the background.
  if (FONT_HOSTS.includes(url.hostname)) {
    event.respondWith(
      caches.open(CACHE_NAME).then((cache) =>
        cache.match(req).then((cached) => {
          const network = fetch(req)
            .then((res) => {
              if (res && (res.status === 200 || res.type === 'opaque')) cache.put(req, res.clone());
              return res;
            })
            .catch(() => cached);
          return cached || network;
        })
      )
    );
    return;
  }

  // Cache-first for other static assets (icons, etc.)
  event.respondWith(
    caches.match(req).then((cached) => {
      if (cached) return cached;
      return fetch(req)
        .then((res) => {
          if (res && res.status === 200 && res.type !== 'opaque') {
            const copy = res.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(req, copy));
          }
          return res;
        })
        .catch(() => cached);
    })
  );
});
