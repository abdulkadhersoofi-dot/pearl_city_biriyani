/*
 * The kitchen screen (counter app, stage 4; app/counterapp/counter_routes.py).
 * Asks the server for this branch's KOTs every few seconds; a new one rings
 * a short chime and glows; Ready moves it to the Ready list (Undo there).
 * The tablet's screen is kept awake while this is open, where the browser
 * allows it.
 */
(function () {
  "use strict";
  var EVERY = 6000;

  function chime() {
    try {
      var Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) return;
      var ctx = chime.ctx || (chime.ctx = new Ctx());
      [0, 0.18].forEach(function (at, i) {
        var osc = ctx.createOscillator(), gain = ctx.createGain();
        osc.frequency.value = i ? 1046 : 784;
        gain.gain.setValueAtTime(0.0001, ctx.currentTime + at);
        gain.gain.exponentialRampToValueAtTime(0.25, ctx.currentTime + at + 0.02);
        gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + at + 0.3);
        osc.connect(gain).connect(ctx.destination);
        osc.start(ctx.currentTime + at);
        osc.stop(ctx.currentTime + at + 0.32);
      });
    } catch (e) { /* no sound - the card still glows */ }
  }

  window.kitchenScreen = function (cfg) {
    return {
      cooking: [], ready: [], fresh: [], seen: null, online: true, loaded: false, showReady: false,
      start: function () {
        var self = this;
        this.poll();
        setInterval(function () { if (!document.hidden) self.poll(); }, EVERY);
        document.addEventListener("visibilitychange", function () { if (!document.hidden) { self.poll(); self.stayAwake(); } });
        window.addEventListener("online", function () { self.poll(); });
        this.stayAwake();
      },
      stayAwake: function () {
        if (navigator.wakeLock && navigator.wakeLock.request) navigator.wakeLock.request("screen").catch(function () {});
      },
      poll: function () {
        var self = this;
        return fetch(cfg.state_url, {credentials: "same-origin", headers: {"X-Requested-With": "XMLHttpRequest"}})
          .then(function (r) {
            // Signed out (the session ended): sign back in with the phone's
            // secret and take a fresh token for marking KOTs ready.
            if (r.status === 401 && window.ArfaApp) {
              return ArfaApp.resume().then(function () {
                return fetch("/app/ping", {credentials: "same-origin"}).then(function (p) { return p.json(); })
                  .then(function (d) { if (d.csrf) cfg.csrf = d.csrf; return null; });
              });
            }
            if (!r.ok) throw new Error("state");
            return r.json();
          })
          .then(function (data) {
            if (!data) return;
            self.online = true;
            var ids = data.cooking.map(function (k) { return k.id; });
            if (self.seen) {
              var arrived = ids.filter(function (id) { return self.seen.indexOf(id) < 0; });
              if (arrived.length) {
                self.fresh = self.fresh.concat(arrived);
                chime();
                if (navigator.vibrate) navigator.vibrate([60, 60, 60]);
                setTimeout(function () { self.fresh = self.fresh.filter(function (id) { return arrived.indexOf(id) < 0; }); }, 20000);
              }
            }
            self.seen = ids.concat(data.ready.map(function (k) { return k.id; }));
            self.cooking = data.cooking;
            self.ready = data.ready;
            self.loaded = true;
          })
          .catch(function () { self.online = false; });
      },
      post: function (k, ready) {
        var self = this;
        return fetch(cfg.ready_url.replace("/0/", "/" + k.id + "/"), {
          method: "POST", credentials: "same-origin",
          headers: {"Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest", "X-CSRFToken": cfg.csrf},
          body: JSON.stringify({ready: ready})
        }).then(function () { return self.poll(); }).catch(function () { self.online = false; });
      },
      markReady: function (k) {
        // Off the list at once; the server confirms on the next look.
        this.cooking = this.cooking.filter(function (x) { return x.id !== k.id; });
        this.post(k, true);
      },
      undo: function (k) { this.post(k, false); this.showReady = false; }
    };
  };
})();
