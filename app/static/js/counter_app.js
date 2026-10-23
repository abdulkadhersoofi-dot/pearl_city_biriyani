/* ARFA Counter app (counter app, stage 1): pairing this phone with a branch
 * login, signing back in with the device's secret, unpairing.
 *
 * The device is kept in this phone's storage under "arfa.app.device"
 * ({id, token, code, name, tenant_id, user_id}). The same device is also
 * written where the counter's offline billing looks for it
 * ("arfa.device.<tenant>.<user>", pos_offline.js), so a paired phone bills
 * offline in its own series (O1/...) without registering a second time.
 */
(function () {
  // Every request to the server gives up after a while: on a phone that is
  // "connected" but gets no data (or a server still waking up) a request
  // would otherwise hang for minutes, and the screen with it. A request
  // that gives up counts as no internet - bills and KOTs are then kept on
  // the phone and sent later (each carries its own id, so one that did
  // reach the server is never booked twice).
  var nativeFetch = window.fetch && window.fetch.bind(window);
  if (nativeFetch && window.AbortController) {
    window.fetch = function (input, init) {
      init = init || {};
      var url = typeof input === "string" ? input : (input && input.url) || "";
      var same = true;
      try { same = new URL(url, window.location.href).origin === window.location.origin; } catch (e) {}
      if (init.signal || !same) return nativeFetch(input, init);
      var ctrl = new AbortController();
      var wait = init.method && init.method.toUpperCase() !== "GET" ? 10000 : 8000;
      var timer = setTimeout(function () { ctrl.abort(); }, wait);
      return nativeFetch(input, Object.assign({}, init, {signal: ctrl.signal})).then(
        function (res) { clearTimeout(timer); return res; },
        function (err) { clearTimeout(timer); throw err; });
    };
  }

  var KEY = "arfa.app.device";
  var CART_KEY = "arfa.app.cart";  // the Bill screen's cart in progress (counter_bill.js)
  // What this phone is for (stage 5) - where the app opens: the counter's
  // Bill screen, a waiter's Tables, or the Kitchen screen.
  var MODE_KEY = "arfa.app.mode";
  var MODES = {counter: "/app/bill", waiter: "/app/tables", kitchen: "/app/kitchen"};
  function mode() { try { return localStorage.getItem(MODE_KEY) || "counter"; } catch (e) { return "counter"; } }
  function setMode(m) { try { localStorage.setItem(MODE_KEY, m); } catch (e) {} }

  function load() {
    try { return JSON.parse(localStorage.getItem(KEY) || "null"); } catch (e) { return null; }
  }
  function save(d) {
    try {
      localStorage.setItem(KEY, JSON.stringify(d));
      // pos_offline.js counts per financial year: {"2026-27": 1}.
      var next = {};
      if (d.fy && d.next_number) next[d.fy] = d.next_number;
      localStorage.setItem("arfa.device." + d.tenant_id + "." + d.user_id, JSON.stringify(
        {id: d.id, token: d.token, code: d.code, name: d.name, next: next}));
    } catch (e) { /* storage off - the phone will ask for a code next time */ }
  }
  function forget() {
    var d = load();
    try {
      localStorage.removeItem(KEY);
      localStorage.removeItem(CART_KEY);
      localStorage.removeItem(MODE_KEY);
      if (d) localStorage.removeItem("arfa.device." + d.tenant_id + "." + d.user_id);
    } catch (e) {}
    // The screens kept for offline use belong to this branch - drop them.
    if (window.caches) ["arfa-app-v1", "arfa-app-v2"].forEach(function (n) { caches.delete(n).catch(function () {}); });
    if (navigator.serviceWorker && navigator.serviceWorker.getRegistrations) {
      navigator.serviceWorker.getRegistrations().then(function (rs) {
        rs.forEach(function (r) { if (/\/app\/$/.test(r.scope)) r.unregister(); });
      }).catch(function () {});
    }
  }
  function post(url, body) {
    return fetch(url, {
      method: "POST", credentials: "same-origin",
      headers: {"Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest"},
      body: JSON.stringify(body || {})
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) { return {ok: r.ok, status: r.status, data: data}; });
    });
  }
  function platform() { return window.AndroidPrint ? "android" : "app"; }

  // Signs this phone back in with its secret when the session has ended
  // (idle sign-out). Resolves true when signed in again.
  function resume() {
    var d = load();
    if (!d) return Promise.resolve(false);
    return post("/app/resume", {device_id: d.id, token: d.token}).then(function (r) {
      // Switched off or unpaired by the owner: this phone's saved screens go,
      // so the next start asks for a new code instead of opening old copies.
      if (r.status === 403) forget();
      return r.ok;
    });
  }

  // The app's offline copy (pos_sw.js): installed on every phone, whatever
  // the business's offline billing setting - the app always works offline.
  function registerWorker() {
    if (!("serviceWorker" in navigator)) return;
    navigator.serviceWorker.register("/app/sw.js", {scope: "/app/"}).catch(function () {});
  }
  // A fresher copy of this screen has been kept (new prices, an item sold
  // out): the screen hears "arfa:screen-updated" and reloads into it when
  // the cashier is free.
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.addEventListener("message", function (ev) {
      var msg = ev.data || {};
      if (msg.type === "screen-updated" && msg.path === window.location.pathname) {
        window.dispatchEvent(new CustomEvent("arfa:screen-updated"));
      }
    });
  }

  // Keeps the app's screens and everything they load in the offline copy
  // the service worker serves (pos_sw.js, cache "arfa-app-v1") - at once,
  // so a phone that opened the Bill screen just once still opens it
  // without internet. A screen is kept only if the server says it may be
  // (X-Counter-Screen), never a sign-in page.
  function keepForOffline(screens) {
    if (!window.caches || !navigator.onLine) return;
    var urls = [];
    document.querySelectorAll("script[src], link[rel=stylesheet], link[rel=preload], link[rel=icon]").forEach(function (el) {
      urls.push(el.src || el.href);
    });
    var sprite = document.getElementById("icon-sprite");
    if (sprite && sprite.dataset.href) urls.push(sprite.dataset.href);
    caches.open("arfa-app-v2").then(function (cache) {
      urls.forEach(function (u) { cache.add(u).catch(function () {}); });
      screens.forEach(function (path) {
        fetch(path, {credentials: "same-origin"}).then(function (res) {
          if (res.ok && !res.redirected && res.headers.get("X-Counter-Screen") === "1") cache.put(path, res);
        }).catch(function () {});
      });
    }).catch(function () {});
  }

  window.ArfaApp = {load: load, save: save, forget: forget, post: post, platform: platform, resume: resume,
                    keepForOffline: keepForOffline, registerWorker: registerWorker, CART_KEY: CART_KEY, mode: mode,
                    setMode: setMode};

  window.pairScreen = function () {
    return {
      state: "code", code: "", name: "", error: "", busy: false,
      pickMode: function (m) { setMode(m); window.location.replace("/app/?start=1"); },
      start: function () {
        var d = load(), self = this;
        if (!d) { this.state = "code"; return; }
        this.state = "resuming";
        post("/app/resume", {device_id: d.id, token: d.token}).then(function (r) {
          // Signed back in: on to where the app was opening (?start=1 -
          // what this phone is for), else home.
          var start = /[?&]start=1/.test(window.location.search) ? "?start=1" : "";
          if (r.ok) { window.location.replace((r.data.home || "/app/") + start); return; }
          if (r.status === 403) { forget(); }
          self.error = (r.data && r.data.error) || "";
          self.state = "code";
        }).catch(function () { self.state = "offline"; });
      },
      submit: function () {
        var self = this;
        if (this.code.length !== 6 || this.busy) return;
        this.busy = true; this.error = "";
        post("/app/pair", {code: this.code, name: this.name, platform: platform()}).then(function (r) {
          self.busy = false;
          if (!r.ok) { self.error = (r.data && r.data.error) || "That didn't work - try again."; return; }
          var d = r.data;
          save({id: d.device_id, token: d.token, code: d.code, name: d.name, tenant_id: d.tenant_id,
                user_id: d.user_id, fy: d.fy, next_number: d.next_number});
          // A restaurant's phone says what it's for; any other is the counter.
          if (d.restaurant) { self.state = "mode"; return; }
          setMode("counter");
          window.location.replace((d.home || "/app/") + "?start=1");
        }).catch(function () { self.busy = false; self.error = "No internet - connect and try again."; });
      }
    };
  };

  window.counterHome = function (deviceId, tablesOn) {
    return {
      online: navigator.onLine, device: null, mode: mode(), choosing: false,
      inApp: !!(window.AndroidPrint && window.AndroidPrint.openSettings),
      start: function () {
        var self = this, d = load();
        registerWorker();
        // The app opening (/app/?start=1): straight to what this phone is for.
        if (/[?&]start=1/.test(window.location.search)) {
          var target = tablesOn ? MODES[this.mode] : MODES.counter;
          window.location.replace(target || MODES.counter);
          return;
        }
        this.device = d && (!deviceId || d.id === deviceId) ? d : null;
        window.addEventListener("online", function () { self.online = true; });
        window.addEventListener("offline", function () { self.online = false; });
      },
      modeLabel: function () { return {counter: "Counter", waiter: "Waiter", kitchen: "Kitchen"}[this.mode] || "Counter"; },
      pickMode: function (m) { setMode(m); this.mode = m; this.choosing = false; },
      appSettings: function () { if (this.inApp) window.AndroidPrint.openSettings(); },
      unpair: function () {
        ArfaDialog.confirm("Unpair this phone? It stops billing for this counter until the owner gives a new code.",
                           {ok: "Unpair", danger: true}).then(function (ok) {
          if (ok) post("/app/unpair").finally(function () { forget(); window.location.replace("/app/"); });
        });
      }
    };
  };
})();
