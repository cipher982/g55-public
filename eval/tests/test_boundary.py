"""The boundary check catches each kind of identifier, and passes ordinary numbers and words."""
from pathlib import Path

import pytest

from geval import boundary

LEAKS = {
    "drive id": "the drive 000162 at dusk",
    "window id": "window d05:8320 was skipped",
    "session timestamp": "session 20261029-221428 recorded",
    "host name": "scored on cube overnight",
    "file path": "frames under /home/drose/agents-scratch",
    "private IP address": "the tablet at 10.55.2.4",
    "MAC address": "BMS C1:06:0D:29:50:05",
    "VIN": "VIN WDCYC7BF9BX123456",
    "money": "about $30 on Modal",
    "owner's name": "David drove it",
    "command frame ID": "taps on 0x238",
}


@pytest.mark.parametrize("kind", sorted(LEAKS))
def test_each_kind_is_caught(tmp_path: Path, kind: str):
    (tmp_path / "page.html").write_text(f"<p>{LEAKS[kind]}</p>")
    hits = boundary.check(tmp_path)
    assert any(f": {kind}:" in h for h in hits), hits


def test_ordinary_text_passes(tmp_path: Path):
    (tmp_path / "page.html").write_text(
        "<p>segment 8011d605be1cbb77/000000cc--8e8d8ec716/6; delta 0.000166, ci [0.000976, 0.02438]; 19.91 Hz; 1,024 values; release 2026-11-05; "
        "CAN-C 500 kbit/s; firmware V15; a cubed term; Rosetta; 0x3D0 telemetry</p>")
    assert boundary.check(tmp_path) == []
