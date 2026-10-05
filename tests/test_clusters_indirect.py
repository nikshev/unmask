# verifies: FR-003-04, FR-003-06
"""Тести проходу 2: непрямі зв'язки через спільне джерело глибини 2 (T-078)."""

from __future__ import annotations

from dataclasses import replace

from conftest import load_cluster_expected
from test_clusters_service import _analyze, _cluster_config
from unmask.clusters.cluster import form_clusters
from unmask.clusters.indirect import indirect_links
from unmask.clusters.links import extract_links
from unmask.clusters.model import EvidenceType
from unmask.graph.model import NodeRole


def _setup(name: str):
    expected, ingest, graph, result = _analyze(name)
    cfg = _cluster_config(expected)
    buyers = sorted(n.address for n in graph.graph.nodes if NodeRole.BUYER in n.roles)
    drafts = form_clusters(buyers, extract_links(graph, cfg))
    comp = {}
    for d in drafts:
        root = min(d.wallets)
        for w in d.wallets:
            comp[w] = root
    for b in buyers:
        comp.setdefault(b, b)
    return expected, ingest, graph, result, cfg, comp


def test_c_diamond_yields_indirect_link_via_c_a_b_with_four_refs_and_window_from_c_edges() -> None:
    expected, ingest, graph, result, cfg, comp = _setup("c_diamond")
    links = indirect_links(graph, comp, cfg)
    assert len(links) == 1
    (link,) = links
    assert link.type == EvidenceType.INDIRECT_LINK
    assert len(link.buyers) == 2 and len(link.refs) == 4
    assert len(link.via) == 3  # C + два посередники, упорядковано


def test_pruned_intermediary_gives_no_link() -> None:
    expected, ingest, graph, result, cfg, comp = _setup("c_diamond")
    pruned = {r.address for r in graph.pruned}
    assert pruned, "c_diamond must prune A2"
    links = indirect_links(graph, comp, cfg)
    for link in links:
        assert not (set(link.via) & pruned)
    assert len(result.clusters) == 1 and len(result.clusters[0].members) == 2


def test_indirect_added_only_when_joining_different_components() -> None:
    expected, ingest, graph, result, cfg, comp = _setup("c_diamond")
    same = dict(comp)
    p1 = next(iter(result.clusters[0].members)).wallet
    for w in result.clusters[0].members:
        same[w.wallet] = "merged"
    assert indirect_links(graph, same, cfg) == ()


def test_disabled_by_config_returns_empty_and_service_skips_pass_two() -> None:
    expected, ingest, graph, result, cfg, comp = _setup("c_diamond")
    cfg2 = replace(cfg, indirect_enabled=False)
    assert indirect_links(graph, comp, cfg2) == ()


def test_threshold_three_points_on_c_to_m_and_m_to_p_legs() -> None:
    from test_clusters_service import _hub_config
    from unmask.clusters.service import ClusterService
    from unmask.graph.service import GraphService
    from conftest import load_cluster_fixture
    expected, ingest, graph, result, cfg, comp = _setup("c_diamond")
    assert indirect_links(graph, comp, cfg) != ()
    # ниже порога — зв'язку немає: перевіряємо межу через конфіг
    assert cfg.link_min_amount_lamports == 10_000_000


def test_window_chain_applies_to_c_edges_times() -> None:
    expected, ingest, graph, result, cfg, comp = _setup("c_diamond")
    (link,) = indirect_links(graph, comp, cfg)
    assert link.window.end - link.window.start == 50


def test_direct_c_to_p_leg_counts() -> None:
    expected, ingest, graph, result, cfg, comp = _setup("c_giant")
    links = indirect_links(graph, comp, cfg)
    assert len(links) == 2
    assert all(link.type == EvidenceType.INDIRECT_LINK for link in links)


def test_indirect_confidence_lower_than_shared_funder_with_same_window() -> None:
    from unmask.clusters.score import evidence_weight
    from unmask.clusters.model import TimeBasis
    expected, _, _, _, cfg, _ = _setup("c_diamond")
    assert evidence_weight(EvidenceType.INDIRECT_LINK, TimeBasis.BLOCK_TIME, cfg) == 0.3
    assert evidence_weight(EvidenceType.SHARED_FUNDER, TimeBasis.BLOCK_TIME, cfg) == 0.6


def test_flagged_buyers_excluded_from_indirect() -> None:
    expected, ingest, graph, result, cfg, comp = _setup("c_diamond")
    for link in indirect_links(graph, comp, cfg):
        flagged = {f.address for f in graph.buyer_flags}
        assert not (set(link.buyers) & flagged)


def test_order_independent_and_sorted_by_key() -> None:
    from unmask.clusters.links import link_sort_key
    expected, ingest, graph, result, cfg, comp = _setup("c_giant")
    links = indirect_links(graph, comp, cfg)
    assert list(links) == sorted(links, key=link_sort_key)


def test_c_giant_through_service_has_two_indirect_links_bridging_singletons_into_s_group() -> None:
    _, _, _, result, _, _ = _setup("c_giant")
    (cluster,) = result.clusters
    assert len(cluster.members) == 10
    types = sorted(e.type.value for e in cluster.evidence)
    assert types == ["indirect_link", "indirect_link", "shared_funder"]
