// The app frame (base.html): the side menu and its folded "rail", the
// phone drawer and New sheet, the search palette (Ctrl K) and toasts.
// Plain JS on top of the vendored Alpine; nothing loaded from outside.
document.addEventListener("alpine:init", function () {
  function readJSON(id) {
    var el = document.getElementById(id);
    if (!el) return [];
    try { return JSON.parse(el.textContent); } catch (e) { return []; }
  }

  function store(key, value) {
    try { localStorage.setItem(key, value); } catch (e) { /* private window - just not remembered */ }
  }

  // Appearance (_theme_pick.html): Auto follows the device; Light or Dark
  // stays on this device. _theme_boot.html applies it before first paint.
  window.Alpine.data("themePick", function () {
    var root = document.documentElement;
    return {
      theme: root.getAttribute("data-theme") || "auto",
      choose: function (value) {
        this.theme = value;
        if (value === "auto") { root.removeAttribute("data-theme"); } else { root.setAttribute("data-theme", value); }
        store("arfa.theme", value);
      }
    };
  });

  window.Alpine.data("shell", function () {
    return {
      drawer: false,
      newSheet: false,
      palette: false,
      query: "",
      pick: 0,
      entries: [],

      init: function () {
        this.entries = readJSON("palette-data");
      },

      // --- side menu ---------------------------------------------------------
      toggleRail: function () {
        var root = document.documentElement;
        var folded = !root.classList.contains("is-rail");
        root.classList.toggle("is-rail", folded);
        store(this.$root.dataset.railKey, folded ? "1" : "0");
      },
      railLabel: function () {
        return document.documentElement.classList.contains("is-rail") ? "Open the menu" : "Fold the menu to icons";
      },

      // --- search palette ----------------------------------------------------
      openPalette: function () {
        if (!this.entries.length) return;
        this.query = "";
        this.pick = 0;
        this.palette = true;
        this.drawer = false;
        var self = this;
        this.$nextTick(function () { if (self.$refs.paletteInput) self.$refs.paletteInput.focus(); });
      },
      results: function () {
        var words = this.query.toLowerCase().split(/\s+/).filter(Boolean);
        if (!words.length) {
          return this.entries.filter(function (e) { return e.group !== "Account"; }).slice(0, 9);
        }
        var scored = [];
        this.entries.forEach(function (e) {
          var label = e.label.toLowerCase();
          var hay = label + " " + e.group.toLowerCase() + " " + (e.keywords || "").toLowerCase();
          if (!words.every(function (w) { return hay.indexOf(w) !== -1; })) return;
          var score = 0;
          if (label.indexOf(words[0]) === 0) score += 3;
          if (label.indexOf(words.join(" ")) !== -1) score += 2;
          if (words.every(function (w) { return label.indexOf(w) !== -1; })) score += 1;
          scored.push({ e: e, score: score });
        });
        // Best match first; between equals the shorter name, then the
        // menu's own screens (listed first).
        scored.forEach(function (s, i) { s.i = i; });
        scored.sort(function (a, b) { return b.score - a.score || a.e.label.length - b.e.label.length || a.i - b.i; });
        return scored.slice(0, 12).map(function (s) { return s.e; });
      },
      paletteKey: function (ev) {
        var count = this.results().length;
        if (ev.key === "ArrowDown") { ev.preventDefault(); this.pick = count ? (this.pick + 1) % count : 0; this.scrollPick(); }
        else if (ev.key === "ArrowUp") { ev.preventDefault(); this.pick = count ? (this.pick - 1 + count) % count : 0; this.scrollPick(); }
        else if (ev.key === "Enter") {
          ev.preventDefault();
          var hit = this.results()[this.pick];
          if (hit) window.location.href = hit.url;
        }
      },
      scrollPick: function () {
        var self = this;
        this.$nextTick(function () {
          var el = self.$refs.paletteList && self.$refs.paletteList.querySelector(".is-picked");
          if (el) el.scrollIntoView({ block: "nearest" });
        });
      },
      iconHref: function (name) {
        var sprite = document.getElementById("icon-sprite");
        return (sprite ? sprite.dataset.href : "") + "#i-" + name;
      },

      // --- keyboard ----------------------------------------------------------
      onKey: function (ev) {
        if ((ev.ctrlKey || ev.metaKey) && (ev.key === "k" || ev.key === "K")) {
          if (!this.entries.length) return;
          ev.preventDefault();
          if (this.palette) { this.palette = false; } else { this.openPalette(); }
          return;
        }
        if (ev.key === "Escape") {
          if (this.palette) { this.palette = false; ev.stopPropagation(); }
          else if (this.newSheet) { this.newSheet = false; }
          else if (this.drawer) { this.drawer = false; }
        }
      }
    };
  });

  // A Settings section's form (settings/_layout.html): the save bar slides
  // up once something changes; Discard puts the form back; leaving with
  // unsaved changes asks first. A form that came back with errors starts
  // "changed", so its Save is in view.
  window.Alpine.data("settingsForm", function (errors) {
    return {
      dirty: !!(errors && Object.keys(errors).length),
      submitting: false,
      init: function () {
        var self = this;
        this.$el.classList.add("is-tracking");
        this.$watch("dirty", function (value) { self.$el.classList.toggle("is-dirty", value); });
        this.$el.classList.toggle("is-dirty", this.dirty);
        window.addEventListener("beforeunload", function (ev) {
          if (self.dirty && !self.submitting) { ev.preventDefault(); ev.returnValue = ""; }
        });
      },
      touch: function (ev) {
        if (ev && ev.target && ev.target.closest && ev.target.closest("[data-no-dirty]")) return;
        this.dirty = true;
      },
      discard: function () {
        this.$root.reset();
        this.dirty = false;
        document.dispatchEvent(new CustomEvent("settings:discarded"));
      },
      saving: function () {
        this.submitting = true;
        var button = this.$root.querySelector('.save-bar [type="submit"]');
        if (button) button.classList.add("is-busy");
      }
    };
  });

  // A toast leaves on its own after a few seconds, unless it reports a
  // problem - those stay until closed, so nobody misses one.
  window.Alpine.data("toast", function (category) {
    return {
      shown: true,
      init: function () {
        var self = this;
        if (category !== "error" && category !== "warning") {
          setTimeout(function () { self.shown = false; }, 5000);
        }
      }
    };
  });
});

