// Hover and focus readout for chart marks and bars, theme switch, interactive multi-metric leaderboard, and setup tabs.
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

// Interactive multi-metric Leaderboard sorting & historical open-loop toggle.
(() => {
  const fig = document.querySelector(".gt");
  if (!fig) return;
  const body = fig.querySelector(".gt-body");
  const sortBtns = [...fig.querySelectorAll(".gt-sort button")];
  const colHeaders = [...fig.querySelectorAll(".gt-th[data-sort]")];
  const moreBtn = fig.querySelector(".gt-more-btn");
  const rows = [...body.querySelectorAll(".gt-row")];
  const MEDALS = { 1: "🥇", 2: "🥈", 3: "🥉" };

  let currentSort = "composite";
  let showOlOnly = false;

  function updateVisibilityAndRanks() {
    const forceShowAll = currentSort === "openloop" || currentSort === "date";
    const visibleOl = showOlOnly || forceShowAll;
    for (const r of rows) {
      if (r.dataset.olOnly === "true") {
        r.hidden = !visibleOl;
      } else {
        r.hidden = false;
      }
    }
    if (moreBtn) {
      const n = moreBtn.dataset.count || "9";
      if (forceShowAll) {
        moreBtn.hidden = true;
      } else {
        moreBtn.hidden = false;
        moreBtn.setAttribute("aria-expanded", String(showOlOnly));
        moreBtn.textContent = showOlOnly
          ? `Hide ${n} earlier open-loop-only releases (2023–2026)`
          : `Show ${n} earlier openpilot releases (2023–2026, scored in G55 open loop on 119 windows)`;
      }
    }

    // Re-number visible rows and assign medals for top 3 when sorted by a performance metric
    const currentRows = [...body.querySelectorAll(".gt-row")].filter((r) => !r.hidden);
    let rankIdx = 0;
    for (const r of currentRows) {
      r.classList.remove("gt-m1", "gt-m2", "gt-m3");
      const rkEl = r.querySelector(".gt-rk");
      if (!rkEl) continue;
      const hasMetric = currentSort === "date" || currentSort === "openloop" || r.dataset.hasCl === "1";
      if (!hasMetric) {
        rkEl.textContent = "–";
        rkEl.setAttribute("aria-label", "unranked in closed loop");
        continue;
      }
      rankIdx += 1;
      if (currentSort !== "date" && MEDALS[rankIdx]) {
        rkEl.textContent = MEDALS[rankIdx];
        r.classList.add(`gt-m${rankIdx}`);
      } else {
        rkEl.textContent = String(rankIdx);
      }
      rkEl.setAttribute("aria-label", `place ${rankIdx}`);
    }

    // Highlight active sorted column cells
    for (const cell of body.querySelectorAll("[data-col]")) {
      cell.classList.toggle("is-sorted-col", cell.dataset.col === currentSort);
    }
  }

  function applySort(mode) {
    currentSort = mode;
    for (const b of sortBtns) b.setAttribute("aria-pressed", String(b.dataset.sort === mode));
    for (const th of colHeaders) th.classList.toggle("is-active", th.dataset.sort === mode);

    rows.sort((a, b) => {
      if (mode === "date") {
        return b.dataset.date.localeCompare(a.dataset.date);
      }
      if (mode === "openloop") {
        return Number(a.dataset.openloop) - Number(b.dataset.openloop);
      }
      // For closed-loop metrics, keep closed-loop models above open-loop-only models
      const aCl = Number(a.dataset.hasCl), bCl = Number(b.dataset.hasCl);
      if (aCl !== bCl) return bCl - aCl;
      if (mode === "composite") {
        const diff = Number(b.dataset.composite) - Number(a.dataset.composite);
        if (Math.abs(diff) > 1e-6) return diff;
        return Number(a.dataset.cl2s) - Number(b.dataset.cl2s);
      }
      return Number(a.dataset[mode]) - Number(b.dataset[mode]);
    });

    for (const r of rows) body.appendChild(r);
    updateVisibilityAndRanks();
  }

  for (const b of sortBtns) {
    b.addEventListener("click", () => applySort(b.dataset.sort));
  }
  for (const th of colHeaders) {
    th.addEventListener("click", () => applySort(th.dataset.sort));
  }
  if (moreBtn) {
    moreBtn.addEventListener("click", () => {
      showOlOnly = !showOlOnly;
      updateVisibilityAndRanks();
    });
  }
  updateVisibilityAndRanks();
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

// Sticky top nav active section highlight (click + hashchange + scroll-spy).
(() => {
  const nav = document.querySelector(".top nav");
  if (!nav) return;
  const links = [...nav.querySelectorAll("a[href*='#']")];
  const items = [];
  for (const a of links) {
    const idx = a.getAttribute("href").indexOf("#");
    if (idx < 0) continue;
    const id = a.getAttribute("href").slice(idx + 1);
    const sec = document.getElementById(id);
    if (sec) items.push({ id, a, sec });
  }
  if (!items.length) return;

  let lockUntil = 0;
  const setActive = (id) => {
    for (const it of items) {
      if (it.id === id) it.a.setAttribute("aria-current", "page");
      else it.a.removeAttribute("aria-current");
    }
  };

  for (const it of items) {
    it.a.addEventListener("click", () => {
      setActive(it.id);
      lockUntil = performance.now() + 700;
    });
  }

  window.addEventListener("hashchange", () => {
    const h = location.hash.replace(/^#/, "");
    if (items.some((it) => it.id === h)) {
      setActive(h);
      lockUntil = performance.now() + 700;
    }
  });

  const onScroll = () => {
    if (performance.now() < lockUntil) return;
    const y = window.scrollY + 130;
    if (window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 24) {
      setActive(items[items.length - 1].id);
      return;
    }
    let cur = items[0].id;
    for (const it of items) {
      const top = it.sec.getBoundingClientRect().top + window.scrollY;
      if (top <= y) cur = it.id;
    }
    setActive(cur);
  };

  window.addEventListener("scroll", onScroll, { passive: true });
  if (location.hash) {
    const h = location.hash.replace(/^#/, "");
    if (items.some((it) => it.id === h)) setActive(h);
  } else {
    onScroll();
  }
})();
