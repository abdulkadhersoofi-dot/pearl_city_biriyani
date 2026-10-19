/*
 * The Tables page, live (restaurant/routes.py tables ?live=1): every few
 * seconds it asks for the grid again and, when anything changed - a KOT
 * from another counter or a waiter's phone, a table billed, moved or
 * merged - swaps it in and lights up the tables that changed. It waits
 * while a table's "More" panel is open or a field there is being typed in.
 */
(function () {
  "use strict";
  var EVERY = 5000;
  var box = document.getElementById("tables-live");
  if (!box || !window.fetch) return;
  var waiting = null, stopped = false;

  function busy() {
    return !!box.querySelector("details[open]") || box.contains(document.activeElement);
  }
  function tiles(root) {
    var out = {};
    root.querySelectorAll(".table-tile").forEach(function (t) { out[t.getAttribute("data-table-id")] = t.outerHTML; });
    return out;
  }
  function swap(data) {
    var before = tiles(box);
    box.innerHTML = data.html;
    box.setAttribute("data-sig", data.sig);
    box.querySelectorAll(".table-tile").forEach(function (t) {
      var id = t.getAttribute("data-table-id");
      if (before[id] !== t.outerHTML) {
        t.classList.add("is-updated");
        setTimeout(function () { t.classList.remove("is-updated"); }, 2400);
      }
    });
  }
  function look() {
    if (stopped || document.hidden) return;
    if (waiting && !busy()) { swap(waiting); waiting = null; }
    fetch(box.getAttribute("data-url"), {credentials: "same-origin", headers: {"X-Requested-With": "XMLHttpRequest"}})
      .then(function (r) {
        // Signed out (the session ended): stop asking - the next tap signs in.
        if (r.redirected || r.status === 401 || r.status === 403) { stopped = true; return null; }
        return r.ok ? r.json() : null;
      })
      .then(function (data) {
        if (!data || data.sig === box.getAttribute("data-sig")) return;
        if (busy()) waiting = data; else swap(data);
      })
      .catch(function () {});
  }
  setInterval(look, EVERY);
  document.addEventListener("visibilitychange", look);
  window.addEventListener("online", look);
})();
