# impl: FR-003-18, FR-003-20
"""Серіалізація `ClusterResult` у JSON за `contracts/cluster-result.schema.json`."""

from __future__ import annotations

import json
from typing import Any

from unmask.clusters.model import ClusterResult

__all__ = ["to_dict", "to_json"]


def _ref_to_dict(ref) -> dict[str, Any]:
    return {
        "signature": ref.signature,
        "slot": ref.slot,
        "instruction_path": ref.instruction_path,
    }


def _window_to_dict(window) -> dict[str, Any]:
    return {"basis": window.basis.value, "start": window.start, "end": window.end}


def _evidence_to_dict(ev) -> dict[str, Any]:
    return {
        "type": ev.type.value,
        "wallets": list(ev.wallets),
        "via": list(ev.via),
        "refs": [_ref_to_dict(r) for r in ev.refs],
        "window": _window_to_dict(ev.window),
        "basis": ev.basis.value,
        "value": ev.value,
        "detail": ev.detail,
        "weight": ev.weight,
    }


def _cluster_to_dict(cluster) -> dict[str, Any]:
    return {
        "cluster_id": cluster.cluster_id,
        "members": [
            {"wallet": m.wallet, "buyer_rank": m.buyer_rank, "received_amount": m.received_amount}
            for m in cluster.members
        ],
        "share_numerator": cluster.share_numerator,
        "share_denominator": cluster.share_denominator,
        "share": cluster.share,
        "confidence": cluster.confidence,
        "evidence": [_evidence_to_dict(e) for e in cluster.evidence],
        "warnings": [w.value for w in cluster.warnings],
    }


def _diagnostic_to_dict(diag) -> dict[str, Any]:
    return {
        "kind": diag.kind.value,
        "wallets": list(diag.wallets),
        "value": diag.value,
        "count": diag.count,
        "detail": list(diag.detail),
    }


def to_dict(result: ClusterResult) -> dict[str, Any]:
    """Словник за схемою 003.1; явне поле за полем, не `asdict`."""
    if not isinstance(result, ClusterResult):
        raise TypeError(f"result: expected ClusterResult, got {type(result).__name__}")
    md = result.metadata
    th = md.thresholds
    return {
        "metadata": {
            "mint": md.mint,
            "schema_version": md.schema_version,
            "cluster_config_version": md.cluster_config_version,
            "thresholds": {
                "link_min_amount_lamports": th.link_min_amount_lamports,
                "funding_window_seconds": th.funding_window_seconds,
                "seconds_per_slot": th.seconds_per_slot,
                "link_through_flagged_buyers": th.link_through_flagged_buyers,
                "link_assets": list(th.link_assets),
                "indirect_enabled": th.indirect_enabled,
                "same_amount_natural_max": th.same_amount_natural_max,
                "same_slot_natural_max": th.same_slot_natural_max,
                "same_slot_window_slots": th.same_slot_window_slots,
                "evidence_weights": dict(th.evidence_weights),
                "slot_fallback_multiplier": th.slot_fallback_multiplier,
                "artifact_buyer_share": th.artifact_buyer_share,
                "artifact_confidence_multiplier": th.artifact_confidence_multiplier,
                "band_clean_max": th.band_clean_max,
                "band_suspicious_max": th.band_suspicious_max,
            },
            "graph_schema_version": md.graph_schema_version,
            "hub_config_version": md.hub_config_version,
            "address_lists_version": md.address_lists_version,
            "lists_applied": md.lists_applied,
            "ingest_config_version": md.ingest_config_version,
            "ingest_analyzed_at": md.ingest_analyzed_at,
            "ingest_source": md.ingest_source,
            "wallets_analyzed": md.wallets_analyzed,
            "share_denominator": md.share_denominator,
            "share_denominator_kind": md.share_denominator_kind,
        },
        "completeness": {
            "graph_status": result.completeness.graph_status.value,
            "ingest_status": result.completeness.ingest_status.value,
            "missing_count": result.completeness.missing_count,
            "delegated_complete": result.completeness.delegated_complete,
            "delegated_reason": result.completeness.delegated_reason,
            "graph_warnings": [w.value for w in result.completeness.graph_warnings],
            "can_be_clean": result.completeness.can_be_clean(md.wallets_analyzed),
        },
        "clusters": [_cluster_to_dict(c) for c in result.clusters],
        "diagnostics": [_diagnostic_to_dict(d) for d in result.diagnostics],
        "risk_score": result.risk_score,
        "computed_band": result.computed_band.value,
        "band": result.band.value,
        "band_reasons": list(result.band_reasons),
    }


def to_json(result: ClusterResult) -> str:
    """Канонічний JSON: сортовані ключі, компактні розділювачі, UTF-8, без NaN."""
    return json.dumps(to_dict(result), sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
