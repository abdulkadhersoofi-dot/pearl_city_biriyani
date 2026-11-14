/* Bill reader training (scan/teach_bill.html): click a heading on the
   bill's picture and ARFA reads it into the "Heading on the bill" box. */
(function () {
  "use strict";
  var box = document.querySelector("[data-teach-picture]");
  if (!box) { return; }
  var img = box.querySelector(".teach-stage-img");
  var pin = box.querySelector(".teach-pin");
  var status = box.querySelector("[data-teach-status]");
  var heading = document.querySelector("[data-teach-heading]");
  var label = box.querySelector("[data-page-label]");
  var pages = parseInt(box.getAttribute("data-pages"), 10) || 1;
  var page = 0;
  var busy = false;

  function say(text) { if (status) { status.textContent = text; } }

  Array.prototype.forEach.call(box.querySelectorAll("[data-page-step]"), function (button) {
    button.addEventListener("click", function () {
      page = (page + parseInt(button.getAttribute("data-page-step"), 10) + pages) % pages;
      img.src = box.getAttribute("data-page-url").replace("{n}", page);
      pin.hidden = true;
      if (label) { label.textContent = "Page " + (page + 1) + " of " + pages; }
    });
  });

  img.addEventListener("click", function (event) {
    var rect = img.getBoundingClientRect();
    if (!rect.width || !rect.height) { return; }
    var x = (event.clientX - rect.left) / rect.width;
    var y = (event.clientY - rect.top) / rect.height;
    pin.style.left = (x * 100) + "%";
    pin.style.top = (y * 100) + "%";
    pin.hidden = false;
    if (!box.getAttribute("data-can-read")) {
      say("Type the heading you clicked in the box.");
      if (heading) { heading.focus(); }
      return;
    }
    if (busy) { return; }
    busy = true;
    say("Reading the heading...");
    fetch(box.getAttribute("data-label-url"), {
      method: "POST", credentials: "same-origin",
      headers: {"Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest",
                "X-CSRFToken": box.getAttribute("data-csrf")},
      body: JSON.stringify({n: page, x: x, y: y})
    }).then(function (response) {
      return response.json().then(function (data) { return {ok: response.ok, data: data}; });
    }).then(function (result) {
      busy = false;
      if (!result.ok || !result.data.text) {
        say((result.data && result.data.error) || "Nothing printed there - click on the heading itself, or type it.");
        if (heading) { heading.focus(); }
        return;
      }
      if (heading) { heading.value = result.data.text; heading.focus(); }
      say("Read: \u201c" + result.data.text + "\u201d. Now pick what it means.");
    }).catch(function () {
      busy = false;
      say("Couldn\u2019t read it just now - type the heading in the box.");
    });
  });
})();
