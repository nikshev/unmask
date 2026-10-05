# impl: FR-003-16, FR-003-18, FR-003-19, FR-003-21
"""Публічний вхід фічі 003: `ClusterService(config).analyze(graph_result, ingest_result)`."""

from __future__ import annotations

from unmask.clusters.behavior import _all_funding_rows, behavioral_evidence, diagnostics
from unmask.clusters.cluster import ClusterDraft, form_clusters
from unmask.clusters.config import ClusterConfig
from unmask.clusters.indirect import indirect_links
from unmask.clusters.links import Link, extract_links, link_sort_key
from unmask.clusters.model import (
    Cluster,
    ClusterCompleteness,
    ClusterInputError,
    ClusterMember,
    ClusterMetadata,
    ClusterResult,
    ClusterWarning,
    Evidence,
    LINK_TYPES,
    ThresholdsSnapshot,
    TimeBasis,
    cluster_id,
    cluster_sort_key,
    evidence_sort_key,
)
from unmask.clusters.score import (
    assess,
    completeness_of,
    confidence,
    evidence_from_link,
    is_artifact,
    share_of,
)
from unmask.graph.model import GraphResult, NodeRole
from unmask.ingest.model import IngestResult, Rejection

__all__ = ["ClusterService"]


def _snapshot(config: ClusterConfig) -> ThresholdsSnapshot:
    weights = {
        "shared_funder": float(config.evidence_weights.shared_funder),
        "direct_transfer": float(config.evidence_weights.direct_transfer),
        "delegated_buy": float(config.evidence_weights.delegated_buy),
        "recovered_edge": float(config.evidence_weights.recovered_edge),
        "indirect_link": float(config.evidence_weights.indirect_link),
        "same_amounts": float(config.evidence_weights.same_amounts),
        "same_slot": float(config.evidence_weights.same_slot),
    }
    return ThresholdsSnapshot(
        link_min_amount_lamports=config.link_min_amount_lamports,
        funding_window_seconds=config.funding_window_seconds,
        seconds_per_slot=float(config.seconds_per_slot),
        link_through_flagged_buyers=config.link_through_flagged_buyers,
        link_assets=tuple(config.link_assets),
        indirect_enabled=config.indirect_enabled,
        same_amount_natural_max=config.same_amount_natural_max,
        same_slot_natural_max=config.same_slot_natural_max,
        same_slot_window_slots=config.same_slot_window_slots,
        evidence_weights=weights,
        slot_fallback_multiplier=float(config.slot_fallback_multiplier),
        artifact_buyer_share=float(config.artifact_buyer_share),
        artifact_confidence_multiplier=float(config.artifact_confidence_multiplier),
        band_clean_max=config.band_clean_max,
        band_suspicious_max=config.band_suspicious_max,
    )


