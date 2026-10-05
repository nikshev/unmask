# verifies: FR-003-06
"""SC-005: жоден кластер не побудований через відсічену вершину (T-082)."""

from __future__ import annotations

import json
from pathlib import Path
from types import MappingProxyType

import pytest

from conftest import GRAPH_FIXTURES, load_ingest_fixture
from test_clusters_service import _analyze as _analyze_cluster
from unmask.clusters.config import load_cluster_config
from unmask.clusters.serialize import to_dict
from unmask.clusters.service import ClusterService
from unmask.graph.service import GraphService
from unmask.hubs.config import AddressLists, HubConfig, HubThresholds

ROOT = Path(__file__).resolve().parents[1]
CLUSTERS_YAML = ROOT / "config" / "clusters.yaml"
SCHEMA = json.loads((ROOT / "specs" / "003-wallet-clusters-risk" / "contracts"
                     / "cluster-result.schema.json").read_text(encoding="utf-8"))
SCENARIOS = sorted(p.name for p in GRAPH_FIXTURES.iterdir() if (p / "expected.json").is_file())

import jsonschema
from jsonschema import Draft202012Validator


def _expected(name: str) -> dict:
    return json.loads((GRAPH_FIXTURES / name / "expected.json").read_text(encoding="utf-8"))


def _hub_config(expected: dict) -> HubConfig:
    cfg = expected["config"]
    cats = {k: tuple(v) for k, v in cfg["lists"]["categories"].items()}
    lists = AddressLists(version=cfg["lists"]["version"], categories=MappingProxyType(cats),
                         index=MappingProxyType({a: n for n, addrs in cats.items() for a in addrs}))
    return HubConfig(thresholds=HubThresholds(version=cfg["version"], **cfg["thresholds"]),
                     lists=lists, thresholds_digest="fixture", lists_digest="fixture")


def _analyze(name: str):
    expected = _expected(name)
    ingest = load_ingest_fixture(name)
    graph = GraphService(_hub_config(expected)).analyze(ingest)
    result = ClusterService(load_cluster_config(CLUSTERS_YAML)).analyze(graph, ingest)
    return expected, graph, result


@pytest.mark.parametrize("name", SCENARIOS)
def test_no_exception_and_schema_valid_on_every_002_fixture(name: str) -> None:
    _, _, result = _analyze(name)
    Draft202012Validator(SCHEMA).validate(to_dict(result))


@pytest.mark.parametrize("name", SCENARIOS)
def test_no_evidence_names_a_pruned_address_except_recovered_edge(name: str) -> None:
    _, graph, result = _analyze(name)
    pruned = {r.address for r in graph.pruned}
    for cluster in result.clusters:
        for ev in cluster.evidence:
            touched = set(ev.via) | set(ev.wallets)
            if ev.type.value != "recovered_edge":
                assert not (touched & pruned), (name, ev.type.value)
            else:
                assert set(ev.via) <= pruned, (name, "recovered via must be pruned")


def test_g_hub_H_and_g_dust_D_do_not_connect_buyers() -> None:
    for name, hub_label in (("g_hub", "H"), ("g_dust", "D")):
        expected, graph, result = _analyze(name)
        hub_addr = expected["wallets"][hub_label]
        for cluster in result.clusters:
            for ev in cluster.evidence:
                assert hub_addr not in ev.via and hub_addr not in ev.wallets


def test_g_financier_inherits_giant_component_and_any_cluster_over_half_is_flagged_artifact() -> None:
    _, graph, result = _analyze("g_financier")
    assert "giant_component" in [w.value for w in graph.report.warnings]
    n = graph.metadata.wallets_analyzed
    for cluster in result.clusters:
        if len(cluster.members) / n > 0.5:
            assert "possible_pruning_artifact" in [w.value for w in cluster.warnings]


def test_g_empty_gives_insufficient_data_empty_input() -> None:
    _, _, result = _analyze("g_empty")
    assert result.band.value == "insufficient_data"
    assert "empty_input" in result.band_reasons
    assert result.clusters == () and result.risk_score == 0


def test_g_incomplete_is_never_clean() -> None:
    _, _, result = _analyze("g_incomplete")
    assert result.band.value != "clean"


def test_g_buyer_hub_flagged_buyer_X_is_excluded_from_links_and_diagnosed() -> None:
    expected, graph, result = _analyze("g_buyer_hub")
    flagged = {f.address for f in graph.buyer_flags}
    assert flagged
    for cluster in result.clusters:
        for ev in cluster.evidence:
            assert not (set(ev.wallets) & flagged)
    kinds = [d.kind.value for d in result.diagnostics]
    assert "flagged_buyers_excluded" in kinds


def test_g_delegated_pair_gives_delegated_buy_evidence_if_both_buyers() -> None:
    # У g_delegated жодна делегована пара не має двох покупців (Q→V, A→R, P3→W) → доказу немає;
    # позитивний випадок (обидва покупці) покрито c_delegated.
    _, _, result = _analyze("g_delegated")
    types = [e.type.value for c in result.clusters for e in c.evidence]
    assert "delegated_buy" not in types
    _, _, _, flagged_result = _analyze_cluster("c_delegated")
    flagged_types = [e.type.value for c in flagged_result.clusters for e in c.evidence]
    assert "delegated_buy" in flagged_types
