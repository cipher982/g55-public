"""The public boundary, checked on the built site (public/AGENTS.md, Boundary).

A net, not a proof: it catches the identifiers agents have leaked before (drive, window and session ids,
host names, file paths, private network and Bluetooth addresses, the VIN prefix, money, the owner's name,
the IDs of frames the gateway sends). Wording that locates the truck or the owner still needs a reader.

    uv run python -m geval.boundary site/_site
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

PATTERNS = {
    "drive id": r"(?<![\w.])000\d{3}(?![\w.])",
    "window id": r"\b[a-e]\d{2}[:_]\d{2,5}\b",
    "session timestamp": r"\b20\d{6}[-T]\d{6}Z?\b",
    "host name": r"(?i)\b(cube|clifford|cinder|bremen|g55-pi|zerg|crunch|pikvm|wisp)\b",
    "file path": r"(/home/|/Users/|/mnt/|/var/lib/g55|/opt/g55|~/agents)",
    "private IP address": r"\b(10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
                          r"|100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3})\b",
    "MAC address": r"(?i)\b[0-9a-f]{2}(:[0-9a-f]{2}){5}\b",
    "VIN": r"\bWD[BC][A-Z0-9]{14}\b",
    "money": r"\$\s?\d",
    "owner's name": r"\b(David|Rose)\b",
    "command frame ID": r"(?i)\b0x0*(111|238)\b",
}
SUFFIXES = {".html", ".xml", ".txt", ".json", ".csv", ".js", ".css", ".svg"}


def check(out: Path) -> list[str]:
    """One line per hit: file, what kind, and the text around it."""
    problems = []
    for f in sorted(out.rglob("*")):
        if f.suffix not in SUFFIXES or not f.is_file():
            continue
        text = f.read_text(errors="ignore")
        for kind, pat in PATTERNS.items():
            for m in re.finditer(pat, text):
                ctx = text[max(0, m.start() - 30):m.end() + 30].replace("\n", " ")
                problems.append(f"{f.relative_to(out)}: {kind}: ...{ctx}...")
    return problems


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "site/_site")
    problems = check(out)
    print("\n".join(problems) or f"{out}: boundary check, 0 hits")
    raise SystemExit(1 if problems else 0)


if __name__ == "__main__":
    main()
