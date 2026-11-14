/* New sale (templates/sell/new_sale.html). As the customer, the items and
   where they come from change, ask the server what saving would make
   (/sell/new/plan.json - app/sell/decide.py) and show it: the sentence above
   the button, the button's own words, the stock beside each line, and the
   serial-number box on a line that needs one to be billed now.
   The server decides again when the form is saved; this only shows it. */
(function () {
  "use strict";
  var box = document.querySelector("[data-sale-outcome]");
  if (!box) return;
  var form = box.closest("form");
  var sentence = box.querySelector("[data-sale-sentence]");
  var problems = box.querySelector("[data-sale-problems]");
  var button = form.querySelector("[data-submit]");
  var url = box.dataset.planUrl;
  var timer = null, last = "", running = 0;

  function rows() { return form.querySelectorAll("tr.line-item-row"); }

  function paint(plan) {
    sentence.textContent = plan.sentence || (plan.problems && plan.problems.length ? "" : "Add an item to see what this will do.");
    problems.innerHTML = "";
    (plan.problems || []).forEach(function (text) {
      var li = document.createElement("li");
      li.textContent = text;
      problems.appendChild(li);
    });
    problems.hidden = !(plan.problems && plan.problems.length);
    box.classList.toggle("has-problems", !!(plan.problems && plan.problems.length));
    if (button && plan.button) button.textContent = plan.button;
    // The plan's lines are the form's non-blank rows, in order.
    var notes = plan.lines || [], n = 0;
    rows().forEach(function (row) {
      var desc = row.querySelector("input[name='line_description[]']");
      var stock = row.querySelector("[data-line-stock]");
      var serials = row.querySelector("[data-line-serials]");
      var note = desc && desc.value.trim() ? notes[n++] : null;
      if (stock) stock.textContent = note ? stockText(note) : "";
      if (serials) serials.hidden = !(note && (note.needs_serials > 0 || serials.value.trim() !== ""));
    });
  }

  function stockText(note) {
    if (note.here === null) return "";
    var parts = ["Here: " + note.here];
    (note.elsewhere || []).forEach(function (w) { parts.push(w.name + ": " + w.free); });
    var doing = [];
    if (note.here_qty && note.here_qty !== "0") doing.push("bill " + note.here_qty);
    if (note.order_qty && note.order_qty !== "0") doing.push("order " + note.order_qty);
    if (note.waiting_qty && note.waiting_qty !== "0") doing.push(note.waiting_qty + " waits for stock");
    return parts.join(" · ") + (doing.length ? " → " + doing.join(", ") : "");
  }

  function ask() {
    var data = new FormData(form);
    var key = new URLSearchParams(data).toString();
    if (key === last) return;
    last = key;
    var mine = ++running;
    fetch(url, { method: "POST", body: data, headers: { "Accept": "application/json", "X-Requested-With": "XMLHttpRequest" },
                 credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (plan) { if (plan && mine === running) paint(plan); })
      .catch(function () { /* offline: the server still decides on save */ });
  }

  function soon() { clearTimeout(timer); timer = setTimeout(ask, 350); }

  form.addEventListener("input", soon);
  form.addEventListener("change", soon);
  form.addEventListener("click", function (e) { if (e.target.closest("button[type=button]")) soon(); });
  soon();
})();
