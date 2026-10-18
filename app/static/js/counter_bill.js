/*
 * The ARFA Counter app's Bill screen (counter app, stage 2;
 * app/counterapp/screens.py, templates/counterapp/bill.html).
 *
 * Tap items, see the total, take cash / UPI / card, print. Online, the bill
 * goes through the counter's own checkout (/pos/checkout) - same number
 * series, GST, stock and audit as the full counter screen. With offline
 * billing on and no connection, it is numbered on this phone in its own
 * series and queued (pos_offline.js - ArfaOffline), exactly as the full
 * counter does, and sent when the connection is back.
 *
 * The cart in progress is kept on the phone (ArfaApp.CART_KEY), so a call
 * or a closed app doesn't lose it. Totals are previewed with GstCalc (the
 * server's arithmetic); where a price depends on more than the item (a
 * restaurant's order type and charges, wholesale slabs) the server's quote
 * is used while online. The server's numbers are what's billed.
 */
(function () {
  "use strict";
  var SHOWN = 120;  // tiles drawn at once - search for the rest

  function uuid4() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    return "c" + Date.now().toString(16) + Math.random().toString(16).slice(2, 14);
  }
  function money(x) {
    x = Number(x) || 0;
    var whole = Math.abs(x - Math.round(x)) < 0.005;
    return "₹" + x.toLocaleString("en-IN", {minimumFractionDigits: whole ? 0 : 2, maximumFractionDigits: 2});
  }
  function qtyText(q) { return String(Number(Number(q).toFixed(3))); }
  function thermalReady() {
    var b = window.AndroidPrint;
    return !!(b && b.printRaw && b.hasPrinter && b.hasPrinter() && window.EscPos);
  }
  // A receipt document (app/pos/receipt_doc.py's shape) straight to the
  // thermal printer; false when that isn't possible here.
  function printDocThermal(doc) {
    var b = window.AndroidPrint;
    if (!thermalReady()) return false;
    var cols = b.printerColumns ? b.printerColumns() : 48;
    window.arfaEscPosFailed = function () { if (b.printHtml) b.printHtml(EscPos.html(doc)); };
    return !!b.printRaw(EscPos.toBase64(EscPos.encode(doc, cols)), !!doc.open_drawer);
  }

  window.counterBill = function (cfg) {
    var byId = {};
    cfg.items.forEach(function (it) { byId[it.id] = it; });
    var screen = makeScreen(cfg, byId);
    // Tables & KOT (counter_tables.js), for a restaurant.
    if (cfg.tables_on && window.counterTables) Object.assign(screen, window.counterTables(cfg, byId));
    return screen;
  };

  function makeScreen(cfg, byId) {
    return {
      cfg: cfg,
      q: "", cat: "all", orderType: cfg.order_type || "",
      cart: [],
      sheet: "",          // "" | cart | pay | qty | done
      mode: "", cashGiven: "", busy: false, error: "", toast: "",
      qtyEdit: {id: null, value: ""},
      quote: null, quoteFor: "",
      csrf: cfg.csrf,
      net: {online: navigator.onLine, pending: 0, problems: [], device: null, lastError: ""},
      cashier: cfg.cashier ? cfg.cashier.current : null,
      pin: "", pinError: "", pinBusy: false,
      done: null,         // {number, billId, total, change, offline, doc, printed, shareUrl, kot}
      checkoutId: "",
      // Tables & KOT (counter_tables.js): "bill" the counter's bill,
      // "tables" the restaurant's tables, "table" one table's order.
      view: cfg.view || "bill",
      table: null,

      // --- start ------------------------------------------------------------------------------
      start: function () {
        var self = this;
        window.ARFA_CASHIER = this.cashier;
        if (this.view === "bill") this.restoreCart();
        this.$watch("cart", function () { self.saveCart(); self.askQuote(); });
        this.$watch("orderType", function () { self.askQuote(); });
        if (cfg.offline && window.ArfaOffline) {
          ArfaOffline.onChange(function (st) {
            var was = self.net.pending;
            self.net = st;
            // Bills that were waiting are on the books now - a table they
            // billed is free.
            if (cfg.tables_on && was && !st.pending) self.refreshTables();
          });
          // A table's KOTs reach the server before the bill that settles it.
          ArfaOffline.init(Object.assign({}, cfg.offline, {
            csrf: this.csrf, beforeSync: cfg.tables_on ? function () { return self.flushKots(); } : null
          }));
          setTimeout(function () {
            var scope = cfg.offline.sw_scope;
            ArfaApp.keepForOffline([scope, scope + "bill"].concat(cfg.tables_on ? [scope + "tables"] : []));
          }, 1500);
        } else {
          window.addEventListener("online", function () { self.net = Object.assign({}, self.net, {online: true}); });
          window.addEventListener("offline", function () { self.net = Object.assign({}, self.net, {online: false}); });
        }
        window.addEventListener("online", function () { self.refreshSession(); self.askQuote(); });
        if (navigator.onLine) this.refreshSession();
        if (cfg.tables_on) this.startTables();
        this.askQuote();
      },
      // A fresh CSRF token (a screen opened from the phone's copy holds an
      // old one), signing the phone back in first if its session ended.
      refreshSession: function () {
        var self = this;
        return fetch(cfg.ping_url, {credentials: "same-origin", headers: {"X-Requested-With": "XMLHttpRequest"}})
          .then(function (r) { return r.json(); })
          .then(function (data) {
            if (data.signed_in && data.user_id === cfg.user_id) { self.csrf = data.csrf; return true; }
            return window.ArfaApp.resume().then(function (ok) {
              if (!ok) { window.location.replace("/app/"); return false; }
              return fetch(cfg.ping_url, {credentials: "same-origin"}).then(function (r) { return r.json(); })
                .then(function (again) { if (again.csrf) self.csrf = again.csrf; return !!again.signed_in; });
            });
          }).catch(function () { return false; });
      },

      // --- the items ---------------------------------------------------------------------------
      matches: function () {
        var q = this.q.trim().toLowerCase(), cat = this.cat;
        return cfg.items.filter(function (it) {
          if (cat === "fav" && !it.fav) return false;
          if (cat !== "all" && cat !== "fav" && it.cat !== cat) return false;
          if (!q) return true;
          return it.name.toLowerCase().indexOf(q) >= 0 || (it.sku && it.sku.toLowerCase() === q) || (it.barcode && it.barcode === q);
        });
      },
      shown: function () { return this.matches().slice(0, SHOWN); },
      more: function () { return Math.max(0, this.matches().length - SHOWN); },
      inCart: function (id) {
        var qty = this.cart.reduce(function (s, l) { return l.product_id === id ? s + Number(l.qty) : s; }, 0);
        return qty ? qtyText(qty) : "";
      },
      tap: function (it) {
        if (it.counter_only) { this.say(it.name + " is billed on the full counter (it needs a serial number, a prescription or the day's rate)."); return; }
        if (it.sold_out) { this.say(it.name + " is sold out today."); return; }
        if (it.loose) { this.qtyEdit = {id: it.id, value: this.inCart(it.id), name: it.name, unit: it.unit, from: ""}; this.sheet = "qty"; return; }
        this.add(it, 1);
        if (navigator.vibrate) navigator.vibrate(12);
      },
      add: function (it, qty) {
        var line = this.cart.find(function (l) { return l.product_id === it.id && !l.note; });
        if (line) { line.qty = Number((Number(line.qty) + qty).toFixed(3)); }
        else { this.cart.push(this.lineFor(it, qty, "")); }
      },
      lineFor: function (it, qty, note) {
        return {product_id: it.id, description: it.name, hsn: it.hsn, qty: qty, rate: it.rate,
                gst_rate: it.gst_rate, incl: it.incl, unit: it.unit, loose: it.loose, note: note || ""};
      },
      step: function (i, by) {
        var line = this.cart[i];
        if (!line) return;
        if (line.loose) { this.qtyEdit = {id: line.product_id, value: qtyText(line.qty), name: line.description, unit: line.unit, from: "cart"}; this.sheet = "qty"; return; }
        var next = Number(line.qty) + by;
        if (next <= 0) this.cart.splice(i, 1); else line.qty = next;
        if (!this.cart.length) this.sheet = "";
      },
      key: function (k) {
        var v = this.qtyEdit.value;
        if (k === "back") v = v.slice(0, -1);
        else if (k === "." && v.indexOf(".") >= 0) return;
        else v = (v + k).replace(/^0+(?=\d)/, "");
        this.qtyEdit.value = v;
      },
      saveQty: function () {
        var id = this.qtyEdit.id, qty = Number(this.qtyEdit.value);
        var i = this.cart.findIndex(function (l) { return l.product_id === id && !l.note; });
        if (!(qty > 0)) { if (i >= 0) this.cart.splice(i, 1); }
        else if (i >= 0) this.cart[i].qty = Number(qty.toFixed(3));
        else this.add(byId[id], Number(qty.toFixed(3)));
        this.sheet = this.qtyEdit.from === "cart" && this.cart.length ? "cart" : "";
      },
      clearBill: function () {
        if (this.cart.length && !window.confirm("Clear this bill?")) return;
        this.cart = []; this.sheet = ""; this.checkoutId = "";
      },

      // --- the cart kept on the phone ---------------------------------------------------------------
      owner: function () { return cfg.tenant_id + ":" + cfg.user_id; },
      saveCart: function () {
        if (this.view !== "bill") return;  // a table's order lives on the table
        try { localStorage.setItem(ArfaApp.CART_KEY, JSON.stringify({owner: this.owner(), lines: this.cart, at: Date.now()})); } catch (e) {}
      },
      restoreCart: function () {
        try {
          var saved = JSON.parse(localStorage.getItem(ArfaApp.CART_KEY) || "null");
          if (!saved || saved.owner !== this.owner() || Date.now() - saved.at > 12 * 3600 * 1000) return;
          // Prices and tax as the items are now; an item since removed drops out.
          this.cart = saved.lines.filter(function (l) { return byId[l.product_id]; }).map(function (l) {
            var it = byId[l.product_id];
            return {product_id: it.id, description: it.name, hsn: it.hsn, qty: l.qty, rate: it.rate,
                    gst_rate: it.gst_rate, incl: it.incl, unit: it.unit, loose: it.loose};
          });
        } catch (e) {}
      },

      // --- totals ------------------------------------------------------------------------------
      cartKey: function () {
        return (this.table ? "T" + this.table.id : this.orderType) + "|" +
          this.cart.map(function (l) { return l.product_id + "x" + l.qty; }).join(",");
      },
      quoted: function () { return this.quote && this.quoteFor === this.cartKey() && this.net.online ? this.quote : null; },
      askQuote: function () {
        var self = this;
        if (!cfg.quote_url || !this.cart.length || !navigator.onLine) return;
        clearTimeout(this._quoteTimer);
        this._quoteTimer = setTimeout(function () {
          var key = self.cartKey(), body = self.formData();
          fetch(cfg.quote_url, {method: "POST", body: body, credentials: "same-origin",
                                headers: {"X-Requested-With": "XMLHttpRequest"}})
            .then(function (r) { return r.ok ? r.json() : null; })
            .then(function (data) { if (data && key === self.cartKey()) { self.quote = data; self.quoteFor = key; } })
            .catch(function () {});
        }, 250);
      },
      // Both safe while Alpine is still taking down a row just removed.
      rateOf: function (i) {
        var q = this.quoted(), line = this.cart[i];
        if (!line) return 0;
        return q && q.lines[i] ? q.lines[i].rate : line.rate;
      },
      lineCalc: function (i) {
        var q = this.quoted(), line = this.cart[i];
        if (!line) return {taxable: 0, cgst: 0, sgst: 0, igst: 0, total: 0};
        var incl = q && q.lines[i] ? q.lines[i].incl : line.incl;
        return GstCalc.line(line.qty, this.rateOf(i), 0, line.gst_rate, incl, cfg.charges_gst, true, 0);
      },
      charges: function () {
        var q = this.quoted();
        if (!q) return [];
        return q.charges.map(function (c) {
          return {description: c.description, amount: GstCalc.line(c.qty, c.rate, 0, c.gst_rate, false, cfg.charges_gst, true, 0).total};
        });
      },
      totals: function () {
        var taxable = 0, tax = 0, exact = 0;
        for (var i = 0; i < this.cart.length; i++) {
          var r = this.lineCalc(i);
          taxable += r.taxable; tax += r.cgst + r.sgst + r.igst; exact += r.total;
        }
        this.charges().forEach(function (c) { exact += c.amount; });
        // One rupee round-off on the whole bill, half-up (compute_invoice_totals).
        var grand = Math.floor(GstCalc.round2(exact) + 0.5 + 1e-9);
        return {taxable: GstCalc.round2(taxable), tax: GstCalc.round2(tax), roundOff: GstCalc.round2(grand - exact), grand: grand};
      },
      count: function () { return this.cart.reduce(function (s, l) { return s + (l.loose ? 1 : Number(l.qty)); }, 0); },
      money: money,
      qtyText: qtyText,

      // --- paying ------------------------------------------------------------------------------
      pay: function (mode) {
        this.mode = mode; this.cashGiven = ""; this.error = "";
        this.sheet = "pay";
      },
      quickCash: function () {
        var total = this.totals().grand, out = [total];
        [50, 100, 200, 500, 2000].forEach(function (n) {
          var up = Math.ceil(total / n) * n;
          if (up > total && out.indexOf(up) < 0 && out.length < 4) out.push(up);
        });
        return out;
      },
      change: function () {
        var given = Number(this.cashGiven);
        return given ? GstCalc.round2(given - this.totals().grand) : null;
      },
      upiQr: function () {
        if (!cfg.upi_qr_url || !this.net.online) return "";
        return cfg.upi_qr_url + "?amount=" + this.totals().grand;
      },
      modeLabel: function (value) {
        var m = cfg.modes.find(function (x) { return x.value === value; });
        return m ? m.label : value;
      },
      formData: function () {
        var body = new FormData(), self = this;
        body.append("csrf_token", this.csrf);
        if (this.table) { body.append("order_type", "dine_in"); body.append("table_id", this.table.id); }
        else if (this.orderType) body.append("order_type", this.orderType);
        this.cart.forEach(function (l) {
          body.append("line_product_id[]", l.product_id);
          body.append("line_description[]", l.description);
          body.append("line_hsn[]", l.hsn || "");
          body.append("line_qty[]", l.qty);
          body.append("line_rate[]", l.rate);
          body.append("line_gst_rate[]", l.gst_rate);
          body.append("line_incl[]", l.incl ? "1" : "0");
          body.append("line_unit[]", l.unit);
          body.append("line_discount_kind[]", "");
          body.append("line_discount_value[]", "");
          body.append("line_note[]", l.note || "");
          body.append("line_alt[]", "0");
          body.append("line_net_weight[]", "");
        });
        return body;
      },
      confirm: function () {
        if (this.busy) return;
        if (this.mode === "cash" && this.cashGiven !== "" && Number(this.cashGiven) < this.totals().grand) {
          this.error = "That's less than the bill - take " + money(this.totals().grand) + " or more."; return;
        }
        this.error = "";
        if (!this.checkoutId) this.checkoutId = uuid4();
        if (cfg.offline && !navigator.onLine) return this.saveOffline();
        return this.checkout(true);
      },
      checkout: function (firstTry) {
        var self = this, body = this.formData(), reached = false;
        body.append("checkout_uuid", this.checkoutId);
        body.append("payment_mode", this.mode);
        if (this.mode === "cash" && this.cashGiven !== "") body.append("cash_tendered", this.cashGiven);
        this.busy = true;
        return fetch(cfg.checkout_url, {method: "POST", body: body, credentials: "same-origin",
                                        headers: {"X-Requested-With": "XMLHttpRequest"}})
          .then(function (res) {
            reached = true;
            return res.json().then(function (data) { return {res: res, data: data}; }, function () { return {res: res, data: null}; });
          })
          .then(function (r) {
            if (r.data === null) {
              // Not an answer from checkout: the session ended or the page's
              // token is old. Sign back in, fresh token, once more.
              if (!firstTry) throw new Error("The server didn't take the bill - try again.");
              return self.refreshSession().then(function () { self.busy = false; return self.checkout(false); });
            }
            if (!r.res.ok) {
              self.error = r.data.error || "The bill wasn't saved.";
              if (cfg.cashier && /PIN/.test(self.error)) self.cashier = null;
              return;
            }
            var kot = r.data.kot_no ? self.counterKot("KOT " + r.data.kot_no) : null;
            self.finish({number: r.data.bill_number, billId: r.data.bill_id, total: r.data.grand_total,
                         change: r.data.change_due, offline: false, shareUrl: r.data.share_url, kot: kot});
          })
          .catch(function (e) {
            if (!reached && cfg.offline) { self.busy = false; return self.saveOffline(); }
            self.error = reached ? (e.message || "The bill wasn't saved.") : "No internet - the bill wasn't saved. Try again when you're connected.";
          })
          .then(function () { self.busy = false; });
      },
      saveOffline: function () {
        var self = this, reason = ArfaOffline.blocker(this.cart, {
          billDiscount: false, credits: false, restaurant: this.charges().length > 0,
          mode: this.mode
        });
        if (reason) { this.error = "No internet, and this bill needs it: " + reason; return; }
        this.busy = true;
        var c = this.cashier;
        return ArfaOffline.queueBill(this.cart, {
          mode: this.mode, mode_label: this.modeLabel(this.mode), cash_tendered: this.mode === "cash" ? this.cashGiven : "",
          customer: null, cashier_id: c ? c.id : null, cashier_name: c ? c.name : "",
          // The online attempt that got no answer (if any) - the server then
          // spots it if that attempt did go through.
          uuid: this.checkoutId,
          order_type: this.table ? "dine_in" : (this.orderType || null), table_id: this.table ? this.table.id : null
        }).then(function (doneBill) {
          var change = self.mode === "cash" && self.cashGiven !== "" ? GstCalc.round2(Number(self.cashGiven) - doneBill.total) : null;
          // A takeaway / delivery order still goes to the kitchen.
          var kot = cfg.tables_on && !self.table && self.orderType ? self.counterKot("KOT " + doneBill.number) : null;
          self.finish({number: doneBill.number, billId: null, total: doneBill.total, change: change, offline: true,
                       doc: doneBill.doc, kot: kot});
        }).catch(function (e) {
          self.error = "Couldn't keep the bill on this phone: " + (e && e.message ? e.message : e);
        }).then(function () { self.busy = false; });
      },
      finish: function (d) {
        d.printed = false;
        if (this.table) this.tableBilled(d);
        this.done = d;
        this.cart = [];
        this.checkoutId = "";
        this.quote = null;
        this.sheet = "done";
        if (navigator.vibrate) navigator.vibrate([20, 40, 20]);
        // A thermal printer in the app prints at once; anywhere else the
        // cashier taps Print (a phone browser's print dialog isn't wanted
        // for every bill).
        if (thermalReady()) { this.print(); if (d.kot) this.printKot(d.kot); }
      },
      print: function () {
        var d = this.done, self = this;
        if (!d) return;
        var copy = d.printed;
        d.printed = true;
        if (d.offline) {
          ArfaOffline.printDoc(copy ? Object.assign({}, d.doc, {copy_label: "DUPLICATE COPY", open_drawer: false}) : d.doc);
          return;
        }
        var original = copy ? "" : "original=1";
        var pdf = cfg.receipt_pdf_url.replace("/0/", "/" + d.billId + "/") + "?format=" + cfg.receipt_format + (original ? "&" + original : "");
        var viaPdf = function () {
          if (window.AndroidPrint && window.AndroidPrint.printReceipt) window.AndroidPrint.printReceipt(pdf);
          else window.ArfaPrint.pdf(pdf);
        };
        if (!thermalReady()) { viaPdf(); return; }
        fetch(cfg.receipt_json_url.replace("/0/", "/" + d.billId + "/") + (original ? "?" + original : ""),
              {credentials: "same-origin", headers: {"X-Requested-With": "XMLHttpRequest"}})
          .then(function (r) { if (!r.ok) throw new Error("receipt"); return r.json(); })
          .then(function (doc) { if (!printDocThermal(doc)) viaPdf(); })
          .catch(function () { self.say("Couldn't reach the printer - printing as a PDF."); viaPdf(); });
      },
      share: function () {
        var d = this.done, self = this;
        if (!d || !d.billId) return;
        fetch(cfg.share_url.replace("/0/", "/" + d.billId + "/"), {credentials: "same-origin"})
          .then(function (r) { return r.json(); })
          .then(function (data) { window.location.href = data.whatsapp; })
          .catch(function () { self.say("Couldn't make a share link - check the internet."); });
      },
      newBill: function () {
        this.done = null; this.sheet = ""; this.mode = ""; this.cashGiven = ""; this.error = "";
        if (this.view === "table") this.showTables();
      },

      // --- cashier PIN (feature "cashier_pin") --------------------------------------------------
      press: function (d) { if (this.pin.length < 6) this.pin += d; this.pinError = ""; },
      unlock: function () {
        var self = this, body = new FormData();
        if (this.pin.length < 4 || this.pinBusy) return;
        body.append("csrf_token", this.csrf);
        body.append("pin", this.pin);
        this.pinBusy = true;
        fetch(cfg.cashier.unlock_url, {method: "POST", body: body, credentials: "same-origin",
                                       headers: {"X-Requested-With": "XMLHttpRequest"}})
          .then(function (r) { return r.json(); })
          .then(function (data) {
            if (data.cashier) { self.cashier = data.cashier; window.ARFA_CASHIER = data.cashier; self.pin = ""; return; }
            self.pinError = data.error || "That PIN didn't work.";
            self.pin = "";
          })
          .catch(function () { self.pinError = "No internet - a cashier taps in while online."; })
          .then(function () { self.pinBusy = false; });
      },
      lockCounter: function () {
        var body = new FormData();
        body.append("csrf_token", this.csrf);
        if (this.cashier) body.append("cashier_id", this.cashier.id);
        this.cashier = null;
        window.ARFA_CASHIER = null;
        fetch(cfg.cashier.lock_url, {method: "POST", body: body, credentials: "same-origin",
                                     headers: {"X-Requested-With": "XMLHttpRequest"}}).catch(function () {});
      },

      say: function (text) {
        var self = this;
        this.toast = text;
        clearTimeout(this._toastTimer);
        this._toastTimer = setTimeout(function () { self.toast = ""; }, 3200);
      },
      // Stand-ins for a shop without tables (counter_tables.js replaces them).
      counterKot: function () { return null; },
      printKot: function () {},
      tableBilled: function () {},
      showTables: function () {},
      flushKots: function () { return Promise.resolve(); }
    };
  }

  window.ArfaBillPrint = {thermalReady: thermalReady, printDocThermal: printDocThermal, money: money, qtyText: qtyText, uuid4: uuid4};
})();
