// Theme switch, hover and focus readouts, the release timeline, "since your last visit", the closed-loop player
// (method page), setup tabs and the clip viewer (inside pages). Nothing here reorders a list: every list of
// models is in the order the page was built with.
(() => {
  const root = document.documentElement;
  const btn = document.querySelector(".theme");
  if (btn) btn.addEventListener("click", () => {
    const cur = root.dataset.theme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    const next = cur === "dark" ? "light" : "dark";
    root.dataset.theme = next;
    try { localStorage.setItem("theme", next); } catch (e) {}
  });
  const tip = document.getElementById("tip");
  if (!tip) return;
  const show = (el) => {
    tip.textContent = el.dataset.tip;
    tip.hidden = false;
    const r = el.getBoundingClientRect(), tr = tip.getBoundingClientRect();
    const x = Math.min(Math.max(8, r.left + r.width / 2 - tr.width / 2), innerWidth - tr.width - 8);
    const y = r.top - tr.height - 8 > 8 ? r.top - tr.height - 8 : r.bottom + 8;
    tip.style.left = `${x}px`;
    tip.style.top = `${y}px`;
  };
  const hide = () => { tip.hidden = true; };
  for (const el of document.querySelectorAll("[data-tip]")) {
    el.addEventListener("mouseenter", () => show(el));
    el.addEventListener("mouseleave", hide);
    el.addEventListener("focus", () => show(el));
    el.addEventListener("blur", hide);
  }
  window.addEventListener("scroll", () => {
    if (!tip.hidden) hide();
  }, { passive: true });
})();

