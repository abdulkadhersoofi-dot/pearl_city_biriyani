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
  var KEY = "arfa.app.device";
  var CART_KEY = "arfa.app.cart";  // the Bill screen's cart in progress (counter_bill.js)

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
      if (d) localStorage.removeItem("arfa.device." + d.tenant_id + "." + d.user_id);
    } catch (e) {}
    // The screens kept for offline use belong to this branch - drop them.
    if (window.caches) caches.delete("arfa-app-v1").catch(function () {});
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
    return post("/app/resume", {device_id: d.id, token: d.token}).then(function (r) { return r.ok; });
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
    caches.open("arfa-app-v1").then(function (cache) {
      urls.forEach(function (u) { cache.add(u).catch(function () {}); });
      screens.forEach(function (path) {
        fetch(path, {credentials: "same-origin"}).then(function (res) {
          if (res.ok && !res.redirected && res.headers.get("X-Counter-Screen") === "1") cache.put(path, res);
        }).catch(function () {});
      });
    }).catch(function () {});
  }

  window.ArfaApp = {load: load, save: save, forget: forget, post: post, platform: platform, resume: resume,
                    keepForOffline: keepForOffline, CART_KEY: CART_KEY};

  window.pairScreen = function () {
    return {
      state: "code", code: "", name: "", error: "", busy: false,
      start: function () {
        var d = load(), self = this;
        if (!d) { this.state = "code"; return; }
        this.state = "resuming";
        post("/app/resume", {device_id: d.id, token: d.token}).then(function (r) {
          if (r.ok) { window.location.replace(r.data.home || "/app/"); return; }
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
          window.location.replace(d.home || "/app/");
        }).catch(function () { self.busy = false; self.error = "No internet - connect and try again."; });
      }
    };
  };

  window.counterHome = function (deviceId) {
    return {
      online: navigator.onLine, device: null,
      start: function () {
        var self = this, d = load();
        this.device = d && (!deviceId || d.id === deviceId) ? d : null;
        window.addEventListener("online", function () { self.online = true; });
        window.addEventListener("offline", function () { self.online = false; });
      },
      unpair: function () {
        if (!window.confirm("Unpair this phone? It stops billing for this counter until the owner gives a new code.")) return;
        post("/app/unpair").finally(function () { forget(); window.location.replace("/app/"); });
      }
    };
  };
})();
