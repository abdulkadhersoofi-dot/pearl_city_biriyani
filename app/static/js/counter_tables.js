/*
 * Tables & KOT in the ARFA Counter app (counter app, stage 3) - merged into
 * the Bill screen (counter_bill.js) for a restaurant.
 *
 * The tables: every table's running order and what the kitchen has had of
 * it (/app/tables/state), kept on the phone so the tables still open with
 * no internet. Tap a table, add or take off items, Send KOT, Bill.
 *
 * A KOT is sent as what CHANGED on the table since the kitchen last heard
 * (+2 Biryani, -1 Tea), never as the whole order - so a phone that was
 * offline, or two waiters on one table, add to the order instead of
 * replacing each other (/app/kot, restaurant.services.apply_kot). Online
 * the kitchen's number comes back at once; offline the phone prints its
 * own (O1-3), keeps the KOT and sends it when the connection is back -
 * before any bill waiting with it, so a table's order is on the server
 * before the bill that settles it.
 */
(function () {
  "use strict";
  var P = window.ArfaBillPrint;

  function key(productId, name, note) { return (productId || "") + "|" + name + "|" + (note || ""); }
  function qtyMap(lines) {
    var m = {};
    (lines || []).forEach(function (l) { var k = key(l.product_id, l.name, l.note); m[k] = Number(((m[k] || 0) + Number(l.qty)).toFixed(3)); });
    return m;
  }
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) { return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]; }); }
  // A KOT slip as a small page, for the browser's print dialog or
  // Android's system print when there's no thermal printer.
  function kotHtml(doc) {
    var h = "<!doctype html><html><head><meta charset='utf-8'><style>@page{size:80mm auto;margin:3mm}body{font-family:'Courier New',monospace;font-size:13pt;margin:0}" +
      ".c{text-align:center}.b{font-weight:700}.r{border-top:1px dashed #000;margin:6px 0}.x{text-decoration:line-through}</style></head><body>";
    h += "<div class='c b'>" + esc(doc.header[0]) + "</div><div class='r'></div><div class='c b'>" + esc(doc.number) + "</div>";
    h += "<div class='c b'>" + esc(doc.title) + "</div>";
    doc.meta.forEach(function (m) { h += "<div>" + esc(m[0]) + ": " + esc(m[1]) + "</div>"; });
    h += "<div class='r'></div>";
    doc.lines.forEach(function (l) {
      h += "<div class='b'>" + esc(l.qty) + "  " + esc(l.desc) + "</div>";
      (l.details || []).forEach(function (d) { h += "<div>&nbsp;&nbsp;" + esc(d) + "</div>"; });
    });
    return h + "<div class='r'></div></body></html>";
  }

  window.counterTables = function (cfg, byId) {
    var ownerKey = cfg.tenant_id + "." + cfg.user_id;
    var TABLES_KEY = "arfa.app.tables." + ownerKey, KOTS_KEY = "arfa.app.kots." + ownerKey, SEQ_KEY = "arfa.app.kotseq." + ownerKey;

    function read(k, fallback) { try { return JSON.parse(localStorage.getItem(k) || "null") || fallback; } catch (e) { return fallback; } }
    function write(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} }

    return {
      tables: [], tablesAt: 0, kotQueue: [], tableSent: {}, kotBusy: false, kotDone: null, section: "",
      // The open table as this phone last saw it (its server signature and
      // item quantities) - to fold in what other screens change meanwhile.
      tableSig: null, tableBase: {},

      // --- start --------------------------------------------------------------------------------
      startTables: function () {
        var self = this;
        this.tables = read(TABLES_KEY, {tables: []}).tables;
        this.kotQueue = read(KOTS_KEY, []);
        window.addEventListener("online", function () { self.flushKots().then(function () { self.refreshTables(); }); });
        setInterval(function () { if (navigator.onLine && self.waitingKots().length) self.flushKots(); }, 20000);
        // Live tables: while the tables or a table are on screen, look again
        // every few seconds - KOTs from the counter and other phones show up.
        setInterval(function () {
          if (navigator.onLine && !document.hidden && !self.busy && !self.kotBusy && (self.view === "tables" || self.view === "table")) {
            self.refreshTables();
          }
        }, 6000);
        if (navigator.onLine) this.flushKots().then(function () { return self.refreshTables(); });
      },
      saveTables: function () { write(TABLES_KEY, {tables: this.tables, at: Date.now()}); },
      saveQueue: function () { write(KOTS_KEY, this.kotQueue); },
      waitingKots: function () { return this.kotQueue.filter(function (k) { return !k.error; }); },
      stuckKots: function () { return this.kotQueue.filter(function (k) { return k.error; }); },
      sections: function () {
        var out = [];
        this.tables.forEach(function (t) { if (t.section && out.indexOf(t.section) < 0) out.push(t.section); });
        return out;
      },
      shownTables: function () {
        var s = this.section;
        return this.tables.filter(function (t) { return !s || t.section === s; });
      },
      tableWaiting: function (id) { return this.kotQueue.some(function (k) { return k.table_id === id && !k.error; }); },

      // The server's tables, with this phone's KOTs that haven't reached it
      // yet laid on top.
      refreshTables: function () {
        var self = this;
        if (!navigator.onLine) return Promise.resolve(false);
        return fetch(cfg.tables_state_url, {credentials: "same-origin", headers: {"X-Requested-With": "XMLHttpRequest"}})
          .then(function (r) { if (!r.ok) throw new Error("state"); return r.json(); })
          .then(function (data) {
            self.tables = data.tables;
            self.kotQueue.forEach(function (k) { if (!k.error) self.applyLocal(k); });
            self.saveTables();
            if (self.view === "table" && self.table) self.mergeOpenTable();
            return true;
          }).catch(function () { return false; });
      },
      applyLocal: function (kot) {
        var t = this.tables.find(function (x) { return x.id === kot.table_id; });
        if (!t) return;
        kot.items.forEach(function (item) {
          var line = t.lines.find(function (l) { return l.product_id === item.product_id && (l.note || "") === (item.note || ""); });
          if (!line) {
            if (item.qty < 0) return;
            var it = byId[item.product_id] || {};
            line = {product_id: item.product_id, name: item.name, note: item.note || "", qty: 0, rate: it.rate || 0};
            t.lines.push(line);
          }
          line.qty = Math.max(0, Number((Number(line.qty) + Number(item.qty)).toFixed(3)));
          var k = key(item.product_id, line.name, item.note);
          t.sent[k] = Math.max(0, Number(((t.sent[k] || 0) + Number(item.qty)).toFixed(3)));
        });
        t.lines = t.lines.filter(function (l) { return l.qty > 0; });
        t.busy = t.lines.length > 0;
        t.total = t.lines.reduce(function (s, l) {
          var it = byId[l.product_id] || {};
          return s + GstCalc.line(l.qty, l.rate, 0, it.gst_rate || 0, it.incl, cfg.charges_gst, true, 0).total;
        }, 0);
        if (t.busy && !t.since) t.since = new Date().toTimeString().slice(0, 5);
      },

      // --- moving about -------------------------------------------------------------------------
      showTables: function () {
        this.view = "tables"; this.table = null; this.cart = []; this.tableSent = {}; this.sheet = ""; this.q = "";
        this.refreshTables();
      },
      openTable: function (t) {
        var self = this;
        var open = function () {
          var fresh = self.tables.find(function (x) { return x.id === t.id; }) || t;
          self.table = {id: fresh.id, name: fresh.name};
          self.cart = fresh.lines.map(function (l) {
            var it = byId[l.product_id] || {id: l.product_id, name: l.name, hsn: "", rate: l.rate, gst_rate: 0, incl: false, unit: "NOS", loose: false};
            var line = self.lineFor(it, Number(l.qty), l.note);
            line.description = l.name;
            line.rate = l.rate;
            return line;
          });
          self.tableSent = Object.assign({}, fresh.sent);
          self.tableSig = fresh.sig === undefined ? null : fresh.sig;
          self.tableBase = qtyMap(fresh.lines);
          self.view = "table"; self.q = ""; self.sheet = ""; self.error = "";
        };
        this.refreshTables().then(open);
      },
      // Another counter or phone changed the open table: what THEY changed
      // goes into this phone's order, on top of what was added here.
      mergeOpenTable: function () {
        var self = this, id = this.table.id;
        var fresh = this.tables.find(function (x) { return x.id === id; });
        if (!fresh) return;
        var now = qtyMap(fresh.lines), base = this.tableBase, said = [], seen = {};
        Object.keys(now).concat(Object.keys(base)).forEach(function (k) {
          if (seen[k]) return;
          seen[k] = true;
          var d = Number(((now[k] || 0) - (base[k] || 0)).toFixed(3));
          if (!d) return;
          var info = fresh.lines.find(function (l) { return key(l.product_id, l.name, l.note) === k; });
          var line = self.cart.find(function (l) { return key(l.product_id, l.description, l.note) === k; });
          if (line) {
            line.qty = Math.max(0, Number((Number(line.qty) + d).toFixed(3)));
          } else if (d > 0 && info) {
            var it = byId[info.product_id];
            if (!it) return;
            line = self.lineFor(it, d, info.note);
            line.description = info.name;
            line.rate = info.rate;
            self.cart.push(line);
          }
          said.push((d > 0 ? "+" : "") + P.qtyText(d) + " " + (info ? info.name : line ? line.description : ""));
        });
        this.cart = this.cart.filter(function (l) { return Number(l.qty) > 0; });
        this.tableSent = Object.assign({}, fresh.sent);
        this.tableBase = now;
        this.tableSig = fresh.sig === undefined ? null : fresh.sig;
        if (said.length) this.say("Changed on another screen: " + said.join(", "));
      },
      leaveTable: function () {
        if (this.unsent().length && !window.confirm("Some changes haven't gone to the kitchen. Leave without sending them?")) return;
        this.showTables();
      },

      // --- what the kitchen hasn't heard yet ----------------------------------------------------
      sentOf: function (line) { return this.tableSent[key(line.product_id, line.description, line.note)] || 0; },
      unsent: function () {
        var wanted = {}, out = [], sent = this.tableSent;
        this.cart.forEach(function (l) {
          var k = key(l.product_id, l.description, l.note);
          if (!wanted[k]) wanted[k] = {product_id: l.product_id, name: l.description, note: l.note || "", qty: 0};
          wanted[k].qty += Number(l.qty);
        });
        Object.keys(wanted).forEach(function (k) {
          var d = Number((wanted[k].qty - (sent[k] || 0)).toFixed(3));
          if (d) out.push(Object.assign({}, wanted[k], {qty: d}));
        });
        Object.keys(sent).forEach(function (k) {
          if (!wanted[k] && sent[k] > 0) {
            var parts = k.split("|");
            out.push({product_id: Number(parts[0]) || null, name: parts[1], note: parts.slice(2).join("|"), qty: -sent[k]});
          }
        });
        return out.filter(function (i) { return i.product_id; });
      },

      // --- sending a KOT ------------------------------------------------------------------------
      sendKot: function () {
        var self = this, items = this.unsent();
        if (!items.length) { this.say("Nothing new for the kitchen."); return; }
        if (this.kotBusy) return;
        var kot = {uuid: P.uuid4(), table_id: this.table.id, table_name: this.table.name, items: items, ref: "", created: Date.now()};
        this.kotBusy = true;
        var offline = function () {
          kot.ref = self.localRef();
          self.kotQueue.push(kot);
          self.saveQueue();
          self.applyLocal(kot);
          self.saveTables();
          self.kotSent(kot, kot.ref, true);
        };
        if (!navigator.onLine) { offline(); this.kotBusy = false; return; }
        this.postKots([kot], true).then(function (res) {
          var r = res && res[0];
          if (!r) { offline(); return; }
          if (r.status === "rejected") { self.error = r.error; self.say(r.error); return; }
          self.kotSent(kot, r.status === "sent" ? String(r.kot_no) : "", false);
          if (r.sig !== undefined && self.table && self.table.id === kot.table_id) self.tableSig = r.sig;
          self.refreshTables();
        }).catch(function () { offline(); }).then(function () { self.kotBusy = false; });
      },
      kotSent: function (kot, number, offline) {
        var sent = this.tableSent, base = this.tableBase;
        kot.items.forEach(function (i) {
          var k = key(i.product_id, i.name, i.note);
          sent[k] = Math.max(0, Number(((sent[k] || 0) + i.qty).toFixed(3)));
          // This phone's own change is on the table now - not "another screen's".
          base[k] = Math.max(0, Number(((base[k] || 0) + i.qty).toFixed(3)));
        });
        this.tableSent = Object.assign({}, sent);
        var doc = number ? this.kotDoc("KOT " + number, "Table " + kot.table_name, kot.items) : null;
        this.kotDone = {number: number, table: kot.table_name, offline: offline, doc: doc, count: kot.items.length};
        this.sheet = "kot";
        if (doc && P.thermalReady()) this.printKot(doc);
        if (navigator.vibrate) navigator.vibrate(20);
      },
      // Posts KOTs; resolves to the server's results, or null when the
      // server couldn't be reached at all.
      postKots: function (kots, firstTry) {
        var self = this;
        return fetch(cfg.kot_url, {
          method: "POST", credentials: "same-origin",
          headers: {"Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest", "X-CSRFToken": this.csrf},
          body: JSON.stringify({kots: kots.map(function (k) {
            return {uuid: k.uuid, table_id: k.table_id, ref: k.ref, items: k.items};
          })})
        }).then(function (r) {
          return r.json().then(function (data) { return {ok: r.ok, data: data}; }, function () { return {ok: false, data: null}; });
        }).then(function (r) {
          if (r.ok && r.data && r.data.results) return r.data.results;
          // Signed out, or the page's token is old: sign back in and once more.
          if (firstTry) return self.refreshSession().then(function () { return self.postKots(kots, false); });
          throw new Error((r.data && r.data.error) || "The server didn't take the KOT.");
        });
      },
      flushKots: function () {
        var self = this, waiting = this.waitingKots();
        if (!waiting.length || !navigator.onLine || this._flushing) return Promise.resolve();
        this._flushing = true;
        return this.postKots(waiting.slice(0, 50), true).then(function (results) {
          (results || []).forEach(function (r) {
            var k = self.kotQueue.find(function (x) { return x.uuid === r.uuid; });
            if (!k) return;
            if (r.status === "rejected") k.error = r.error || "The server refused this KOT.";
            else self.kotQueue = self.kotQueue.filter(function (x) { return x !== k; });
          });
          self.saveQueue();
        }).catch(function () {}).then(function () { self._flushing = false; });
      },
      dropKot: function (k) {
        if (!window.confirm("Drop this KOT? It won't reach the books - the kitchen slip already printed stays as it is.")) return;
        this.kotQueue = this.kotQueue.filter(function (x) { return x !== k; });
        this.saveQueue();
        this.refreshTables();
      },
      // This phone's own KOT numbers while offline: its series code and a
      // count that starts again each day (O1-1, O1-2...).
      localRef: function () {
        var d = (window.ArfaApp && ArfaApp.load()) || {};
        var code = d.code || (window.ArfaOffline && ArfaOffline.deviceCode()) || "P";
        var today = new Date().toISOString().slice(0, 10), seq = read(SEQ_KEY, {day: today, n: 0});
        if (seq.day !== today) seq = {day: today, n: 0};
        seq.n += 1;
        write(SEQ_KEY, seq);
        return code + "-" + seq.n;
      },

      // --- KOT slips ----------------------------------------------------------------------------
      kotDoc: function (number, title, items) {
        var now = new Date();
        return {
          header: [cfg.business], copy_label: null, number: number, title: title,
          meta: [["Time", now.toLocaleString("en-IN", {day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", hour12: false})],
                 ["By", (this.cashier && this.cashier.name) || cfg.branch]],
          lines: items.map(function (i) {
            var q = Number(i.qty);
            return {desc: (q < 0 ? "CANCEL " : "") + i.name, details: i.note ? ["* " + i.note] : [],
                    qty: P.qtyText(Math.abs(q)) + " x", amount: ""};
          }),
          totals: [], grand: null, payments: [], status: null, footer: [], qr: null, open_drawer: false
        };
      },
      // A counter (takeaway / delivery) order's slip, from the bill's lines.
      counterKot: function (number) {
        var label = (cfg.order_types.find(function (t) { return t.value === this.orderType; }, this) || {}).label || "";
        return this.kotDoc(number, label, this.cart.map(function (l) {
          return {name: l.description, note: l.note, qty: Number(l.qty)};
        }));
      },
      printKot: function (doc) {
        if (!doc) return;
        if (P.printDocThermal(doc)) return;
        var b = window.AndroidPrint;
        if (b && b.printHtml) { b.printHtml(kotHtml(doc)); return; }
        var frame = document.getElementById("receipt-print-frame");
        if (!frame) return;
        frame.onload = function () { try { frame.contentWindow.print(); } catch (e) {} };
        frame.srcdoc = kotHtml(doc);
      },

      // --- a table billed -----------------------------------------------------------------------
      tableBilled: function (done) {
        var id = this.table.id;
        done.table = this.table.name;
        this.tableBase = {};
        this.tableSig = "";
        if (done.offline) {
          var t = this.tables.find(function (x) { return x.id === id; });
          if (t) { t.lines = []; t.sent = {}; t.busy = false; t.total = 0; t.kots = 0; t.since = ""; this.saveTables(); }
        } else {
          this.refreshTables();
        }
      }
    };
  };
})();
