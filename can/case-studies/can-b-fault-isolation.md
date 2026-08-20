# Isolating an Intermittent Fault-Tolerant CAN-B Failure

A W463 G-Class developed an intermittent electrical fault that looked like a
collection of unrelated body-electronics problems: lighting changes, cluster
warnings, infotainment resets, and brief loss of body-network traffic.

The useful result was not a vehicle-specific repair recipe. It was a diagnostic
method: characterize the physical layer, isolate branches with controlled
A/B/A tests, and only then inspect connectors.

## Observed mechanism

The fault was eventually traced to water intrusion at a rear-door control-module
connector. A missing moisture barrier let water reach the door harness; a CAN
terminal at the module connector had corroded until the conductor failed
intermittently.

The bus did not simply go silent. The fault-tolerant CAN-B physical layer lost
its normal complementary relationship between the two conductors and repeatedly
entered a single-wire-like failure mode. That distinction mattered: a normal
frame-count monitor could describe the symptom but could not explain it.

## Isolation method

The investigation used three controlled comparisons:

1. Run the complete branch connected and record the physical-layer signature.
2. Disconnect one suspected branch and repeat the same lossless capture.
3. Reconnect the branch, then split the branch at the module connector and repeat
   the capture again.

With the suspected branch disconnected, the sustained anomaly disappeared from
the observation window. Reconnecting that branch reproduced it. Leaving the
branch connected but separating the module connector removed it again. This
localized the fault to the module/connector boundary rather than the main bus.

The oscilloscope and CAN recorder were treated as one experiment: analog
measurements described the line state while digital capture established whether
valid frames were being lost. The comparison used identical probe placement,
termination, bitrate, and capture settings.

## Diagnostic signature

Across repeated captures, the recurring signature was:

- lost H/L complementarity rather than a permanent rail clamp;
- both-low and both-high states occupying substantial portions of the event;
- common-mode voltage excursions while each conductor still visited its normal
  resting region;
- a complete CAN-B valid-frame outage while the separate high-speed bus
  continued operating;
- recurrence delays that were irregular, so a quiet three- or five-minute
  interval was not evidence of a clean system.

The exact thresholds belong to the instrument setup and should not be treated as
universal limits. The transferable lesson is to measure the analog state and
compare controlled branch configurations instead of replacing modules by
symptom.

## Evidence grade

- **Measured:** repeated lossless captures and physical-layer observations on one
  W463 vehicle.
- **Derived:** branch localization and the connection between lost complementarity
  and an intermittent fault-tolerant transceiver path.
- **Borrowed:** platform topology and connector pin descriptions from identified
  service documentation.
- **Not claimed:** applicability to every W463, every CAN-B transceiver, or every
  intermittent body-network fault.

Raw captures, vehicle identity, service records, and current repair state are not
part of this public case study. The public DBCs are for passive analysis and
bench decoding only; they are not a vehicle transmission guide.
