// Service worker for the QR scanner, served from /scanner/sw.js so its scope
// covers /scanner/. It lets the scanner page open with no wi-fi: the page is
// network-first with the last good copy as a fallback, and the scripts,
// styles and fonts it needs are served from cache. Scans themselves are never
// cached here; offline scans wait in the page's localStorage outbox.

const CACHE = 'scanner-v1';

const PRECACHE = [
    '/static/scanner/scanner.js',
    '/static/scanner/qr-scanner.min.js',
    '/static/scanner/qr-scanner-worker.min.js',
    '/static/images/logo.png',
    'https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css',
    'https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/js/bootstrap.bundle.min.js',
];

self.addEventListener('install', (event) => {
    event.waitUntil(
        caches.open(CACHE)
            // One missing file must not stop the rest from being cached.
            .then((cache) => Promise.all(PRECACHE.map((url) => cache.add(url).catch(() => {}))))
            .then(() => self.skipWaiting())
    );
});

self.addEventListener('activate', (event) => {
    event.waitUntil(
        caches.keys()
            .then((keys) => Promise.all(keys.filter((k) => k.startsWith('scanner-') && k !== CACHE).map((k) => caches.delete(k))))
            .then(() => self.clients.claim())
    );
});

function isScannerPage(request, url) {
    return request.mode === 'navigate' && url.origin === self.location.origin && url.pathname === '/scanner/';
}

function isAsset(url) {
    return url.pathname.startsWith('/static/') ||
           url.hostname === 'cdn.jsdelivr.net' ||
           url.hostname === 'fonts.googleapis.com' ||
           url.hostname === 'fonts.gstatic.com';
}

self.addEventListener('fetch', (event) => {
    const request = event.request;
    if (request.method !== 'GET') return;
    const url = new URL(request.url);

    if (isScannerPage(request, url)) {
        // Network first, so a signed-in, up-to-date page wins whenever there is
        // a connection. Only a real page is stored, never a login redirect.
        event.respondWith(
            fetch(request)
                .then((response) => {
                    if (response.ok && !response.redirected) {
                        const copy = response.clone();
                        caches.open(CACHE).then((cache) => cache.put('/scanner/', copy));
                    }
                    return response;
                })
                .catch(() => caches.match('/scanner/').then((hit) => hit || Response.error()))
        );
        return;
    }

    if (isAsset(url)) {
        // Stale-while-revalidate: answer from cache, refresh it in the background.
        event.respondWith(
            caches.open(CACHE).then((cache) =>
                cache.match(request).then((hit) => {
                    const network = fetch(request)
                        .then((response) => {
                            if (response.ok || response.type === 'opaque') cache.put(request, response.clone());
                            return response;
                        })
                        .catch(() => hit);
                    return hit || network;
                })
            )
        );
    }
});
