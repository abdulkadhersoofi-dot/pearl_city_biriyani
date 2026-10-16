/*
 * POS service worker (feature "offline_pos") - served at /pos/sw.js with
 * scope /pos/ by app.pos.counter.service_worker.
 *
 * Keeps the POS screen working without a connection: the billing screen
 * (/pos/), its scripts and styles (/static/) and item photos (/media/) are
 * fetched from the network as usual and a copy kept; when the network is
 * gone, the copy is used. Nothing else is cached - no bill, report or
 * other page - and nothing is ever sent from here: queued bills are sent
 * by the page itself (pos_offline.js).
 */
var CACHE = "arfa-pos-v1";

self.addEventListener("install", function () { self.skipWaiting(); });
self.addEventListener("activate", function (event) { event.waitUntil(self.clients.claim()); });

function cacheable(url) {
  return url.pathname.indexOf("/static/") === 0 || url.pathname.indexOf("/media/") === 0;
}

function offlinePage() {
  var html = "<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>" +
    "<title>Offline</title><body style='font-family:sans-serif;padding:24px;max-width:480px;margin:auto'>" +
    "<h1>You're offline</h1><p>Only billing works without the internet. Day-book, reports and settings open again once " +
    "the connection is back.</p><p><a href='/pos/'>Back to billing</a></p></body>";
  return new Response(html, {headers: {"Content-Type": "text/html; charset=utf-8"}});
}

self.addEventListener("fetch", function (event) {
  var req = event.request;
  if (req.method !== "GET") return;
  var url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  if (req.mode === "navigate") {
    if (url.pathname !== "/pos/") {
      if (url.pathname.indexOf("/pos/") === 0) {
        event.respondWith(fetch(req).catch(function () { return offlinePage(); }));
      }
      return;
    }
    // The billing screen: always the live page when online (a redirect -
    // signed out - is never kept); the last good copy when offline.
    event.respondWith(fetch(req).then(function (res) {
      if (res.ok && !res.redirected && !url.search) {
        var copy = res.clone();
        caches.open(CACHE).then(function (c) { c.put("/pos/", copy); });
      }
      return res;
    }).catch(function () {
      return caches.match("/pos/").then(function (hit) { return hit || offlinePage(); });
    }));
    return;
  }

  if (cacheable(url)) {
    event.respondWith(fetch(req).then(function (res) {
      if (res.ok) {
        var copy = res.clone();
        caches.open(CACHE).then(function (c) { c.put(req, copy); });
      }
      return res;
    }).catch(function () {
      return caches.match(req).then(function (hit) { return hit || Response.error(); });
    }));
  }
});
