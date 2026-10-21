/*
 * The app's own confirm and question boxes, drawn in the page - never the
 * browser's confirm() / prompt(). Those don't show inside the ARFA Counter
 * Android app (its WebView answers "Cancel" without asking) and look and
 * behave differently from phone to phone.
 *
 * In a template, no script needed:
 *   <form ... data-confirm="Void INV-12?">                 asks before sending
 *   <button ... data-confirm="Mark it bounced?">          asks for that button only
 *   <form ... data-prompt="Why is it voided?" data-prompt-field="reason">
 *       asks for a reason, puts it in the form's "reason" field, then sends
 *   data-confirm-ok="Discard"   the OK button's words (default "OK")
 *   data-danger                 a red OK button (voids, deletes, discards)
 *
 * From a script: ArfaDialog.confirm(message, {ok, danger}) and
 * ArfaDialog.prompt(message, {ok, value, placeholder, required}) return
 * Promises (true/false; the text typed, or null when cancelled).
 */
(function () {
  "use strict";
  if (window.ArfaDialog) return;

  var open = null;  // the box on screen: {close: function (result)}

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  function show(message, opts, withInput) {
    opts = opts || {};
    if (open) open.close(withInput ? null : false);
    return new Promise(function (resolve) {
      var before = document.activeElement;
      var scrim = el("div", "arfa-dialog-scrim");
      var box = el("div", "arfa-dialog");
      box.setAttribute("role", withInput ? "dialog" : "alertdialog");
      box.setAttribute("aria-modal", "true");
      var text = el("p", "arfa-dialog-text", message);
      text.id = "arfa-dialog-text-" + Date.now();
      box.setAttribute("aria-labelledby", text.id);
      box.appendChild(text);
      var input = null, error = null;
      if (withInput) {
        input = el("textarea", "field-input arfa-dialog-input");
        input.rows = 2;
        input.value = opts.value || "";
        if (opts.placeholder) input.placeholder = opts.placeholder;
        input.setAttribute("aria-labelledby", text.id);
        box.appendChild(input);
        error = el("p", "arfa-dialog-error");
        error.hidden = true;
        box.appendChild(error);
        input.addEventListener("input", function () { error.hidden = true; });
      }
      var row = el("div", "arfa-dialog-actions");
      var cancel = el("button", "btn btn-secondary", opts.cancel || (withInput ? "Cancel" : "Go back"));
      cancel.type = "button";
      var ok = el("button", "btn " + (opts.danger ? "btn-danger" : "btn-primary"), opts.ok || "OK");
      ok.type = "button";
      row.appendChild(cancel);
      row.appendChild(ok);
      box.appendChild(row);
      scrim.appendChild(box);
      document.body.appendChild(scrim);
      document.documentElement.classList.add("arfa-dialog-open");

      function close(result) {
        if (!open) return;
        open = null;
        document.removeEventListener("keydown", onKey, true);
        scrim.remove();
        document.documentElement.classList.remove("arfa-dialog-open");
        if (before && before.focus) { try { before.focus({preventScroll: true}); } catch (e) {} }
        resolve(result);
      }
      function accept() {
        if (!withInput) return close(true);
        var value = input.value.trim();
        if (opts.required !== false && !value) {
          error.textContent = "Please write something here first.";
          error.hidden = false;
          input.focus();
          return;
        }
        close(value);
      }
      function onKey(e) {
        if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); close(withInput ? null : false); }
        else if (e.key === "Enter" && !e.shiftKey && (!withInput || e.target === input)) {
          if (e.target === cancel) return;
          e.preventDefault(); e.stopPropagation(); accept();
        } else if (e.key === "Tab") {
          // Keep the keyboard inside the box.
          var items = [input, cancel, ok].filter(Boolean), i = items.indexOf(document.activeElement);
          e.preventDefault();
          items[(i + (e.shiftKey ? items.length - 1 : 1)) % items.length].focus();
        }
      }
      cancel.addEventListener("click", function () { close(withInput ? null : false); });
      ok.addEventListener("click", accept);
      scrim.addEventListener("click", function (e) { if (e.target === scrim) close(withInput ? null : false); });
      document.addEventListener("keydown", onKey, true);
      open = {close: close};
      (input || (opts.danger ? cancel : ok)).focus();
    });
  }

  window.ArfaDialog = {
    confirm: function (message, opts) { return show(message, opts, false); },
    prompt: function (message, opts) { return show(message, opts, true); },
    isOpen: function () { return !!open; }
  };

  // --- forms and buttons marked data-confirm / data-prompt -------------------------------------
  function ask(form, submitter) {
    var source = function (name) {
      if (submitter && submitter.hasAttribute(name)) return submitter.getAttribute(name);
      return form.hasAttribute(name) ? form.getAttribute(name) : null;
    };
    var danger = (submitter && submitter.hasAttribute("data-danger")) || form.hasAttribute("data-danger");
    var question = source("data-prompt"), message = source("data-confirm");
    var step = Promise.resolve(true);
    if (question) {
      step = step.then(function () {
        return ArfaDialog.prompt(question, {ok: "Continue"}).then(function (answer) {
          if (answer === null) return false;
          var name = source("data-prompt-field") || "reason";
          var field = form.elements[name];
          if (!field) {
            field = document.createElement("input");
            field.type = "hidden";
            field.name = name;
            form.appendChild(field);
          }
          field.value = answer;
          return true;
        });
      });
    }
    if (message) {
      step = step.then(function (go) {
        return go && ArfaDialog.confirm(message, {ok: source("data-confirm-ok") || pressedLabel(form, submitter), danger: danger});
      });
    }
    return step;
  }

  // The confirm button says what the pressed button said ("Void",
  // "Discard", "Cancel order"), so the answer reads as the action itself.
  function pressedLabel(form, submitter) {
    var b = submitter || form.querySelector("button[type=submit], button:not([type]), input[type=submit]");
    var text = b ? (b.value && b.tagName === "INPUT" ? b.value : b.textContent || "").replace(/\s+/g, " ").trim() : "";
    return text && text.length <= 28 ? text : "OK";
  }

  function send(form, submitter) {
    form.dataset.arfaAsked = "1";
    try {
      if (form.requestSubmit) { form.requestSubmit(submitter || undefined); return; }
    } catch (e) { /* an old browser: below */ }
    // No requestSubmit: carry the pressed button's name/value by hand.
    if (submitter && submitter.name) {
      var carry = document.createElement("input");
      carry.type = "hidden";
      carry.name = submitter.name;
      carry.value = submitter.value;
      form.appendChild(carry);
    }
    form.submit();
  }

  // The button that sent a form, for browsers whose submit event doesn't
  // say (older Android WebViews).
  var lastPressed = null;
  document.addEventListener("click", function (e) {
    var b = e.target && e.target.closest ? e.target.closest("button, input[type=submit], input[type=image]") : null;
    lastPressed = b && b.form && (b.type === "submit" || b.type === "image") ? b : null;
  }, true);

  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!(form instanceof HTMLFormElement)) return;
    if (form.dataset.arfaAsked) { delete form.dataset.arfaAsked; return; }
    var submitter = e.submitter || (lastPressed && lastPressed.form === form ? lastPressed : null);
    var marked = function (b) { return b && (b.hasAttribute("data-confirm") || b.hasAttribute("data-prompt")); };
    if (!marked(form) && !marked(submitter)) return;
    e.preventDefault();
    e.stopImmediatePropagation();
    ask(form, submitter).then(function (go) { if (go) send(form, submitter); });
  }, true);
})();
