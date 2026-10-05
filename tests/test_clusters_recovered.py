# verifies: FR-003-06, FR-003-02
"""Тести відновлених ребер відсіченого змішаного відправника і слотового базису (T-081)."""

from __future__ import annotations

from conftest import load_cluster_expected
from test_clusters_service import _analyze, _cluster_config
from unmask.clusters.links import extract_links
from unmask.clusters.model import EvidenceType, TimeBasis
from unmask.graph.service import GraphService
from test_clusters_service import _hub_config
from conftest import load_cluster_fixture


def _graph(name: str):
    expected = load_cluster_expected(name)
    ingest = load_cluster_fixture(name)
    return expected, ingest, GraphService(_hub_config(expected)).analyze(ingest)


def test_c_recovered_yields_recovered_edge_link_via_x_with_three_refs() -> None:
    expected, _, graph = _graph("c_recovered")
    links = extract_links(graph, _cluster_config(expected))
    assert len(links) == 1
    (link,) = links
    assert link.type == EvidenceType.RECOVERED_EDGE
    assert len(link.buyers) == 3 and len(link.refs) == 3
    assert len(link.via) == 1


def test_dust_edges_of_pruned_sender_are_not_links_three_points() -> None:
    expected, _, graph = _graph("c_recovered")
    links = extract_links(graph, _cluster_config(expected))
    wallets = {w for link in links for w in link.buyers}
    assert len(wallets) == 3  # пилові B1..B4, B8..B10 — поза зв'язком


def test_pruned_node_with_only_dust_edges_gives_nothing() -> None:
    expected, _, graph = _graph("c_recovered")
    pruned = {r.address for r in graph.pruned}
    assert len(pruned) == 2  # X і W
    links = extract_links(graph, _cluster_config(expected))
    for link in links:
        assert link.type == EvidenceType.RECOVERED_EDGE


def test_recovered_never_changes_pruning() -> None:
    expected, _, graph = _graph("c_recovered")
    before = sorted(r.address for r in graph.pruned)
    extract_links(graph, _cluster_config(expected))
    assert sorted(r.address for r in graph.pruned) == before


def test_g_dust_mixed_fixture_002_yields_recovered_edge_for_x_real_receivers_only() -> None:
    import json
    from pathlib import Path
    from conftest import GRAPH_FIXTURES, load_ingest_fixture
    from unmask.hubs.config import AddressLists, HubConfig, HubThresholds
    from types import MappingProxyType
    expected = json.loads((GRAPH_FIXTURES / "g_dust_mixed" / "expected.json").read_text(encoding="utf-8"))
    cfg = expected["config"]
    lists = AddressLists(version=cfg["lists"]["version"],
                         categories=MappingProxyType({k: tuple(v) for k, v in cfg["lists"]["categories"].items()}),
                         index=MappingProxyType({a: n for n, addrs in cfg["lists"]["categories"].items() for a in addrs}))
    hub = HubConfig(thresholds=HubThresholds(version=cfg["version"], **cfg["thresholds"]), lists=lists,
                    thresholds_digest="x", lists_digest="x")
    ingest = load_ingest_fixture("g_dust_mixed")
    graph = GraphService(hub).analyze(ingest)
    from unmask.clusters.config import load_cluster_config
    ccfg = load_cluster_config(Path(__file__).resolve().parents[1] / "config" / "clusters.yaml")
    links = extract_links(graph, ccfg)
    recovered = [l for l in links if l.type == EvidenceType.RECOVERED_EDGE]
    assert len(recovered) == 1 and len(recovered[0].buyers) == 3


def test_recovered_edges_to_flagged_buyer_excluded() -> None:
    _, _, _, result = _analyze("c_recovered")
    assert result.clusters[0].evidence[0].type.value == "recovered_edge"


def test_recovered_weight_is_0_4_and_type_distinct_from_shared_funder() -> None:
    _, _, _, result = _analyze("c_recovered")
    (ev,) = result.clusters[0].evidence
    assert ev.weight == 0.4
    assert ev.type == EvidenceType.RECOVERED_EDGE != EvidenceType.SHARED_FUNDER


def test_c_no_time_end_to_end_slot_basis_weight_0_48_and_fallback_warning() -> None:
    _, _, _, result = _analyze("c_no_time")
    (cluster,) = result.clusters
    (ev,) = cluster.evidence
    assert ev.basis.value == "slot" and ev.weight == 0.48
    assert "slot_time_fallback" in [w.value for w in cluster.warnings]
    assert result.risk_score == 24


def test_c_recovered_and_c_no_time_match_expected_through_service() -> None:
    from unmask.clusters.serialize import to_dict
    for name in ("c_recovered", "c_no_time"):
        expected, _, _, result = _analyze(name)
        assert to_dict(result) == expected["result"]
