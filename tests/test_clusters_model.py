# verifies: FR-003-07, FR-003-09, FR-003-11, FR-003-12, FR-003-14, FR-003-15
"""Тести моделі кластерів: переліки, вікно, доказ, кластер, діагностика, повнота, метадані, результат."""

from __future__ import annotations

import pytest

from unmask.clusters.model import (
    BEHAVIORAL_TYPES,
    CLUSTER_SCHEMA_VERSION,
    LINK_TYPES,
    Cluster,
    ClusterCompleteness,
    ClusterInputError,
    ClusterMember,
    ClusterMetadata,
    ClusterResult,
    ClusterWarning,
    DiagnosticKind,
    DiagnosticSignal,
    Evidence,
    EvidenceType,
    RiskBand,
    SHARE_DENOMINATOR_KIND,
    ThresholdsSnapshot,
    TimeBasis,
    Window,
    cluster_id,
    cluster_sort_key,
    diagnostic_sort_key,
    evidence_sort_key,
)
from unmask.graph.model import (
    EdgeRef,
    GraphCompletenessStatus,
    GraphWarning,
)
from unmask.ingest.model import CompletenessStatus


def _ref(sig: str = "sig1", slot: int = 10, path: str | None = "0") -> EdgeRef:
    return EdgeRef(signature=sig, slot=slot, instruction_path=path)


def _window() -> Window:
    return Window(basis=TimeBasis.BLOCK_TIME, start=100, end=200)


def _link_evidence(wallets=("w1", "w2"), weight: float = 0.6,
                   etype: EvidenceType = EvidenceType.SHARED_FUNDER) -> Evidence:
    via = ("src",) if etype in (EvidenceType.SHARED_FUNDER, EvidenceType.RECOVERED_EDGE,
                                EvidenceType.INDIRECT_LINK) else ()
    return Evidence(
        type=etype, wallets=tuple(wallets), via=via,
        refs=(_ref(),), window=_window(), basis=TimeBasis.BLOCK_TIME,
        value=None, detail=None, weight=weight,
    )


def _cluster(wallets=("w1", "w2"), received=(100, 200), denom: int = 600,
             weights=(0.6,), etype: EvidenceType = EvidenceType.SHARED_FUNDER) -> Cluster:
    members = tuple(ClusterMember(w, i + 1, r) for i, (w, r) in enumerate(zip(sorted(wallets), received)))
    ev = []
    for i, w in enumerate(weights):
        refs = (_ref(f"sig{i}"),)
        via = ("src",) if etype in (EvidenceType.SHARED_FUNDER, EvidenceType.RECOVERED_EDGE,
                                    EvidenceType.INDIRECT_LINK) else ()
        ev.append(Evidence(type=etype, wallets=tuple(sorted(wallets)), via=via, refs=refs,
                           window=_window(), basis=TimeBasis.BLOCK_TIME,
                           value=None, detail=None, weight=w))
    ev_sorted = tuple(sorted(ev, key=evidence_sort_key))
    import math as _m
    prod = 1.0
    for w in weights:
        prod *= 1.0 - w
    conf = min(max(round(1.0 - prod, 4), 10**-4), 1.0 - 10**-4)  # як прод: межа насичення
    num = sum(received)
    return Cluster(
        cluster_id=cluster_id(wallets), members=members,
        share_numerator=num, share_denominator=denom,
        share=round(num / denom, 4), evidence=ev_sorted,
        confidence=conf, warnings=(),
    )


def _thresholds(**over) -> ThresholdsSnapshot:
    base = dict(
        link_min_amount_lamports=10_000_000, funding_window_seconds=3600, seconds_per_slot=0.4,
        link_through_flagged_buyers=False, link_assets=("sol",), indirect_enabled=True,
        same_amount_natural_max=3, same_slot_natural_max=5, same_slot_window_slots=0,
        evidence_weights={"shared_funder": 0.6, "direct_transfer": 0.5, "delegated_buy": 0.6,
                          "recovered_edge": 0.4, "indirect_link": 0.3, "same_amounts": 0.2,
                          "same_slot": 0.15},
        slot_fallback_multiplier=0.8, artifact_buyer_share=0.5, artifact_confidence_multiplier=0.5,
        band_clean_max=20, band_suspicious_max=50,
    )
    base.update(over)
    return ThresholdsSnapshot(**base)


def _metadata(**over) -> ClusterMetadata:
    base = dict(
        mint="MINT", schema_version="003.1", cluster_config_version=1, thresholds=_thresholds(),
        graph_schema_version="002.1", hub_config_version=3, address_lists_version=1, lists_applied=True,
        ingest_config_version=2, ingest_analyzed_at=1000, ingest_source="fixture:t",
        wallets_analyzed=2, share_denominator=600,
        share_denominator_kind="analyzed_buyers_received_amount",
    )
    base.update(over)
    return ClusterMetadata(**base)


