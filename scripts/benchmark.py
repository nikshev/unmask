#!/usr/bin/env python3
# trace: ignore-file
"""Точка входу бенчмарку 005 (FR-005-01): `uv run python scripts/benchmark.py --ground-truth ... --fixtures ...`."""

import sys

from unmask.benchmark.cli import main

if __name__ == "__main__":
    sys.exit(main())
