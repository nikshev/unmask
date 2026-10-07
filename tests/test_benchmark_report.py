# verifies: FR-005-03
"""Тести генератора звіту (T-117)."""

from __future__ import annotations

from pathlib import Path

from unmask.benchmark.ground_truth import load_ground_truth
from unmask.benchmark.runner import run_benchmark
from unmask.benchmark.report import generate_report

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "real"
GT_FILE = ROOT / "data" / "ground_truth.jsonl"


def _report():
    gt = load_ground_truth(GT_FILE)
    preds = run_benchmark(gt, FIXTURES, live=False)
    return generate_report(
        predictions=preds,
        ground_truth=list(gt),
        config_versions={"ingest": 3, "hubs": 3, "hub_addresses": 1, "clusters": 2},
        latency_stats={},
        calibrated_thresholds={},
    )


def test_report_contains_all_sections() -> None:
    report = _report()
    for section in (
        "# Benchmark Report (005)",
        "## Confusion Matrix (by band)",
        "## Precision / Recall / F1 (cluster detection)",
        "## Calibration Curve (risk_score bins)",
        "## PR-AUC / ROC-AUC",
        "## False Positives (clean tokens with clusters)",
        "## False Negatives (insider tokens without clusters)",
        "## Latency Stats",
        "## Calibrated Thresholds",
    ):
        assert section in report, section


def test_calibration_plot_ascii() -> None:
    report = _report()
    assert "█" in report and "░" in report


def test_fp_fn_tables_formatted() -> None:
    report = _report()
    assert "| mint | pred_band | risk_score | clusters | max_share |" in report
    assert "| mint | gt_source | pred_band | risk_score |" in report
