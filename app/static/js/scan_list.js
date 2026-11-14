/* Scan bills - the list: search as you type (every word must match: the
   supplier, GSTIN, bill number, amounts with or without commas, dates,
   file name), the filters, and sorting by any column - all instant, on the
   bills already on the page. Enter sends the search to the server too, for
   bills beyond the ones shown. The address keeps the search, so a refresh
   or a shared link opens the same list. */
(function () {
  "use strict";
  var finder = document.querySelector("[data-scan-finder]");
  var table = document.querySelector("[data-scan-table]");
  if (!finder) return;
  var search = finder.querySelector("[data-scan-search]");
  var countEl = finder.querySelector("[data-count]");
  var clearBtn = finder.querySelector("[data-clear]");
  var range = finder.querySelector("[data-range]");
  var none = document.querySelector("[data-none]");
  var body = table ? table.tBodies[0] : null;
  var rows = body ? Array.prototype.slice.call(body.rows) : [];

  // What each row is searched on: its text, and the same with every
  // comma, space and rupee sign out (so 225556 finds "2,25,556.23").
  rows.forEach(function (tr) {
    var t = tr.getAttribute("data-text") || "";
    tr._text = t + " " + t.replace(/[,\s₹]/g, "");
    tr.querySelectorAll("[data-hl]").forEach(function (el) { el._plain = el.textContent; });
  });

  function val(name) {
    var el = finder.querySelector("[data-filter='" + name + "']");
    return el ? el.value : "";
  }
  function isoDay(d) { return d.getFullYear() + "-" + ("0" + (d.getMonth() + 1)).slice(-2) + "-" + ("0" + d.getDate()).slice(-2); }
  function since(days) { var d = new Date(); d.setDate(d.getDate() - days); return isoDay(d); }

  function escapeHtml(s) {
    return s.replace(/[&<>"']/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]; });
  }
  function highlight(el, words) {
    var plain = el._plain || "";
    if (!words.length) { el.textContent = plain; return; }
    var re = new RegExp("(" + words.map(function (w) { return w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); }).join("|") + ")", "gi");
    el.innerHTML = escapeHtml(plain).replace(re, "<mark>$1</mark>");
  }

  function apply() {
    var q = search.value.trim().toLowerCase();
    var words = q ? q.split(/\s+/) : [];
    var supplier = val("supplier"), check = val("check"), kind = val("kind"), up = val("uploaded");
    var from = "", to = "";
    if (up === "today") from = isoDay(new Date());
    else if (up === "7") from = since(6);
    else if (up === "30") from = since(29);
    else if (up === "range") { from = val("from"); to = val("to"); }
    if (range) range.hidden = up !== "range";
    var shown = 0;
    rows.forEach(function (tr) {
      var ok = words.every(function (w) { return tr._text.indexOf(w) >= 0 || tr._text.indexOf(w.replace(/[,₹]/g, "")) >= 0; }) &&
        (!supplier || tr.getAttribute("data-supplier") === supplier) &&
        (!check || tr.getAttribute("data-check") === check) &&
        (!kind || tr.getAttribute("data-kind") === kind) &&
        (!from || tr.getAttribute("data-uploaded") >= from) &&
        (!to || tr.getAttribute("data-uploaded") <= to);
      tr.hidden = !ok;
      if (ok) {
        shown += 1;
        var n = tr.querySelector("[data-n]");
        if (n) n.textContent = shown;
        tr.querySelectorAll("[data-hl]").forEach(function (el) { highlight(el, words); });
      }
    });
    if (countEl) countEl.textContent = shown;
    if (none) none.hidden = shown > 0 || !rows.length;
    var active = !!(q || supplier || check || kind || up);
    if (clearBtn) clearBtn.hidden = !active;
    // keep it in the address: a refresh or a shared link shows the same list
    var params = new URLSearchParams(new FormData(finder));
    Array.from(params.keys()).forEach(function (k) { if (!params.get(k)) params.delete(k); });
    history.replaceState(null, "", "?" + params.toString());
  }

  var timer = null;
  search.addEventListener("input", function () { clearTimeout(timer); timer = setTimeout(apply, 40); });
  finder.querySelectorAll("[data-filter]").forEach(function (el) { el.addEventListener("change", apply); });
  if (clearBtn) clearBtn.addEventListener("click", function () {
    search.value = "";
    finder.querySelectorAll("[data-filter]").forEach(function (el) { el.value = ""; });
    apply();
    search.focus();
  });
  // Enter: the server searches every bill in the tab, not only those on the page.
  finder.addEventListener("submit", function () { clearTimeout(timer); });

  document.addEventListener("keydown", function (e) {
    var typing = /^(INPUT|TEXTAREA|SELECT)$/.test((document.activeElement || {}).tagName || "");
    if (e.key === "/" && !typing) { e.preventDefault(); search.focus(); search.select(); }
    else if (e.key === "Escape" && document.activeElement === search && search.value) { search.value = ""; apply(); }
  });

  // --- sorting by a column -------------------------------------------------------------------------
  if (table) {
    var numeric = { taxable: 1, gst: 1, total: 1 };
    table.querySelectorAll("th[data-sort]").forEach(function (th) {
      th.tabIndex = 0;
      th.setAttribute("role", "button");
      th.title = "Sort by this column";
      function sort() {
        var key = th.getAttribute("data-sort");
        var dir = th.getAttribute("aria-sort") === "ascending" ? -1 : 1;
        table.querySelectorAll("th[data-sort]").forEach(function (h) { h.removeAttribute("aria-sort"); });
        th.setAttribute("aria-sort", dir === 1 ? "ascending" : "descending");
        rows.sort(function (a, b) {
          var x = a.getAttribute("data-s-" + key) || "", y = b.getAttribute("data-s-" + key) || "";
          if (numeric[key]) { x = parseFloat(x) || 0; y = parseFloat(y) || 0; }
          return (x < y ? -1 : x > y ? 1 : 0) * dir;
        });
        rows.forEach(function (tr) { body.appendChild(tr); });
        apply();
      }
      th.addEventListener("click", sort);
      th.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); sort(); } });
    });
  }

  if (search.value || val("supplier") || val("check") || val("kind") || val("uploaded")) apply();
})();
