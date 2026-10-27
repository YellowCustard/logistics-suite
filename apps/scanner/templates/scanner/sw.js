{% load static %}// Scanner service worker, build {{ build_id }}. Caches the app shell only; data lives in IndexedDB.
const CACHE = "scanner-{{ build_id }}";
const SHELL = "{% url 'scanner:index' %}";
const ASSETS = [
  SHELL,
{% for asset in assets %}  "{% static asset %}",
{% endfor %}  "{% url 'scanner:manifest' %}",
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(ASSETS)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k.startsWith("scanner-") && k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== self.location.origin || url.pathname.startsWith("/api/")) {
    return; // API calls always go to the network; the app queues writes itself when offline
  }
  if (event.request.mode === "navigate") {
    // Network first so a deploy shows up at once; the cached shell when offline.
    event.respondWith(fetch(event.request).catch(() => caches.match(SHELL)));
    return;
  }
  event.respondWith(caches.match(event.request).then((hit) => hit || fetch(event.request)));
});
