#!/usr/bin/env python3
"""K-Zahl calculator for the W463 KI203M instrument cluster.

The cluster's stock K-Zahl is 1664 at a 2,419 mm rolling circumference.
The K-Zahl scales with 1664 * 2419 / circumference_mm. A 2,666 mm tire
gives 1510 (0x05E6) in slot 1 at 0x3900FA.

    uv run --with pytest pytest test_kzahl_calc.py
    uv run kzahl_calc.py --circumference 2666
    uv run kzahl_calc.py --preset 35
    uv run kzahl_calc.py --table

The calculator is arithmetic only. The write path is bench-only until the
0xA2C4 header validation is solved; see ki203m-reference.md.
"""
from __future__ import annotations

import argparse

STOCK_MM = 2419
STOCK_K = 1664
SLOT1 = 0x3900FA

PRESETS = {
    "stock": 2419,
    "235/85R16": 2530,
    "33": 2630,
    "35": 2666,
}


def kzahl(circumference_mm: float) -> int:
    """The cluster's 16-bit K-Zahl for a rolling circumference in mm."""
    if circumference_mm <= 0:
        raise ValueError("circumference must be positive")
    return int(STOCK_K * STOCK_MM / circumference_mm + 0.5)


def hex16(value: int) -> str:
    return f"0x{value:04X}"


def error_pct(circumference_mm: float) -> float:
    """Actual speed above the indicated speed, in percent."""
    return (circumference_mm / STOCK_MM - 1.0) * 100.0


def odometer_pct(circumference_mm: float) -> float:
    """The odometer under-count, in percent of the distance travelled."""
    return (1.0 - STOCK_MM / circumference_mm) * 100.0


def actual(indicated_mph: float, circumference_mm: float) -> float:
    return indicated_mph * circumference_mm / STOCK_MM


def table() -> str:
    rows = [
        "label,circumference_mm,k_zahl,k_zahl_hex,slot_1_address,actual_over_indicated_pct,odometer_under_count_pct"
    ]
    for label, circumference in [("stock", STOCK_MM), ("235/85R16", 2530), ("33-inch", 2630), ("35-inch", 2666)]:
        value = kzahl(circumference)
        rows.append(
            f"{label},{circumference},{value},{hex16(value)},0x{SLOT1:04X},"
            f"{error_pct(circumference):.1f},{odometer_pct(circumference):.1f}"
        )
    return "\n".join(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--circumference", type=float, help="rolling circumference in mm")
    parser.add_argument("--preset", choices=sorted(PRESETS), help="a named circumference")
    parser.add_argument("--indicated", type=float, default=70.0, help="indicated speed for the actual speed, default 70 mph")
    parser.add_argument("--table", action="store_true", help="print the CSV table")
    args = parser.parse_args(argv)

    if args.table:
        print(table())
        return 0

    circumference = args.circumference
    if circumference is None:
        if args.preset is None:
            parser.error("give --circumference, --preset or --table")
        circumference = PRESETS[args.preset]

    value = kzahl(circumference)
    print(f"circumference: {circumference:.0f} mm")
    print(f"K-Zahl: {value} ({hex16(value)})")
    print(f"slot 1 address: 0x{SLOT1:04X}")
    print(f"actual over indicated: {error_pct(circumference):+.1f}%")
    print(f"odometer under-count: {odometer_pct(circumference):.1f}%")
    print(f"at an indicated {args.indicated:.0f} mph: {actual(args.indicated, circumference):.1f} mph")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
