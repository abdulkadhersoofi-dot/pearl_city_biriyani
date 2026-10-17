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

  function load() {
    try { return JSON.parse(localStorage.getItem(KEY) || "null"); } catch (e) { return null; }
  }
  function save(d) {
    try {
      localStorage.setItem(KEY, JSON.stringify(d));
      localStorage.setItem("arfa.device." + d.tenant_id + "." + d.user_id, JSON.stringify(
        {id: d.id, token: d.token, code: d.code, name: d.name, next: d.next_number || 1}));
    } catch (e) { /* storage off - the phone will ask for a code next time */ }
  }
  function forget() {
    var d = load();
    try {
      localStorage.removeItem(KEY);
      if (d) localStorage.removeItem("arfa.device." + d.tenant_id + "." + d.user_id);
    } catch (e) {}
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

  window.ArfaApp = {load: load, save: save, forget: forget, post: post, platform: platform};

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
                user_id: d.user_id, next_number: d.next_number});
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
