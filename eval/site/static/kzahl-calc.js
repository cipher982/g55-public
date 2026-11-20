(() => {
  const STOCK_MM = 2419;
  const STOCK_K = 1664;
  const SLOT = "0x3900FA";
  const root = document.getElementById("kz");
  if (!root) return;

  const preset = root.querySelector("#kz-preset");
  const circumference = root.querySelector("#kz-circ");
  const k = root.querySelector("#kz-k");
  const hex = root.querySelector("#kz-hex");
  const addr = root.querySelector("#kz-addr");
  const err = root.querySelector("#kz-err");
  const note = root.querySelector("#kz-note");

  const pad = (n) => n.toString(16).toUpperCase().padStart(4, "0");

  function render() {
    const raw = Number(circumference.value);
    if (!Number.isFinite(raw) || raw < 1800 || raw > 3200) {
      note.textContent = "Enter a rolling circumference between 1,800 and 3,200 mm.";
      return;
    }
    const c = raw;
    const value = Math.round((STOCK_K * STOCK_MM) / c);
    const error = (c / STOCK_MM - 1) * 100;
    const actual = (70 * c) / STOCK_MM;
    const odometer = (100 * STOCK_MM) / c;

    k.textContent = String(value);
    hex.textContent = "0x" + pad(value);
    addr.textContent = SLOT;
    err.textContent = (error >= 0 ? "+" : "") + error.toFixed(1) + "%";
    note.textContent =
      "At an indicated 70 mph the truck is doing " + actual.toFixed(1) +
      " mph; the odometer logs " + odometer.toFixed(1) + " miles per 100 driven.";
  }

  preset.addEventListener("change", () => {
    circumference.value = preset.value;
    render();
  });
  circumference.addEventListener("input", render);
  render();
})();
