/*
 * Service worker for offline billing (feature "offline_pos"). Served at
 * /pos/sw.js with scope /pos/ (app.pos.counter.service_worker) for the
 * counter screen, and at /app/sw.js with scope /app/ for the ARFA Counter
 * app (app.counterapp.routes.service_worker).
 *
 * Keeps the billing screens working without a connection: the screens
 * (the counter's /pos/; the app's home /app/ and Bill /app/bill), their
 * scripts and styles (/static/) and item photos (/media/) are fetched from
 * the network as usual and a copy kept; when the network is gone, the copy
 * is used. Nothing else is cached - no bill, report or other page - and
 * nothing is ever sent from here: queued bills are sent by the page itself
 * (pos_offline.js). The app only keeps a screen that says it may be kept
 * (the X-Counter-Screen header), so a signed-out phone's pairing screen is
 * never what opens offline.
 */
var SCOPE = new URL(self.registration.scope).pathname;
var IN_APP = SCOPE !== "/pos/";
var CACHE = IN_APP ? "arfa-app-v1" : "arfa-pos-v1";
var SCREENS = IN_APP ? [SCOPE, SCOPE + "bill"] : [SCOPE];
var BILLING = SCREENS[SCREENS.length - 1];

self.addEventListener("install", function () { self.skipWaiting(); });
self.addEventListener("activate", function (event) { event.waitUntil(self.clients.claim()); });

function cacheable(url) {
  return url.pathname.indexOf("/static/") === 0 || url.pathname.indexOf("/media/") === 0;
}

function keep(res) {
  return res.ok && !res.redirected && (!IN_APP || res.headers.get("X-Counter-Screen") === "1");
}

function offlinePage() {
  var html = "<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>" +
    "<title>Offline</title><body style='font-family:sans-serif;padding:24px;max-width:480px;margin:auto'>" +
    "<h1>You're offline</h1><p>Only billing works without the internet. Day-book, reports and settings open again once " +
    "the connection is back.</p><p><a href='" + BILLING + "'>Back to billing</a></p></body>";
  return new Response(html, {headers: {"Content-Type": "text/html; charset=utf-8"}});
}

self.addEventListener("fetch", function (event) {
  var req = event.request;
  if (req.method !== "GET") return;
  var url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  if (req.mode === "navigate") {
    if (SCREENS.indexOf(url.pathname) < 0) {
      if (url.pathname.indexOf(SCOPE) === 0) {
        event.respondWith(fetch(req).catch(function () { return offlinePage(); }));
      }
      return;
    }
    // A billing screen: always the live page when online (a redirect -
    // signed out - is never kept); the last good copy when offline.
    event.respondWith(fetch(req).then(function (res) {
      if (keep(res) && !url.search) {
        var copy = res.clone();
        caches.open(CACHE).then(function (c) { c.put(url.pathname, copy); });
      }
      return res;
    }).catch(function () {
      return caches.match(url.pathname).then(function (hit) { return hit || offlinePage(); });
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
