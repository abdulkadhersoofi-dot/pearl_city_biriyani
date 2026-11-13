/* The dashboard in 3D (reports/dashboard.html): the branch skyline and the
   14-day sales ribbon. It only *shows* figures already printed on the page:
   every number stays as text, the flat charts stay in the page for screen
   readers and as the fallback, and nothing here is fetched from another
   server (three.js lives in /static/vendor/three).

   Flat by itself: no WebGL, "reduce motion" set, data saver on, or a weak
   phone. The "Flat view" / "3D view" button is remembered on this device. */

const script = document.getElementById("dash3d-script");
const button = document.querySelector("[data-dash-view]");
const skyBox = document.querySelector("[data-sky3d]");
const skyFlat = document.querySelector("[data-sky-flat]");
const ribbonBox = document.querySelector("[data-ribbon3d]");
const ribbonFlat = document.querySelector("[data-ribbon-flat]");
const KEY = "arfa_dash_view";
let running = null;             // {stop()} while the scenes are up

function remembered() {
  try { return localStorage.getItem(KEY); } catch (e) { return null; }
}
function remember(value) {
  try { localStorage.setItem(KEY, value); } catch (e) { /* private window: just not remembered */ }
}

function hasWebGL() {
  try {
    const c = document.createElement("canvas");
    return !!(window.WebGLRenderingContext && (c.getContext("webgl2") || c.getContext("webgl")));
  } catch (e) { return false; }
}

function flatByItself() {
  if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) { return true; }
  if (navigator.connection && navigator.connection.saveData) { return true; }
  if (navigator.deviceMemory && navigator.deviceMemory <= 2) { return true; }
  if (navigator.hardwareConcurrency && navigator.hardwareConcurrency <= 2) { return true; }
  return false;
}

function css(name, fallback) {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}

function show3d(on, crowded) {
  // On a narrow screen the towers stand too close for labels: the list stays under them.
  if (skyBox) { skyBox.hidden = !on; skyFlat.hidden = on && !crowded; }
  if (ribbonBox) { ribbonBox.hidden = !on; ribbonFlat.hidden = on; }
  document.documentElement.classList.toggle("dash-3d", on);
  if (button) {
    button.textContent = on ? "Flat view" : "3D view";
    button.setAttribute("aria-pressed", on ? "true" : "false");
  }
}

let THREE = null;

async function start() {
  if (!THREE) {
    try {
      THREE = await import(script.dataset.three);
    } catch (e) {
      show3d(false);
      return;
    }
  }
  show3d(true);
  const scenes = [];
  try {
    if (skyBox) { scenes.push(skyline(skyBox, skyFlat)); }
    if (ribbonBox) { scenes.push(ribbon(ribbonBox, ribbonFlat)); }
  } catch (e) {
    scenes.forEach((s) => s.dispose());
    show3d(false);
    return;
  }
  show3d(true, scenes.some((s) => s.crowded));
  let frame = 0;
  // Each scene grows in when it is first seen, and only draws while it is on screen.
  scenes.forEach((s) => { s.began = 0; s.draw(0); });
  function tick(now) {
    frame = 0;
    if (document.hidden) { return; }
    let any = false;
    scenes.forEach((s) => {
      if (!s.onScreen()) { return; }
      any = true;
      s.began = s.began || now;
      s.draw((now - s.began) / 1000);
    });
    if (any) { frame = requestAnimationFrame(tick); }
  }
  function wake() { if (!frame && running) { frame = requestAnimationFrame(tick); } }
  const seen = new IntersectionObserver(wake);
  window.addEventListener("scroll", wake, {passive: true});
  scenes.forEach((s) => seen.observe(s.box));
  // A scene is laid out for the shape of its box: a much wider or narrower box gets a new one.
  let again = 0;
  const sized = new ResizeObserver(() => {
    if (scenes.some((s) => s.reshaped())) {
      clearTimeout(again);
      again = setTimeout(() => { if (running) { running.stop(); start(); } }, 150);
    }
  });
  scenes.forEach((s) => sized.observe(s.box));
  document.addEventListener("visibilitychange", wake);
  running = {
    stop() {
      if (frame) { cancelAnimationFrame(frame); }
      clearTimeout(again);
      seen.disconnect(); sized.disconnect();
      document.removeEventListener("visibilitychange", wake);
      window.removeEventListener("scroll", wake);
      scenes.forEach((s) => s.dispose());
      running = null;
    },
  };
  wake();
}

