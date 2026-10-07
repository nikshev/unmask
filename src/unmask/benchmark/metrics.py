# impl: FR-005-03
"""Метрики для бенчмарку 005: confusion matrix, PR/ROC-AUC, calibration curve."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = [
    "confusion_matrix",
    "precision_recall_f1",
    "calibration_curve",
    "pr_auc",
    "roc_auc",
    "fp_fn_tables",
]


@dataclass
class MetricResult:
    precision: float
    recall: float
    f1: float
    tp: int
    fp: int
    tn: int
    fn: int


def confusion_matrix(
    predictions: list,
    ground_truth: list,
    bands_order: list[str] = ("clean", "suspicious", "high_concentration", "insufficient_data"),
) -> list[list[int]]:
    """Confusion matrix за смугами.

    Args:
        predictions: список Prediction об'єктів.
        ground_truth: список GroundTruthRecord або dict.
        bands_order: порядок смуг для матриці.

    Returns:
        Матриця [gt_idx][pred_idx] з кількістю токенів.
    """
    n = len(bands_order)
    cm = [[0] * n for _ in range(n)]
    band_to_idx = {b: i for i, b in enumerate(bands_order)}

    gt_map = {}
    for gt in ground_truth:
        if hasattr(gt, "mint"):
            gt_map[gt.mint] = gt.class_
        else:
            gt_map[gt["mint"]] = gt["class_"]

    for pred in predictions:
        gt_class = gt_map.get(pred.mint)
        if gt_class is None:
            continue
        # map gt class to band
        if gt_class == "insider":
            # insider tokens: expected bands are suspicious/high_concentration
            # map to suspicious for binary classification
            gt_band = "suspicious"
        else:
            gt_band = "clean"

        pred_band = pred.band
        if pred_band not in band_to_idx:
            continue
        if gt_band not in band_to_idx:
            continue

        cm[band_to_idx[gt_band]][band_to_idx[pred_band]] += 1

    return cm


def precision_recall_f1(
    predictions: list,
    ground_truth: list,
) -> dict[str, float]:
    """Precision, Recall, F1 для детекції кластерів (binary: has_cluster vs no_cluster).

    has_cluster ⇔ clusters_count > 0 (або band != clean/insufficient_data)
    insider ⇔ gt.class == "insider"
    """
    tp = fp = tn = fn = 0

    gt_map = {}
    for gt in ground_truth:
        if hasattr(gt, "mint"):
            gt_map[gt.mint] = gt.class_
        else:
            gt_map[gt["mint"]] = gt["class_"]

    for pred in predictions:
        gt_class = gt_map.get(pred.mint)
        if gt_class is None:
            continue
        pred_cluster = pred.pred_has_cluster
        gt_is_insider = gt_class == "insider"

        if pred_cluster and gt_is_insider:
            tp += 1
        elif pred_cluster and not gt_is_insider:
            fp += 1
        elif not pred_cluster and gt_is_insider:
            fn += 1
        else:
            tn += 1

    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0

    return {
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "tp": tp,
        "fp": fp,
        "tn": tn,
        "fn": fn,
    }


def calibration_curve(
    risk_scores: list[int],
    gt_labels: list[int],
    n_bins: int = 10,
) -> list[dict[str, Any]]:
    """Calibration curve: P(insider|bin) по біннах risk_score.

    Args:
        risk_scores: список risk_score (0-100).
        gt_labels: 1 для insider, 0 для clean.
        n_bins: кількість біннів.

    Returns:
        Список dict: bin_center, count, insider_rate, avg_risk_score.
    """
    if len(risk_scores) != len(gt_labels):
        raise ValueError("risk_scores and gt_labels must have same length")

    bins = [[] for _ in range(n_bins)]
    bin_edges = [i * 100 / n_bins for i in range(n_bins + 1)]

    for score, label in zip(risk_scores, gt_labels):
        if score >= 100:
            bin_idx = n_bins - 1
        else:
            bin_idx = int(score / 100 * n_bins)
        bins[bin_idx].append(label)

    result = []
    for i, bin_labels in enumerate(bins):
        if not bin_labels:
            continue
        count = len(bin_labels)
        insider_rate = sum(bin_labels) / count
        avg_risk = (bin_edges[i] + bin_edges[i + 1]) / 2
        result.append({
            "bin_center": round(avg_risk, 1),
            "count": count,
            "insider_rate": round(insider_rate, 3),
            "avg_risk_score": round(avg_risk, 1),
        })

    return result


def _trapezoidal_auc(x: list[float], y: list[float]) -> float:
    """Trapezoidal rule for AUC."""
    if len(x) != len(y) or len(x) < 2:
        return 0.0
    auc = 0.0
    for i in range(len(x) - 1):
        auc += (x[i + 1] - x[i]) * (y[i + 1] + y[i]) / 2
    return auc


def pr_auc(scores: list[int], labels: list[int]) -> float:
    """PR-AUC (precision-recall AUC) для binary classification.

    scores: risk_score (0-100), higher = more likely insider.
    labels: 1 = insider, 0 = clean.
    """
    if len(scores) != len(labels):
        raise ValueError("scores and labels must have same length")

    # Sort by score descending
    pairs = sorted(zip(scores, labels), key=lambda x: x[0], reverse=True)

    tp = 0
    fp = 0
    total_pos = sum(labels)
    if total_pos == 0:
        return 0.0

    precisions = []
    recalls = []

    for score, label in pairs:
        if label == 1:
            tp += 1
        else:
            fp += 1
        precision = tp / (tp + fp) if (tp + fp) > 0 else 1.0
        recall = tp / total_pos
        precisions.append(precision)
        recalls.append(recall)

    # Add point (recall=0, precision=1) at start
    recalls = [0.0] + recalls
    precisions = [1.0] + precisions
    # Add point (recall=1, precision=last_precision) at end
    recalls.append(1.0)
    precisions.append(precisions[-1])

    return _trapezoidal_auc(recalls, precisions)


def roc_auc(scores: list[int], labels: list[int]) -> float:
    """ROC-AUC для binary classification."""
    if len(scores) != len(labels):
        raise ValueError("scores and labels must have same length")

    pairs = sorted(zip(scores, labels), key=lambda x: x[0], reverse=True)

    tp = 0
    fp = 0
    total_pos = sum(labels)
    total_neg = len(labels) - total_pos

    if total_pos == 0 or total_neg == 0:
        return 0.0

    tpr_list = [0.0]
    fpr_list = [0.0]

    for score, label in pairs:
        if label == 1:
            tp += 1
        else:
            fp += 1
        tpr_list.append(tp / total_pos)
        fpr_list.append(fp / total_neg)

    tpr_list.append(1.0)
    fpr_list.append(1.0)

    return _trapezoidal_auc(fpr_list, tpr_list)


def fp_fn_tables(
    predictions: list,
    ground_truth: list,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """False Positives / False Negatives таблиці.

    Returns:
        (fp_list, fn_list) — списки dict з деталями.
    """
    gt_map = {}
    gt_source = {}
    for gt in ground_truth:
        if hasattr(gt, "mint"):
            gt_map[gt.mint] = gt.class_
            gt_source[gt.mint] = gt.source
        else:
            gt_map[gt["mint"]] = gt["class_"]
            gt_source[gt["mint"]] = gt.get("source", "")

    fp_list = []
    fn_list = []

    for pred in predictions:
        gt_class = gt_map.get(pred.mint)
        if gt_class is None:
            continue
        pred_cluster = pred.pred_has_cluster
        gt_is_insider = gt_class == "insider"

        if pred_cluster and not gt_is_insider:
            fp_list.append({
                "mint": pred.mint,
                "pred_band": pred.band,
                "risk_score": pred.risk_score,
                "clusters": pred.clusters_count,
                "max_share": pred.max_share,
            })
        elif not pred_cluster and gt_is_insider:
            fn_list.append({
                "mint": pred.mint,
                "gt_source": gt_source.get(pred.mint, ""),
                "pred_band": pred.band,
                "risk_score": pred.risk_score,
            })

    return fp_list, fn_list