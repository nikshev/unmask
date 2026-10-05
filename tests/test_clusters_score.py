# verifies: FR-003-11, FR-003-13, FR-003-14, FR-003-17
"""Тести впевненості, частки, ваг і правила артефакту (T-068, T-079)."""

from __future__ import annotations

import random
from pathlib import Path
from types import SimpleNamespace

import pytest

from unmask.clusters.cluster import ClusterDraft, form_clusters
from unmask.clusters.config import load_cluster_config
from unmask.clusters.links import Link
from unmask.clusters.model import Evidence, EvidenceType, TimeBasis, Window, evidence_sort_key
from unmask.clusters.score import confidence, evidence_from_link, evidence_weight, is_artifact, share_of
from unmask.graph.model import EdgeRef, GraphWarning

CFG = load_cluster_config(Path(__file__).resolve().parents[1] / "config" / "clusters.yaml")


def _ev(etype: EvidenceType, weight: float, basis=TimeBasis.BLOCK_TIME) -> Evidence:
    via = ("s",) if etype in (EvidenceType.SHARED_FUNDER, EvidenceType.INDIRECT_LINK) else ()
    window = Window(basis=basis, start=1, end=1)
    refs = (EdgeRef(signature="sig_" + etype.value, slot=1,
                    instruction_path=None if etype in (EvidenceType.DELEGATED_BUY,) else "0"),)
    wallets = ("w1", "w2")
    if etype in (EvidenceType.SAME_AMOUNTS, EvidenceType.SAME_SLOT):
        is_funding = etype == EvidenceType.SAME_AMOUNTS
        refs = (EdgeRef(signature="sig_" + etype.value, slot=1,
                        instruction_path="0" if is_funding else None),)
        window = Window(basis=TimeBasis.SLOT, start=1, end=1)
        return Evidence(type=etype, wallets=wallets, via=(), refs=refs, window=window,
                        basis=TimeBasis.SLOT, value=7,
                        detail="funding" if is_funding else None,
                        weight=weight)
    return Evidence(type=etype, wallets=wallets, via=via, refs=refs, window=window,
                    basis=basis, value=None, detail=None, weight=weight)


def test_single_shared_funder_is_0_6() -> None:
    assert confidence([_ev(EvidenceType.SHARED_FUNDER, 0.6)], CFG, artifact=False) == 0.6


def test_shared_plus_same_amounts_is_0_68_plus_same_slot_is_0_728() -> None:
    two = [_ev(EvidenceType.SHARED_FUNDER, 0.6), _ev(EvidenceType.SAME_AMOUNTS, 0.2)]
    assert confidence(two, CFG, artifact=False) == 0.68
    three = two + [_ev(EvidenceType.SAME_SLOT, 0.15)]
    assert confidence(three, CFG, artifact=False) == 0.728


def test_two_direct_transfers_are_0_75() -> None:
    assert confidence([_ev(EvidenceType.DIRECT_TRANSFER, 0.5), _ev(EvidenceType.DIRECT_TRANSFER, 0.5)],
                      CFG, artifact=False) == 0.75


def test_indirect_only_is_0_3_and_lower_than_shared_funder() -> None:
    assert confidence([_ev(EvidenceType.INDIRECT_LINK, 0.3)], CFG, artifact=False) == 0.3 < 0.6


def test_shared_funder_in_slots_is_0_48_via_multiplier_on_weight() -> None:
    w = evidence_weight(EvidenceType.SHARED_FUNDER, TimeBasis.SLOT, CFG)
    assert w == 0.48
    assert confidence([_ev(EvidenceType.SHARED_FUNDER, w, TimeBasis.SLOT)], CFG, artifact=False) == 0.48


def test_behavioral_evidence_never_gets_slot_multiplier() -> None:
    assert evidence_weight(EvidenceType.SAME_SLOT, TimeBasis.SLOT, CFG) == 0.15
    assert evidence_weight(EvidenceType.SAME_AMOUNTS, TimeBasis.SLOT, CFG) == 0.2


def test_artifact_halves_confidence() -> None:
    assert confidence([_ev(EvidenceType.SHARED_FUNDER, 0.6)], CFG, artifact=True) == 0.3
    from dataclasses import replace
    cfg2 = replace(CFG, artifact_confidence_multiplier=0.25)
    assert confidence([_ev(EvidenceType.SHARED_FUNDER, 0.6)], cfg2, artifact=True) == 0.15


def test_confidence_is_monotone_under_added_evidence() -> None:
    rng = random.Random(7)
    for _ in range(100):
        weights = [round(rng.uniform(0.05, 0.9), 4) for _ in range(rng.randint(1, 5))]
        base = confidence([_ev(EvidenceType.DIRECT_TRANSFER, w) for w in weights], CFG, artifact=False)
        extra = weights + [round(rng.uniform(0.05, 0.9), 4)]
        grown = confidence([_ev(EvidenceType.DIRECT_TRANSFER, w) for w in extra], CFG, artifact=False)
        assert grown >= base


def test_confidence_strictly_between_0_and_1() -> None:
    c = confidence([_ev(EvidenceType.SHARED_FUNDER, 0.6)], CFG, artifact=False)
    assert 0 < c < 1


def test_weights_come_from_config_not_constants() -> None:
    from dataclasses import replace
    from unmask.clusters.config import EvidenceWeights
    w2 = replace(CFG.evidence_weights, shared_funder=0.9)
    cfg2 = replace(CFG, evidence_weights=w2)
    assert evidence_weight(EvidenceType.SHARED_FUNDER, TimeBasis.BLOCK_TIME, cfg2) == 0.9
    assert evidence_weight(EvidenceType.SHARED_FUNDER, TimeBasis.BLOCK_TIME, CFG) == 0.6


