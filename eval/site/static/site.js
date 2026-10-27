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

// The closed-loop hero: move the bird's-eye dots and the distance readouts with the video's frame.
(() => {
  const fig = document.querySelector(".cl");
  const raw = document.getElementById("cl-data");
  if (!fig || !raw) return;
  const d = JSON.parse(raw.textContent);
  const video = fig.querySelector("video");
  const svg = fig.querySelector(".cl-map");
  const time = fig.querySelector(".cl-time");
  // screen positions per step, read from the drawn paths (driver first, then the models in data order)
  const screen = [...svg.querySelectorAll("polyline")].map((pl) => pl.getAttribute("points").split(" ").map((q) => q.split(",").map(Number)));
  const keys = ["rec", ...Object.keys(d.paths)];
  const dots = Object.fromEntries([...svg.querySelectorAll(".bd")].map((c) => [c.dataset.m, c]));
  const reads = Object.fromEntries([...fig.querySelectorAll(".cl-lab b")].map((b) => [b.dataset.m, b]));
  let last = -1;
  const show = (i) => {
    if (i === last) return;
    last = i;
    const k = Math.max(0, Math.min(i - d.history + 1, d.steps));   // path index: 0 at the recorded start
    keys.forEach((m, n) => {
      const p = screen[n][Math.min(k, screen[n].length - 1)];
      if (dots[m]) { dots[m].setAttribute("cx", p[0]); dots[m].setAttribute("cy", p[1]); }
    });
    for (const [m, b] of Object.entries(reads)) b.textContent = `${d.paths[m].dist[k].toFixed(2)} m`;
    time.textContent = i < d.history ? `recorded, ${((i - d.history + 1) * 0.2).toFixed(1)} s` : `${(k * 0.2).toFixed(1)} s`;
  };
  const tick = () => {
    show(Math.min(Math.floor(video.currentTime * d.fps + 1e-6), d.history + d.steps - 1));
    if (!video.paused) requestAnimationFrame(tick);
  };
  video.addEventListener("play", () => requestAnimationFrame(tick));
  video.addEventListener("seeked", tick);
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) {
    video.removeAttribute("autoplay");
    video.pause();
    video.controls = true;
    show(d.history + d.steps - 1);
  } else {
    show(0);
    video.play().catch(() => { video.controls = true; show(d.history + d.steps - 1); });
  }
})();