def _completeness(**over) -> ClusterCompleteness:
    base = dict(
        graph_status=GraphCompletenessStatus.COMPLETE, ingest_status=CompletenessStatus.COMPLETE,
        missing_count=0, delegated_complete=True, delegated_reason=None, graph_warnings=(),
    )
    base.update(over)
    return ClusterCompleteness(**base)


# --- переліки й константи ---


def test_evidence_types_and_link_sets() -> None:
    assert len(EvidenceType) == 7
    assert len(LINK_TYPES) == 5
    assert len(BEHAVIORAL_TYPES) == 2
    assert not (LINK_TYPES & BEHAVIORAL_TYPES)
    assert CLUSTER_SCHEMA_VERSION == "003.1"
    assert SHARE_DENOMINATOR_KIND == "analyzed_buyers_received_amount"


def test_cluster_id_stable_and_sorted() -> None:
    assert cluster_id(("b", "a")) == cluster_id(("a", "b"))
    assert cluster_id(("a", "b")).startswith("c-") and len(cluster_id(("a",)) ) == 18


# --- Window ---


def test_window_rejects_bad_bounds() -> None:
    with pytest.raises(ValueError):
        Window(basis=TimeBasis.SLOT, start=5, end=4)
    with pytest.raises(ValueError):
        Window(basis=TimeBasis.SLOT, start=-1, end=0)
    with pytest.raises(TypeError):
        Window(basis="slot", start=True, end=1)  # noqa — bool не int


# --- Evidence ---


def test_evidence_wallets_sorted_unique_at_least_two() -> None:
    with pytest.raises(ValueError):
        _link_evidence(wallets=("w1",))
    with pytest.raises(ValueError):
        _link_evidence(wallets=("w2", "w1"))  # не відсортовано
    with pytest.raises(ValueError):
        _link_evidence(wallets=("w1", "w1"))


def test_evidence_via_rules_per_type() -> None:
    with pytest.raises(ValueError, match="via"):
        Evidence(type=EvidenceType.SHARED_FUNDER, wallets=("w1", "w2"), via=(),
                 refs=(_ref(),), window=_window(), basis=TimeBasis.BLOCK_TIME,
                 value=None, detail=None, weight=0.6)
    with pytest.raises(ValueError, match="via"):
        Evidence(type=EvidenceType.DIRECT_TRANSFER, wallets=("w1", "w2"), via=("x",),
                 refs=(_ref(),), window=_window(), basis=TimeBasis.BLOCK_TIME,
                 value=None, detail=None, weight=0.5)


def test_evidence_refs_sorted_and_path_rules() -> None:
    r1, r2 = _ref("b", 11), _ref("a", 10)
    with pytest.raises(ValueError, match="sorted"):
        Evidence(type=EvidenceType.SHARED_FUNDER, wallets=("w1", "w2"), via=("s",),
                 refs=(r1, r2), window=_window(), basis=TimeBasis.BLOCK_TIME,
                 value=None, detail=None, weight=0.6)
    with pytest.raises(ValueError, match="instruction_path"):
        Evidence(type=EvidenceType.DELEGATED_BUY, wallets=("w1", "w2"), via=(),
                 refs=(_ref("s", 1, "0"),), window=_window(), basis=TimeBasis.BLOCK_TIME,
                 value=None, detail=None, weight=0.6)


def test_behavioral_evidence_slot_basis_value_detail() -> None:
    w = Window(basis=TimeBasis.SLOT, start=7, end=7)
    e = Evidence(type=EvidenceType.SAME_AMOUNTS, wallets=("w1", "w2", "w3", "w4"), via=(),
                 refs=(_ref("s", 7, "0"),), window=w, basis=TimeBasis.SLOT,
                 value=1_000, detail="funding", weight=0.2)
    assert e.weight == 0.2
    with pytest.raises(ValueError, match="basis"):
        Evidence(type=EvidenceType.SAME_SLOT, wallets=("w1", "w2"), via=(),
                 refs=(_ref("s", 7, None),), window=_window(), basis=TimeBasis.BLOCK_TIME,
                 value=7, detail=None, weight=0.15)
    with pytest.raises(ValueError, match="value"):
        Evidence(type=EvidenceType.SHARED_FUNDER, wallets=("w1", "w2"), via=("s",),
                 refs=(_ref(),), window=_window(), basis=TimeBasis.BLOCK_TIME,
                 value=5, detail=None, weight=0.6)


def test_evidence_weight_range_and_rounding() -> None:
    with pytest.raises(ValueError, match="weight"):
        _link_evidence(weight=1.0)
    with pytest.raises(ValueError, match="weight"):
        _link_evidence(weight=0.60001)


# --- Cluster ---


