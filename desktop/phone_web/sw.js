"use strict";
const CACHE = "jarvis-phone-static-v2";
const ASSETS = ["/", "/index.html", "/app.js", "/style.css", "/manifest.webmanifest"];
self.addEventListener("install", (event) => { event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(ASSETS))); self.skipWaiting(); });
self.addEventListener("activate", (event) => { event.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((key) => key.startsWith("jarvis-phone-static-") && key !== CACHE).map((key) => caches.delete(key)))).then(() => self.clients.claim())); });
self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  // API responses, authorization headers and personal plans are never cached.
  if (event.request.method !== "GET" || event.request.headers.has("Authorization") || event.request.headers.has("X-Jarvis-Control") || url.origin !== self.location.origin || url.search || !ASSETS.includes(url.pathname)) return;
  event.respondWith(fetch(event.request).then((response) => { if (response.ok) { const copy = response.clone(); caches.open(CACHE).then((cache) => cache.put(event.request, copy)); } return response; }).catch(() => caches.match(url.pathname)));
});