def test_share_is_rounded_ratio_with_integers_kept() -> None:
    assert share_of([1], 3) == (1, 3, 0.3333)
    assert share_of([100, 200], 600) == (300, 600, 0.5)


def test_evidence_from_link_copies_fields_and_sets_weight_value_detail() -> None:
    from unmask.clusters.model import Window as W
    link = Link(type=EvidenceType.SHARED_FUNDER, buyers=frozenset(("w1", "w2")), via=("src",),
                refs=(EdgeRef(signature="sigx", slot=3, instruction_path="0"),),
                window=W(basis=TimeBasis.BLOCK_TIME, start=5, end=9), basis=TimeBasis.BLOCK_TIME)
    ev = evidence_from_link(link, CFG)
    assert ev.wallets == ("w1", "w2") and ev.via == ("src",) and ev.value is None and ev.detail is None
    assert ev.weight == 0.6 and ev.window.start == 5 and ev.basis == TimeBasis.BLOCK_TIME


def _graph_ns(wallets_analyzed: int, warnings=()) -> SimpleNamespace:
    return SimpleNamespace(metadata=SimpleNamespace(wallets_analyzed=wallets_analyzed),
                           report=SimpleNamespace(warnings=tuple(warnings)))


def _draft_with_types(*types: EvidenceType) -> ClusterDraft:
    from unmask.clusters.model import Window as W
    links = []
    for i, t in enumerate(types):
        via = ("s",) if t != EvidenceType.DIRECT_TRANSFER else ()
        links.append(Link(type=t, buyers=frozenset(("w1", "w2")), via=via,
                          refs=(EdgeRef(signature=f"sig{i}", slot=1,
                                        instruction_path=None if t == EvidenceType.DELEGATED_BUY else "0"),),
                          window=W(basis=TimeBasis.BLOCK_TIME, start=1, end=1),
                          basis=TimeBasis.BLOCK_TIME))
    (draft,) = form_clusters(["w1", "w2"], links)
    return draft


def test_artifact_share_three_points_with_giant() -> None:
    from dataclasses import replace
    draft5 = ClusterDraft(wallets=frozenset(f"w{i}" for i in range(5)), links=())
    g10 = _graph_ns(10, (GraphWarning.GIANT_COMPONENT,))
    assert is_artifact(draft5, g10, CFG) is False  # 5/10 — не строго більше
    draft6 = ClusterDraft(wallets=frozenset(f"w{i}" for i in range(6)), links=())
    assert is_artifact(draft6, g10, CFG) is True
    cfg6 = replace(CFG, artifact_buyer_share=0.6)
    assert is_artifact(draft6, g10, cfg6) is False
    draft7 = ClusterDraft(wallets=frozenset(f"w{i}" for i in range(7)), links=())
    assert is_artifact(draft7, g10, cfg6) is True


def test_artifact_requires_giant_or_indirect_majority() -> None:
    g = _graph_ns(10, ())
    mixed_equal = _draft_with_types(EvidenceType.SHARED_FUNDER, EvidenceType.INDIRECT_LINK)
    big = ClusterDraft(wallets=frozenset(f"w{i}" for i in range(6)), links=mixed_equal.links)
    assert is_artifact(big, g, CFG) is False  # 1 непрямий проти 1 — не більшість
    majority = _draft_with_types(EvidenceType.SHARED_FUNDER, EvidenceType.INDIRECT_LINK,
                                 EvidenceType.INDIRECT_LINK)
    big2 = ClusterDraft(wallets=frozenset(f"w{i}" for i in range(6)), links=majority.links)
    assert is_artifact(big2, g, CFG) is True
    giant = _graph_ns(10, (GraphWarning.GIANT_COMPONENT,))
    assert is_artifact(big, giant, CFG) is True


def test_multiplier_from_config() -> None:
    from dataclasses import replace
    cfg2 = replace(CFG, artifact_confidence_multiplier=0.25)
    assert confidence([_ev(EvidenceType.SHARED_FUNDER, 0.6)], cfg2, artifact=True) == 0.15


def test_c_giant_cluster_has_warning_and_confidence_0_402_risk_40() -> None:
    from test_clusters_service import _analyze as _a
    _, _, _, result = _a("c_giant")
    (cluster,) = result.clusters
    assert cluster.warnings == ("possible_pruning_artifact",) or \
        "possible_pruning_artifact" in [w.value for w in cluster.warnings]
    assert cluster.confidence == 0.402
    assert result.risk_score == 40


def test_c_all_one_cluster_has_warning_and_confidence_0_3_risk_30() -> None:
    from test_clusters_service import _analyze as _a
    _, _, _, result = _a("c_all_one")
    (cluster,) = result.clusters
    assert "possible_pruning_artifact" in [w.value for w in cluster.warnings]
    assert cluster.confidence == 0.3
    assert result.risk_score == 30


def test_c_shared_and_c_two_clusters_have_no_artifact_warning_and_full_confidence() -> None:
    from test_clusters_service import _analyze as _a
    for name in ("c_shared", "c_two_clusters"):
        _, _, _, result = _a(name)
        for cluster in result.clusters:
            assert "possible_pruning_artifact" not in [w.value for w in cluster.warnings]
            assert cluster.confidence == 0.6


def test_evidence_weights_still_reproduce_pre_multiplier_confidence() -> None:
    from test_clusters_service import _analyze as _a
    _, _, _, result = _a("c_giant")
    (cluster,) = result.clusters
    prod = 1.0
    for e in cluster.evidence:
        prod *= 1.0 - e.weight
    assert round(1.0 - prod, 4) == 0.804
    assert cluster.confidence == round(0.804 * 0.5, 4)
