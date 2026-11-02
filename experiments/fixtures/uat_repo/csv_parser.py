"""Minimal CSV parser used by the AtlasOS UAT fixture."""

from __future__ import annotations


def parse_csv(text: str) -> list[list[str]]:
    """Parse rows of comma-separated values."""

    rows: list[list[str]] = []
    for line in text.strip().splitlines():
        if not line:
            continue
        rows.append(line.split(","))
    return rows
