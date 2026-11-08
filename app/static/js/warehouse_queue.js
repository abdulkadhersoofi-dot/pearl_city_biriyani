// The warehouse computer's Ready to invoice page: every 20 seconds it asks
// for the queue, and an order newly picked on the ARFA Warehouse app pops
// up with a sound - then the page reloads to list it.
(function () {
  const box = document.querySelector("[data-wh-queue]");
  if (!box) return;
  const known = new Set((box.dataset.known || "").split(",").filter(Boolean));

  function chime() {
    try {
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      [880, 1175].forEach((f, i) => {
        const o = ctx.createOscillator();
        const g = ctx.createGain();
        o.frequency.value = f;
        o.connect(g); g.connect(ctx.destination);
        const t = ctx.currentTime + i * 0.18;
        g.gain.setValueAtTime(0.0001, t);
        g.gain.exponentialRampToValueAtTime(0.3, t + 0.02);
        g.gain.exponentialRampToValueAtTime(0.0001, t + 0.16);
        o.start(t); o.stop(t + 0.17);
      });
    } catch (e) { /* no sound */ }
  }

  function toast(text) {
    const stack = document.querySelector(".toast-stack") || document.body;
    const el = document.createElement("div");
    el.className = "toast toast-success";
    el.setAttribute("role", "status");
    el.textContent = text;
    stack.appendChild(el);
    setTimeout(() => el.remove(), 8000);
  }

  async function poll() {
    try {
      const res = await fetch(box.dataset.url, { credentials: "same-origin" });
      if (!res.ok) return;
      const body = await res.json();
      const fresh = body.ready.filter((o) => !known.has(String(o.id)));
      if (fresh.length) {
        fresh.forEach((o) => known.add(String(o.id)));
        chime();
        toast(fresh.length === 1 ? `Ready to invoice: ${fresh[0].number} - ${fresh[0].customer}`
                                 : `${fresh.length} orders ready to invoice`);
        setTimeout(() => window.location.reload(), 2500);
      }
    } catch (e) { /* try again */ }
  }
  setInterval(poll, 20000);
})();
