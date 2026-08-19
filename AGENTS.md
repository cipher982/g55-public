# G55 Public Engineering Workspace

This repository is public. It is an AI-maintained collection of reusable engineering artifacts from a 2011 Mercedes-Benz G55 AMG project.

## Boundary

Never add owner or vehicle identity, registration or insurance records, addresses, plates, VIN payloads, raw drive or location logs, private hosts or networks, maintenance state, firmware or tune binaries, WIS material, forum-archive data, or private deployment instructions.

Technical facts may be specific to the W463 platform, but every narrative
finding must state whether it is measured, derived, borrowed, or hypothetical.
DBC comments must identify provisional or unverified fields. Do not present a
result from one vehicle as a universal Mercedes specification.

## Layout

- `can/` — curated CAN-C/CAN-B DBCs and reverse-engineering notes.
- `cad/` — reusable CAD tooling and selected vehicle parts.
- `l2/` — public high-level autonomy architecture and safety reasoning.
- `txgate/` — reusable bench-only CAN safety machinery when it is ready for this workspace.

## Working rules

- This repository is the source of truth for its public artifacts.
- Private vehicle work belongs in the private vehicle workspace, never here.
- Public versions of private findings are rewritten explanations, not copied private ledgers.
- Keep generated CAD outputs reproducible; do not edit generated files as source.
- Preserve the distinction between desk, bench, and vehicle validation.
- Run the narrowest relevant check before committing a behavioral change.
