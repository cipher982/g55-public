# W463 CAN Findings

Curated signal definitions and reverse-engineering notes from a 2011 Mercedes-Benz G55 AMG (W463).

The DBCs describe signals observed on one vehicle. They are useful starting points for research, logging, and comparison—not a guarantee for every W463 model year, market, ECU revision, or wiring configuration.

Evidence labels used in accompanying notes:

- **Measured** — directly observed in a capture or physical measurement.
- **Derived** — calculated from measured observations.
- **Borrowed** — taken from an identified external source.
- **Hypothetical** — a testable interpretation, not a settled fact.

The public copies intentionally exclude captured VIN payload definitions and private vehicle identity. Raw drive captures are not included.

These files are for passive analysis, logging, and bench decoding. They are not
instructions for transmitting or injecting frames into a vehicle.

## Files

- [`g55-canc.dbc`](g55-canc.dbc) — curated CAN-C, nominally 500 kbit/s.
- [`g55-canb.dbc`](g55-canb.dbc) — curated CAN-B, nominally 83.333 kbit/s fault-tolerant CAN.

Signal identity and encoding confidence remain uneven. Treat comments in the DBCs as part of the evidence record, not as a factory specification.
