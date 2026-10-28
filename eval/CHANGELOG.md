# Suite and method changes

The site's log reads this file: one line per change, newest first, `- YYYY-MM-DD: what changed`,
optionally ending in a link. Model releases and scoring runs come from the results files and are
not listed here.

- 2026-10-28: The G55 ranking shows each model's median distance from the recorded path at 2 s, lebowski included, ordered by median and unranked until 10 drives; it was ordered by windows closer than lebowski. Findings, model summaries and the bars on each model's row use the G55 results; comma's segments stay in the harness check. The G55 clip at the top of the page is removed. Scoring runs in the log are grouped per day.
- 2026-10-27: comma's earlier driving models added, openpilot 0.9.5 (November 2023) to 0.11.1: nine models from Farmville to WMI, each run with its own release's inputs, camera warp and command and checked against comma's CI model replay of that model. Open loop on the G55 and on comma's segments; closed loop not yet. Their float16 feature normalisation runs in float32 here (in float16 it overflowed on some frames under ONNX Runtime). New over-time panel: G55 open-loop lateral error.
- 2026-10-27: The G55 sections lead: the ranking is G55 closed loop at 2 s, ranked with intervals once it has 10 or more drives and until then ordered by windows closer than lebowski. comma's public drives move to a harness check near the bottom; the world-model player moves to the method page.
- 2026-10-27: The closed-loop video is replaced by a player: the world model's 256×128 frames as generated, the paths drawn by the page over them, one view at a time.
- 2026-10-27: Over-time charts and this log replace the one-off results section.
- 2026-10-27: Closed-loop video of one public segment, with each model's path drawn on the generated view. [fe1a5ed](https://github.com/cipher982/g55-public/commit/fe1a5ed)
- 2026-10-27: "Lateral lag" corrected to the lead of the lateral request over the car's motion: a larger value means the request comes earlier. No better direction is marked for it or for lateral action rate. [8cebf52](https://github.com/cipher982/g55-public/commit/8cebf52)
- 2026-10-27: Models ranked by the median distance from the recorded path at 2 s in closed loop; the 4 s "primary" horizon is dropped. [8cebf52](https://github.com/cipher982/g55-public/commit/8cebf52)
- 2026-10-27: Results recomputed with the new intervals from the stored per-segment values, no new runs. [1b06fee](https://github.com/cipher982/g55-public/commit/1b06fee)
- 2026-10-27: Intervals resample whole drives; no interval below 10 drives, so G55 sections report win counts. Lead values at the 0 and 1.5 s search limits are left out. [102710f](https://github.com/cipher982/g55-public/commit/102710f)
- 2026-10-27: Release watcher started: Hugging Face commaai/openpilot_driving_models and openpilot's branches, every 20 minutes. [7856299](https://github.com/cipher982/g55-public/commit/7856299)
- 2026-10-27: G55 recovery after a 0.5 m offset added, 18 windows. [a98409f](https://github.com/cipher982/g55-public/commit/a98409f)
- 2026-10-27: Closed loop on comma's public segments added: 28 segments in worldmodel-4B, comma's ideal car, seeds 0 and 1. [f0ec83f](https://github.com/cipher982/g55-public/commit/f0ec83f)
- 2026-10-27: First edition: 8 models, scores backfilled from the 2026-10-27 deep dive. [3a0056f](https://github.com/cipher982/g55-public/commit/3a0056f)
