// Hover and focus readout for chart marks and bars (every value is also in a table), and the theme switch.
(() => {
  const root = document.documentElement;
  const btn = document.querySelector(".theme");
  if (btn) btn.addEventListener("click", () => {
    const dark = root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
    root.dataset.theme = dark ? "light" : "dark";
    try { localStorage.setItem("theme", root.dataset.theme); } catch (e) { /* private window: the choice lasts this page only */ }
  });
  const tip = document.getElementById("tip");
  if (!tip) return;
  const show = (el) => {
    tip.textContent = el.dataset.tip;
    tip.hidden = false;
    const r = el.getBoundingClientRect();
    const w = tip.offsetWidth, h = tip.offsetHeight;
    const x = Math.min(Math.max(8, r.left + r.width / 2 - w / 2), window.innerWidth - w - 8);
    const y = r.top - h - 8 > 8 ? r.top - h - 8 : r.bottom + 8;
    tip.style.left = `${x}px`;
    tip.style.top = `${y}px`;
  };
  const hide = () => { tip.hidden = true; };
  for (const el of document.querySelectorAll("[data-tip]")) {
    el.addEventListener("pointerenter", () => show(el));
    el.addEventListener("pointerleave", hide);
    el.addEventListener("focus", () => show(el));
    el.addEventListener("blur", hide);
  }
  window.addEventListener("scroll", () => {
    const el = document.activeElement;
    if (el && el.dataset && el.dataset.tip) show(el); else hide();
  }, { passive: true });
})();

