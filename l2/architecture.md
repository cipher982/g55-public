# L2 Preparation: Separate the Layers

This project studies how modern driver-assistance components might be prepared
for an older vehicle that shipped without an integrated ADAS controller. The
public architectural claim is deliberately modest: the useful work starts with
truthful observation and explicit boundaries, not with vehicle actuation.

## Layered shape

```text
vehicle networks
      |
      v
listen-only acquisition -> recorded signals -> perception / planning research
                                                     |
                                                     v
                                            separately reviewed command path
                                                     |
                                                     v
                                          independent safety boundary
```

The acquisition path is useful now for telemetry, decoding, and replay. It must
not silently become a transmit path. Perception and planning are untrusted
research components; they do not get direct authority over a vehicle interface.

## Validation boundaries

Results are reported at one of three levels:

- **Desk:** code, replay fixtures, protocol reasoning, and static analysis.
- **Bench:** simulated or electrically isolated hardware with no vehicle control.
- **Vehicle:** passive observation or a separately approved experiment with its
  own safety gate.

Evidence at one level does not imply evidence at the next. In particular, a
successful replay test is not a road-readiness claim.

## Safety posture

Any future control work requires an independently reviewed safety boundary,
explicit cancellation behavior, bounded interfaces, and a validation ladder that
starts away from the vehicle. This document does not authorize transmission,
actuation, road testing, or a particular hardware design.

The detailed vehicle-specific hazard analysis remains private until it can be
rewritten as a self-contained public argument without maintenance state, wiring
locations, network details, or live-vehicle instructions.
