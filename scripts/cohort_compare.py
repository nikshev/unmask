#!/usr/bin/env python3
# impl: FR-006-06, FR-006-07
"""Порівняння когорт A/B зі зведених CSV (FR-006-06, FR-006-07).

Usage: python3 scripts/cohort_compare.py <summary_a.csv> <summary_b.csv>
Друкує Markdown-блок: розподіл risk_score (median/p25/p75), частка токенів
з кластерами, розкладка смуг і coordination_category + таблиця preregistered
очікувань (met/unmet). Числа — лише з вхідних файлів. Без мережі.
"""

from __future__ import annotations

import csv
import statistics
import sys
from pathlib import Path


def _load(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _risks(rows: list[dict]) -> list[int]:
    out = []
    for r in rows:
        try:
            out.append(int(float(r["risk_score"])))
        except (TypeError, ValueError, KeyError):
            continue
    return sorted(out)


def _pct(part: int, whole: int) -> str:
    return f"{100.0 * part / whole:.1f}%" if whole else "n/a"


def _quartiles(xs: list[int]) -> tuple:
    if not xs:
        return ("n/a", "n/a", "n/a")
    return (statistics.median(xs),
            statistics.median(xs[: (len(xs) + 1) // 2]),
            statistics.median(xs[len(xs) // 2:]))


def _split(rows: list[dict], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in rows:
        k = r.get(key, "") or "?"
        counts[k] = counts.get(k, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def compare(name_a: str, rows_a: list[dict], name_b: str, rows_b: list[dict]) -> str:
    """Markdown-блок порівняння (чиста функція — тестується без файлів)."""
    ra, rb = _risks(rows_a), _risks(rows_b)
    med_a, p25_a, p75_a = _quartiles(ra)
    med_b, p25_b, p75_b = _quartiles(rb)
    ca = sum(1 for r in rows_a if (r.get("clusters_count") or "0") not in ("", "0"))
    cb = sum(1 for r in rows_b if (r.get("clusters_count") or "0") not in ("", "0"))
    lines = [
        f"## Cohort comparison: {name_a} (n={len(rows_a)}) vs {name_b} (n={len(rows_b)})",
        "",
        "| metric | A | B |",
        "|---|---|---|",
        f"| risk_score median [p25–p75] | {med_a} [{p25_a}–{p75_a}] | {med_b} [{p25_b}–{p75_b}] |",
        f"| tokens with ≥1 cluster | {ca} ({_pct(ca, len(rows_a))}) | {cb} ({_pct(cb, len(rows_b))}) |",
        "",
        "### Bands",
        "",
        "| band | A | B |",
        "|---|---|---|",
    ]
    bands = set(_split(rows_a, "band")) | set(_split(rows_b, "band"))
    sa, sb = _split(rows_a, "band"), _split(rows_b, "band")
    for band in sorted(bands):
        lines.append(f"| {band} | {sa.get(band, 0)} | {sb.get(band, 0)} |")
    lines += ["", "### Coordination categories", "", "| category | A | B |",
              "|---|---|---|"]
    cats = set(_split(rows_a, "coordination_category")) | set(_split(rows_b, "coordination_category"))
    ga, gb = _split(rows_a, "coordination_category"), _split(rows_b, "coordination_category")
    for cat in sorted(cats):
        lines.append(f"| {cat} | {ga.get(cat, 0)} | {gb.get(cat, 0)} |")
    lines += ["", "### Preregistered expectations", "", "| expectation | outcome |",
              "|---|---|---|"]
    exp = [
        ("median risk A > median risk B",
         isinstance(med_a, (int, float)) and isinstance(med_b, (int, float)) and med_a > med_b),
        ("share-with-cluster A > share-with-cluster B",
         len(rows_a) > 0 and len(rows_b) > 0 and ca / len(rows_a) > cb / len(rows_b)),
    ]
    for text, met in exp:
        lines.append(f"| {text} | {'met' if met else 'NOT MET'} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: cohort_compare.py <summary_a.csv> <summary_b.csv>", file=sys.stderr)
        return 1
    sys.stdout.write(compare("A", _load(Path(argv[1])), "B", _load(Path(argv[2]))))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
