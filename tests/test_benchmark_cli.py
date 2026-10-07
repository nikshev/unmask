# verifies: FR-005-01
"""Тести CLI (T-116)."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from unmask.benchmark.cli import main


def test_cli_help() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])
    assert excinfo.value.code == 0


def test_cli_offline_generates_report() -> None:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        out_path = Path(f.name)
    try:
        # This should not crash
        exit_code = main([
            "--ground-truth", "data/ground_truth.jsonl",
            "--fixtures", "tests/fixtures/real",
            "--output", str(out_path),
        ])
        assert exit_code == 0
        assert out_path.exists()
        content = out_path.read_text(encoding="utf-8")
        assert "Benchmark Report" in content
    finally:
        out_path.unlink(missing_ok=True)


def test_cli_live_flag_requires_env() -> None:
    exit_code = main(["--ground-truth", "data/ground_truth.jsonl", "--live"])
    assert exit_code == 1