// The closed-loop hero: the world model's frames at their own resolution drawn to a canvas, and the paths drawn
// over them as vectors with the camera geometry the data carries. Frames change every 0.2 s as generated; the
// pose the paths are projected from, the rings and the distances move between steps. The bird's-eye map follows.
(() => {
  const fig = document.querySelector(".cl");
  const raw = document.getElementById("cl-data");
  if (!fig || !raw) return;
  const d = JSON.parse(raw.textContent);
  const cv = fig.querySelector("canvas"), ctx = cv.getContext("2d");
  const box = fig.querySelector(".cl-vid");
  const lab = fig.querySelector(".cl-lab"), time = fig.querySelector(".cl-time");
  const playBtn = fig.querySelector(".cl-play"), scrub = fig.querySelector(".cl-scrub");
  const svg = fig.querySelector(".cl-map");
  const [FW, FH] = d.frame, C = d.camera, K2 = Math.round(2 * d.fps);
  const LAST = d.history + d.steps - 1, T = (d.history + d.steps + d.hold) / d.fps;
  const models = Object.keys(d.paths);
  const track = { rec: { xy: d.ref_ahead, yaw: d.ref_yaw } };
  for (const m of models) track[m] = d.paths[m];
  const imgs = {};
  for (const [m, src] of Object.entries(d.sprites)) { const im = new Image(); im.decoding = "async"; im.src = src; im.onload = () => draw(); imgs[m] = im; }
  const screen = Object.fromEntries([...svg.querySelectorAll("polyline")].map((pl) => [pl.dataset.m, pl.getAttribute("points").split(" ").map((q) => q.split(",").map(Number))]));
  const dots = Object.fromEntries([...svg.querySelectorAll(".bd")].map((c) => [c.dataset.m, c]));
  const reads = Object.fromEntries([...fig.querySelectorAll(".cl-views b")].map((b) => [b.dataset.m, b]));
  const btns = [...fig.querySelectorAll(".cl-views button")];
  let view = d.start, t = 0, playing = false, last = null, visible = true;

  const lerp = (a, b, u) => a + (b - a) * u;
  const at = (arr, p) => { const i = Math.min(Math.floor(p), arr.length - 1), j = Math.min(i + 1, arr.length - 1); return [i, j, p - i]; };
  const pose = (m, p) => { const tr = track[m], [i, j, u] = at(tr.xy, p); return [lerp(tr.xy[i][0], tr.xy[j][0], u), lerp(tr.xy[i][1], tr.xy[j][1], u), lerp(tr.yaw[i], tr.yaw[j], u)]; };
  const dist = (m, p) => { const a = d.paths[m].dist, [i, j, u] = at(a, p); return lerp(a[i], a[j], u); };
  const colour = (m) => getComputedStyle(fig).getPropertyValue(`--c-${d.cls[m]}`).trim();
  // car frame (x forward, y left) -> canvas pixels
  const project = (pts, ps, k) => {
    const c = Math.cos(-ps[2]), s = Math.sin(-ps[2]);
    return pts.map(([x, y]) => { const dx = x - ps[0], dy = y - ps[1]; return [c * dx - s * dy, s * dx + c * dy]; });
  };
  const px = ([x, y], k) => [(C.cx - C.f * y / x) * k, (C.cy + C.f * C.h / x) * k];
  const roadLine = (xy, ps, k, colourStr, dash, w) => {
    let car = project(xy, ps).filter((q) => q[0] > 3);
    if (car.length < 2) return;
    // resample every 0.25 m of road so dashes are measured on the ground
    const arc = [0];
    for (let i = 1; i < car.length; i++) arc.push(arc[i - 1] + Math.hypot(car[i][0] - car[i - 1][0], car[i][1] - car[i - 1][1]));
    const pts = [];
    for (let a = 0, i = 0; a <= arc[arc.length - 1]; a += 0.25) {
      while (arc[i + 1] < a) i++;
      const u = (a - arc[i]) / (arc[i + 1] - arc[i] || 1);
      pts.push(px([lerp(car[i][0], car[i + 1][0], u), lerp(car[i][1], car[i + 1][1], u)], k));
    }
    const runs = [];
    if (!dash) runs.push(pts);
    else for (let i = 0; i < pts.length; i += 2 * dash / 0.25) runs.push(pts.slice(i, i + dash / 0.25 + 1));
    for (const [stroke, lw] of [["rgb(20 20 20 / 0.85)", w + 3 * devicePixelRatio], [colourStr, w]]) {
      ctx.strokeStyle = stroke; ctx.lineWidth = lw; ctx.lineCap = "round"; ctx.lineJoin = "round";
      for (const r of runs) { if (r.length < 2) continue; ctx.beginPath(); ctx.moveTo(...r[0]); for (const q of r.slice(1)) ctx.lineTo(...q); ctx.stroke(); }
    }
  };
  const ring = (pt, ps, k, colourStr) => {
    const [q] = project([pt], ps);
    if (q[0] <= 3) return;
    const [u, v] = px(q, k), r = 7 * devicePixelRatio;
    ctx.lineWidth = 2.5 * devicePixelRatio;
    ctx.strokeStyle = "rgb(20 20 20 / 0.85)"; ctx.beginPath(); ctx.arc(u, v, r + 1.5 * devicePixelRatio, 0, 7); ctx.stroke();
    ctx.strokeStyle = colourStr; ctx.beginPath(); ctx.arc(u, v, r, 0, 7); ctx.stroke();
  };

  function draw() {
    const f = Math.min(t * d.fps, LAST + 0.999), i = Math.floor(f);
    const s = Math.max(0, Math.min(f - d.history, d.steps - 1));  // generated step, continuous
    const p = i < d.history ? 0 : s + 1;                            // path index of the car's pose
    const key = `${view}|${p.toFixed(4)}|${i}|${cv.width}`;
    if (key !== last) {
      last = key;
      const k = cv.width / FW;
      const m = i < d.history ? "rec" : view, n = i < d.history ? i : m === "rec" ? i : i - d.history;
      const im = imgs[m];
      ctx.fillStyle = "#000"; ctx.fillRect(0, 0, cv.width, cv.height);
      if (im.complete && im.naturalWidth) {
        ctx.imageSmoothingEnabled = true; ctx.imageSmoothingQuality = "high";
        ctx.drawImage(im, (n % d.cols) * FW, Math.floor(n / d.cols) * FH, FW, FH, 0, 0, cv.width, cv.height);
      }
      if (i >= d.history) {
        const w = 3.5 * devicePixelRatio, ps = pose(view, p);
        roadLine(d.ref_ahead, ps, k, colour("rec"), 2, w);
        ring(d.ref_ahead[K2], ps, k, colour("rec"));
        if (view !== "rec") { roadLine(d.paths[view].xy, ps, k, colour(view), 0, w); ring(d.paths[view].xy[K2], ps, k, colour(view)); }
      }
    }
    const sec = (f - d.history + 1) / d.fps;
    time.textContent = sec < -0.05 ? `recorded, ${sec.toFixed(1)} s` : `${Math.max(0, Math.min(sec, d.steps / d.fps)).toFixed(1)} s`;
    for (const [m, b] of Object.entries(reads)) b.textContent = `${dist(m, p).toFixed(2)} m`;
    lab.textContent = i < d.history ? "Recorded, the start every model shares" : view === "rec" ? "Recorded drive" : `${d.names[view]}: ${dist(view, p).toFixed(2)} m from the driver's path`;
    for (const m of Object.keys(dots)) {
      const pts = screen[m], [a, b, u] = at(pts, p);
      dots[m].setAttribute("cx", lerp(pts[a][0], pts[b][0], u).toFixed(1));
      dots[m].setAttribute("cy", lerp(pts[a][1], pts[b][1], u).toFixed(1));
    }
    if (document.activeElement !== scrub) scrub.value = t.toFixed(2);
  }

  const select = (m) => {
    view = m;
    for (const b of btns) b.setAttribute("aria-pressed", String(b.dataset.v === m));
    for (const pl of svg.querySelectorAll("polyline")) {
      pl.classList.toggle("on", pl.dataset.m === m && m !== "rec");
      pl.classList.toggle("dim", pl.dataset.m !== m && pl.dataset.m !== "rec");
    }
    draw();
  };
  const setPlaying = (on) => {
    playing = on;
    playBtn.classList.toggle("paused", !on);
    playBtn.setAttribute("aria-label", on ? "Pause" : "Play");
    if (on) { prev = null; requestAnimationFrame(frame); }
  };
  let prev = null;
  function frame(now) {
    if (!playing) return;
    if (visible && prev !== null) { t += (now - prev) / 1000; if (t >= T) t %= T; draw(); }
    prev = now;
    requestAnimationFrame(frame);
  }
  const fit = () => {
    const w = Math.round(box.clientWidth * devicePixelRatio);
    if (w && w !== cv.width) { cv.width = w; cv.height = Math.round(w * FH / FW); last = null; draw(); }
  };
  new ResizeObserver(fit).observe(box);
  new IntersectionObserver((es) => { visible = es[0].isIntersecting; }).observe(box);
  new MutationObserver(() => { last = null; draw(); }).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { last = null; draw(); });
  for (const b of btns) b.addEventListener("click", () => select(b.dataset.v));
  playBtn.addEventListener("click", () => { if (!playing && t >= LAST / d.fps - 0.01) t = 0; setPlaying(!playing); });
  scrub.addEventListener("input", () => { setPlaying(false); t = Number(scrub.value); draw(); });
  fit();
  select(view);
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) { t = LAST / d.fps; setPlaying(false); draw(); }
  else setPlaying(true);
})();

