// The Warehouse picker on a sales order (warehouse dispatch): as items are
// added, each warehouse's option shows what is free there - in stock less
// what other open orders hold - so the salesperson picks one that has it.
(function () {
  const select = document.querySelector("select[name=dispatch_location_id][data-free-url]");
  if (!select) return;
  const hint = document.querySelector("[data-free-hint]");
  const url = select.dataset.freeUrl;
  let last = "";
  let data = null;

  function productIds() {
    const ids = [];
    document.querySelectorAll("input[name='line_product_id[]']").forEach((el) => {
      if (el.value && !ids.includes(el.value)) ids.push(el.value);
    });
    return ids;
  }

  function fmt(n) {
    const v = Number(n);
    return Number.isInteger(v) ? String(v) : v.toFixed(2);
  }

  function paint() {
    if (!data) return;
    const wanted = data.wanted || {};
    data.warehouses.forEach((w) => {
      const opt = select.querySelector(`option[value="${w.id}"]`);
      if (!opt) return;
      const bits = data.products.map((p) => `${p.name}: ${fmt(w.free[p.id])} free`);
      opt.textContent = bits.length ? `${w.name} · ${bits.join(" · ")}` : w.name;
    });
    const chosen = data.warehouses.find((w) => String(w.id) === select.value);
    if (!hint) return;
    if (!chosen || !data.products.length) {
      hint.textContent = "Free quantity at each warehouse shows here as you add items.";
      hint.classList.remove("field-error-msg");
      return;
    }
    const short = data.products.filter((p) => Number(chosen.free[p.id]) < Number(wanted[p.id] || 0));
    if (short.length) {
      hint.textContent = `${chosen.name} hasn't enough free: ` +
        short.map((p) => `${p.name} (${fmt(chosen.free[p.id])} free)`).join(", ") + ".";
      hint.classList.add("field-error-msg");
    } else {
      hint.textContent = `${chosen.name} has every item free.`;
      hint.classList.remove("field-error-msg");
    }
  }

  function wantedQty() {
    const out = {};
    const ids = document.querySelectorAll("input[name='line_product_id[]']");
    const qtys = document.querySelectorAll("input[name='line_qty[]']");
    ids.forEach((el, i) => {
      if (!el.value) return;
      out[el.value] = (out[el.value] || 0) + Number((qtys[i] && qtys[i].value) || 0);
    });
    return out;
  }

  async function refresh() {
    const ids = productIds();
    const key = ids.join(",");
    if (key !== last) {
      last = key;
      if (!ids.length) {
        data = { warehouses: [], products: [] };
        select.querySelectorAll("option[data-name]").forEach((o) => { o.textContent = o.dataset.name; });
      } else {
        try {
          const qs = ids.map((id) => `p=${encodeURIComponent(id)}`).join("&");
          const res = await fetch(`${url}?${qs}`, { credentials: "same-origin" });
          if (res.ok) data = await res.json();
        } catch (e) { /* keep the last figures */ }
      }
    }
    if (data) {
      data.wanted = wantedQty();
      paint();
    }
  }

  select.addEventListener("change", paint);
  refresh();
  setInterval(refresh, 1200);
})();
