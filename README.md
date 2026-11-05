# G55 Public Engineering Workspace

An AI-maintained collection of reusable engineering work from a 2011 Mercedes-Benz G55 AMG (W463).

The project explores five connected areas:

- **CAN reverse engineering**: curated CAN-C/CAN-B DBCs, body PDM/gateway topology notes, and physical-layer diagnostics.
- **BLE auxiliary power telemetry & control**: Victron Orion-Tr Smart BLE GATT register read/write & Instant Readout codecs plus JBD/Eco-Worthy LiFePO4 BMS telemetry.
- **Driving model evaluation**: comma's released openpilot driving models tested on this truck's drives and on comma's public segments, with a public site (https://openpilot-gwagon-evals.drose.io) that lays out every release's results with 95% intervals and ranks nothing.
- **Code-first CAD**: parametric vehicle parts and the render/measure loop used by agents to inspect their own geometry.
- **L2 preparation**: high-level architecture and safety reasoning for adding modern sensing and control to an older platform.

This is an engineering notebook and artifact collection, not a supported product, a certified safety case, or a complete openpilot port. Findings are often confirmed on one vehicle only. Read the evidence grade and validation status before relying on one.

The private vehicle workspace is maintained separately. It contains personal identity, complete captures, operational state, and other material that does not belong here.

## Current contents

The public workspace currently contains:

- curated passive-analysis CAN-C/CAN-B DBCs and W463 body PDM / gateway topology notes;
- a rewritten CAN-B physical-layer fault-isolation case study;
- reference Python codecs for Victron Orion-Tr Smart BLE GATT registers, Instant Readout advertisements, and JBD/Eco-Worthy 4S LiFePO4 BMS notifications;
- the open-loop and closed-loop driving model evaluation benchmark (`eval/`);
- the agent CAD review tool and one trim-only faceplate prototype;
- high-level L2 layering and validation boundaries;
- a platform-level, evidence-graded supercharger-clutch control visual (SVG and PNG).

## Explore

| Area | Start here | What is inside |
|---|---|---|
| CAN | [`can/README.md`](can/README.md) | DBCs, evidence grades, body PDM/gateway topology, and diagnostic case studies |
| Power & BLE | [`power/README.md`](power/README.md) | Victron Orion-Tr Smart BLE GATT register read/write + Instant Readout and JBD/Eco-Worthy BMS codecs |
| Driving Eval | [`eval/README.md`](eval/README.md) | Open-loop and closed-loop results for comma's released driving models, and the site built from them |
| CAD | [`cad/README.md`](cad/README.md) | Agent geometry review tooling and printable parts |
| L2 | [`l2/README.md`](l2/README.md) | Layer boundaries and validation principles |
| Engine-control visual | [Shareable PNG](m113k-clutch-control.png) · [Editable SVG](m113k-clutch-control.svg) | Factory clutch switching, an offline diagnostic finding, and the unverified vehicle-control boundary |
The repository is intentionally not a vehicle controller, a supported hardware
product, or a road-use instruction set.

## Agent maintenance

Future changes are made by AI agents. Read [`AGENTS.md`](AGENTS.md) before editing.
