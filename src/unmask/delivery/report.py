# impl: FR-004-01, FR-004-12
"""Документ відповіді API з трійки результатів 001/002/003 (схема 004.1).

Числа 003 проходять слово в слово: жодного перерахунку часток, впевненостей,
`risk_score` і смуги. `insufficient_data` копіюється як є і ніколи не стає `clean`.
"""

from __future__ import annotations

import json
from typing import Any

from unmask.clusters.model import ClusterResult
from unmask.clusters.serialize import to_dict as cluster_to_dict
from unmask.graph.model import GraphResult
from unmask.ingest.model import IngestResult

__all__ = ["build_report", "to_json"]


def _require(name: str, value: Any, cls: type) -> None:
    if not isinstance(value, cls):
        raise TypeError(f"{name}: expected {cls.__name__}, got {type(value).__name__}")


def build_report(ingest_result: IngestResult, graph_result: GraphResult,
                 cluster_result: ClusterResult) -> dict[str, Any]:
    """Документ відповіді 004.1. Входи не змінюються."""
    _require("ingest_result", ingest_result, IngestResult)
    _require("graph_result", graph_result, GraphResult)
    _require("cluster_result", cluster_result, ClusterResult)
    doc003 = cluster_to_dict(cluster_result)

    clusters = []
    for c in doc003["clusters"]:
        evidence = []
        for e in c["evidence"]:
            evidence.append({
                "type": e["type"],
                "source": sorted(set(e["via"]) | set(e["wallets"])),
                "window": dict(e["window"]),
            })
        clusters.append({
            "cluster_id": c["cluster_id"],
            "wallets": [m["wallet"] for m in c["members"]],
            "supply_share": c["share"],
            "supply_share_denominator": "analyzed_buyers_received_amount",
            "confidence": c["confidence"],
            "evidence": evidence,
        })

    md = doc003["metadata"]
    comp = doc003["completeness"]
    reasons: list[str] = []
    if md["wallets_analyzed"] == 0:
        reasons.append("empty_input")
    else:
        if comp["ingest_status"] != "complete":
            reasons.append("ingest_incomplete")
        if comp["missing_count"] > 0:
            reasons.append(f"missing_histories:{comp['missing_count']}")
        if not comp["delegated_complete"]:
            reasons.append("delegated_incomplete")
    if not reasons:
        reasons.append("complete_data")

    # coordination_category: none | weak | moderate | strong | high_concentration
    risk = doc003["risk_score"]
    if not clusters:
        coord = "none"
    elif risk >= 50:
        coord = "strong"
    elif risk >= 20:
        coord = "moderate"
    elif any(c["supply_share"] > 0.5 for c in clusters):
        coord = "high_concentration"
    else:
        coord = "weak"

    return {
        "mint": md["mint"],
        "analyzed_at": md["ingest_analyzed_at"],
        "wallets_analyzed": md["wallets_analyzed"],
        "clusters": clusters,
        "risk_score": doc003["risk_score"],
        "band": doc003["band"],
        "coordination_category": coord,
        "band_reasons": list(doc003["band_reasons"]),
        "provenance": {
            "ingest_config_version": md["ingest_config_version"],
            "hub_config_version": md["hub_config_version"],
            "address_lists_version": md["address_lists_version"],
            "cluster_config_version": md["cluster_config_version"],
            "analyzed_at": md["ingest_analyzed_at"],
            "graph_status": comp["graph_status"],
            "completeness_reasons": reasons,
        },
        "error": None,
    }


def to_json(doc: dict[str, Any]) -> str:
    """Канонічний JSON документа відповіді."""
    if not isinstance(doc, dict):
        raise TypeError(f"doc: expected dict, got {type(doc).__name__}")
    return json.dumps(doc, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
