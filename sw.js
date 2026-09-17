/* Visa Bulletin tracker service worker.
 *
 * Caching strategy, and why:
 *
 *   shell (html, css-in-html, icons, manifest)  cache-first, precached
 *   data/*.json                                 network-first, cache fallback
 *
 * The data must be network-first. A tracker that cheerfully serves a
 * three-week-old cutoff date from cache, with no indication it is stale, is
 * worse than one that fails to load -- so the network always gets first
 * refusal, and the cached copy is a fallback with the page's own freshness
 * indicator telling the reader how old it is.
 */

const VERSION = "v1";
const SHELL = `shell-${VERSION}`;
const DATA = `data-${VERSION}`;

const SHELL_ASSETS = [
  "./",
  "./index.html",
  "./manifest.webmanifest",
  "./icon-192.png",
  "./icon-512.png",
  "./apple-touch-icon.png",
];

self.addEventListener("install", event => {
  event.waitUntil(
    caches.open(SHELL)
      // Individually, so one 404 does not fail the whole install.
      .then(c => Promise.allSettled(SHELL_ASSETS.map(a => c.add(a))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(
        keys.filter(k => k !== SHELL && k !== DATA).map(k => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  );
});

const isData = url => url.pathname.includes("/data/") && url.pathname.endsWith(".json");

self.addEventListener("fetch", event => {
  const req = event.request;
  if (req.method !== "GET") return;

  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  if (isData(url)) {
    event.respondWith(
      fetch(req)
        .then(res => {
          if (res && res.ok) {
            const copy = res.clone();
            caches.open(DATA).then(c => c.put(req, copy));
          }
          return res;
        })
        .catch(() => caches.match(req).then(hit => hit || Response.json(
          { offline: true }, { status: 503 }
        )))
    );
    return;
  }

  // Navigations: try the network so a redeploy is picked up, fall back to
  // the cached shell when offline.
  if (req.mode === "navigate") {
    event.respondWith(
      fetch(req).catch(() => caches.match("./index.html").then(h => h || caches.match("./")))
    );
    return;
  }

  event.respondWith(
    caches.match(req).then(hit => hit || fetch(req).then(res => {
      if (res && res.ok && res.type === "basic") {
        const copy = res.clone();
        caches.open(SHELL).then(c => c.put(req, copy));
      }
      return res;
    }))
  );
});

/* Push (P2b). The transport is not wired up yet -- no subscriptions are
 * stored anywhere -- but the handler is here so that enabling it later is a
 * server-side change only. */
self.addEventListener("push", event => {
  let payload = {};
  try { payload = event.data ? event.data.json() : {}; } catch (e) { /* plain text */ }
  const title = payload.title || "New Visa Bulletin";
  event.waitUntil(self.registration.showNotification(title, {
    body: payload.body || "A new bulletin has been published.",
    icon: "./icon-192.png",
    badge: "./icon-192.png",
    tag: payload.tag || "visa-bulletin",
    renotify: true,
    data: { url: payload.url || "./" },
  }));
});

self.addEventListener("notificationclick", event => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || "./";
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(list => {
      for (const c of list) {
        if ("focus" in c) { c.navigate(target); return c.focus(); }
      }
      return self.clients.openWindow(target);
    })
  );
});