// Figures marked data-count-up="1234.50" count up once when the page
// opens (skipped for anyone who has asked for less motion).
document.addEventListener("DOMContentLoaded", function () {
  var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  document.querySelectorAll("[data-count-up]").forEach(function (el) {
    var target = parseFloat(el.dataset.countUp);
    if (reduce || !isFinite(target)) return;
    var finalText = el.textContent;
    var decimals = (el.dataset.countUp.split(".")[1] || "").length;
    var start = null;
    function frame(ts) {
      if (start === null) start = ts;
      var t = Math.min(1, (ts - start) / 700);
      var eased = 1 - Math.pow(1 - t, 3);
      el.textContent = (target * eased).toLocaleString("en-IN", { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
      if (t < 1) requestAnimationFrame(frame); else el.textContent = finalText;
    }
    requestAnimationFrame(frame);
  });
});

// Page patterns (redesign stage 3), applied to every page without
// touching its template:
//  - a page header with three or more actions keeps its main one (the
//    green button, else the first) in view and folds the rest into "More";
//    data-keep on an action keeps it out, data-more sends it in;
//  - list tables (no inputs inside) get each cell's column name, so on a
//    phone their rows stack into cards (app.css, table.is-stack).
// The counter (POS) is left alone; it gets its own layout.
(function () {
  function isAction(el) {
    if (el.hasAttribute("data-keep")) return false;
    if (el.matches("a.btn, button.btn")) return true;
    if (el.tagName === "FORM") {
      var controls = el.querySelectorAll("input:not([type=hidden]), select, textarea");
      return controls.length === 0 && el.querySelectorAll(".btn").length === 1;
    }
    return false;
  }

  function closeAll(except) {
    document.querySelectorAll(".more-wrap.is-open").forEach(function (w) {
      if (w === except) return;
      w.classList.remove("is-open");
      w.querySelector(".more-pop").hidden = true;
      w.querySelector(".more-btn").setAttribute("aria-expanded", "false");
    });
  }

  function foldHeader(header) {
    var spots = [header];
    Array.prototype.forEach.call(header.children, function (child, i) {
      if (i > 0 && child.tagName === "DIV") spots.push(child);
    });
    var actions = [];
    spots.forEach(function (spot) {
      Array.prototype.forEach.call(spot.children, function (el) { if (isAction(el)) actions.push(el); });
    });
    var forced = actions.filter(function (el) { return el.hasAttribute("data-more"); });
    if (actions.length < 3 && !forced.length) return;
    var primary = actions.filter(function (el) {
      return !el.hasAttribute("data-more") && (el.matches(".btn-primary") || el.querySelector(".btn-primary"));
    })[0] || actions.filter(function (el) { return !el.hasAttribute("data-more"); })[0];
    var folded = actions.filter(function (el) { return el !== primary; });
    if (!folded.length) return;

    var wrap = document.createElement("div");
    wrap.className = "pop-wrap more-wrap";
    var sprite = document.getElementById("icon-sprite");
    var href = (sprite ? sprite.dataset.href : "") + "#i-chevron-down";
    wrap.innerHTML = '<button type="button" class="btn btn-secondary more-btn" aria-haspopup="menu" aria-expanded="false">' +
      'More <svg class="icon" aria-hidden="true"><use href="' + href + '"></use></svg></button>' +
      '<div class="pop pop-right more-pop" role="menu" hidden></div>';
    var pop = wrap.querySelector(".more-pop");
    folded.forEach(function (el) {
      el.classList.remove("btn-lg");
      el.querySelectorAll(".btn").forEach(function (b) { b.classList.remove("btn-lg"); });
      pop.appendChild(el);
    });
    if (primary) { primary.parentNode.insertBefore(wrap, primary); } else { spots[spots.length - 1].appendChild(wrap); }
    var button = wrap.querySelector(".more-btn");
    button.addEventListener("click", function (ev) {
      ev.stopPropagation();
      var open = !wrap.classList.contains("is-open");
      closeAll(wrap);
      wrap.classList.toggle("is-open", open);
      pop.hidden = !open;
      button.setAttribute("aria-expanded", open ? "true" : "false");
    });
  }

  function labelTable(table) {
    if (table.closest(".no-stack") || table.querySelector("input:not([type=hidden]), select, textarea")) return;
    var heads = Array.prototype.map.call(table.querySelectorAll("thead th"), function (th) { return th.textContent.trim(); });
    if (heads.length < 3) return;
    table.classList.add("is-stack");
    table.querySelectorAll("tbody tr").forEach(function (tr) {
      var col = 0;
      Array.prototype.forEach.call(tr.children, function (cell) {
        var span = cell.colSpan || 1;
        if (span === 1 && heads[col]) cell.setAttribute("data-label", heads[col]);
        col += span;
      });
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    if (document.body.classList.contains("is-counter")) return;
    document.querySelectorAll(".app-main .page-header").forEach(function (header) {
      if (!header.hasAttribute("data-no-more")) foldHeader(header);
    });
    document.querySelectorAll(".app-main table").forEach(labelTable);
    document.addEventListener("click", function (ev) {
      if (!ev.target.closest || !ev.target.closest(".more-pop")) closeAll(null);
    });
    document.addEventListener("keydown", function (ev) { if (ev.key === "Escape") closeAll(null); });
  });
})();

// Accessibility helpers (redesign stage 6), on every page:
//  - a field with no label of its own (filter bars, line-item tables)
//    gets a name for screen readers: its table column heading plus the
//    row's first cell, else its placeholder, else a select's first option;
//  - pop-up menus (role="menu") move between their entries with the
//    arrow keys, Home and End.
(function () {
  function hasLabel(el) {
    if (el.getAttribute("aria-label") || el.getAttribute("aria-labelledby") || el.getAttribute("title")) return true;
    if (el.closest("label")) return true;
    return !!(el.id && document.querySelector('label[for="' + (window.CSS && CSS.escape ? CSS.escape(el.id) : el.id) + '"]'));
  }

  function columnName(el) {
    var cell = el.closest("td, th");
    var table = cell && cell.closest("table");
    if (!table) return "";
    var row = cell.parentElement;
    var col = 0;
    for (var i = 0; i < row.children.length && row.children[i] !== cell; i++) col += row.children[i].colSpan || 1;
    var heads = table.querySelectorAll("thead tr:last-child th");
    var head = heads[col] ? heads[col].textContent.trim() : "";
    if (!head) return "";
    var first = row.children[0];
    var rowName = first && first !== cell && !first.querySelector("input, select, textarea") ? first.textContent.trim() : "";
    return rowName ? head + " - " + rowName : head;
  }

  function nameFields() {
    document.querySelectorAll("input:not([type=hidden]):not([type=submit]):not([type=button]), select, textarea").forEach(function (el) {
      if (hasLabel(el)) return;
      var name = columnName(el) || el.getAttribute("placeholder") ||
        (el.tagName === "SELECT" && el.options.length ? el.options[0].textContent.trim() : "");
      if (name) el.setAttribute("aria-label", name);
    });
  }

  document.addEventListener("DOMContentLoaded", nameFields);
  // Rows added later (a new invoice line) get named too.
  document.addEventListener("focusin", function (ev) {
    var el = ev.target;
    if (el.matches && el.matches("input, select, textarea") && !hasLabel(el)) {
      var name = columnName(el) || el.getAttribute("placeholder");
      if (name) el.setAttribute("aria-label", name);
    }
  });

  document.addEventListener("keydown", function (ev) {
    var menu = ev.target.closest && ev.target.closest('[role="menu"]');
    if (!menu || ["ArrowDown", "ArrowUp", "Home", "End"].indexOf(ev.key) === -1) return;
    var items = Array.prototype.filter.call(menu.querySelectorAll('[role="menuitem"], .pop-item, .btn'), function (el) {
      return el.offsetParent !== null;
    });
    if (!items.length) return;
    ev.preventDefault();
    var at = items.indexOf(document.activeElement);
    var next = ev.key === "Home" ? 0 : ev.key === "End" ? items.length - 1
      : ev.key === "ArrowDown" ? (at + 1) % items.length : (at - 1 + items.length) % items.length;
    items[next].focus();
  });
  // Opening a menu from its button with the arrow keys lands on its first entry.
  document.addEventListener("keydown", function (ev) {
    if (ev.key !== "ArrowDown" || !ev.target.matches || !ev.target.matches('[aria-haspopup="menu"]')) return;
    var wrap = ev.target.closest(".pop-wrap");
    if (!wrap) return;
    ev.preventDefault();
    if (ev.target.getAttribute("aria-expanded") !== "true") ev.target.click();
    setTimeout(function () {
      var first = wrap.querySelector('[role="menu"] [role="menuitem"], [role="menu"] .pop-item, [role="menu"] .btn');
      if (first) first.focus();
    }, 30);
  });
})();
