// The Warehouse picker on a sales order (warehouse dispatch). As the customer,
// the delivery pincode and the items change, it asks the server for the
// nearest warehouse (/warehouse/suggest.json: service area > nearest with
// stock > nearest; an unknown pincode -> the branch's fallback): every option
// shows its distance and what it has free, ARFA's default is selected (until
// the salesperson picks one by hand), with a one-line reason, and when no
// warehouse has the whole order the order may be split across them.
(function () {
  const select = document.querySelector("select[name=dispatch_location_id][data-suggest-url]");
  if (!select) return;
  const url = select.dataset.suggestUrl;
  const hint = document.querySelector("[data-free-hint]");
  const reasonEl = document.querySelector("[data-suggest-reason]");
  const splitBox = document.querySelector("[data-split-box]");
  const splitText = document.querySelector("[data-split-text]");
  const summary = document.querySelector("[data-ship-summary]");
  let addresses = {};
  try { addresses = JSON.parse(document.getElementById("customer-addresses").textContent || "{}"); } catch (e) { /* none */ }
  let manual = select.value !== "" && document.querySelector(".flash-error") !== null;
  let last = "";
  let data = null;
  const REASONS = { service_area: "service area", nearest: "nearest", nearest_in_stock: "nearest with stock",
                    fallback: "fallback warehouse" };

  select.addEventListener("change", () => { manual = true; paint(); });

  function lines() {
    const ids = document.querySelectorAll("input[name='line_product_id[]']");
    const qtys = document.querySelectorAll("input[name='line_qty[]']");
    const descs = document.querySelectorAll("input[name='line_description[]']");
    const out = [];
    ids.forEach((el, i) => {
      if (!el.value) return;
      out.push({ id: el.value, qty: Number((qtys[i] && qtys[i].value) || 0), desc: descs[i] ? descs[i].value : "" });
    });
    return out;
  }

  function shipPincode() {
    const other = document.querySelector("input[name=ship_elsewhere]");
    if (other && other.checked) {
      const pin = document.querySelector("[data-ship-pincode]");
      if (summary) summary.textContent = "A different address (below).";
      return pin ? pin.value.trim() : "";
    }
    const cust = document.querySelector("select[name=customer_id]");
    const a = cust ? addresses[cust.value] : null;
    if (summary) {
      summary.textContent = a ? [a.address_line1, a.city, a.pincode].filter(Boolean).join(", ") || "No address on the customer - add a pincode below."
                              : "The customer's address.";
    }
    return a ? (a.pincode || "") : "";
  }

  function fmt(n) {
    const v = Number(n);
    return Number.isInteger(v) ? String(v) : v.toFixed(2);
  }

  function paint() {
    if (!data) return;
    const names = {};
    (data.products || []).forEach((p) => { names[p.id] = p.name; });
    data.warehouses.forEach((w) => {
      const opt = select.querySelector(`option[value="${w.id}"]`);
      if (!opt) return;
      const bits = [w.name];
      if (w.km !== null && w.km !== undefined) bits.push(`${Number(w.km).toFixed(w.km < 10 ? 1 : 0)} km`);
      Object.keys(w.free).forEach((pid) => bits.push(`${names[pid] || "item"}: ${fmt(w.free[pid])} free`));
      opt.textContent = bits.join(" · ");
    });
    if (!manual && data.default) select.value = String(data.default);
    if (reasonEl) {
      const chosen = String(select.value);
      reasonEl.textContent = manual && data.default && chosen !== String(data.default)
        ? `Chosen by hand (ARFA suggested the ${REASONS[data.reason] || data.reason}). ${data.message}`
        : (data.message ? `${data.message} (${REASONS[data.reason] || data.reason})` : "");
      reasonEl.classList.toggle("field-error-msg", data.reason === "fallback");
    }
    const chosen = data.warehouses.find((w) => String(w.id) === select.value);
    if (hint) {
      if (!chosen || !Object.keys(chosen.free).length) {
        hint.textContent = "";
      } else {
        const wanted = {};
        lines().forEach((l) => { wanted[l.id] = (wanted[l.id] || 0) + l.qty; });
        const short = Object.keys(chosen.free).filter((pid) => Number(chosen.free[pid]) < (wanted[pid] || 0));
        hint.textContent = short.length
          ? `${chosen.name} hasn't enough free: ` + short.map((pid) => `${names[pid] || "item"} (${fmt(chosen.free[pid])} free)`).join(", ") + "."
          : `${chosen.name} has every item free.`;
        hint.classList.toggle("field-error-msg", short.length > 0);
      }
    }
    if (splitBox) {
      const show = !!(chosen && !chosen.enough && data.split);
      splitBox.hidden = !show;
      if (show && splitText) {
        const ls = lines();
        splitText.textContent = "Split across warehouses: " + data.split.map((part) =>
          part.name + " - " + Object.keys(part.lines).map((i) => `${fmt(part.lines[i])} × ${(ls[i] || {}).desc || "item"}`).join(", ")
        ).join("; ");
      }
      if (!show) { const box = splitBox.querySelector("input"); if (box) box.checked = false; }
      if (chosen && !chosen.enough && !data.split && !data.warehouses.some((w) => w.enough) && reasonEl) {
        reasonEl.textContent += " Even all the warehouses together don't have this much free.";
      }
    }
  }

  async function refresh() {
    const ls = lines();
    const pin = shipPincode();
    const key = pin + "|" + ls.map((l) => `${l.id}:${l.qty}`).join(",");
    if (key === last) return;
    last = key;
    const qs = new URLSearchParams({ pincode: pin });
    ls.forEach((l) => { qs.append("p", l.id); qs.append("q", String(l.qty)); });
    try {
      const res = await fetch(`${url}?${qs.toString()}`, { credentials: "same-origin" });
      if (res.ok) { data = await res.json(); paint(); }
    } catch (e) { /* keep the last answer */ }
  }

  refresh();
  setInterval(refresh, 1000);
})();
