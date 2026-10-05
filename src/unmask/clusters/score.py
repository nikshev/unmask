# impl: FR-003-11, FR-003-13, FR-003-14, FR-003-15, FR-003-16, FR-003-17
"""Частка, впевненість (noisy-OR), правило артефакту, оцінка токена й смуги, чесна неповнота."""

from __future__ import annotations

import math
from typing import Sequence

from unmask.clusters.cluster import ClusterDraft
from unmask.clusters.config import ClusterConfig
from unmask.clusters.links import Link
from unmask.clusters.model import (
    Cluster,
    ClusterCompleteness,
    Evidence,
    EvidenceType,
    LINK_TYPES,
    RiskBand,
    TimeBasis,
)
from unmask.graph.model import GraphCompletenessStatus, GraphWarning
from unmask.ingest.model import CompletenessStatus

__all__ = [
    "evidence_weight",
    "evidence_from_link",
    "confidence",
    "share_of",
    "risk_score",
    "computed_band",
    "completeness_of",
    "is_artifact",
    "assess",
]


def evidence_weight(etype: EvidenceType, basis: TimeBasis, config: ClusterConfig) -> float:
    """Вага доказу: `evidence_weights[type] × (slot_fallback_multiplier, якщо зв'язок у слотах)`."""
    base = float(getattr(config.evidence_weights, etype.value))
    if etype in LINK_TYPES and basis == TimeBasis.SLOT:
        base *= float(config.slot_fallback_multiplier)
    return round(base, 4)


def evidence_from_link(link: Link, config: ClusterConfig) -> Evidence:
    """`Evidence` з полів `Link`; вага — за `evidence_weight`."""
    return Evidence(
        type=link.type,
        wallets=tuple(sorted(link.buyers)),
        via=link.via,
        refs=link.refs,
        window=link.window,
        basis=link.basis,
        value=None,
        detail=None,
        weight=evidence_weight(link.type, link.basis, config),
    )


def confidence(evidence: Sequence[Evidence], config: ClusterConfig, *, artifact: bool) -> float:
    """Noisy-OR ваг: `round(1 − Π(1 − w), 4)`; з артефактом — `× artifact_confidence_multiplier`."""
    prod = 1.0
    for e in evidence:
        prod *= 1.0 - e.weight
    value = round(1.0 - prod, 4)
    if artifact:
        value = round(value * float(config.artifact_confidence_multiplier), 4)
    return value


def share_of(members_received: Sequence[int], denominator: int) -> tuple[int, int, float]:
    """`(чисельник, знаменник, round(чисельник/знаменник, 4))`."""
    num = sum(members_received)
    return (num, denominator, round(num / denominator, 4))


def risk_score(clusters: Sequence[Cluster]) -> int:
    """`floor(100 · Σ share·confidence + 0.5)` за збереженими округленими значеннями."""
    return math.floor(100 * sum(c.share * c.confidence for c in clusters) + 0.5)


def computed_band(score: int, config: ClusterConfig) -> RiskBand:
    """Смуга з числа: `<= band_clean_max` → clean; `<= band_suspicious_max` → suspicious; інакше high."""
    if score <= config.band_clean_max:
        return RiskBand.CLEAN
    if score <= config.band_suspicious_max:
        return RiskBand.SUSPICIOUS
    return RiskBand.HIGH_CONCENTRATION


def completeness_of(graph_result) -> ClusterCompleteness:
    """Копія повноти графа 002."""
    gc = graph_result.completeness
    return ClusterCompleteness(
        graph_status=gc.status,
        ingest_status=gc.ingest_status,
        missing_count=len(gc.missing),
        delegated_complete=gc.delegated_complete,
        delegated_reason=gc.delegated_reason,
        graph_warnings=tuple(graph_result.report.warnings),
    )


def is_artifact(draft: ClusterDraft, graph_result, config: ClusterConfig) -> bool:
    """Артефакт відсікання: частка покупців вибірки `> artifact_buyer_share` строго і
    (`giant_component` у попередженнях графа або непрямих доказів більше за решту зв'язків)."""
    wallets_analyzed = graph_result.metadata.wallets_analyzed
    if wallets_analyzed <= 0:
        return False
    if not (len(draft.wallets) / wallets_analyzed > float(config.artifact_buyer_share)):
        return False
    if GraphWarning.GIANT_COMPONENT in graph_result.report.warnings:
        return True
    indirect = sum(1 for link in draft.links if link.type == EvidenceType.INDIRECT_LINK)
    return indirect > (len(draft.links) - indirect)


def assess(clusters: Sequence[Cluster], completeness: ClusterCompleteness,
           wallets_analyzed: int, config: ClusterConfig) -> tuple[int, RiskBand, RiskBand, tuple[str, ...]]:
    """`(risk_score, computed_band, band, band_reasons)` з правилом чесної неповноти."""
    score = risk_score(clusters)
    computed = computed_band(score, config)
    if wallets_analyzed == 0:
        return (0, RiskBand.CLEAN, RiskBand.INSUFFICIENT_DATA, ("empty_input",))
    if computed == RiskBand.CLEAN and not completeness.can_be_clean(wallets_analyzed):
        reasons: list[str] = []
        if completeness.ingest_status != CompletenessStatus.COMPLETE:
            reasons.append("ingest_incomplete")
        if completeness.missing_count > 0:
            reasons.append(f"missing_histories:{completeness.missing_count}")
        if not completeness.delegated_complete:
            reasons.append("delegated_incomplete")
        return (score, computed, RiskBand.INSUFFICIENT_DATA, tuple(reasons))
    if computed == RiskBand.CLEAN:
        if len(clusters) == 0:
            return (score, computed, computed, ("no_clusters_on_complete_data",))
        return (score, computed, computed, ("clusters_within_clean_threshold",))
    return (score, computed, computed, ("clusters_share_weighted",))
