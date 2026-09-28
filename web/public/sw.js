// Kestrel's service worker. Two jobs, deliberately small:
//
// 1. Cache the app shell so the PWA still *opens* with no network, well
//    enough to show a clear "can't reach Kestrel" state rather than the
//    browser's own offline page. Nothing else is cached - an API response is
//    never served stale, because a dead server must never look alive.
// 2. Handle `push` and `notificationclick` for Web Push (kestrel.push on the
//    server side), so a notification arrives with the app fully closed.

const SHELL_CACHE = "kestrel-shell-v1";
const SHELL_URLS = ["/", "/manifest.webmanifest"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(SHELL_CACHE).then((cache) => cache.addAll(SHELL_URLS)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== SHELL_CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const { request } = event;
  // Only navigations fall back to the cached shell. Every API call - proxied
  // in dev, direct to the tailnet host in production - always hits the
  // network and is never intercepted here.
  if (request.method !== "GET" || request.mode !== "navigate") return;
  event.respondWith(fetch(request).catch(() => caches.match("/")));
});

self.addEventListener("push", (event) => {
  let payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch {
    payload = { title: "Kestrel", body: event.data ? event.data.text() : "" };
  }
  const title = payload.title || "Kestrel";
  const options = {
    body: payload.body || "",
    icon: "/icons/icon-256.png",
    badge: "/icons/icon-128.png",
    // Same id re-notifies in place instead of stacking - deliveries already
    // dedupe by nature (one thing said, once), so this mirrors that.
    tag: payload.delivery_id || undefined,
    data: payload,
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const data = event.notification.data || {};
  const url = data.delivery_id ? `/?delivery=${encodeURIComponent(data.delivery_id)}` : "/";
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((clients) => {
      for (const client of clients) {
        if ("focus" in client) {
          if ("navigate" in client) client.navigate(url);
          return client.focus();
        }
      }
      return self.clients.openWindow(url);
    }),
  );
});
