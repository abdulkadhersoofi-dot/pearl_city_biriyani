/*
 * Service worker for working without a connection. Served at /pos/sw.js
 * with scope /pos/ (app.pos.counter.service_worker) for the website's
 * counter, and at /app/sw.js with scope /app/ for the ARFA Counter app
 * (app.counterapp.routes.service_worker).
 *
 * The app (scope /app/) opens at once from the phone, whatever the network:
 *  - its screens (home /app/, Bill /app/bill, Tables /app/tables) come from
 *    the phone's copy straight away, and a fresh copy is fetched behind
 *    them; when the fresh one differs (the screen's X-Screen-Version - new
 *    prices, an item sold out) it is kept and the page is told
 *    ("screen-updated"), so it can reload into it when the cashier is free.
 *    With no copy yet, the network - given a few seconds, then the
 *    "you're offline" page.
 *  - scripts, styles, fonts and icons carry their own version (?v=, app/
 *    utils/delivery.py): kept once, never asked about again; item photos
 *    (/media/) likewise.
 * The website counter (scope /pos/) asks the network first, as before, but
 * falls back to its copy after a few seconds instead of waiting forever.
 *
 * A screen is kept only if the server says it may be (X-Counter-Screen), so
 * a signed-out phone's pairing screen is never what opens offline; nothing
 * is ever sent from here - queued bills and KOTs are sent by the page.
 */
var SCOPE = new URL(self.registration.scope).pathname;
var IN_APP = SCOPE !== "/pos/";
var CACHE = IN_APP ? "arfa-app-v2" : "arfa-pos-v2";
var OLD_CACHES = ["arfa-app-v1", "arfa-pos-v1"];
var SCREENS = IN_APP ? [SCOPE, SCOPE + "bill", SCOPE + "tables"] : [SCOPE];
var BILLING = IN_APP ? SCOPE + "bill" : SCOPE;
var NETWORK_WAIT = 4000;   // a page with no copy yet, or the website counter

self.addEventListener("install", function (event) {
  self.skipWaiting();
  // Carry the copies kept by the previous version over, so an update never
  // leaves a phone without its screens.
  event.waitUntil(caches.open(CACHE).then(function (cache) {
    return Promise.all(OLD_CACHES.map(function (name) {
      return caches.open(name).then(function (old) {
        return old.keys().then(function (reqs) {
          return Promise.all(reqs.map(function (r) {
            return old.match(r).then(function (res) { return res ? cache.put(r, res) : null; });
          }));
        });
      });
    }));
  }).catch(function () {}));
});
self.addEventListener("activate", function (event) {
  event.waitUntil(Promise.all(OLD_CACHES.map(function (n) { return caches.delete(n); }))
    .then(function () { return self.clients.claim(); }));
});

function versioned(url) {
  return (url.pathname.indexOf("/static/") === 0 && url.searchParams.has("v")) || url.pathname.indexOf("/media/") === 0;
}
function staticFile(url) { return url.pathname.indexOf("/static/") === 0; }

function keep(res) {
  return res && res.ok && !res.redirected && (!IN_APP || res.headers.get("X-Counter-Screen") === "1");
}

// fetch() that gives up after `ms` - a phone "connected" with no data
// would otherwise wait for minutes.
function fetchWithin(req, ms) {
  return new Promise(function (resolve, reject) {
    var timer = setTimeout(function () { reject(new Error("timeout")); }, ms);
    fetch(req).then(function (res) { clearTimeout(timer); resolve(res); },
                    function (err) { clearTimeout(timer); reject(err); });
  });
}

function offlinePage() {
  var html = "<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>" +
    "<title>Offline</title><body style='font-family:sans-serif;padding:24px;max-width:480px;margin:auto'>" +
    "<h1>You're offline</h1><p>Billing works without the internet. This screen opens again once the connection " +
    "is back.</p><p><a href='" + BILLING + "'>Back to billing</a></p></body>";
  return new Response(html, {headers: {"Content-Type": "text/html; charset=utf-8"}});
}

function tell(path) {
  self.clients.matchAll({type: "window"}).then(function (list) {
    list.forEach(function (c) { c.postMessage({type: "screen-updated", path: path}); });
  });
}

// The app's screens: the phone's copy at once, a fresh one fetched behind it.
function appScreen(event, url) {
  var key = url.pathname;
  return caches.open(CACHE).then(function (cache) {
    return cache.match(key).then(function (copy) {
      var fresh = fetch(event.request).then(function (res) {
        if (keep(res)) {
          var changed = !copy || copy.headers.get("X-Screen-Version") !== res.headers.get("X-Screen-Version");
          return cache.put(key, res.clone()).then(function () {
            if (copy && changed) tell(key);
            return res;
          });
        }
        return res;
      });
      if (copy) {
        event.waitUntil(fresh.catch(function () {}));
        return copy;
      }
      return Promise.race([fresh, new Promise(function (_, reject) { setTimeout(reject, NETWORK_WAIT * 2); })])
        .catch(function () { return offlinePage(); });
    });
  });
}

self.addEventListener("fetch", function (event) {
  var req = event.request;
  if (req.method !== "GET") return;
  var url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  if (req.mode === "navigate") {
    if (SCREENS.indexOf(url.pathname) >= 0) {
      if (IN_APP) { event.respondWith(appScreen(event, url)); return; }
      // The website counter: the live page when it comes in time, else the copy.
      event.respondWith(fetchWithin(req, NETWORK_WAIT).then(function (res) {
        if (keep(res) && !url.search) {
          var copy = res.clone();
          caches.open(CACHE).then(function (c) { c.put(url.pathname, copy); });
        }
        return res;
      }).catch(function () {
        return caches.match(url.pathname).then(function (hit) { return hit || fetch(req); }).catch(offlinePage);
      }));
      return;
    }
    if (url.pathname.indexOf(SCOPE) === 0) {
      event.respondWith(fetchWithin(req, NETWORK_WAIT * 2).catch(function () { return offlinePage(); }));
    }
    return;
  }

  if (versioned(url)) {
    // Never changes under its version: the copy, or fetch it once.
    event.respondWith(caches.match(req).then(function (hit) {
      return hit || fetch(req).then(function (res) {
        if (res.ok) { var copy = res.clone(); caches.open(CACHE).then(function (c) { c.put(req, copy); }); }
        return res;
      });
    }));
    return;
  }
  if (staticFile(url)) {
    // An unversioned file: the copy now, a fresh one for next time.
    event.respondWith(caches.match(req).then(function (hit) {
      var fresh = fetch(req).then(function (res) {
        if (res.ok) { var copy = res.clone(); caches.open(CACHE).then(function (c) { c.put(req, copy); }); }
        return res;
      });
      if (hit) { event.waitUntil(fresh.catch(function () {})); return hit; }
      return fresh;
    }).catch(function () { return Response.error(); }));
  }
});
