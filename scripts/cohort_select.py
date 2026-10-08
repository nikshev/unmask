#!/usr/bin/env python3
# impl: FR-006-03
"""Відбір топ-30 когорти A зі зведення скринінгу (FR-006-03).

Usage: python3 scripts/cohort_select.py <screen.csv> <pool.jsonl> > data/cohort_a.jsonl
Правило: risk_score desc, clusters_count desc, max_share desc, mint asc.
Порожній risk (EXC/помилка) — униз списку (не відбирається мовчки: такі рядки
не потрапляють у топ за визначенням сортування). Без мережі.
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path


def _num(row: dict, key: str, default: float = -1.0) -> float:
    try:
        return float(row.get(key) or default)
    except (TypeError, ValueError):
        return default


def select_top(rows: list[dict], n: int = 30) -> list[dict]:
    """Детермінований топ-n за правилом FR-006-03."""
    return sorted(
        rows,
        key=lambda r: (-_num(r, "risk_score"), -_num(r, "clusters_count", 0.0),
                       -_num(r, "max_share", 0.0), r.get("mint", "")),
    )[:n]


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: cohort_select.py <screen.csv> <pool.jsonl>", file=sys.stderr)
        return 1
    screen = Path(argv[1])
    with open(screen, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    top = select_top(rows)
    out = []
    for rank, row in enumerate(top, 1):
        out.append({"version": 1, "mint": row["mint"], "cohort": "a",
                    "source": "screen_rank",
                    "notes": f"rank {rank}: risk={row.get('risk_score')}, "
                             f"clusters={row.get('clusters_count')}, "
                             f"max_share={row.get('max_share')}, band={row.get('band')}"})
    sys.stdout.write("\n".join(json.dumps(r) for r in out) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