def test_cluster_requires_link_evidence_and_checks_share_and_confidence() -> None:
    w = Window(basis=TimeBasis.SLOT, start=7, end=7)
    beh = Evidence(type=EvidenceType.SAME_SLOT, wallets=("w1", "w2"), via=(),
                   refs=(_ref("s", 7, None),), window=w, basis=TimeBasis.SLOT,
                   value=7, detail=None, weight=0.15)
    with pytest.raises(ValueError, match="LINK_TYPES"):
        Cluster(cluster_id=cluster_id(("w1", "w2")),
                members=(ClusterMember("w1", 1, 100), ClusterMember("w2", 2, 200)),
                share_numerator=300, share_denominator=600, share=0.5,
                evidence=(beh,), confidence=0.15, warnings=())
    c = _cluster()
    assert c.share == 0.5 and c.confidence == 0.6
    assert cluster_sort_key(c) == (-300, c.cluster_id)


def test_cluster_confidence_noisy_or_with_two_evidences() -> None:
    c = _cluster(weights=(0.6, 0.5), etype=EvidenceType.DIRECT_TRANSFER)
    # direct_transfer via must be empty — helper used shared via; rebuild properly
    assert c.confidence == round(1 - 0.4 * 0.5, 4)


def test_cluster_validates_saturated_confidence_below_1() -> None:
    """Перевищення доказів насичує noisy-OR: кластер валідується з 0.9999,
    а не падає (реальні токени скринінгу 006)."""
    c = _cluster(weights=tuple([0.6] * 20))
    assert c.confidence == 0.9999


def test_cluster_slot_fallback_warning_parity() -> None:
    w = Window(basis=TimeBasis.SLOT, start=1, end=2)
    e = Evidence(type=EvidenceType.SHARED_FUNDER, wallets=("w1", "w2"), via=("s",),
                 refs=(_ref("a", 1),), window=w, basis=TimeBasis.SLOT,
                 value=None, detail=None, weight=0.48)
    with pytest.raises(ValueError, match="slot_time_fallback"):
        Cluster(cluster_id=cluster_id(("w1", "w2")),
                members=(ClusterMember("w1", 1, 100), ClusterMember("w2", 2, 100)),
                share_numerator=200, share_denominator=400, share=0.5,
                evidence=(e,), confidence=0.48, warnings=())


# --- DiagnosticSignal ---


def test_diagnostic_flagged_shape() -> None:
    d = DiagnosticSignal(kind=DiagnosticKind.FLAGGED_BUYERS_EXCLUDED, wallets=("a",),
                         value=None, count=1, detail=("a:known_list",))
    assert diagnostic_sort_key(d)[0] == "flagged_buyers_excluded"
    with pytest.raises(ValueError, match="count"):
        DiagnosticSignal(kind=DiagnosticKind.FLAGGED_BUYERS_EXCLUDED, wallets=("a", "b"),
                         value=None, count=1, detail=("a:known_list", "b:degree"))


# --- Completeness / Metadata / Result ---


def test_completeness_parities() -> None:
    c = _completeness()
    assert c.can_be_clean(1) is True
    assert c.can_be_clean(0) is False
    with pytest.raises(ValueError, match="delegated"):
        _completeness(delegated_complete=True, delegated_reason="x",
                      graph_warnings=(GraphWarning.DELEGATED_INCOMPLETE,))


def test_result_clean_only_on_complete_data() -> None:
    c = _cluster()
    md = _metadata()
    comp = _completeness()
    r = ClusterResult(metadata=md, completeness=comp, clusters=(c,), diagnostics=(),
                      risk_score=30, computed_band=RiskBand.SUSPICIOUS,
                      band=RiskBand.SUSPICIOUS, band_reasons=("clusters_share_weighted",))
    assert r.risk_score == 30
    bad_comp = _completeness(graph_status=GraphCompletenessStatus.INCOMPLETE,
                             ingest_status=CompletenessStatus.INCOMPLETE,
                             missing_count=1, delegated_complete=False,
                             delegated_reason="not_analyzed",
                             graph_warnings=(GraphWarning.DELEGATED_INCOMPLETE,))
    with pytest.raises(ValueError, match="clean"):
        ClusterResult(metadata=md, completeness=bad_comp, clusters=(), diagnostics=(),
                      risk_score=0, computed_band=RiskBand.CLEAN, band=RiskBand.CLEAN,
                      band_reasons=("no_clusters_on_complete_data",))


def test_result_members_do_not_overlap_and_score_formula() -> None:
    c1 = _cluster(wallets=("w1", "w2"), received=(100, 200), denom=600)
    with pytest.raises(ValueError, match="overlap"):
        ClusterResult(metadata=_metadata(), completeness=_completeness(),
                      clusters=(c1, c1), diagnostics=(), risk_score=50,
                      computed_band=RiskBand.SUSPICIOUS, band=RiskBand.SUSPICIOUS,
                      band_reasons=("clusters_share_weighted",))
    c2 = _cluster(wallets=("w1", "w2"), received=(100, 200), denom=600)
    with pytest.raises(ValueError, match="risk_score"):
        ClusterResult(metadata=_metadata(), completeness=_completeness(),
                      clusters=(c2,), diagnostics=(), risk_score=99,
                      computed_band=RiskBand.HIGH_CONCENTRATION, band=RiskBand.HIGH_CONCENTRATION,
                      band_reasons=("clusters_share_weighted",))
