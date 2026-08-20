# CAN Provenance

The public CAN files combine three kinds of evidence. They are deliberately
curated instead of being a copy of any upstream database.

## Vehicle measurements

Signal identity, timing, physical-layer behavior, and fit to vehicle actions were
measured on one W463 test vehicle. The public case study describes the CAN-B
fault-isolation method without publishing raw captures or vehicle identity.

## Candidate Mercedes atlas

Some initial field labels and layouts were informed by the adjacent-platform
[`rnd-ash/mb-w211-pc`](https://github.com/rnd-ash/mb-w211-pc) CAN atlas. That
source is a candidate reference, not ground truth for a W463. The public DBCs
re-express fields that were independently checked or explicitly marked as
provisional; no source files or GPL-licensed implementation code are copied
here.

## Service documentation

Platform topology and connector context were checked against the Mercedes-Benz
Model 463 interior CAN diagram `PE00.19-U-2300GC`, accessed through the
Operation CHARM mirror. The service material is not redistributed here. The
public notes retain only short factual references needed to explain provenance.

## Interpretation rule

A DBC entry is not automatically a factory specification. Read its comments and
the accompanying case study. Measured, derived, borrowed, and unverified facts
must remain distinguishable.
