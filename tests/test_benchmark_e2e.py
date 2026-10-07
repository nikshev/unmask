# verifies: FR-005-01, FR-005-03, FR-005-04, FR-005-05
"""E2E тест повного пайплайну бенчмарку (T-120)."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from unmask.benchmark.ground_truth import load_ground_truth
from unmask.benchmark.runner import run_benchmark
from unmask.benchmark.report import generate_report

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "real"
GT_FILE = ROOT / "data" / "ground_truth.jsonl"


def test_full_pipeline_offline_generates_report() -> None:
    """Повний офлайн прогін: ground truth → runner → metrics → report."""
    gt = load_ground_truth(GT_FILE)
    assert len(gt) == 20

    preds = run_benchmark(gt, FIXTURES, live=False)
    assert len(preds) == len(gt)

    # Перевірка що всі предікшн мають необхідні поля
    for p in preds:
        assert hasattr(p, "mint")
        assert hasattr(p, "pred_has_cluster")
        assert hasattr(p, "risk_score")
        assert hasattr(p, "band")
        assert hasattr(p, "clusters_count")
        assert hasattr(p, "max_share")
        assert hasattr(p, "coordination_category")
        assert hasattr(p, "elapsed_s")
        assert hasattr(p, "rpc_calls")

    # Генерація звіту
    import yaml
    ingest_cfg = yaml.safe_load((ROOT / "config" / "ingest.yaml").read_text())
    hubs_cfg = yaml.safe_load((ROOT / "config" / "hubs.yaml").read_text())
    hub_lists_cfg = yaml.safe_load((ROOT / "config" / "hub_addresses.yaml").read_text())
    clusters_cfg = yaml.safe_load((ROOT / "config" / "clusters.yaml").read_text())

    config_versions = {
        "ingest": ingest_cfg.get("version"),
        "hubs": hubs_cfg.get("thresholds", {}).get("version"),
        "hub_addresses": hub_lists_cfg.get("version"),
        "clusters": clusters_cfg.get("version"),
    }

    report = generate_report(
        predictions=preds,
        ground_truth=gt,
        config_versions=config_versions,
        latency_stats={},
        calibrated_thresholds={},
    )

    assert "Benchmark Report (005)" in report
    assert "Confusion Matrix" in report
    assert "Precision / Recall / F1" in report
    assert "Calibration Curve" in report
    assert "PR-AUC" in report
    assert "False Positives" in report
    assert "False Negatives" in report
    assert "Latency Stats" in report
    assert "Calibrated Thresholds" in report


def test_e2e_offline_on_9_fixtures_matches_expected_structure() -> None:
    """Прогін на 9 фікстурах (з tests/fixtures/real/manifest.yaml) — структура звіту."""
    gt = load_ground_truth(GT_FILE)
    # Filter to only those in fixtures manifest
    import yaml
    manifest = yaml.safe_load((FIXTURES / "manifest.yaml").read_text(encoding="utf-8"))
    fixture_mints = {t["mint"] for t in manifest["tokens"]}
    gt_9 = [g for g in load_ground_truth(GT_FILE) if (hasattr(g, "mint") and g.mint in fixture_mints) or (isinstance(g, dict) and g["mint"] in fixture_mints)]

    # Only 5 of the 9 fixtures have ground truth entries
    preds = run_benchmark(gt_9, FIXTURES, live=False)
    assert len(preds) == 5  # only 5 have ground truth entries

    report = generate_report(
        predictions=preds,
        ground_truth=gt_9,
        config_versions={"ingest": 3, "hubs": 3, "hub_addresses": 1, "clusters": 2},
        latency_stats={},
        calibrated_thresholds={},
    )

    assert "Benchmark Report (005)" in report
    # Перевірка структури
    assert "## Confusion Matrix (by band)" in report
    assert "## Precision / Recall / F1" in report
    assert "## Calibration Curve" in report
    assert "## PR-AUC / ROC-AUC" in report