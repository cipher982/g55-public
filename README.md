# G55 Public Engineering Workspace

An AI-maintained collection of reusable engineering work from a 2011 Mercedes-Benz G55 AMG (W463).

The project explores three connected areas:

- **CAN reverse engineering** — curated CAN-C/CAN-B DBCs, signal findings, and physical-layer diagnostics.
- **Code-first CAD** — parametric vehicle parts and the render/measure loop used by agents to inspect their own geometry.
- **L2 preparation** — high-level architecture and safety reasoning for adding modern sensing and control to an older platform.

This is an engineering notebook and artifact collection, not a supported product, a certified safety case, or a complete openpilot port. Findings are often confirmed on one vehicle only. Read the evidence grade and validation status before relying on one.

The private vehicle workspace is maintained separately. It contains personal identity, complete captures, operational state, and other material that does not belong here.

## Initial release

This first public release contains:

- curated passive-analysis CAN-C/CAN-B DBCs;
- a rewritten CAN-B physical-layer fault-isolation case study;
- the agent CAD review tool and one trim-only faceplate prototype;
- high-level L2 layering and validation boundaries.

The repository is intentionally not a vehicle controller, a supported hardware
product, or a road-use instruction set.

## Agent maintenance

Future changes are made by AI agents. Read [`AGENTS.md`](AGENTS.md) before editing.
