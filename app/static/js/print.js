// Printing a receipt / return / test-page PDF through the browser.
//
// The PDF is loaded into a hidden frame and that frame's print() is
// called - the print dialog opens over the current screen. Newer Chrome
// shows a PDF in a frame of its own that the page may not reach into
// (a SecurityError: "Blocked a frame ... from accessing a cross-origin
// frame"), so when that call is refused the PDF opens in a new tab
// instead, where the browser's own Print button is one click away; if a
// pop-up blocker stops that too, a note with a link to it appears.
//
// The Android app's own printing (window.AndroidPrint) is handled by the
// callers before they get here.
window.ArfaPrint = (function () {
  function note(url) {
    var stack = document.querySelector(".toast-stack");
    var box = document.createElement("div");
    box.className = "toast toast-info print-note";
    box.setAttribute("role", "status");
    var text = document.createElement("span");
    text.className = "toast-text";
    text.textContent = "This browser can't print straight from here. ";
    var link = document.createElement("a");
    link.href = url;
    link.target = "_blank";
    link.rel = "noopener";
    link.textContent = "Open it to print";
    link.addEventListener("click", function () { box.remove(); });
    text.appendChild(link);
    var close = document.createElement("button");
    close.type = "button";
    close.className = "toast-close";
    close.setAttribute("aria-label", "Close");
    close.textContent = "×";
    close.addEventListener("click", function () { box.remove(); });
    box.appendChild(text);
    box.appendChild(close);
    (stack || document.body).appendChild(box);
    setTimeout(function () { box.remove(); }, 20000);
  }

  function openInTab(url) {
    var win = null;
    try { win = window.open(url, "_blank"); } catch (e) { win = null; }
    if (!win) note(url);
  }

  // pdf(url, {frame: "receipt-print-frame", after: fn}) - "after" runs once
  // the print dialog (or the fallback) is done with.
  function pdf(url, options) {
    options = options || {};
    var frame = document.getElementById(options.frame || "receipt-print-frame");
    var after = options.after;
    if (!frame) {
      openInTab(url);
      if (after) after();
      return;
    }
    frame.onload = function () {
      frame.onload = null;
      var printed = false;
      try {
        frame.contentWindow.focus();
        frame.contentWindow.print();
        printed = true;
      } catch (e) {
        printed = false;
      }
      if (!printed) openInTab(url);
      if (after) after();
    };
    frame.src = url;
  }

  return { pdf: pdf, openInTab: openInTab };
})();
