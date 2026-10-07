# verifies: FR-005-01, FR-005-05
"""Тести benchmark runner (T-113)."""

from __future__ import annotations

from pathlib import Path

import pytest

from unmask.benchmark.ground_truth import GroundTruthRecord, load_ground_truth
from unmask.benchmark.runner import Prediction, run_benchmark

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "real"
GT_FILE = ROOT / "data" / "ground_truth.jsonl"


def test_offline_on_fixtures_matches_ground_truth_shape() -> None:
    gt = load_ground_truth(GT_FILE)
    preds = run_benchmark(gt, FIXTURES, live=False)
    assert len(preds) == len(gt)
    for p in preds:
        assert isinstance(p, Prediction)
        assert hasattr(p, "mint")
        assert hasattr(p, "pred_has_cluster")
        assert hasattr(p, "risk_score")
        assert hasattr(p, "band")
        assert hasattr(p, "clusters_count")
        assert hasattr(p, "max_share")
        assert hasattr(p, "coordination_category")
        assert hasattr(p, "elapsed_s")
        assert hasattr(p, "rpc_calls")


def test_prediction_has_all_fields() -> None:
    gt = load_ground_truth(GT_FILE)
    preds = run_benchmark(gt[:2], FIXTURES, live=False)
    for p in preds:
        # перевірка всіх полів
        assert isinstance(p.mint, str)
        assert isinstance(p.pred_has_cluster, bool)
        assert isinstance(p.risk_score, int)
        assert isinstance(p.band, str)
        assert isinstance(p.clusters_count, int)
        assert isinstance(p.max_share, float)
        assert isinstance(p.coordination_category, str)
        assert isinstance(p.elapsed_s, float)
        assert isinstance(p.rpc_calls, int)
        assert p.error is None or isinstance(p.error, str)