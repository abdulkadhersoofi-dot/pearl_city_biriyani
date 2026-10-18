/*
 * Offline billing for the POS counter (feature "offline_pos"; server side
 * in app/pos/offline.py).
 *
 * - This device registers once and gets its own series code (O1, O2...).
 *   While offline it numbers each bill itself - "O1/2026-27/0001" - so the
 *   number printed on the customer's bill is final.
 * - Bills wait in IndexedDB (they survive closing the browser) and are
 *   sent oldest first whenever the connection is back; the server books
 *   each exactly once (its uuid) and answers synced / failed / rejected.
 * - Only plain bills: catalog items at their price, one payment by cash,
 *   UPI or card, no discounts (see blocker()).
 *
 * Exposes window.ArfaOffline. Uses no library; GstCalc (gst_calc.js) does
 * the tax math, the same as the online preview.
 */
(function () {
  "use strict";
  var DB_NAME = "arfa-offline", STORE = "bills", DB_VERSION = 1;
  var cfg = null, device = null, listeners = [], syncing = false;
  // online: the browser has a network AND the server answered last time.
  var state = {online: navigator.onLine, pending: 0, problems: [], device: null, lastError: ""};
  var reachable = true;

  // --- storage -----------------------------------------------------------------------------
  function deviceKey() { return "arfa.device." + cfg.tenant_id + "." + cfg.user_id; }
  function loadDevice() {
    try { return JSON.parse(localStorage.getItem(deviceKey()) || "null"); } catch (e) { return null; }
  }
  function saveDevice(d) {
    device = d;
    try { localStorage.setItem(deviceKey(), JSON.stringify(d)); } catch (e) {}
    state.device = d ? {code: d.code, name: d.name} : null;
  }
  function openDb() {
    return new Promise(function (resolve, reject) {
      var req = indexedDB.open(DB_NAME, DB_VERSION);
      req.onupgradeneeded = function () {
        var store = req.result.createObjectStore(STORE, {keyPath: "uuid"});
        store.createIndex("owner", "owner");
      };
      req.onsuccess = function () { resolve(req.result); };
      req.onerror = function () { reject(req.error); };
    });
  }
  function tx(mode, fn) {
    return openDb().then(function (db) {
      return new Promise(function (resolve, reject) {
        var t = db.transaction(STORE, mode);
        var result = fn(t.objectStore(STORE));
        t.oncomplete = function () { resolve(result && result.result !== undefined ? result.result : result); };
        t.onerror = function () { reject(t.error); };
      });
    });
  }
  function owner() { return cfg.tenant_id + ":" + cfg.user_id; }
  function allBills() {
    return tx("readonly", function (s) { return s.index("owner").getAll(owner()); }).then(function (rows) {
      return (rows || []).sort(function (a, b) { return a.created - b.created; });
    });
  }
  function putBill(row) { return tx("readwrite", function (s) { s.put(row); }); }
  function deleteBill(uuid) { return tx("readwrite", function (s) { s.delete(uuid); }); }

  // --- numbering ---------------------------------------------------------------------------
  function financialYear(d) {
    var y = d.getMonth() >= 3 ? d.getFullYear() : d.getFullYear() - 1;
    return y + "-" + String(y + 1).slice(-2);
  }
  function pad4(n) { return n < 10000 ? ("000" + n).slice(-4) : String(n); }
  function uuid4() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    var b = new Uint8Array(16);
    crypto.getRandomValues(b);
    b[6] = (b[6] & 0x0f) | 0x40; b[8] = (b[8] & 0x3f) | 0x80;
    var h = Array.prototype.map.call(b, function (x) { return ("0" + x.toString(16)).slice(-2); }).join("");
    return h.slice(0, 8) + "-" + h.slice(8, 12) + "-" + h.slice(12, 16) + "-" + h.slice(16, 20) + "-" + h.slice(20);
  }
  // {financial year: next number}; anything else stored (a phone paired
  // before stage 2 kept a bare number) starts over from the server's count.
  function nextMap(next) { return next && typeof next === "object" ? next : {}; }
  // The next number in this device's series - under a cross-tab lock where
  // the browser has one, so two open tabs can't hand out the same number.
  function withLock(fn) {
    if (navigator.locks && navigator.locks.request) return navigator.locks.request("arfa-offline-number", fn);
    return Promise.resolve().then(fn);
  }
  function takeNumber(now) {
    return withLock(function () {
      var d = loadDevice() || device;
      var fy = financialYear(now);
      d.next = nextMap(d.next);
      var seq = d.next[fy] || 1;
      d.next[fy] = seq + 1;
      saveDevice(d);
      return {fy: fy, seq: seq, number: d.code + "/" + fy + "/" + pad4(seq)};
    });
  }
  // The server's count can only move the device forward, never back.
  function mergeNext(fy, serverNext) {
    if (!device || !fy || !serverNext) return;
    device.next = nextMap(device.next);
    device.next[fy] = Math.max(device.next[fy] || 1, serverNext);
    saveDevice(device);
  }

  // --- talking to the server ------------------------------------------------------------------
  function headers(extra) {
    var h = {"X-Requested-With": "XMLHttpRequest", "X-Background": "1"};
    if (device) { h["X-Device-Id"] = String(device.id); h["X-Device-Token"] = device.token; }
    return Object.assign(h, extra || {});
  }
  // A reply that isn't JSON is the sign-in page (session ended) or an error page.
  function readJson(res) {
    return res.json().then(function (data) { return {ok: res.ok, status: res.status, data: data}; }, function () {
      throw new Error(res.redirected || res.status === 401 ? "Sign in again to send the bills waiting on this device."
                                                          : "The server didn't answer properly - it will be tried again.");
    });
  }
  function getJson(url) {
    return fetch(url, {headers: headers(), credentials: "same-origin"}).then(function (res) {
      reachable = true;
      return readJson(res);
    });
  }
  function postJson(url, body, csrf) {
    return fetch(url, {method: "POST", credentials: "same-origin",
                       headers: headers({"Content-Type": "application/json", "X-CSRFToken": csrf || cfg.csrf}),
                       body: JSON.stringify(body)}).then(readJson);
  }
  function register() {
    // One registration per device, even with two tabs opening at once.
    return withLock(function () {
      var stored = loadDevice();
      if (stored) { device = stored; state.device = {code: stored.code, name: stored.name}; return null; }
      return registerNow();
    });
  }
  function registerNow() {
    return postJson(cfg.register_url, {platform: window.AndroidPrint ? "android" : "web"}).then(function (r) {
      if (!r.ok) throw new Error(r.data.error || "Could not register this device.");
      var next = {};
      next[r.data.fy] = r.data.next_number;
      saveDevice({id: r.data.device_id, token: r.data.token, code: r.data.code, name: r.data.name, next: next});
    });
  }

  function refreshCounts() {
    return allBills().then(function (rows) {
      state.pending = rows.filter(function (r) { return r.status === "queued"; }).length;
      state.problems = rows.filter(function (r) { return r.status !== "queued"; });
      notify();
      return rows;
    }).catch(function () { notify(); });
  }

  function flush() {
    if (syncing || !device || !navigator.onLine) { notify(); return Promise.resolve(); }
    syncing = true;
    return allBills().then(function (rows) {
      var queued = rows.filter(function (r) { return r.status === "queued"; });
      if (!queued.length) return getJson(cfg.state_url).then(function (s) { if (s.ok) mergeNext(s.data.fy, s.data.next_number); });
      return getJson(cfg.state_url).then(function (s) {
        if (!s.ok) {
          state.lastError = s.data.error || "The server didn't accept this device.";
          throw new Error(state.lastError);
        }
        mergeNext(s.data.fy, s.data.next_number);
        var batch = queued.slice(0, 20);
        return postJson(cfg.sync_url, {bills: batch.map(function (r) { return r.payload; })}, s.data.csrf_token).then(function (r) {
          if (!r.ok) { state.lastError = r.data.error || "Sending bills failed."; throw new Error(state.lastError); }
          state.lastError = "";
          mergeNext(r.data.fy, r.data.next_number);
          var byUuid = {};
          batch.forEach(function (row) { byUuid[row.uuid] = row; });
          return Promise.all((r.data.results || []).map(function (res) {
            var row = byUuid[res.uuid];
            if (!row) return null;
            // Synced, or held on the server for the Client Admin: either
            // way it is the server's now and leaves this device.
            if (res.status === "synced" || res.status === "failed" || res.status === "voided") return deleteBill(row.uuid);
            row.status = "rejected";
            row.error = res.error || "The server refused this bill.";
            return putBill(row);
          })).then(function () { if (queued.length > batch.length) { syncing = false; return flush(); } });
        });
      });
    }).catch(function (e) {
      // No answer at all: just offline. Anything else is the server
      // refusing - say why on the counter.
      if (e instanceof TypeError) reachable = false;
      else state.lastError = state.lastError || (e && e.message) || "";
    }).then(function () {
      syncing = false;
      return refreshCounts();
    });
  }

  function notify() {
    state.online = navigator.onLine && reachable;
    listeners.forEach(function (fn) { try { fn(Object.assign({}, state)); } catch (e) {} });
  }

  // --- what can be billed offline ----------------------------------------------------------------
  // A reason this cart can't be billed offline, or "" when it can.
  function blocker(cart, extras) {
    if (!cfg) return "Offline billing is off.";
    if (!device) return "This device isn't set up for offline billing yet - open the POS once while online.";
    if (!cart.length) return "The cart is empty.";
    for (var i = 0; i < cart.length; i++) {
      var l = cart[i];
      if (!l.product_id) return l.description + " isn't a catalog item.";
      if (cfg.blocked_ids.indexOf(l.product_id) >= 0) return l.description + " needs the server's checks (serial number, margin scheme, jewellery or prescription).";
      if (Number(l.disc_value)) return "Discounts need the connection.";
      if (l.alt) return "Selling by the pack needs the connection.";
    }
    if (extras.billDiscount) return "Discounts need the connection.";
    if (extras.credits) return "Exchange, old gold and points need the connection.";
    if (extras.restaurant) return "Restaurant charges and tables need the connection.";
    if (cfg.modes.indexOf(extras.mode) < 0) return "Offline, a bill is paid by cash, UPI or card in one go.";
    return "";
  }

  // --- the receipt (same shape as app/pos/receipt_doc.py) ---------------------------------------
  function money(x) { return GstCalc.round2(Number(x) || 0).toFixed(2); }
  function qty(x) { return String(Number(x)); }
  function receiptDoc(bill) {
    var meta = [["Date", bill.billed_label]];
    if (bill.customer && bill.customer.name) meta.push(["Customer", bill.customer.name]);
    if (bill.cashier_name) meta.push(["Cashier", bill.cashier_name]);
    meta.push(["Counter", device.code + " (billed offline)"]);
    var totals = [["Taxable value", money(bill.taxable)]];
    if (bill.cgst) totals.push(["CGST", money(bill.cgst)]);
    if (bill.sgst) totals.push(["SGST", money(bill.sgst)]);
    totals.push(["Round off", money(bill.round_off)]);
    var payments = [["Paid via", bill.mode_label.toUpperCase()]];
    if (bill.cash_tendered !== null && bill.cash_tendered !== undefined && bill.cash_tendered !== "") {
      payments.push(["Cash received", money(bill.cash_tendered)]);
      payments.push(["Change", money(Number(bill.cash_tendered) - bill.total)]);
    }
    return {
      header: [cfg.receipt.name].concat(cfg.receipt.gstin ? ["GSTIN: " + cfg.receipt.gstin] : []),
      copy_label: bill.copy_label || null, number: bill.number, title: null, meta: meta,
      lines: bill.lines.map(function (l) { return {desc: l.desc, details: [], qty: qty(l.qty) + " " + l.unit + " x " + money(l.rate), amount: money(l.amount), discount: null}; }),
      totals: totals, grand: ["TOTAL", money(bill.total)], payments: payments, status: null,
      footer: ["Thank you, visit again!"], qr: null, open_drawer: !bill.copy_label && bill.mode === "cash"
    };
  }

  // Bill a cart offline. `cart` lines as the terminal holds them. Returns
  // {number, doc} once it is safely queued on this device.
  function queueBill(cart, opts) {
    var now = new Date();
    var lines = cart.map(function (l) {
      var r = GstCalc.line(l.qty, l.rate, 0, l.gst_rate, l.incl, cfg.charges_gst, true, 0);
      return {product_id: l.product_id, desc: l.description, qty: Number(l.qty), unit: l.unit, rate: Number(l.rate), amount: r.total, r: r};
    });
    var exact = lines.reduce(function (s, l) { return s + l.r.total; }, 0);
    var total = Math.floor(GstCalc.round2(exact) + 0.5 + 1e-9);
    var taxable = lines.reduce(function (s, l) { return s + l.r.taxable; }, 0);
    var cgst = lines.reduce(function (s, l) { return s + l.r.cgst; }, 0);
    var sgst = lines.reduce(function (s, l) { return s + l.r.sgst; }, 0);
    return takeNumber(now).then(function (n) {
      var payload = {
        uuid: opts.uuid || uuid4(), number: n.number, fy: n.fy, seq: n.seq, billed_at: now.toISOString(),
        lines: lines.map(function (l) { return {product_id: l.product_id, qty: String(l.qty), rate: String(l.rate)}; }),
        payment_mode: opts.mode, cash_tendered: opts.cash_tendered || null, total: total.toFixed(2),
        customer: opts.customer || null, cashier_id: opts.cashier_id || null
      };
      var bill = {number: n.number, billed_label: now.toLocaleString("en-IN", {day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit", hour12: false}),
                  lines: lines, total: total, taxable: GstCalc.round2(taxable), cgst: GstCalc.round2(cgst), sgst: GstCalc.round2(sgst),
                  round_off: GstCalc.round2(total - GstCalc.round2(exact)), mode: opts.mode, mode_label: opts.mode_label || opts.mode,
                  cash_tendered: opts.cash_tendered, customer: opts.customer, cashier_name: opts.cashier_name};
      var doc = receiptDoc(bill);
      reachable = false;  // it was billed here because the server couldn't be reached
      return putBill({uuid: payload.uuid, owner: owner(), created: Date.now(), status: "queued", payload: payload, doc: doc})
        .then(function () { refreshCounts(); return {number: n.number, doc: doc, total: total}; });
    });
  }

  // --- printing ----------------------------------------------------------------------------------
  // Prints a receipt document: straight to the thermal printer in the
  // Android app (ESC/POS), else through the browser's print dialog.
  function printDoc(doc) {
    var bridge = window.AndroidPrint;
    if (bridge && bridge.printRaw && window.EscPos && (!bridge.hasPrinter || bridge.hasPrinter())) {
      var cols = bridge.printerColumns ? bridge.printerColumns() : 48;
      window.arfaEscPosFailed = function () { if (bridge.printHtml) bridge.printHtml(EscPos.html(doc)); };
      if (bridge.printRaw(EscPos.toBase64(EscPos.encode(doc, cols)), !!doc.open_drawer)) return true;
    }
    var html = EscPos.html(doc);
    if (bridge && bridge.printHtml) { bridge.printHtml(html); return true; }
    var frame = document.getElementById("receipt-print-frame");
    if (!frame) return false;
    frame.onload = function () { try { frame.contentWindow.print(); } catch (e) {} };
    frame.srcdoc = html;
    return true;
  }

  // --- start -------------------------------------------------------------------------------------
  function init(config) {
    cfg = config;
    device = loadDevice();
    state.device = device ? {code: device.code, name: device.name} : null;
    if ("serviceWorker" in navigator && cfg.sw_url) {
      navigator.serviceWorker.register(cfg.sw_url, {scope: cfg.sw_scope}).catch(function () {});
    }
    window.addEventListener("online", function () { notify(); flush(); });
    window.addEventListener("offline", notify);
    // Try again every 20 s while bills wait (every minute otherwise).
    var tick = 0;
    setInterval(function () { tick++; if (navigator.onLine && (state.pending || tick % 3 === 0)) flush(); }, 20000);
    var ready = Promise.resolve();
    if (!device && navigator.onLine) ready = register().catch(function (e) { state.lastError = e.message; });
    return ready.then(function () { notify(); return flush(); });
  }

  function onChange(fn) { listeners.push(fn); fn(Object.assign({}, state)); }

  window.ArfaOffline = {
    init: init, onChange: onChange, flush: flush, blocker: blocker, queueBill: queueBill, printDoc: printDoc,
    financialYear: financialYear,
    deviceCode: function () { return device ? device.code : null; }
  };
})();
