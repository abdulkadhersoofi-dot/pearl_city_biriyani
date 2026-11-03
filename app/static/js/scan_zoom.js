/* Scan bills - checking a bill: point at (or tap into) a value and see the
   spot on the bill it was read from.

   - Wide screens: the bill sits in a tall viewer beside the invoice; the
     viewer itself glides in on the spot and marks it.
   - Narrower screens (the viewer scrolled away above): a large zoom opens
     beside the field instead.
   - Over the bill itself, a magnifier.

   Each field carries data-zoom='[page, ymin, xmin, ymax, xmax]' (0-1000 of
   that page, from the reader); the viewer carries data-page-url with {n}
   for the page number. Nothing here changes a value. */
(function () {
  "use strict";

  var form = document.querySelector("form[data-page-url]");
  var viewer = document.querySelector("[data-scan-viewer]");
  if (!form && !viewer) return;
  var tpl = (viewer || form).getAttribute("data-page-url");
  var pages = viewer ? parseInt(viewer.getAttribute("data-pages") || "1", 10) : 1;
  var stage = viewer && viewer.querySelector(".scan-stage");
  var stageImg = viewer && viewer.querySelector(".scan-stage-img");
  var stageMark = viewer && viewer.querySelector(".scan-stage-mark");
  var pageLabel = document.querySelector("[data-page-label]");
  var sizes = {}, shownPage = 0, current = null, anchor = null, hideTimer = null, zoomed = false;

  function pageUrl(n) { return tpl.replace("{n}", String(n)); }
  function el(tag, cls, parent) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    (parent || document.body).appendChild(e);
    return e;
  }
  function size(n, done) {
    if (sizes[n]) return done(sizes[n]);
    var img = new Image();
    img.onload = function () { sizes[n] = { w: img.naturalWidth, h: img.naturalHeight }; done(sizes[n]); };
    img.src = pageUrl(n);
  }
  // The value's box with enough around it to read it in context (0-1).
  function region(b, roomX, roomY) {
    var y0 = b[1] / 1000, x0 = b[2] / 1000, y1 = b[3] / 1000, x1 = b[4] / 1000;
    var padX = Math.max((x1 - x0) * 0.3, roomX), padY = Math.max((y1 - y0) * 1.2, roomY);
    return { x0: x0, y0: y0, x1: x1, y1: y1,
             rx0: Math.max(0, x0 - padX), rx1: Math.min(1, x1 + padX),
             ry0: Math.max(0, y0 - padY), ry1: Math.min(1, y1 + padY) };
  }

  // --- the viewer beside the invoice ------------------------------------------------------------
  var fit = null;   // {w, h, left, top} of the page fitted in the viewer

  function layout() {
    if (!viewer || !stageImg.naturalWidth) return;
    var vw = viewer.clientWidth, vh = viewer.clientHeight;
    var k = Math.min(vw / stageImg.naturalWidth, vh / stageImg.naturalHeight);
    var w = stageImg.naturalWidth * k, h = stageImg.naturalHeight * k;
    fit = { w: w, h: h, left: (vw - w) / 2, top: (vh - h) / 2 };
    stage.style.width = w + "px";
    stage.style.height = h + "px";
    stage.style.left = fit.left + "px";
    stage.style.top = fit.top + "px";
  }

  function showPage(n, done) {
    if (!viewer) return;
    n = Math.max(0, Math.min(pages - 1, n));
    if (pageLabel) pageLabel.textContent = "Page " + (n + 1) + " of " + pages;
    if (n === shownPage && stageImg.complete && stageImg.naturalWidth) {
      if (done) done();
      return;
    }
    shownPage = n;
    stageImg.onload = function () { layout(); if (done) done(); };
    stageImg.src = pageUrl(n);
  }

  function unzoom() {
    if (!viewer) return;
    zoomed = false;
    viewer.classList.remove("is-zoomed");
    stage.style.transform = "";
    stageMark.hidden = true;
  }

  function zoomViewer(b) {
    showPage(b[0], function () {
      if (!fit) layout();
      if (!fit) return;
      var r = region(b, 0.05, 0.04);
      var vw = viewer.clientWidth, vh = viewer.clientHeight;
      var regW = (r.rx1 - r.rx0) * fit.w, regH = (r.ry1 - r.ry0) * fit.h;
      var k = Math.max(1, Math.min(vw / regW, vh / regH, 8));
      var cx = (r.rx0 + r.rx1) / 2 * fit.w, cy = (r.ry0 + r.ry1) / 2 * fit.h;
      var tx = vw / 2 - fit.left - cx * k, ty = vh / 2 - fit.top - cy * k;
      // never past the page's edges when it's bigger than the viewer
      var bw = fit.w * k, bh = fit.h * k;
      tx = bw > vw ? Math.min(-fit.left, Math.max(vw - fit.left - bw, tx)) : (vw - bw) / 2 - fit.left;
      ty = bh > vh ? Math.min(-fit.top, Math.max(vh - fit.top - bh, ty)) : (vh - bh) / 2 - fit.top;
      stage.style.transform = "translate(" + tx + "px, " + ty + "px) scale(" + k + ")";
      stageMark.style.left = (r.x0 * 100) + "%";
      stageMark.style.top = (r.y0 * 100) + "%";
      stageMark.style.width = ((r.x1 - r.x0) * 100) + "%";
      stageMark.style.height = ((r.y1 - r.y0) * 100) + "%";
      stageMark.style.borderWidth = (2 / k) + "px";
      stageMark.style.boxShadow = "0 0 0 " + (4000 / k) + "px rgba(20, 24, 22, 0.18)";
      stageMark.hidden = false;
      zoomed = true;
      viewer.classList.add("is-zoomed");
    });
  }

  // The viewer is the zoom when it sits beside the form and is on screen.
  function viewerBeside() {
    if (!viewer || !form) return false;
    var v = viewer.getBoundingClientRect(), f = form.getBoundingClientRect();
    return v.width >= 340 && v.right <= f.left + 2 && v.bottom > 80 && v.top < window.innerHeight - 80;
  }

  if (viewer) {
    if (stageImg.complete && stageImg.naturalWidth) layout();
    stageImg.addEventListener("load", layout);
    window.addEventListener("resize", function () { layout(); if (zoomed) unzoom(); });
    document.querySelectorAll("[data-page-step]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        unzoom();
        showPage(shownPage + parseInt(btn.getAttribute("data-page-step"), 10));
      });
    });
  }

  // --- the zoom beside a field (narrower screens) ------------------------------------------------
  var panel = el("div", "scan-zoom");
  panel.setAttribute("aria-hidden", "true");
  panel.hidden = true;
  var view = el("div", "scan-zoom-view", panel);
  var mark = el("span", "scan-zoom-mark", view);
  var caption = el("span", "scan-zoom-caption", panel);

  function popup(found) {
    var b = found.b, n = b[0];
    size(n, function (s) {
      if (current !== found.host) return;
      var r = region(b, 0.07, 0.035);
      var regW = (r.rx1 - r.rx0) * s.w, regH = (r.ry1 - r.ry0) * s.h;
      var maxW = Math.min(980, window.innerWidth - 32), maxH = Math.min(460, window.innerHeight * 0.5);
      var scale = Math.min(maxW / regW, maxH / regH, 4);
      var pw = Math.round(regW * scale), ph = Math.round(regH * scale);
      view.style.width = pw + "px";
      view.style.height = ph + "px";
      view.style.backgroundImage = "url(\"" + pageUrl(n) + "\")";
      view.style.backgroundSize = (s.w * scale) + "px " + (s.h * scale) + "px";
      view.style.backgroundPosition = (-r.rx0 * s.w * scale) + "px " + (-r.ry0 * s.h * scale) + "px";
      mark.style.left = ((r.x0 - r.rx0) * s.w * scale - 3) + "px";
      mark.style.top = ((r.y0 - r.ry0) * s.h * scale - 3) + "px";
      mark.style.width = ((r.x1 - r.x0) * s.w * scale + 6) + "px";
      mark.style.height = ((r.y1 - r.y0) * s.h * scale + 6) + "px";
      caption.textContent = "As printed on the bill" + (pages > 1 ? " · page " + (n + 1) : "");
      var a = (anchor || found.host).getBoundingClientRect();
      var top = a.bottom + 8;
      if (top + ph + 34 > window.innerHeight) top = Math.max(8, a.top - ph - 34);
      panel.style.top = top + "px";
      panel.style.left = Math.min(Math.max(8, a.left), window.innerWidth - pw - 20) + "px";
      panel.hidden = false;
      requestAnimationFrame(function () { panel.classList.add("is-on"); });
    });
  }

  // --- pointing at a value -------------------------------------------------------------------------
  function boxOf(target) {
    var host = target && target.closest ? target.closest("[data-zoom]") : null;
    if (!host || !form || !form.contains(host)) return null;
    try {
      var b = JSON.parse(host.getAttribute("data-zoom"));
      return Array.isArray(b) && b.length === 5 ? { host: host, b: b } : null;
    } catch (e) { return null; }
  }

  function show(found) {
    if (viewerBeside()) {
      panel.hidden = true;
      panel.classList.remove("is-on");
      zoomViewer(found.b);
    } else {
      unzoom();
      popup(found);
    }
  }

  function hide() {
    current = anchor = null;
    panel.classList.remove("is-on");
    panel.hidden = true;
    unzoom();
  }

  function enter(e) {
    var found = boxOf(e.target);
    if (!found) return;
    clearTimeout(hideTimer);
    // a line's zoom sits by the row pointed at, not under the whole line
    var row = e.target.closest ? e.target.closest("tr") : null;
    var near = row && found.host.contains(row) ? row : found.host;
    if (current === found.host && anchor === near) return;
    current = found.host;
    anchor = near;
    show(found);
  }

  function leave(e) {
    var host = current;
    if (!host) return;
    if (e.relatedTarget && host.contains(e.relatedTarget)) return;
    if (e.type === "mouseout" && host.contains(document.activeElement)) return;   // still typing in it
    clearTimeout(hideTimer);
    hideTimer = setTimeout(hide, 160);
  }

  if (form) {
    form.addEventListener("mouseover", enter);
    form.addEventListener("focusin", enter);
    form.addEventListener("mouseout", leave);
    form.addEventListener("focusout", leave);
  }
  // Scrolling keeps the zoom by its field (or closes it once the field is off screen).
  var queued = false;
  window.addEventListener("scroll", function () {
    if (!current || queued) return;
    queued = true;
    requestAnimationFrame(function () {
      queued = false;
      var found = current && boxOf(current);
      if (!found) return;
      var r = (anchor || found.host).getBoundingClientRect();
      if (r.bottom < 0 || r.top > window.innerHeight) return hide();
      show(found);
    });
  }, { passive: true });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") hide(); });

  // --- the magnifier over the bill ------------------------------------------------------------------
  if (viewer && window.matchMedia && window.matchMedia("(hover: hover)").matches) {
    var lens = el("span", "scan-lens", viewer);
    lens.hidden = true;
    viewer.addEventListener("mousemove", function (e) {
      if (zoomed || !fit) { lens.hidden = true; return; }
      var v = viewer.getBoundingClientRect();
      var fx = (e.clientX - v.left - fit.left) / fit.w, fy = (e.clientY - v.top - fit.top) / fit.h;
      if (fx < 0 || fx > 1 || fy < 0 || fy > 1) { lens.hidden = true; return; }
      var zoom = 2.6, d = lens.offsetWidth || 220;
      lens.hidden = false;
      lens.style.backgroundImage = "url(\"" + stageImg.src + "\")";
      lens.style.backgroundSize = (fit.w * zoom) + "px " + (fit.h * zoom) + "px";
      lens.style.backgroundPosition = (d / 2 - fx * fit.w * zoom) + "px " + (d / 2 - fy * fit.h * zoom) + "px";
      lens.style.left = (e.clientX - v.left - d / 2) + "px";
      lens.style.top = (e.clientY - v.top - d / 2) + "px";
    });
    viewer.addEventListener("mouseleave", function () { lens.hidden = true; });
  }
})();
