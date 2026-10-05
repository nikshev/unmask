#!/usr/bin/env python3
# trace: ignore-file
"""Точка входу доставки 004 (FR-004-10): `uv run python scripts/serve_unmask.py [--port N] [--no-bot]`."""

import sys

from unmask.delivery.main import main

if __name__ == "__main__":
    sys.exit(main())
