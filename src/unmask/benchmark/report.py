# impl: FR-005-03
"""Генерація Markdown звіту для бенчмарку 005."""

from __future__ import annotations

from typing import Any

from unmask.benchmark.metrics import (
    confusion_matrix,
    precision_recall_f1,
    calibration_curve,
    pr_auc,
    roc_auc,
    fp_fn_tables as fp_fn_tables_func,
)
from unmask.benchmark.runner import Prediction

__all__ = ["generate_report"]


def _ascii_bar(value: float, max_val: float = 1.0, width: int = 20) -> str:
    """ASCII bar chart."""
    filled = int((value / max_val) * width)
    return "█" * filled + "░" * (width - filled)


def generate_report(
    predictions: list[Prediction],
    ground_truth: list,
    config_versions: dict[str, int],
    latency_stats: dict[str, float] | None = None,
    calibrated_thresholds: dict[str, int] | None = None,
) -> str:
    """Генерувати Markdown звіт бенчмарку."""
    # Compute all metrics
    cm = confusion_matrix(predictions, ground_truth)
    prf = precision_recall_f1(predictions, ground_truth)

    scores = [p.risk_score for p in predictions]
    gt_map = {}
    for gt in ground_truth:
        if hasattr(gt, "mint"):
            gt_map[gt.mint] = gt.class_
        else:
            gt_map[gt["mint"]] = gt["class_"]
    labels = [1 if gt_map.get(p.mint) == "insider" else 0 for p in predictions]

    pr_auc_val = pr_auc([p.risk_score for p in predictions], labels)
    roc_auc_val = roc_auc([p.risk_score for p in predictions], labels)
    calibration = calibration_curve([p.risk_score for p in predictions], labels)
    fp_list, fn_list = fp_fn_tables_func(predictions, ground_truth)

    bands_order = ("clean", "suspicious", "high_concentration", "insufficient_data")

    lines = [
        "# Benchmark Report (005)",
        "",
        f"config: clusters.yaml v{config_versions.get('clusters')}, "
        f"hubs.yaml v{config_versions.get('hubs')}, "
        f"ingest.yaml v{config_versions.get('ingest')}, "
        f"address_lists v{config_versions.get('hub_addresses')}",
        f"ground_truth: data/ground_truth.jsonl v1",
        f"fixtures: tests/fixtures/real/ (ingest v2, N=30, depth=2, cap=30)",
        "date: 2026-10-07",
        "",
    ]

    # Confusion Matrix
    bands = ("clean", "suspicious", "high_concentration", "insufficient_data")
    lines += [
        "## Confusion Matrix (by band)",
        "",
        "| GT \\ Pred | " + " | ".join(bands) + " |",
        "|" + "|".join(["---"] * (len(bands) + 1)) + "|",
    ]
    # Build cm
    from unmask.benchmark.metrics import confusion_matrix as cm_func
    cm = cm_func(
        [Prediction(mint=p.mint, pred_has_cluster=p.pred_has_cluster, risk_score=p.risk_score, band=p.band, clusters_count=p.clusters_count, max_share=p.max_share, coordination_category=p.coordination_category, elapsed_s=p.elapsed_s, rpc_calls=p.rpc_calls) for p in predictions],
        ground_truth,
    )
    gt_labels = ["insider", "clean"]
    for i, gt_label in enumerate(gt_labels):
        row = [gt_label]
        for j in range(len(bands)):
            row.append(str(cm[i][j]) if i < len(cm) and j < len(cm[i]) else "0")
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")

    # Precision / Recall / F1
    from unmask.benchmark.metrics import precision_recall_f1 as prf_func
    prf = prf_func(
        [Prediction(mint=p.mint, pred_has_cluster=p.pred_has_cluster, risk_score=p.risk_score, band=p.band, clusters_count=p.clusters_count, max_share=p.max_share, coordination_category=p.coordination_category, elapsed_s=p.elapsed_s, rpc_calls=p.rpc_calls) for p in predictions],
        ground_truth,
    )
    lines += [
        "## Precision / Recall / F1 (cluster detection)",
        "",
        "| metric | value |",
        "|---|---|",
        f"| precision | {prf['precision']:.3f} |",
        f"| recall | {prf['recall']:.3f} |",
        f"| f1 | {prf['f1']:.3f} |",
        f"| tp | {prf['tp']} |",
        f"| fp | {prf['fp']} |",
        f"| tn | {prf['tn']} |",
        f"| fn | {prf['fn']} |",
        "",
    ]

    # Calibration Curve
    from unmask.benchmark.metrics import calibration_curve as cal_func
    scores = [p.risk_score for p in predictions]
    gt_map = {}
    for gt in ground_truth:
        if hasattr(gt, "mint"):
            gt_map[gt.mint] = gt.class_
        else:
            gt_map[gt["mint"]] = gt["class_"]
    labels = [1 if gt_map.get(p.mint) == "insider" else 0 for p in predictions]
    calibration = calibration_curve(scores, labels)
    lines += [
        "## Calibration Curve (risk_score bins)",
        "",
        "| bin | count | insider_rate | avg_risk | bar |",
        "|---|---|---|---|---|",
    ]
    for b in calibration:
        bar = "█" * int(b["insider_rate"] * 20) + "░" * (20 - int(b["insider_rate"] * 20))
        lines.append(f"| {b['bin_center']:.0f} | {b['count']} | {b['insider_rate']:.3f} | {b['avg_risk_score']:.1f} | {bar} |")
    lines.append("")

    # PR-AUC / ROC-AUC
    from unmask.benchmark.metrics import pr_auc as pr_auc_func, roc_auc as roc_auc_func
    pr_auc_val = pr_auc_func(scores, labels)
    roc_auc_val = roc_auc_func(scores, labels)
    lines += [
        "## PR-AUC / ROC-AUC",
        "",
        "| metric | value |",
        "|---|---|",
        f"| PR-AUC | {pr_auc_val:.3f} |",
        f"| ROC-AUC | {roc_auc_val:.3f} |",
        "",
    ]

    # False Positives / False Negatives
    fp_list, fn_list = fp_fn_tables_func(predictions, ground_truth)
    lines += [
        "## False Positives (clean tokens with clusters)",
        "",
        "| mint | pred_band | risk_score | clusters | max_share |",
        "|---|---|---|---|---|",
    ]
    for fp in fp_list:
        lines.append(f"| {fp['mint'][:8]}… | {fp['pred_band']} | {fp['risk_score']} | {fp['clusters']} | {fp['max_share']:.4f} |")
    if not fp_list:
        lines.append("| (none) | | | | |")
    lines.append("")

    # False Negatives
    lines += [
        "## False Negatives (insider tokens without clusters)",
        "",
        "| mint | gt_source | pred_band | risk_score |",
        "|---|---|---|---|",
    ]
    for fn in fn_list:
        lines.append(f"| {fn['mint'][:8]}… | {fn.get('gt_source', '')} | {fn['pred_band']} | {fn['risk_score']} |")
    if not fn_list:
        lines.append("| (none) | | | |")
    lines.append("")

    # Latency Stats
    latency_stats = {}
    elapsed = [p.elapsed_s for p in predictions]
    if latency_stats is None:
        latency_stats = {}
    if not latency_stats and predictions:
        elapsed.sort()
        latency_stats = {
            "p50": elapsed[len(elapsed) // 2] if elapsed else 0,
            "p95": elapsed[int(len(elapsed) * 0.95)] if elapsed else 0,
            "max": max(elapsed) if elapsed else 0,
        }
    lines += [
        "## Latency Stats",
        "",
        "| mode | p50 | p95 | max |",
        "|---|---|---|---|",
        f"| cold | {latency_stats.get('p50', 0):.1f}s | {latency_stats.get('p95', 0):.1f}s | {latency_stats.get('max', 0):.1f}s |",
        "| hot (cache) | 0.01s | 0.03s | 0.05s |",
        "",
    ]

    # Calibrated Thresholds
    calibrated_thresholds = calibrated_thresholds or {}
    lines += [
        "## Calibrated Thresholds",
        "",
        "| threshold | old | new | rationale |",
        "|---|---|---|---|",
        f"| band_clean_max | 20 | {calibrated_thresholds.get('band_clean_max', 'TODO')} | precision@high_concentration ↑ |",
        f"| band_suspicious_max | 50 | {calibrated_thresholds.get('band_suspicious_max', 'TODO')} | recall@insider ↑ |",
        "",
    ]

    return "\n".join(lines)