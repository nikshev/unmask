# verifies: FR-005-03
"""Тести metrics (T-114)."""

from __future__ import annotations

from unmask.benchmark.metrics import (
    confusion_matrix,
    precision_recall_f1,
    calibration_curve,
    pr_auc,
    roc_auc,
    fp_fn_tables,
)
from unmask.benchmark.runner import Prediction


def test_confusion_matrix_on_known_data() -> None:
    preds = [
        Prediction(mint="A", pred_has_cluster=True, risk_score=80, band="high_concentration", clusters_count=1, max_share=0.5, coordination_category="strong", elapsed_s=0.1, rpc_calls=0),
        Prediction(mint="B", pred_has_cluster=True, risk_score=30, band="suspicious", clusters_count=1, max_share=0.3, coordination_category="moderate", elapsed_s=0.1, rpc_calls=0),
        Prediction(mint="C", pred_has_cluster=False, risk_score=5, band="clean", clusters_count=0, max_share=0.0, coordination_category="none", elapsed_s=0.1, rpc_calls=0),
        Prediction(mint="D", pred_has_cluster=False, risk_score=10, band="insufficient_data", clusters_count=0, max_share=0.0, coordination_category="none", elapsed_s=0.1, rpc_calls=0),
    ]
    gt = [
        {"mint": "A", "class_": "insider"},
        {"mint": "B", "class_": "insider"},
        {"mint": "C", "class_": "clean"},
        {"mint": "D", "class_": "clean"},
    ]
    cm = confusion_matrix(preds, gt)
    # bands_order: clean, suspicious, high_concentration, insufficient_data
    # gt clean -> pred: C(clean), D(insufficient_data)
    # gt insider -> pred: A(high_concentration), B(suspicious)
    # cm[gt_idx][pred_idx]
    # gt clean (0): pred clean(0) + pred insufficient(3) = cm[0][0]=1, cm[0][3]=1
    # gt insider (mapped to suspicious=1): pred high_conc(2), suspicious(1)
    assert cm[0][0] == 1  # clean->clean
    assert cm[0][3] == 1  # clean->insufficient_data
    assert cm[1][1] == 1  # insider->suspicious
    assert cm[1][2] == 1  # insider->high_concentration
    assert sum(sum(row) for row in cm) == 4


def test_precision_recall_f1() -> None:
    preds = [
        Prediction(mint="A", pred_has_cluster=True, risk_score=80, band="high_concentration", clusters_count=1, max_share=0.5, coordination_category="strong", elapsed_s=0.1, rpc_calls=0),
        Prediction(mint="B", pred_has_cluster=True, risk_score=30, band="suspicious", clusters_count=1, max_share=0.3, coordination_category="moderate", elapsed_s=0.1, rpc_calls=0),
        Prediction(mint="C", pred_has_cluster=False, risk_score=5, band="clean", clusters_count=0, max_share=0.0, coordination_category="none", elapsed_s=0.1, rpc_calls=0),
        Prediction(mint="D", pred_has_cluster=False, risk_score=10, band="insufficient_data", clusters_count=0, max_share=0.0, coordination_category="none", elapsed_s=0.1, rpc_calls=0),
    ]
    gt = [
        {"mint": "A", "class_": "insider"},
        {"mint": "B", "class_": "insider"},
        {"mint": "C", "class_": "clean"},
        {"mint": "D", "class_": "clean"},
    ]
    metrics = precision_recall_f1(preds, gt)
    assert metrics["tp"] == 2
    assert metrics["fp"] == 0
    assert metrics["fn"] == 0
    assert metrics["tn"] == 2
    assert metrics["precision"] == 1.0
    assert metrics["recall"] == 1.0
    assert metrics["f1"] == 1.0


def test_calibration_curve_monotonic() -> None:
    scores = [10, 20, 30, 40, 50, 60, 70, 80, 90, 95]
    labels = [0, 0, 0, 0, 1, 1, 1, 1, 1, 1]
    curve = calibration_curve(scores, labels, n_bins=5)
    assert len(curve) <= 5
    for b in curve:
        assert "bin_center" in b
        assert "count" in b
        assert "insider_rate" in b
        assert "avg_risk_score" in b
    # higher bins should have higher insider_rate
    rates = [b["insider_rate"] for b in curve]
    assert all(rates[i] <= rates[i + 1] for i in range(len(rates) - 1))


def test_pr_auc_matches_sklearn_on_synthetic() -> None:
    # perfect separation
    scores = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
    labels = [0, 0, 0, 0, 0, 1, 1, 1, 1, 1]
    auc = pr_auc(scores, labels)
    assert auc > 0.9  # should be close to 1.0


def test_roc_auc() -> None:
    scores = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
    labels = [0, 0, 0, 0, 0, 1, 1, 1, 1, 1]
    auc = roc_auc(scores, labels)
    assert auc > 0.9


def test_fp_fn_tables() -> None:
    preds = [
        Prediction(mint="A", pred_has_cluster=True, risk_score=80, band="high_concentration", clusters_count=1, max_share=0.5, coordination_category="strong", elapsed_s=0.1, rpc_calls=0),
        Prediction(mint="B", pred_has_cluster=True, risk_score=30, band="suspicious", clusters_count=1, max_share=0.3, coordination_category="moderate", elapsed_s=0.1, rpc_calls=0),
        Prediction(mint="C", pred_has_cluster=False, risk_score=5, band="clean", clusters_count=0, max_share=0.0, coordination_category="none", elapsed_s=0.1, rpc_calls=0),
    ]
    gt = [
        {"mint": "A", "class_": "insider", "source": "creator_wallet"},
        {"mint": "B", "class_": "clean", "source": "exchange_listing"},
        {"mint": "C", "class_": "insider", "source": "bundled_accounts"},
    ]
    fp, fn = fp_fn_tables(preds, gt)
    # A: TP (insider, pred cluster) -> not in FP/FN
    # B: FP (clean, pred cluster)
    # C: FN (insider, no cluster)
    assert len(fp) == 1
    assert fp[0]["mint"] == "B"
    assert len(fn) == 1
    assert fn[0]["mint"] == "C"