/* What every scene shares: a canvas in its box, a camera, light, and HTML
   labels laid over the canvas at the 3D points they belong to. `across` is
   how wide the camera sees at the middle of the scene - each scene spreads
   itself over it, so a wide card and a phone both use their whole width. */
function stage(box, height, cameraAt, lookAt) {
  // The box is measured before the canvas goes in: a canvas has a size of its
  // own until it is told one, and would stretch the box it is measured by.
  const width = Math.max(box.clientWidth, 200);
  const renderer = new THREE.WebGLRenderer({antialias: true, alpha: true, powerPreference: "low-power"});
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.setSize(width, height, false);
  const canvas = renderer.domElement;
  canvas.setAttribute("aria-hidden", "true");       // the same figures are text beside it
  canvas.style.width = "100%";
  canvas.style.height = height + "px";
  box.insertBefore(canvas, box.firstChild);
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(32, width / height, 0.1, 100);
  camera.position.set(cameraAt[0], cameraAt[1], cameraAt[2]);
  camera.lookAt(lookAt[0], lookAt[1], lookAt[2]);
  const far = camera.position.distanceTo(new THREE.Vector3(lookAt[0], lookAt[1], lookAt[2]));
  const across = 2 * far * Math.tan(16 * Math.PI / 180) * (width / height);
  scene.add(new THREE.HemisphereLight(0xffffff, 0x8899aa, 1.5));
  const sun = new THREE.DirectionalLight(0xffffff, 1.6);
  sun.position.set(4, 9, 6);
  scene.add(sun);
  const labels = box.querySelector("[data-labels]");
  const pins = [];                                   // {el, at: Vector3 in `group` space, group, below}
  const v = new THREE.Vector3();
  return {
    box, scene, camera, renderer, canvas, labels, pins, across, width, crowded: false,
    place() {
      pins.forEach((pin) => {
        v.copy(pin.at);
        pin.group.localToWorld(v);
        v.project(camera);
        pin.el.style.transform = "translate(-50%, " + (pin.below ? "0" : "-100%") + ") translate("
          + ((v.x + 1) / 2 * width).toFixed(1) + "px," + ((1 - v.y) / 2 * height).toFixed(1) + "px)";
        pin.el.style.zIndex = String(1000 - Math.round(v.z * 1000));
      });
    },
    reshaped() {
      const now = box.clientWidth;
      return now > 0 && Math.abs(now - width) / width > 0.08;
    },
    onScreen() {
      const r = box.getBoundingClientRect();
      return r.width > 0 && r.bottom > 0 && r.top < window.innerHeight;
    },
    dispose() {
      scene.traverse((o) => {
        if (o.geometry) { o.geometry.dispose(); }
        if (o.material) { o.material.dispose(); }
      });
      renderer.dispose();
      canvas.remove();
      labels.textContent = "";
    },
  };
}

const ease = (t) => 1 - Math.pow(1 - Math.min(Math.max(t, 0), 1), 3);

