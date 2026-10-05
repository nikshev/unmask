#!/usr/bin/env python3
# trace: ignore-file
"""Тонка обгортка оцінювальної команди 003 (FR-003-22/23)."""

import sys

from unmask.clusters.evaluate import main

if __name__ == "__main__":
    sys.exit(main())
