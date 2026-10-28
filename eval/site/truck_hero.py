"""The front-page hero: recorded G55 footage with each model's closed-loop path drawn on the road.

Reads truck-hero.json (beside this file): the clip's frame rate and size, the camera (K, two radial distortion
terms, the rotation from the mount calibration, the road plane), the driver's recorded path with heading, each
model's closed-loop path (seed 0) and its distance from the driver's path at every 0.2 s step. Paths are metres
from the window start; nothing in the file names a drive, a window or a place. The clip is
static/truck-hero.{av1,h264}.mp4 with static/truck-hero-poster.jpg; the page script (site.js) projects the
paths onto it frame by frame with the same math as the private script that made the file.
"""
import json
from html import escape as esc
from pathlib import Path

SITE = Path(__file__).resolve().parent


def truck_hero(models, cfg) -> str:
    f = SITE / "truck-hero.json"
    if not f.exists():
        return ""
    h = json.loads(f.read_text())
    idx = {m["model"]["id"]: m for m in models}
    order = [mid for mid in h["paths"] if mid in idx]
    cls = {x[0]: x[1] for x in cfg.get("hero", {}).get("highlight", [])}
    name = {mid: idx[mid]["model"]["name"] or mid for mid in order}
    k2 = 10
    nw = max((m["sections"].get("truck_closed_loop", {}).get("n") or 0 for m in models), default=0)
    d2 = ", ".join(f"{esc(name[mid])} {h['paths'][mid]['dist'][k2]:.2f} m" for mid in order)
    chips = "".join(
        f'<span class="th-chip"><i class="sw sw-{cls.get(mid, "other")}"></i>{esc(name[mid])} <b data-m="{mid}">0.00 m</b></span>'
        for mid in order)
    w, hh = h["size"]
    secs = h["frames"] / h["fps"]
    data = json.dumps({"fps": h["fps"], "frames": h["frames"], "size": h["size"], "camera": h["camera"],
                       "ref": h["ref"], "paths": {mid: h["paths"][mid] for mid in order},
                       "cls": {"rec": "rec", **{mid: cls.get(mid, "other") for mid in order}}, "hood": h.get("hood", 950)})
    return f"""<figure class="th">
<div class="th-vid">
<video muted autoplay loop playsinline preload="auto" poster="truck-hero-poster.jpg" aria-label="The G55's camera on a wooded two-lane road, with the driver's path and each model's closed-loop path drawn on the road">
<source src="truck-hero.av1.mp4" type='video/mp4; codecs="av01.0.08M.08"'>
<source src="truck-hero.h264.mp4" type="video/mp4">
</video>
<canvas aria-hidden="true"></canvas>
<span class="th-time" aria-hidden="true"></span>
</div>
<div class="th-ctl">
<button type="button" class="cl-play" aria-label="Pause"><i></i></button>
<div class="th-chips"><span class="th-chip"><i class="sw sw-rec"></i>driver</span>{chips}</div>
</div>
<figcaption><p><strong>The G55 in closed loop, on its own camera.</strong> One of the {nw} G55 closed-loop windows: the truck's recorded camera, {w}&times;{hh} at {h["fps"]:.1f} frames per second, {secs:.1f} s, starting at {h["v0"]:.1f} m/s; the driver's heading turns {abs(h["turn_deg"]):.0f}&deg; to the {"left" if h["turn_deg"] > 0 else "right"} over the window. Each solid line is a model's closed-loop path from this start: the model drove a simulation of the G55's measured steering and powertrain response in <code>worldmodel-4B</code> for 7.2 s (seed {h["seed"]}), and its path is drawn on the real road from the truck's recorded camera position. Dashed magenta is the driver's path; the rings are where each is at 2 s. Distance from the driver's path at 2 s: {d2}.</p>
<p class="th-how">Chosen as a daylight window on a road with nothing that names the place. Every frame was checked; roadside houses, mailboxes and a sign are blurred. No location data is published.</p></figcaption>
<script type="application/json" id="th-data">{data}</script>
</figure>"""
