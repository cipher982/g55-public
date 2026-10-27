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
