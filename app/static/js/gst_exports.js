/*
 * Bulk GST exports, client by client (tenants/gst_exports.html): one
 * request per client's monthly pack, so no request runs into Firebase
 * Hosting's 60-second limit however many clients are ticked. Each ZIP is
 * saved as it arrives; a failed client is marked and the rest carry on.
 */
(function () {
  var form = document.getElementById("gst-exports");
  if (!form) return;
  var button = form.querySelector("[data-one-by-one]");
  var progress = form.querySelector("[data-progress]");

  function packUrl(id, period) {
    return form.dataset.packUrl.replace(/\/0\//, "/" + id + "/") + "?period=" + encodeURIComponent(period);
  }

  function fileName(res, fallback) {
    var header = res.headers.get("Content-Disposition") || "";
    var match = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(header);
    return match ? decodeURIComponent(match[1]) : fallback;
  }

  function save(blob, name) {
    var link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = name;
    document.body.appendChild(link);
    link.click();
    setTimeout(function () { URL.revokeObjectURL(link.href); link.remove(); }, 2000);
  }

  function line(text) {
    var li = document.createElement("li");
    li.textContent = text;
    progress.appendChild(li);
    return li;
  }

  button.addEventListener("click", function () {
    var period = form.querySelector("[name=period]").value;
    var picked = Array.prototype.slice.call(form.querySelectorAll(".client-check:checked"));
    if (!picked.length) { line("Pick at least one client."); progress.hidden = false; return; }
    button.disabled = true;
    progress.hidden = false;
    progress.textContent = "";
    var done = 0;
    var chain = Promise.resolve();
    picked.forEach(function (box) {
      chain = chain.then(function () {
        var row = line(box.dataset.name + " - preparing...");
        return fetch(packUrl(box.value, period), {credentials: "same-origin"}).then(function (res) {
          if (!res.ok) throw new Error(res.status);
          return res.blob().then(function (blob) {
            save(blob, fileName(res, "pack-" + box.value + ".zip"));
            done += 1;
            row.textContent = box.dataset.name + " - saved";
          });
        }).catch(function () {
          row.textContent = box.dataset.name + " - couldn't be prepared; try it again on its own";
        });
      });
    });
    chain.then(function () {
      line(done + " of " + picked.length + " saved.");
      button.disabled = false;
    });
  });
})();