// The closed-loop hero player on the method page.
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
  const project = (pts, ps, k) => {
    const [x0, y0, psi] = ps, c = Math.cos(psi), s = Math.sin(psi), out = [];
    for (const [x, y] of pts) { const dx = x - x0, dy = y - y0, xf = c * dx + s * dy, yl = -s * dx + c * dy; if (xf >= 2.2) out.push(px([xf, yl], k)); }
    return out;
  };
  const px = ([x, y], k) => [(C.cx - C.f * y / x) * k, (C.cy + C.f * C.h / x) * k];
  const roadLine = (xy, ps, k, colourStr, dash, w) => {
    const pts = project(xy, ps, k);
    if (pts.length < 2) return;
    ctx.save();
    ctx.beginPath();
    ctx.moveTo(pts[0][0], pts[0][1]);
    for (let i = 1; i < pts.length; i++) ctx.lineTo(pts[i][0], pts[i][1]);
    ctx.lineCap = "round"; ctx.lineJoin = "round";
    if (!dash.length) { ctx.strokeStyle = "rgba(0,0,0,0.55)"; ctx.lineWidth = (w + 2) * k; ctx.stroke(); }
    ctx.setLineDash(dash.map((x) => x * k));
    ctx.strokeStyle = colourStr; ctx.lineWidth = w * k; ctx.stroke();
    ctx.restore();
  };
  const ring = (pt, ps, k, colourStr) => {
    const p = project([pt], ps, k)[0];
    if (!p) return;
    ctx.save();
    ctx.beginPath(); ctx.arc(p[0], p[1], 3.8 * k, 0, Math.PI * 2);
    ctx.fillStyle = "#111"; ctx.fill();
    ctx.lineWidth = 2 * k; ctx.strokeStyle = colourStr; ctx.stroke();
    ctx.restore();
  };

  function draw() {
    const W = cv.width, k = W / FW;
    const f = Math.min(t * d.fps, LAST), fi = Math.min(Math.floor(f), LAST);
    const im = imgs[view];
    ctx.fillStyle = "#000"; ctx.fillRect(0, 0, W, cv.height);
    if (im && im.complete && im.naturalWidth) {
      const sx = (fi % d.cols) * FW, sy = Math.floor(fi / d.cols) * FH;
      ctx.drawImage(im, sx, sy, FW, FH, 0, 0, W, cv.height);
    }
    const p = Math.max(0, f - d.history), ps = pose(view, p);
    roadLine(d.ref_ahead, ps, k, colour("rec"), [5, 4], 1.8);
    if (view === "rec") {
      for (const m of models) roadLine(d.paths[m].xy, ps, k, colour(m), [], 1.5);
    } else {
      roadLine(d.paths[view].xy, ps, k, colour(view), [], 2.4);
      ring(d.ref_ahead[K2], ps, k, colour("rec"));
      ring(d.paths[view].xy[K2], ps, k, colour(view));
    }
    const key = `${view}:${fi >> 1}`;
    if (key !== last) {
      last = key;
      const sw = `<i class="sw sw-${d.cls[view]}"></i>`;
      lab.innerHTML = view === "rec" ? `${sw}${d.names.rec}` : `${sw}${d.names[view]} · ${ f < d.history ? "0.00 m" : `${dist(view, p).toFixed(2)} m`}`;
      time.textContent = f < d.history ? `history ${(f / d.fps).toFixed(1)} s` : `${(p / d.fps).toFixed(1)} / ${((d.steps - 1) / d.fps).toFixed(1)} s`;
    }
    for (const m of models) reads[m].textContent = `${(f < d.history ? 0 : dist(m, p)).toFixed(2)} m`;
    for (const [m, pts] of Object.entries(screen)) {
      const [i, j, u] = at(pts, p), c = dots[m];
      if (c) { c.setAttribute("cx", lerp(pts[i][0], pts[j][0], u).toFixed(1)); c.setAttribute("cy", lerp(pts[i][1], pts[j][1], u).toFixed(1)); }
    }
  }

  const select = (m) => {
    view = m; last = null;
    for (const b of btns) b.setAttribute("aria-pressed", String(b.dataset.v === m));
    for (const pl of svg.querySelectorAll(".bp")) {
      const on = m === "rec" || pl.dataset.m === m || pl.dataset.m === "rec";
      pl.classList.toggle("dim", !on);
      pl.classList.toggle("on", pl.dataset.m === m);
    }
    draw();
  };
  const setPlaying = (on) => {
    playing = on; prev = null;
    playBtn.classList.toggle("paused", !on);
    playBtn.setAttribute("aria-label", on ? "Pause" : "Play");
    if (on) requestAnimationFrame(frame);
  };
  let prev = null;
  function frame(now) {
    if (!playing) return;
    if (prev !== null && visible) { t += (now - prev) / 1000; if (t >= T) t = 0; scrub.value = Math.min(t, T).toFixed(2); draw(); }
    prev = now;
    requestAnimationFrame(frame);
  }
  const fit = () => {
    const r = box.getBoundingClientRect(), dpr = Math.min(devicePixelRatio || 1, 2);
    const w = Math.max(FW, Math.round(r.width * dpr)), h = Math.round(w * FH / FW);
    if (cv.width !== w || cv.height !== h) { cv.width = w; cv.height = h; draw(); }
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

// Camera setups tabbed matrix (#setups).
(() => {
  const sec = document.querySelector(".setups-sec");
  if (!sec) return;
  const tabs = [...sec.querySelectorAll("[data-setup-tab]")];
  const panels = [...sec.querySelectorAll("[data-setup-panel]")];
  for (const tab of tabs) {
    tab.addEventListener("click", () => {
      const k = tab.dataset.setupTab;
      for (const t of tabs) t.setAttribute("aria-selected", String(t.dataset.setupTab === k));
      for (const p of panels) p.hidden = p.dataset.setupPanel !== k;
    });
  }
})();

// The release timeline: on a narrow screen it scrolls; start at the newest end.
(() => {
  for (const w of document.querySelectorAll(".tlw")) {
    if (w.scrollWidth > w.clientWidth) w.scrollLeft = w.scrollWidth;
  }
})();

// "Since your last visit": entries newer than the last time this browser opened the front page or the log.
(() => {
  const feed = document.querySelector(".feedsec");
  if (!feed) return;
  const items = [...feed.querySelectorAll(".fe[data-date]")];
  if (!items.length) return;
  const newest = items.map((e) => e.dataset.date).sort().pop();
  let seen = null;
  try { seen = localStorage.getItem("seen"); } catch (e) {}
  if (seen) {
    const fresh = items.filter((e) => e.dataset.date > seen);
    for (const e of fresh) e.classList.add("fe-new");
    const note = feed.querySelector(".since");
    if (note && fresh.length) {
      note.textContent = `${fresh.length} ${fresh.length === 1 ? "entry" : "entries"} since your last visit on ${seen}.`;
      note.hidden = false;
    }
  }
  try { if (!seen || newest > seen) localStorage.setItem("seen", newest); } catch (e) {}
})();

// The clip viewer: two camera views with the 4 x 8 effect grid over them, a steering trace and a rows-over-time strip.
(() => {
  const box = document.querySelector(".xv");
  const raw = document.getElementById("xv-data");
  if (!box || !raw) return;
  const clips = JSON.parse(raw.textContent).clips;
  const imgs = { n: box.querySelector('img[data-view="n"]'), w: box.querySelector('img[data-view="w"]') };
  const grids = { n: box.querySelector('[data-grid="n"]'), w: box.querySelector('[data-grid="w"]') };
  for (const g of Object.values(grids)) g.innerHTML = "<i></i>".repeat(32);
  const cells = { n: [...grids.n.children], w: [...grids.w.children] };
  const scrub = box.querySelector(".xv-scrub"), read = box.querySelector(".xv-read"), play = box.querySelector(".xv-play");
  const svg = box.querySelector(".xv-trace svg"), cv = box.querySelector(".xv-rows canvas"), ctx = cv.getContext("2d");
  const btns = [...box.querySelectorAll(".xv-pick button")];
  const cache = {};
  let clip = null, dir = "", i = 0, playing = false, timer = null, scale = 1;
  const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
  const colour = (e, a) => `rgba(${css(e > 0 ? "--left" : "--right")}, ${a.toFixed(3)})`;
  const src = (f, v) => `${dir}/${clip.images.pattern.replace("{f:04d}", String(f).padStart(4, "0")).replace("{n|w}", v)}`;

  const draw = () => {
    const fr = clip.frames[i];
    for (const v of ["n", "w"]) imgs[v].src = src(fr.f, v);
    let top = 0;
    fr.eff.forEach((e, k) => { if (Math.abs(e) > Math.abs(fr.eff[top])) top = k; });
    for (const v of ["n", "w"]) cells[v].forEach((c, k) => {
      const a = Math.sqrt(Math.min(1, Math.abs(fr.eff[k]) / scale)) * 0.75;
      c.style.background = colour(fr.eff[k], a);
      c.classList.toggle("top", k === top);
    });
    const r = Math.floor(top / 8) + 1, c = (top % 8) + 1;
    read.textContent = `${fr.t.toFixed(1)} s, ${(fr.v * 3.6).toFixed(0)} km/h, strongest: row ${r}, column ${c}`;
    const cur = svg.querySelector(".cur");
    if (cur) { const x = (i / (clip.frames.length - 1)) * 1000; cur.setAttribute("x1", x); cur.setAttribute("x2", x); }
    scrub.value = i;
  };

  const trace = () => {
    const F = clip.frames, n = F.length;
    const all = F.flatMap((f) => [f.a_lat, f.a_lat_meas]).filter((x) => x !== null && x !== undefined);
    const lim = Math.max(0.5, ...all.map(Math.abs)) * 1.1;
    const y = (v) => (60 - (v / lim) * 55).toFixed(1);
    const path = (key) => F.map((f, k) => (f[key] === null || f[key] === undefined ? null : `${((k / (n - 1)) * 1000).toFixed(1)},${y(f[key])}`)).filter(Boolean).join(" ");
    const sc = box.querySelector(".xv-scale");
    if (sc) sc.textContent = `grey line: 0; top and bottom edges: ±${lim.toFixed(1)} m/s², left positive`;
    svg.innerHTML = `<line class="z" x1="0" x2="1000" y1="60" y2="60"/><polyline class="pr" points="${path("a_lat_meas")}"/><polyline class="pm" points="${path("a_lat")}"/><line class="cur" x1="0" x2="0" y1="0" y2="120"/>`;
  };

  const rows = () => {
    const F = clip.frames, n = F.length;
    cv.width = n; cv.height = 4;
    const sums = F.map((f) => [0, 1, 2, 3].map((r) => f.eff.slice(r * 8, r * 8 + 8).reduce((a, b) => a + b, 0)));
    const m = Math.max(1e-6, ...sums.flat().map(Math.abs));
    sums.forEach((s, k) => s.forEach((v, r) => { ctx.fillStyle = colour(v, Math.sqrt(Math.min(1, Math.abs(v) / m)) * 0.9 + 0.03); ctx.fillRect(k, r, 1, 1); }));
  };

  const load = async (k) => {
    for (const b of btns) b.setAttribute("aria-pressed", String(Number(b.dataset.clip) === k));
    dir = clips[k].dir;
    clip = cache[dir] || (cache[dir] = await (await fetch(`${dir}/clip.json`)).json());
    const mags = clip.frames.flatMap((f) => f.eff.map(Math.abs)).sort((a, b) => a - b);
    scale = Math.max(1e-6, mags[Math.floor(mags.length * 0.99)]);
    scrub.max = clip.frames.length - 1;
    i = 0;
    trace(); rows(); draw();
    for (const f of clip.frames) { const im = new Image(); im.src = src(f.f, "n"); }
  };

  const setPlaying = (on) => {
    playing = on;
    play.classList.toggle("paused", !on);
    play.setAttribute("aria-label", on ? "Pause" : "Play");
    clearInterval(timer);
    if (on) timer = setInterval(() => { i = (i + 1) % clip.frames.length; draw(); }, 1000 / (clip.images.hz || 5));
  };
  play.addEventListener("click", () => setPlaying(!playing));
  scrub.addEventListener("input", () => { setPlaying(false); i = Number(scrub.value); draw(); });
  for (const b of btns) b.addEventListener("click", async () => { setPlaying(false); await load(Number(b.dataset.clip)); });
  new MutationObserver(() => { if (clip) { rows(); draw(); } }).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  load(0).then(() => setPlaying(false));
})();