/* --- the branch skyline ---------------------------------------------------- */
function skyline(box, flat) {
  const items = Array.prototype.slice.call(flat.querySelectorAll(".sky-item"));
  const st = stage(box, 300, [0, 4.2, 10.5], [0, 1.7, 0]);
  const tones = {up: css("--color-primary", "#1f7a4d"), down: "#d9822b", same: "#7b8794", none: "#9aa5b1"};
  const top = Math.max.apply(null, items.map((i) => parseFloat(i.dataset.value) || 0).concat([1]));
  const group = new THREE.Group();
  st.scene.add(group);
  const n = Math.max(items.length, 1);
  const gap = Math.min(2.7, st.across * 0.8 / n);
  const side = Math.min(1.15, gap * 0.6);
  st.crowded = st.width / n < 104;                  // no room for a label over every tower
  box.classList.toggle("is-crowded", st.crowded);
  const ground = new THREE.Mesh(new THREE.BoxGeometry(gap * n + 0.9, 0.08, Math.max(side * 2.4, 2)),
    new THREE.MeshStandardMaterial({color: css("--color-border", "#d9dee4"), roughness: 1}));
  ground.position.y = -0.04;
  group.add(ground);
  const towers = items.map((item, i) => {
    const value = parseFloat(item.dataset.value) || 0;
    const tall = 0.1 + 3.3 * (value > 0 ? value / top : 0);
    const mesh = new THREE.Mesh(new THREE.BoxGeometry(side, 1, side),
      new THREE.MeshStandardMaterial({color: tones[item.dataset.tone] || tones.none, roughness: 0.55, metalness: 0.05}));
    mesh.position.x = (i - (n - 1) / 2) * gap;
    mesh.scale.y = 0.001;
    group.add(mesh);
    // The label is the branch's own link and figure, taken from the page as it is.
    const link = item.querySelector(".sky-name");
    const el = document.createElement("a");
    el.className = "dash3d-label tone-" + (item.dataset.tone || "none");
    el.href = link.href;
    el.innerHTML = "<b></b><span></span>";
    el.firstChild.textContent = link.textContent;
    el.lastChild.textContent = item.querySelector(".sky-value").textContent;
    el.title = item.querySelector(".sky-delta").textContent.replace(/\s+/g, " ").trim();
    st.labels.appendChild(el);
    const pin = {el, at: new THREE.Vector3(mesh.position.x, 0.2, 0), group};
    st.pins.push(pin);
    return {mesh, tall, pin, el, delay: i * 0.08};
  });

  // Point at a tower: it lights up and shows its label; a click opens its
  // branch's sales (on a crowded phone, the first tap shows the label - a link).
  const ray = new THREE.Raycaster();
  const at = new THREE.Vector2();
  let hot = null;
  function pick(event) {
    const r = st.canvas.getBoundingClientRect();
    at.set((event.clientX - r.left) / r.width * 2 - 1, -((event.clientY - r.top) / r.height) * 2 + 1);
    ray.setFromCamera(at, st.camera);
    const hit = ray.intersectObjects(towers.map((t) => t.mesh))[0];
    const found = hit ? towers.filter((t) => t.mesh === hit.object)[0] : null;
    if (found !== hot) {
      if (hot) { hot.mesh.material.emissive.setHex(0x000000); hot.el.classList.remove("is-hot"); }
      hot = found;
      if (hot) { hot.mesh.material.emissive.setHex(0x222222); hot.el.classList.add("is-hot"); }
      st.canvas.style.cursor = hot ? "pointer" : "";
      return true;
    }
    return false;
  }
  st.canvas.addEventListener("pointermove", (event) => { if (event.pointerType === "mouse") { pick(event); } });
  st.canvas.addEventListener("pointerleave", (event) => {
    if (event.pointerType === "mouse") { pick({clientX: -9999, clientY: -9999}); }
  });
  st.canvas.addEventListener("click", (event) => {
    const changed = pick(event);
    if (hot && !(st.crowded && changed)) { window.location.href = hot.el.href; }
  });

  return Object.assign(st, {
    draw(t) {
      group.rotation.y = Math.sin(t * 0.3) * 0.3;              // a slow turn, back and forth
      towers.forEach((tw) => {
        const h = Math.max(tw.tall * ease((t - tw.delay) / 0.9), 0.001);
        tw.mesh.scale.y = h;
        tw.mesh.position.y = h / 2;
        tw.pin.at.y = h + 0.12;
      });
      st.renderer.render(st.scene, st.camera);
      st.place();
    },
  });
}

