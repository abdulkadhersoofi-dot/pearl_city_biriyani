/* Scan bills (app/scan): pick or photograph bills, shrink photos on the
   phone before they travel (a 4 MB photo becomes ~400 KB - the slow part
   on mobile data), upload three at a time, then read two at a time. A PDF
   that turns out to hold several invoices comes back as its parts, which
   join the queue. Documents uploaded earlier and still waiting are read
   as soon as the page opens. */
(function () {
  "use strict";
  var root = document.getElementById("scan-app");
  if (!root) return;
  var csrf = root.dataset.csrf;
  var uploadUrl = root.dataset.upload;
  var readUrl = function (id) { return root.dataset.read.replace(/0\/read$/, id + "/read"); };
  var reviewUrl = function (id) { return root.dataset.review.replace(/0$/, String(id)); };
  var ready = root.dataset.ready === "1";
  var list = root.querySelector("[data-scan-queue]");
  var doneBox = root.querySelector("[data-scan-done]");
  var MAX_PX = 2400, QUALITY = 0.85, UPLOADS = 3, READS = 2;
  var uploadQ = [], readQ = [], uploading = 0, reading = 0, toCheck = 0;

  function row(name) {
    var li = document.createElement("li");
    li.className = "scan-row";
    li.innerHTML = '<span class="scan-row-name"></span><span class="scan-row-state">Waiting…</span>';
    li.querySelector(".scan-row-name").textContent = name;
    list.appendChild(li);
    return li;
  }
  function state(li, text, kind, href) {
    var el = li.querySelector(".scan-row-state");
    el.className = "scan-row-state" + (kind ? " is-" + kind : "");
    if (href) {
      el.innerHTML = "";
      var a = document.createElement("a");
      a.href = href; a.textContent = text;
      el.appendChild(a);
    } else {
      el.textContent = text;
    }
  }

  function shrink(file) {
    if (!/^image\//.test(file.type) || file.size < 700 * 1024 || !window.createImageBitmap) {
      return Promise.resolve(file);
    }
    return createImageBitmap(file, { imageOrientation: "from-image" }).then(function (bmp) {
      var scale = Math.min(1, MAX_PX / Math.max(bmp.width, bmp.height));
      var canvas = document.createElement("canvas");
      canvas.width = Math.round(bmp.width * scale);
      canvas.height = Math.round(bmp.height * scale);
      canvas.getContext("2d").drawImage(bmp, 0, 0, canvas.width, canvas.height);
      return new Promise(function (resolve) {
        canvas.toBlob(function (blob) {
          resolve(blob && blob.size < file.size ? new File([blob], file.name.replace(/\.\w+$/, "") + ".jpg",
                                                           { type: "image/jpeg" }) : file);
        }, "image/jpeg", QUALITY);
      });
    }).catch(function () { return file; });   // e.g. HEIC on a browser that can't open it
  }

  function post(url, body) {
    return fetch(url, {
      method: "POST", body: body, credentials: "same-origin",
      headers: { "X-CSRFToken": csrf, "X-Requested-With": "XMLHttpRequest", "Accept": "application/json" }
    }).then(function (r) {
      return r.json().catch(function () { return { error: "The server answered " + r.status + "." }; })
        .then(function (j) { j._status = r.status; return j; });
    });
  }

  function pump() {
    while (uploading < UPLOADS && uploadQ.length) startUpload(uploadQ.shift());
    while (reading < READS && readQ.length) startRead(readQ.shift());
    if (!uploading && !reading && !uploadQ.length && !readQ.length && toCheck) doneBox.hidden = false;
  }

  function startUpload(job) {
    uploading++;
    state(job.li, "Uploading…");
    shrink(job.file).then(function (file) {
      var fd = new FormData();
      fd.append("file", file, file.name);
      return post(uploadUrl, fd);
    }).then(function (res) {
      if (res.error) { state(job.li, res.error, "bad"); return; }
      if (res.existing && res.status !== "uploaded" && res.status !== "failed") {
        state(job.li, "Uploaded before - open it", "warn", reviewUrl(res.id));
        return;
      }
      queueRead(res.id, job.li);
    }).catch(function () {
      state(job.li, "Upload failed - check the connection and try again.", "bad");
    }).then(function () { uploading--; pump(); });
  }

  function queueRead(id, li) {
    if (!ready) { state(li, "Uploaded - it will be read once reading is switched on.", "warn"); return; }
    state(li, "Waiting to be read…");
    readQ.push({ id: id, li: li });
    pump();
  }

  function startRead(job) {
    reading++;
    state(job.li, "Reading…");
    post(readUrl(job.id)).then(function (res) {
      if (res.split && res.split.length) {
        state(job.li, "Holds " + res.split.length + " invoices - reading each", "ok");
        res.split.forEach(function (part) { queueRead(part.id, row(part.filename)); });
      } else if (res.error) {
        state(job.li, res.error, "bad");
      } else if (res.status === "to_check") {
        toCheck++;
        var label = (res.supplier || "Bill") + (res.total ? " · Rs. " + Number(res.total).toLocaleString("en-IN") : "");
        state(job.li, label + " - ready to check", res.check === "verified" ? "ok" : "warn", reviewUrl(res.id));
      } else if (res.status === "rejected") {
        state(job.li, res.message || "Rejected", "bad", reviewUrl(res.id));
      } else {
        state(job.li, res.message || "Couldn't be read", "bad", reviewUrl(res.id));
      }
    }).catch(function () {
      state(job.li, "Reading was interrupted - it will retry when you reopen this page.", "bad");
    }).then(function () { reading--; pump(); });
  }

  function addFiles(files) {
    Array.prototype.forEach.call(files, function (file) {
      uploadQ.push({ file: file, li: row(file.name) });
    });
    doneBox.hidden = true;
    pump();
  }

  root.querySelectorAll("[data-scan-input]").forEach(function (input) {
    input.addEventListener("change", function () { addFiles(input.files); input.value = ""; });
  });
  root.addEventListener("dragover", function (e) { e.preventDefault(); root.classList.add("is-over"); });
  root.addEventListener("dragleave", function () { root.classList.remove("is-over"); });
  root.addEventListener("drop", function (e) {
    e.preventDefault();
    root.classList.remove("is-over");
    if (e.dataTransfer && e.dataTransfer.files.length) addFiles(e.dataTransfer.files);
  });

  (root.dataset.pending || "").split(",").filter(Boolean).forEach(function (id) {
    queueRead(Number(id), row("Uploaded bill #" + id));
  });
})();
