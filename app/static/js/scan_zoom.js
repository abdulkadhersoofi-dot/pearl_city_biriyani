/* Scan bills - checking a bill: point at (or tap into) a value and the
   part of the bill it was read from opens enlarged beside it, marked, and
   is outlined on the picture. Over the picture itself, a magnifier.

   Each field carries data-zoom='[page, ymin, xmin, ymax, xmax]' (0-1000 of
   that page, from the reader); the form carries data-page-url with {n}
   for the page number. Nothing here changes a value. */
(function () {
  "use strict";

  var form = document.querySelector("form[data-page-url]");
  if (!form) return;
  var tpl = form.getAttribute("data-page-url");
  var sizes = {}, current = null, anchor = null, hideTimer = null;

  function pageUrl(n) { return tpl.replace("{n}", String(n)); }

  function el(tag, cls, parent) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    (parent || document.body).appendChild(e);
    return e;
  }

  var panel = el("div", "scan-zoom");
  panel.setAttribute("aria-hidden", "true");
  panel.hidden = true;
  var view = el("div", "scan-zoom-view", panel);
  var mark = el("span", "scan-zoom-mark", view);
  var caption = el("span", "scan-zoom-caption", panel);

  var picture = document.querySelector("[data-scan-picture]");
  var picImg = picture && picture.querySelector("img");
  var picMark = picture ? el("span", "scan-pic-mark", picture) : null;
  if (picMark) picMark.hidden = true;

  function size(n, done) {
    if (sizes[n]) return done(sizes[n]);
    var img = new Image();
    img.onload = function () { sizes[n] = { w: img.naturalWidth, h: img.naturalHeight }; done(sizes[n]); };
    img.src = pageUrl(n);
  }

  function boxOf(target) {
    var host = target && target.closest ? target.closest("[data-zoom]") : null;
    if (!host || !form.contains(host)) return null;
    try {
      var b = JSON.parse(host.getAttribute("data-zoom"));
      return Array.isArray(b) && b.length === 5 ? { host: host, b: b } : null;
    } catch (e) { return null; }
  }

  function show(found) {
    var b = found.b, n = b[0];
    size(n, function (s) {
      if (current !== found.host) return;
      var y0 = b[1] / 1000, x0 = b[2] / 1000, y1 = b[3] / 1000, x1 = b[4] / 1000;
      var bw = x1 - x0, bh = y1 - y0;
      // the value with enough around it to read it in context
      var padX = Math.max(bw * 0.3, 0.07), padY = Math.max(bh * 1.2, 0.035);
      var rx0 = Math.max(0, x0 - padX), rx1 = Math.min(1, x1 + padX);
      var ry0 = Math.max(0, y0 - padY), ry1 = Math.min(1, y1 + padY);
      var regW = (rx1 - rx0) * s.w, regH = (ry1 - ry0) * s.h;
      var maxW = Math.min(620, window.innerWidth - 24), maxH = Math.min(300, window.innerHeight * 0.4);
      var scale = Math.min(maxW / regW, maxH / regH, 2.5);
      var pw = Math.round(regW * scale), ph = Math.round(regH * scale);

      view.style.width = pw + "px";
      view.style.height = ph + "px";
      view.style.backgroundImage = "url(\"" + pageUrl(n) + "\")";
      view.style.backgroundSize = (s.w * scale) + "px " + (s.h * scale) + "px";
      view.style.backgroundPosition = (-rx0 * s.w * scale) + "px " + (-ry0 * s.h * scale) + "px";
      mark.style.left = ((x0 - rx0) * s.w * scale - 3) + "px";
      mark.style.top = ((y0 - ry0) * s.h * scale - 3) + "px";
      mark.style.width = (bw * s.w * scale + 6) + "px";
      mark.style.height = (bh * s.h * scale + 6) + "px";
      caption.textContent = "As printed on the bill" + (n > 0 || form.hasAttribute("data-pages") ? " · page " + (n + 1) : "");

      // beside the field: below it, or above when there's no room
      var r = (anchor || found.host).getBoundingClientRect();
      var top = r.bottom + 8;
      if (top + ph + 34 > window.innerHeight) top = Math.max(8, r.top - ph - 34);
      var left = Math.min(Math.max(8, r.left), window.innerWidth - pw - 20);
      panel.style.top = top + "px";
      panel.style.left = left + "px";
      panel.hidden = false;
      requestAnimationFrame(function () { panel.classList.add("is-on"); });
      markPicture(n, x0, y0, bw, bh);
    });
  }

  function markPicture(n, x0, y0, bw, bh) {
    if (!picMark || !picImg || n !== 0 || !picImg.naturalWidth || !picImg.clientWidth) return;
    // the picture is fitted inside its box (object-fit: contain)
    var cw = picImg.clientWidth, ch = picImg.clientHeight;
    var k = Math.min(cw / picImg.naturalWidth, ch / picImg.naturalHeight);
    var dw = picImg.naturalWidth * k, dh = picImg.naturalHeight * k;
    var ox = picImg.offsetLeft + (cw - dw) / 2, oy = picImg.offsetTop + (ch - dh) / 2;
    picMark.style.left = (ox + x0 * dw - 3) + "px";
    picMark.style.top = (oy + y0 * dh - 3) + "px";
    picMark.style.width = (bw * dw + 6) + "px";
    picMark.style.height = (bh * dh + 6) + "px";
    picMark.hidden = false;
  }

  function hide() {
    current = anchor = null;
    panel.classList.remove("is-on");
    panel.hidden = true;
    if (picMark) picMark.hidden = true;
  }

  function enter(e) {
    var found = boxOf(e.target);
    if (!found) return;
    clearTimeout(hideTimer);
    // a line's zoom sits by the row pointed at, not under the whole line
    var row = e.target.closest ? e.target.closest("tr") : null;
    var near = row && found.host.contains(row) ? row : found.host;
    if (current === found.host && anchor === near && !panel.hidden) return;
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
    hideTimer = setTimeout(hide, 120);
  }

  form.addEventListener("mouseover", enter);
  form.addEventListener("focusin", enter);
  form.addEventListener("mouseout", leave);
  form.addEventListener("focusout", leave);
  // Scrolling keeps the zoom beside its field (or closes it once the field is off screen).
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

  // Warm the first page, so the first zoom opens at once.
  if (form.querySelector("[data-zoom]")) size(0, function () {});

  // --- the magnifier over the picture ---------------------------------------------------------
  if (picImg && window.matchMedia && window.matchMedia("(hover: hover)").matches) {
    var lens = el("span", "scan-lens", picture);
    lens.hidden = true;
    var lensSrc = pageUrl(0);
    picImg.addEventListener("mousemove", function (e) {
      var r = picImg.getBoundingClientRect();
      var k = Math.min(r.width / picImg.naturalWidth, r.height / picImg.naturalHeight);
      var dw = picImg.naturalWidth * k, dh = picImg.naturalHeight * k;
      var ox = (r.width - dw) / 2, oy = (r.height - dh) / 2;
      var fx = (e.clientX - r.left - ox) / dw, fy = (e.clientY - r.top - oy) / dh;
      if (fx < 0 || fx > 1 || fy < 0 || fy > 1) { lens.hidden = true; return; }
      var zoom = 3, size = lens.offsetWidth || 190;
      lens.hidden = false;
      lens.style.backgroundImage = "url(\"" + lensSrc + "\")";
      lens.style.backgroundSize = (dw * zoom) + "px " + (dh * zoom) + "px";
      lens.style.backgroundPosition = (size / 2 - fx * dw * zoom) + "px " + (size / 2 - fy * dh * zoom) + "px";
      lens.style.left = (picImg.offsetLeft + ox + fx * dw - size / 2) + "px";
      lens.style.top = (picImg.offsetTop + oy + fy * dh - size / 2) + "px";
    });
    picImg.addEventListener("mouseleave", function () { lens.hidden = true; });
  }
})();
