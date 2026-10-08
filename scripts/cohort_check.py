#!/usr/bin/env python3
# impl: FR-006-01, FR-006-04, FR-006-08
"""Перевірка когортних списків 006 за `contracts/cohort.schema.json` (FR-006-01).

Usage: python3 scripts/cohort_check.py <list.jsonl> [--expect-pool-100 | --expect-control-30]
Exit 0 — валідно, інакше 1 з причиною в stderr. Без мережі.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

BASE58 = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
COHORTS = {"pool_a", "a", "b"}
SOURCES = {"melt_high", "melt_low", "liquid", "liquid_draft", "screen_rank"}


def _fail(msg: str) -> int:
    print(f"cohort_check: {msg}", file=sys.stderr)
    return 1


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        return _fail("usage: cohort_check.py <list.jsonl> [--expect-pool-100 | --expect-control-30]")
    path = Path(argv[1])
    try:
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except OSError as exc:
        return _fail(f"cannot read {path}: {exc}")
    seen: set[str] = set()
    for i, line in enumerate(lines, 1):
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            return _fail(f"line {i}: invalid JSON ({exc})")
        if row.get("version") != 1:
            return _fail(f"line {i}: version must be 1")
        mint = row.get("mint")
        if not isinstance(mint, str) or not (32 <= len(mint) <= 44) or set(mint) - BASE58:
            return _fail(f"line {i}: bad mint")
        if row.get("cohort") not in COHORTS:
            return _fail(f"line {i}: bad cohort")
        if row.get("source") not in SOURCES:
            return _fail(f"line {i}: bad source")
        if mint in seen:
            return _fail(f"line {i}: duplicate mint")
        seen.add(mint)
    if "--expect-pool-100" in argv and len(lines) < 100:
        return _fail(f"pool has {len(lines)} rows, need >= 100")
    if "--expect-control-30" in argv and len(lines) != 30:
        return _fail(f"control has {len(lines)} rows, need exactly 30")
    print(f"cohort_check: {path}: {len(lines)} rows OK")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