// The G55 hero: the recorded clip with the driver's and each model's closed-loop path projected onto the road for
// the frame on screen (openpilot device frame, the clip's K, radial distortion, mount rotation and road plane).
(() => {
  const fig = document.querySelector(".th");
  const raw = document.getElementById("th-data");
  if (!fig || !raw) return;
  const d = JSON.parse(raw.textContent);
  const video = fig.querySelector("video"), cv = fig.querySelector("canvas"), ctx = cv.getContext("2d");
  const time = fig.querySelector(".th-time"), playBtn = fig.querySelector(".cl-play");
  const reads = Object.fromEntries([...fig.querySelectorAll(".th-chip b")].map((b) => [b.dataset.m, b]));
  const [W, H] = d.size, C = d.camera, K = C.K, R = C.R, LAST = (d.frames - 1) / 4, K2 = 10;
  const lerp = (a, b, u) => a + (b - a) * u;
  const colour = (m) => getComputedStyle(fig).getPropertyValue(`--c-${d.cls[m]}`).trim();
  const pose = (p) => {
    const i = Math.min(Math.floor(p), d.ref.length - 2), u = p - i, a = d.ref[i], b = d.ref[i + 1];
    return [lerp(a[0], b[0], u), lerp(a[1], b[1], u), lerp(a[2], b[2], u)];
  };
  // ground point (x fwd, y left, m, window frame) -> frame pixels, or null outside 2-65 m
  const proj = (x, y, ps) => {
    const c = Math.cos(-ps[2]), s = Math.sin(-ps[2]), dx = x - ps[0], dy = y - ps[1];
    const cx = c * dx - s * dy, cyl = s * dx + c * dy;
    const dev = [cx, -cyl, C.h];
    const r = [0, 1, 2].map((k) => R[k][0] * dev[0] + R[k][1] * dev[1] + R[k][2] * dev[2]);
    const v0 = r[1], v1 = r[2] * C.hk, depth = r[0];
    if (!(depth >= 2 && depth <= 65)) return null;
    const xn = v0 / depth, yn = v1 / depth, r2 = xn * xn + yn * yn, rad = 1 + C.dist[0] * r2 + C.dist[1] * r2 * r2;
    return [xn * rad * K[0][0] + K[0][2], yn * rad * K[1][1] + K[1][2]];
  };
  const dense = (xy) => {   // every 0.5 m along the path, so the projected line follows the road
    const out = [];
    for (let i = 0; i + 1 < xy.length; i++) {
      const [a, b] = [xy[i], xy[i + 1]], n = Math.max(1, Math.ceil(Math.hypot(b[0] - a[0], b[1] - a[1]) / 0.5));
      for (let k = 0; k < n; k++) out.push([lerp(a[0], b[0], k / n), lerp(a[1], b[1], k / n)]);
    }
    out.push(xy[xy.length - 1]);
    return out;
  };
  const lines = { rec: dense(d.ref), ...Object.fromEntries(Object.entries(d.paths).map(([m, p]) => [m, dense(p.xy)])) };
  const stroke = (pts, col, w, dash) => {
    const k = cv.width / W;
    ctx.setLineDash(dash ? [14 * devicePixelRatio, 10 * devicePixelRatio] : []);
    for (const [s, lw] of [["rgb(15 15 15 / 0.75)", w + 3 * devicePixelRatio], [col, w]]) {
      ctx.strokeStyle = s; ctx.lineWidth = lw; ctx.lineCap = "round"; ctx.lineJoin = "round";
      ctx.beginPath();
      let on = false;
      for (const q of pts) {
        if (!q) { on = false; continue; }
        if (on) ctx.lineTo(q[0] * k, q[1] * k); else { ctx.moveTo(q[0] * k, q[1] * k); on = true; }
      }
      ctx.stroke();
    }
    ctx.setLineDash([]);
  };
  const ring = (q, col) => {
    if (!q) return;
    const k = cv.width / W, r = 8 * devicePixelRatio;
    for (const [s, lw, rr] of [["rgb(15 15 15 / 0.75)", 5 * devicePixelRatio, r], [col, 2.5 * devicePixelRatio, r]]) {
      ctx.strokeStyle = s; ctx.lineWidth = lw; ctx.beginPath(); ctx.arc(q[0] * k, q[1] * k, rr, 0, 7); ctx.stroke();
    }
  };
  const draw = (mediaTime) => {
    const f = Math.min(Math.round(mediaTime * d.fps), d.frames - 1), p = Math.min(f / 4, LAST), ps = pose(p);
    ctx.clearRect(0, 0, cv.width, cv.height);
    ctx.save();
    ctx.beginPath(); ctx.rect(0, 0, cv.width, d.hood * cv.width / W); ctx.clip();   // not over the bonnet
    const w = 3.5 * devicePixelRatio;
    for (const m of ["rec", ...Object.keys(d.paths)]) {
      const pts = lines[m].map(([x, y]) => proj(x, y, ps));
      stroke(pts, colour(m), w, m === "rec");
    }
    ring(proj(d.ref[K2][0], d.ref[K2][1], ps), colour("rec"));
    for (const m of Object.keys(d.paths)) ring(proj(d.paths[m].xy[K2][0], d.paths[m].xy[K2][1], ps), colour(m));
    ctx.restore();
    const i = Math.min(Math.floor(p), 35), u = p - i;
    for (const [m, b] of Object.entries(reads)) b.textContent = `${lerp(d.paths[m].dist[i], d.paths[m].dist[i + 1], u).toFixed(2)} m`;
    time.textContent = `${(p * 0.2).toFixed(1)} s`;
  };
  const fit = () => {
    const r = video.getBoundingClientRect(), w = Math.round(r.width * devicePixelRatio);
    if (w && w !== cv.width) { cv.width = w; cv.height = Math.round(w * H / W); draw(video.currentTime); }
  };
  new ResizeObserver(fit).observe(video);
  if ("requestVideoFrameCallback" in HTMLVideoElement.prototype) {
    const onFrame = (now, meta) => { draw(meta.mediaTime); video.requestVideoFrameCallback(onFrame); };
    video.requestVideoFrameCallback(onFrame);
  } else {
    const tick = () => { draw(video.currentTime); requestAnimationFrame(tick); };
    requestAnimationFrame(tick);
  }
  video.addEventListener("seeked", () => draw(video.currentTime));
  new MutationObserver(() => draw(video.currentTime)).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  const setPlaying = (on) => {
    playBtn.classList.toggle("paused", !on);
    playBtn.setAttribute("aria-label", on ? "Pause" : "Play");
    if (on) video.play().catch(() => setPlaying(false)); else video.pause();
  };
  playBtn.addEventListener("click", () => setPlaying(video.paused));
  new IntersectionObserver((es) => { if (!es[0].isIntersecting) video.pause(); else if (!playBtn.classList.contains("paused")) video.play().catch(() => {}); }).observe(video);
  fit();
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) { video.removeAttribute("autoplay"); setPlaying(false); }
  draw(0);
})();
