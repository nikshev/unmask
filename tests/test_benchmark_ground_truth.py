# verifies: FR-005-02
"""Тести ground truth loader (T-111, T-112)."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from unmask.benchmark.ground_truth import (
    GroundTruthRecord,
    ValidationError,
    load_ground_truth,
    load_ground_truth_dicts,
)

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
GT_FILE = DATA_DIR / "ground_truth.jsonl"


def test_schema_rejects_invalid() -> None:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write('{"version": 1, "mint": "invalid", "class": "insider", "source": "creator_wallet"}\n')
        f.flush()
        with pytest.raises(ValidationError, match="does not match"):
            load_ground_truth(Path(f.name))

    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write('{"version": 1, "mint": "AedQTgnVjAvzSaCjUdDX6QN4rGD9aQwVwfhbovgDpump", "class": "insiderX", "source": "creator_wallet"}\n')
        f.flush()
        with pytest.raises(ValidationError, match="is not one of"):
            load_ground_truth(Path(f.name))

    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write('{"version": 1, "mint": "AedQTgnVjAvzSaCjUdDX6QN4rGD9aQwVwfhbovgDpump", "class": "insider", "source": "unknown_source"}\n')
        f.flush()
        with pytest.raises(ValidationError, match="is not one of"):
            load_ground_truth(Path(f.name))

    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write('{"version": 2, "mint": "AedQTgnVjAvzSaCjUdDX6QN4rGD9aQwVwfhbovgDpump", "class": "insider", "source": "creator_wallet"}\n')
        f.flush()
        with pytest.raises(ValidationError, match="was expected"):
            load_ground_truth(Path(f.name))


def test_loader_returns_typed_records() -> None:
    records = load_ground_truth(GT_FILE)
    assert len(records) == 20
    for r in records:
        assert isinstance(r, GroundTruthRecord)
        assert r.version == 1
        assert r.class_ in ("insider", "clean")
        assert r.source in ("creator_wallet", "bundled_accounts", "court_filings", "exchange_listing", "manual_review")
        assert len(r.mint) >= 32


def test_ground_truth_file_passes_validation() -> None:
    records = load_ground_truth(GT_FILE)
    insider = [r for r in records if r.class_ == "insider"]
    clean = [r for r in records if r.class_ == "clean"]
    assert len(insider) >= 10
    assert len(clean) >= 10
    sources = {r.source for r in insider}
    assert "creator_wallet" in sources
    assert "bundled_accounts" in sources
    assert "court_filings" in sources
    clean_sources = {r.source for r in clean}
    assert "exchange_listing" in clean_sources


def test_load_ground_truth_dicts() -> None:
    dicts = load_ground_truth_dicts(GT_FILE)
    assert len(dicts) == 20
    for d in dicts:
        assert "mint" in d
        assert "class_" in d
        assert d["class_"] in ("insider", "clean")


def test_empty_lines_skipped() -> None:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write('\n{"version": 1, "mint": "AedQTgnVjAvzSaCjUdDX6QN4rGD9aQwVwfhbovgDpump", "class": "insider", "source": "creator_wallet"}\n\n')
        f.flush()
        records = load_ground_truth(Path(f.name))
        assert len(records) == 1