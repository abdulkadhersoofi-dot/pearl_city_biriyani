/*
 * A receipt document (app/pos/receipt_doc.py, or an offline bill from
 * pos_offline.js) as ESC/POS bytes for a thermal receipt printer - the
 * command set nearly every 58 mm / 80 mm printer understands (Epson
 * ESC/POS). The Android app sends the bytes to the printer over Bluetooth
 * or USB as they are; nothing here talks to any device.
 *
 *   columns: 32 for a 58 mm (2 inch) roll, 48 for 80 mm (3 inch) - Font A.
 *
 * Text is plain ASCII ("Rs." not the rupee sign): printers' built-in code
 * pages differ, ASCII prints the same on all of them.
 */
(function (root) {
  "use strict";
  var ESC = 0x1b, GS = 0x1d;
  var CMD = {
    init: [ESC, 0x40],
    left: [ESC, 0x61, 0], center: [ESC, 0x61, 1],
    boldOn: [ESC, 0x45, 1], boldOff: [ESC, 0x45, 0],
    bigOn: [GS, 0x21, 0x11], bigOff: [GS, 0x21, 0x00],
    feed: function (n) { return [ESC, 0x64, n]; },
    cut: [GS, 0x56, 0x42, 0x00],             // feed to the cutter and cut (partial)
    drawer: [ESC, 0x70, 0x00, 0x19, 0xfa]   // kick the cash drawer on pin 2
  };

  function ascii(text) {
    text = String(text == null ? "" : text).replace(/₹/g, "Rs.").replace(/[‘’]/g, "'").replace(/[“”]/g, '"').replace(/[–—]/g, "-");
    var out = [];
    for (var i = 0; i < text.length; i++) {
      var c = text.charCodeAt(i);
      out.push(c >= 0x20 && c < 0x7f ? c : 0x3f);
    }
    return out;
  }

  // Word-wrap to the paper width; a word longer than a line is split.
  function wrap(text, cols) {
    var words = String(text == null ? "" : text).split(/\s+/).filter(Boolean), lines = [], line = "";
    words.forEach(function (w) {
      while (w.length > cols) {
        if (line) { lines.push(line); line = ""; }
        lines.push(w.slice(0, cols));
        w = w.slice(cols);
      }
      if (!line) line = w;
      else if (line.length + 1 + w.length <= cols) line += " " + w;
      else { lines.push(line); line = w; }
    });
    if (line || !lines.length) lines.push(line);
    return lines;
  }

  // "Label ........ value" on one line (the label wraps if it must).
  function pair(left, right, cols) {
    left = String(left == null ? "" : left);
    right = String(right == null ? "" : right);
    if (left.length + 1 + right.length <= cols) {
      return [left + new Array(cols - left.length - right.length + 1).join(" ") + right];
    }
    var lines = wrap(left, cols);
    var last = lines[lines.length - 1];
    if (last.length + 1 + right.length <= cols) {
      lines[lines.length - 1] = last + new Array(cols - last.length - right.length + 1).join(" ") + right;
    } else {
      lines.push(new Array(Math.max(cols - right.length, 0) + 1).join(" ") + right);
    }
    return lines;
  }

  function encode(doc, columns) {
    var cols = columns === 32 ? 32 : 48;
    var bytes = [];
    var put = function (arr) { for (var i = 0; i < arr.length; i++) bytes.push(arr[i]); };
    var text = function (line) { put(ascii(line)); bytes.push(0x0a); };
    var rule = function () { text(new Array(cols + 1).join("-")); };

    put(CMD.init);
    put(CMD.center);
    (doc.header || []).forEach(function (h, i) {
      if (i === 0) { put(CMD.boldOn); put(CMD.bigOn); wrap(h, Math.floor(cols / 2)).forEach(text); put(CMD.bigOff); put(CMD.boldOff); }
      else wrap(h, cols).forEach(text);
    });
    if (doc.copy_label) { put(CMD.boldOn); text("*** " + doc.copy_label + " ***"); put(CMD.boldOff); }
    put(CMD.left);
    rule();
    put(CMD.center);
    put(CMD.boldOn);
    wrap(doc.number || "", cols).forEach(text);
    if (doc.title) wrap(doc.title, cols).forEach(text);
    put(CMD.boldOff);
    put(CMD.left);
    (doc.meta || []).forEach(function (m) { pair(m[0], m[1], cols).forEach(text); });
    rule();
    (doc.lines || []).forEach(function (l) {
      wrap(l.desc, cols).forEach(text);
      (l.details || []).forEach(function (d) { wrap("  " + d, cols).forEach(text); });
      pair("  " + l.qty, l.amount, cols).forEach(text);
      if (l.discount) wrap("  " + l.discount, cols).forEach(text);
    });
    rule();
    (doc.totals || []).forEach(function (t) { pair(t[0], t[1], cols).forEach(text); });
    if (doc.grand) {
      put(CMD.boldOn); put(CMD.bigOn);
      pair(doc.grand[0], doc.grand[1], Math.floor(cols / 2)).forEach(text);
      put(CMD.bigOff); put(CMD.boldOff);
    }
    rule();
    (doc.payments || []).forEach(function (p) { pair(p[0], p[1], cols).forEach(text); });
    if (doc.status) { rule(); put(CMD.center); put(CMD.boldOn); text(doc.status); put(CMD.boldOff); put(CMD.left); }
    rule();
    put(CMD.center);
    (doc.footer || []).forEach(function (f) { wrap(f, cols).forEach(text); });
    put(CMD.left);
    put(CMD.feed(4));
    put(CMD.cut);
    return new Uint8Array(bytes);
  }

  function escapeHtml(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) { return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]; }); }
  // The same document as a small HTML page - for the browser's print
  // dialog, or Android's system print when no thermal printer is set up.
  function receiptHtml(doc) {
    var row = function (pair, cls) { return "<tr" + (cls ? " class='" + cls + "'" : "") + "><td>" + escapeHtml(pair[0]) + "</td><td style='text-align:right'>" + escapeHtml(pair[1]) + "</td></tr>"; };
    var h = "<!doctype html><html><head><meta charset='utf-8'><style>@page{size:80mm auto;margin:3mm}body{font-family:'Courier New',monospace;font-size:12pt;margin:0}table{width:100%;border-collapse:collapse}td{padding:2px 0;vertical-align:top}.c{text-align:center}.b{font-weight:700}.r{border-top:1px dashed #000;margin:6px 0}.g td{font-size:15pt;font-weight:700}</style></head><body>";
    h += "<div class='c'>" + doc.header.map(function (x, i) { return "<div" + (i === 0 ? " class='b'" : "") + ">" + escapeHtml(x) + "</div>"; }).join("") + "</div>";
    if (doc.copy_label) h += "<div class='c b'>" + escapeHtml(doc.copy_label) + "</div>";
    h += "<div class='r'></div><div class='c b'>" + escapeHtml(doc.number) + "</div><table>" + doc.meta.map(function (m) { return row(m); }).join("") + "</table><div class='r'></div><table>";
    doc.lines.forEach(function (l) { h += "<tr><td colspan='2'>" + escapeHtml(l.desc) + "</td></tr>" + row([l.qty, l.amount]); });
    h += "</table><div class='r'></div><table>" + doc.totals.map(function (t) { return row(t); }).join("") + row(doc.grand, "g") + "</table><div class='r'></div><table>" + doc.payments.map(function (p) { return row(p); }).join("") + "</table>";
    h += "<div class='r'></div>" + doc.footer.map(function (f) { return "<div class='c'>" + escapeHtml(f) + "</div>"; }).join("") + "</body></html>";
    return h;
  }
  function drawerKick() { return new Uint8Array(CMD.drawer); }

  function toBase64(u8) {
    var s = "";
    for (var i = 0; i < u8.length; i++) s += String.fromCharCode(u8[i]);
    return typeof btoa === "function" ? btoa(s) : Buffer.from(u8).toString("base64");
  }

  root.EscPos = {encode: encode, html: receiptHtml, drawerKick: drawerKick, toBase64: toBase64, wrap: wrap, pair: pair, CMD: CMD};
})(typeof window !== "undefined" ? window : globalThis);
