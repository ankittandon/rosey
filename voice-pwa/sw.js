// Minimal service worker — exists so the app is installable as a PWA
// (Add to Home Screen) and launches standalone/fullscreen.
//
// We deliberately keep caching minimal: the realtime session, Porcupine
// models, and token endpoint must always hit the network, so we use a
// network-first strategy for the app shell and never cache API calls.

const CACHE = "rosey-pwa-v1";
const SHELL = [
  "./index.html",
  "./app.js",
  "./manifest.webmanifest",
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);

  // Never cache API/session/realtime traffic — always go to network.
  const isApi =
    url.pathname.startsWith("/session") ||
    url.hostname.endsWith("openai.com") ||
    url.hostname.endsWith("api.openai.com");
  if (isApi || event.request.method !== "GET") return; // let it hit the network directly

  // Network-first for the shell, fall back to cache offline.
  event.respondWith(
    fetch(event.request)
      .then((resp) => {
        const copy = resp.clone();
        caches.open(CACHE).then((c) => c.put(event.request, copy)).catch(() => {});
        return resp;
      })
      .catch(() => caches.match(event.request))
  );
});