/* --- the 14-day ribbon ------------------------------------------------------ */
function ribbon(box, flat) {
  const days = Array.prototype.slice.call(flat.querySelectorAll(".bar-slot"));
  const st = stage(box, 240, [0, 2.9, 8.2], [0, 1.25, 0]);
  const values = days.map((d) => parseFloat(d.dataset.value) || 0);
  const top = Math.max.apply(null, values.concat([1]));
  const n = values.length;
  const span = Math.min(st.across * 0.84, 22), depth = 0.9;
  const roomy = st.width / n >= 58;                 // room for a figure over every day
  const xs = values.map((_, i) => (i / Math.max(n - 1, 1) - 0.5) * span);
  const group = new THREE.Group();
  st.scene.add(group);
  const positions = new Float32Array((n - 1) * 2 * 6 * 3);      // per segment: the top and the front, two triangles each
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
  const band = new THREE.Mesh(geometry, new THREE.MeshStandardMaterial({
    color: css("--color-primary", "#1f7a4d"), roughness: 0.5, metalness: 0.05, side: THREE.DoubleSide}));
  group.add(band);
  const floor = new THREE.Mesh(new THREE.BoxGeometry(span + 0.8, 0.06, depth + 0.8),
    new THREE.MeshStandardMaterial({color: css("--color-border", "#d9dee4"), roughness: 1}));
  floor.position.y = -0.03;
  group.add(floor);
  const today = new THREE.Mesh(new THREE.SphereGeometry(0.15, 24, 16),
    new THREE.MeshStandardMaterial({color: "#f2b632", roughness: 0.3}));
  group.add(today);
  const peak = values.indexOf(Math.max.apply(null, values));
  const tops = [];
  days.forEach((day, i) => {
    const marked = i === n - 1 || (i === peak && values[i] > 0);
    const kind = (i === n - 1 ? " is-today" : "") + (i === peak && values[i] > 0 ? " is-peak" : "");
    // The day under the ribbon, its figure over it (every day when there is room, else today and the best day).
    const name = document.createElement("span");
    name.className = "dash3d-label dash3d-day" + kind;
    name.textContent = day.dataset.day;
    name.title = day.dataset.tip;
    st.labels.appendChild(name);
    st.pins.push({el: name, at: new THREE.Vector3(xs[i], -0.12, depth / 2 + 0.4), group, below: true});
    if (values[i] > 0 && (roomy || marked)) {
      const figure = document.createElement("span");
      figure.className = "dash3d-label dash3d-figure" + kind;
      figure.textContent = day.dataset.short;
      figure.title = day.dataset.tip;
      st.labels.appendChild(figure);
      const pin = {el: figure, at: new THREE.Vector3(xs[i], 0, 0), group};
      st.pins.push(pin);
      tops[i] = pin;
    }
  });

  function shape(grow) {
    const ys = values.map((v) => 0.05 + 2.3 * (v / top) * grow);
    let k = 0;
    const put = (x, y, z) => { positions[k++] = x; positions[k++] = y; positions[k++] = z; };
    for (let i = 0; i < n - 1; i++) {
      const x0 = xs[i], x1 = xs[i + 1], y0 = ys[i], y1 = ys[i + 1], zf = depth / 2, zb = -depth / 2;
      put(x0, y0, zf); put(x1, y1, zf); put(x1, y1, zb);        // the top of the ribbon
      put(x0, y0, zf); put(x1, y1, zb); put(x0, y0, zb);
      put(x0, 0, zf); put(x1, 0, zf); put(x1, y1, zf);          // its front edge, down to the floor
      put(x0, 0, zf); put(x1, y1, zf); put(x0, y0, zf);
    }
    geometry.attributes.position.needsUpdate = true;
    geometry.computeVertexNormals();
    tops.forEach((pin, i) => { if (pin) { pin.at.y = ys[i] + 0.16; } });
    today.position.set(xs[n - 1], ys[n - 1], 0);
  }

  let grown = -1;
  return Object.assign(st, {
    draw(t) {
      const g = ease(t / 1.1);
      if (g !== grown) { grown = g; shape(g); }
      group.rotation.y = Math.sin(t * 0.25 + 1) * 0.1;
      st.renderer.render(st.scene, st.camera);
      st.place();
    },
  });
}

/* --- the tiles lift and tilt under the pointer (3D view only) --------------- */
function tiltTiles() {
  if (!window.matchMedia || !window.matchMedia("(hover: hover) and (pointer: fine)").matches) { return; }
  Array.prototype.forEach.call(document.querySelectorAll(".stat-tile"), (tile) => {
    tile.addEventListener("pointermove", (event) => {
      if (!document.documentElement.classList.contains("dash-3d")) { return; }
      const r = tile.getBoundingClientRect();
      const x = (event.clientX - r.left) / r.width - 0.5, y = (event.clientY - r.top) / r.height - 0.5;
      tile.style.transform = "perspective(800px) rotateX(" + (-y * 5).toFixed(2) + "deg) rotateY(" + (x * 6).toFixed(2)
        + "deg) translateY(-3px)";
    });
    tile.addEventListener("pointerleave", () => { tile.style.transform = ""; });
  });
}

if (script && (skyBox || ribbonBox) && hasWebGL()) {
  const choice = remembered();
  const want3d = choice === "3d" || (choice !== "flat" && !flatByItself());
  if (button) {
    button.hidden = false;
    button.textContent = want3d ? "Flat view" : "3D view";
    button.addEventListener("click", () => {
      if (running) {
        running.stop();
        show3d(false);
        remember("flat");
      } else {
        remember("3d");
        start();
      }
    });
  }
  tiltTiles();
  if (want3d) {
    // After the page has painted its numbers.
    (window.requestIdleCallback || ((f) => setTimeout(f, 200)))(() => start());
  }
}