class ClusterService:
    """Кластеризація ранніх покупців і оцінка ризику токена."""

    def __init__(self, config: ClusterConfig) -> None:
        if not isinstance(config, ClusterConfig):
            raise TypeError(f"config: expected ClusterConfig, got {type(config).__name__}")
        self._config = config

    @property
    def config(self) -> ClusterConfig:
        return self._config

    def analyze(self, graph_result: GraphResult, ingest_result: IngestResult) -> ClusterResult:
        if not isinstance(graph_result, GraphResult) or isinstance(graph_result, Rejection):
            raise TypeError(f"graph_result: expected GraphResult, got {type(graph_result).__name__}")
        if not isinstance(ingest_result, IngestResult) or isinstance(ingest_result, Rejection):
            raise TypeError(f"ingest_result: expected IngestResult, got {type(ingest_result).__name__}")
        self._check_consistency(graph_result, ingest_result)

        config = self._config
        graph_buyers = sorted(n.address for n in graph_result.graph.nodes if NodeRole.BUYER in n.roles)
        buyers_by_wallet = {b.wallet: b for b in ingest_result.buyers}

        links = extract_links(graph_result, config)
        drafts = form_clusters(graph_buyers, links)
        if config.indirect_enabled:
            so_far = self._components(drafts, graph_buyers)
            extra = indirect_links(graph_result, so_far, config)
            if extra:
                links = tuple(sorted(links + extra, key=link_sort_key))
                drafts = form_clusters(graph_buyers, links)

        denominator = sum(b.received_amount for b in ingest_result.buyers)
        funding_by_receiver: dict[str, list] = {}
        for row in _all_funding_rows(graph_result, config):
            funding_by_receiver.setdefault(row[1], []).append(row)
        clusters: list[Cluster] = []
        for draft in drafts:
            link_evidences = [evidence_from_link(link, config) for link in draft.links]
            behavioral = list(behavioral_evidence(draft, graph_result, ingest_result, config,
                                                 _funding_rows=funding_by_receiver))
            evidences = tuple(sorted(link_evidences + behavioral, key=evidence_sort_key))
            artifact = is_artifact(draft, graph_result, config)
            conf = confidence(evidences, config, artifact=artifact)
            members = tuple(
                ClusterMember(wallet=w, buyer_rank=buyers_by_wallet[w].rank,
                              received_amount=buyers_by_wallet[w].received_amount)
                for w in sorted(draft.wallets, key=lambda w: buyers_by_wallet[w].rank)
            )
            num, den, share = share_of([m.received_amount for m in members], denominator)
            warnings: list[ClusterWarning] = []
            if artifact:
                warnings.append(ClusterWarning.POSSIBLE_PRUNING_ARTIFACT)
            if any(e.basis == TimeBasis.SLOT for e in link_evidences):
                warnings.append(ClusterWarning.SLOT_TIME_FALLBACK)
            warnings_sorted = tuple(sorted(warnings, key=lambda w: w.value))
            clusters.append(Cluster(
                cluster_id=cluster_id(draft.wallets),
                members=members,
                share_numerator=num,
                share_denominator=den,
                share=share,
                evidence=evidences,
                confidence=conf,
                warnings=warnings_sorted,
            ))
        clusters_sorted = tuple(sorted(clusters, key=cluster_sort_key))

        diags = diagnostics(drafts, graph_result, ingest_result, config, _funding_rows=funding_by_receiver)
        completeness = completeness_of(graph_result)
        gm = graph_result.metadata
        risk, computed, band, reasons = assess(
            clusters_sorted, completeness, gm.wallets_analyzed, config)
        metadata = ClusterMetadata(
            mint=gm.mint,
            schema_version="003.1",
            cluster_config_version=config.version,
            thresholds=_snapshot(config),
            graph_schema_version=gm.schema_version,
            hub_config_version=gm.hub_config_version,
            address_lists_version=gm.address_lists_version,
            lists_applied=gm.lists_applied,
            ingest_config_version=gm.ingest_config_version,
            ingest_analyzed_at=gm.ingest_analyzed_at,
            ingest_source=gm.ingest_source,
            wallets_analyzed=gm.wallets_analyzed,
            share_denominator=denominator,
            share_denominator_kind="analyzed_buyers_received_amount",
        )
        return ClusterResult(
            metadata=metadata,
            completeness=completeness,
            clusters=clusters_sorted,
            diagnostics=diags,
            risk_score=risk,
            computed_band=computed,
            band=band,
            band_reasons=reasons,
        )

    @staticmethod
    def _components(drafts: tuple[ClusterDraft, ...], buyers: list[str]) -> dict[str, str]:
        comp: dict[str, str] = {}
        for d in drafts:
            root = min(d.wallets)
            for w in d.wallets:
                comp[w] = root
        for b in buyers:
            comp.setdefault(b, b)
        return comp

    @staticmethod
    def _check_consistency(graph_result: GraphResult, ingest_result: IngestResult) -> None:
        gm = graph_result.metadata
        im = ingest_result.metadata
        if gm.mint != im.mint:
            raise ClusterInputError(f"mint mismatch: graph {gm.mint} != ingest {im.mint}")
        graph_buyers = {n.address for n in graph_result.graph.nodes if NodeRole.BUYER in n.roles}
        ingest_buyers = {b.wallet for b in ingest_result.buyers}
        if graph_buyers != ingest_buyers:
            raise ClusterInputError("buyer sets differ between graph and ingest")
        ranks = {n.address: n.buyer_rank for n in graph_result.graph.nodes if NodeRole.BUYER in n.roles}
        for b in ingest_result.buyers:
            if ranks.get(b.wallet) != b.rank:
                raise ClusterInputError(f"buyer_rank mismatch for {b.wallet}")
        if gm.wallets_analyzed != im.wallets_analyzed:
            raise ClusterInputError("wallets_analyzed mismatch")
        if gm.ingest_analyzed_at != im.analyzed_at:
            raise ClusterInputError("ingest_analyzed_at mismatch")
