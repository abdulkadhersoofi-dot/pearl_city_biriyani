/* Small page behaviours set by data- attributes, in place of inline
   onclick="..." / onchange="..." handlers - the Content-Security-Policy
   (app/security_headers.py) refuses those. One listener each on the
   document, so content added later (htmx, dialogs) works too.

     <select data-autosubmit>                 submits its form when changed
     <input type=checkbox checked data-locked> can't be unticked
     <button data-print>                      prints the page
     <a href=... data-share>                  opens a share link (openShare in base.html)
     <input type=checkbox data-check-all=".x"> ticks / unticks every .x
     <button data-clear-inputs=".x">          empties every .x
     <button data-fill-oldest>                fillOldestFirst() (accounts/_fill_oldest.html)
     <select data-split-into="a b">           value "p|q" -> form fields a=p, b=q
     <select data-shows="id" data-shows-when="v">  shows #id only while the value is v
*/
(function () {
  "use strict";

  document.addEventListener("change", function (e) {
    var el = e.target;
    if (!el || !el.matches) return;
    if (el.matches("[data-autosubmit]") && el.form) {
      el.form.submit();
    }
    if (el.matches("[data-split-into]") && el.form) {
      var names = el.getAttribute("data-split-into").split(/\s+/);
      var parts = String(el.value).split("|");
      names.forEach(function (name, i) {
        var field = el.form.elements[name];
        if (field) field.value = parts[i] || "";
      });
    }
    if (el.matches("[data-shows]")) {
      var target = document.getElementById(el.getAttribute("data-shows"));
      if (target) target.style.display = el.value === el.getAttribute("data-shows-when") ? "" : "none";
    }
  });

  document.addEventListener("click", function (e) {
    var start = e.target && e.target.closest ? e.target : null;
    if (!start) return;
    var locked = start.closest("[data-locked]");
    if (locked) { e.preventDefault(); return; }
    var el = start.closest("[data-print],[data-share],[data-check-all],[data-clear-inputs],[data-fill-oldest]");
    if (!el) return;
    if (el.hasAttribute("data-print")) {
      window.print();
    } else if (el.hasAttribute("data-share")) {
      e.preventDefault();
      if (typeof window.openShare === "function") window.openShare(el.href);
      else window.open(el.href, "_blank", "noopener");
    } else if (el.hasAttribute("data-check-all")) {
      document.querySelectorAll(el.getAttribute("data-check-all")).forEach(function (box) { box.checked = el.checked; });
    } else if (el.hasAttribute("data-clear-inputs")) {
      document.querySelectorAll(el.getAttribute("data-clear-inputs")).forEach(function (input) { input.value = ""; });
    } else if (el.hasAttribute("data-fill-oldest")) {
      if (typeof window.fillOldestFirst === "function") window.fillOldestFirst();
    }
  });
})();
