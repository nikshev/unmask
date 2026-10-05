# verifies: FR-003-08, FR-003-09, FR-003-10
"""Тести поведінкових доказів і діагностики токена (T-076)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from conftest import load_cluster_expected
from test_clusters_service import _analyze, _cluster_config
from unmask.clusters.behavior import behavioral_evidence, diagnostics
from unmask.clusters.cluster import form_clusters
from unmask.clusters.links import extract_links
from unmask.clusters.model import EvidenceType
from unmask.graph.model import NodeRole


def _setup(name: str = "c_behavior"):
    expected, ingest, graph, result = _analyze(name)
    cfg = _cluster_config(expected)
    buyers = sorted(n.address for n in graph.graph.nodes if NodeRole.BUYER in n.roles)
    drafts = form_clusters(buyers, extract_links(graph, cfg))
    return expected, ingest, graph, result, cfg, drafts


def test_same_amounts_funding_three_points() -> None:
    expected, ingest, graph, result, cfg, (draft,) = _setup()
    assert len(draft.wallets) == 7
    assert any(e.type == EvidenceType.SAME_AMOUNTS and e.detail == "funding"
               and e.value == 2_000_000_000 for e in behavioral_evidence(draft, graph, ingest, cfg))
    cfg4 = replace(cfg, same_amount_natural_max=4)
    assert not [e for e in behavioral_evidence(draft, graph, ingest, cfg4)
                if e.type == EvidenceType.SAME_AMOUNTS and e.detail == "funding"]
    cfg3 = replace(cfg, same_amount_natural_max=3)
    assert any(e.type == EvidenceType.SAME_AMOUNTS and e.detail == "funding"
               for e in behavioral_evidence(draft, graph, ingest, cfg3))


def test_same_amounts_first_buy_spent_three_points() -> None:
    expected, ingest, graph, result, cfg, (draft,) = _setup()
    assert any(e.type == EvidenceType.SAME_AMOUNTS and e.detail == "first_buy_spent"
               and e.value == 1_500_000_000 for e in behavioral_evidence(draft, graph, ingest, cfg))
    cfg5 = replace(cfg, same_amount_natural_max=5)
    assert not [e for e in behavioral_evidence(draft, graph, ingest, cfg5)
                if e.type == EvidenceType.SAME_AMOUNTS and e.detail == "first_buy_spent"]


def test_same_slot_three_points_and_window_slots_param() -> None:
    expected, ingest, graph, result, cfg, (draft,) = _setup()
    assert any(e.type == EvidenceType.SAME_SLOT and e.value == 300
               for e in behavioral_evidence(draft, graph, ingest, cfg))
    cfg6 = replace(cfg, same_slot_natural_max=6)
    assert any(e.type == EvidenceType.SAME_SLOT
               for e in behavioral_evidence(draft, graph, ingest, cfg6))
    cfg7 = replace(cfg, same_slot_natural_max=7)
    assert not [e for e in behavioral_evidence(draft, graph, ingest, cfg7)
                if e.type == EvidenceType.SAME_SLOT]


def test_exact_equality_no_tolerance() -> None:
    # 1_500_000_000 проти 1_500_000_001 — різні суми, доказ spent тримається на 5 повторах рівно
    expected, ingest, graph, result, cfg, (draft,) = _setup()
    evidences = behavioral_evidence(draft, graph, ingest, cfg)
    spent = [e for e in evidences if e.type == EvidenceType.SAME_AMOUNTS and e.detail == "first_buy_spent"]
    assert len(spent) == 1 and len(spent[0].wallets) == 5


def test_funding_amounts_count_only_link_edges_at_or_above_threshold_and_not_flagged() -> None:
    expected, ingest, graph, result, cfg, (draft,) = _setup()
    funding = [e for e in behavioral_evidence(draft, graph, ingest, cfg)
               if e.type == EvidenceType.SAME_AMOUNTS and e.detail == "funding"]
    assert len(funding) == 1
    assert len(funding[0].wallets) == 4  # 4 × 2.0 SOL; поодинокі суми — не доказ


def test_behavioral_evidence_never_forms_cluster() -> None:
    expected, ingest, graph, result, cfg, drafts = _setup()
    assert len(drafts) == 1 and len(drafts[0].wallets) == 7
    kinds = [d.kind.value for d in diagnostics(drafts, graph, ingest, cfg)]
    assert "same_amounts_unlinked" in kinds and "same_slot_unlinked" in kinds


def test_unlinked_diagnostics_require_two_components() -> None:
    expected, ingest, graph, result, cfg, (draft,) = _setup()
    diags = diagnostics((draft,), graph, ingest, cfg)
    spent_unlinked = [d for d in diags if d.kind.value == "same_amounts_unlinked"]
    # 1.5 SOL — усередині одного кластера → доказ, не діагностика
    assert all(d.value != 1_500_000_000 for d in spent_unlinked)
    assert any(d.value == 700_000_000 and d.count == 4 for d in spent_unlinked)


def test_confidence_higher_with_behavioral_evidence_than_without() -> None:
    _, _, _, result, _, _ = _setup()
    assert result.clusters[0].confidence == 0.7824 > 0.6


def test_evidence_refs_are_first_buy_signatures_with_null_path_and_slot_window() -> None:
    expected, ingest, graph, result, cfg, (draft,) = _setup()
    for e in behavioral_evidence(draft, graph, ingest, cfg):
        assert e.basis.value == "slot"
        if e.type == EvidenceType.SAME_SLOT:
            assert e.window.start == e.value and e.window.end == e.value + cfg.same_slot_window_slots
            assert all(r.instruction_path is None for r in e.refs)


def test_natural_max_from_config_not_constants() -> None:
    expected, ingest, graph, result, cfg, (draft,) = _setup()
    cfg4 = replace(cfg, same_amount_natural_max=4)
    remaining = behavioral_evidence(draft, graph, ingest, cfg4)
    # funding (4 повтори) зникає при max=4; spent (5) і slot (7) лишаються
    assert len(remaining) == 2
    assert not any(e.detail == "funding" for e in remaining)


def test_c_behavior_matches_expected_through_service() -> None:
    expected, _, _, result, _, _ = _setup()
    from unmask.clusters.serialize import to_dict
    assert to_dict(result) == expected["result"]
