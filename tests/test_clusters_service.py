# verifies: FR-003-19, FR-003-21, FR-003-07, FR-003-16
"""Наскрізний тест сервісу: усі закомічені сценарії `c_*` збігаються з `expected.json` (T-071)."""

from __future__ import annotations

import copy
from pathlib import Path
from types import MappingProxyType

import pytest

from conftest import CLUSTER_FIXTURES, load_cluster_expected, load_cluster_fixture
from unmask.clusters.config import ClusterConfig, EvidenceWeights
from unmask.clusters.model import ClusterInputError
from unmask.clusters.serialize import to_dict, to_json
from unmask.clusters.service import ClusterService
from unmask.graph.service import GraphService
from unmask.hubs.config import AddressLists, HubConfig, HubThresholds
from unmask.ingest.model import Rejection

SCENARIOS = sorted(p.name for p in CLUSTER_FIXTURES.iterdir() if (p / "expected.json").is_file())


def _hub_config(expected: dict) -> HubConfig:
    hub = expected["hub_config"]
    lists = hub["lists"]
    parsed = {name: tuple(lists["categories"][name]) for name in lists["categories"]}
    index = {a: n for n, addrs in parsed.items() for a in addrs}
    address_lists = AddressLists(version=lists["version"], categories=MappingProxyType(parsed),
                                 index=MappingProxyType(index))
    return HubConfig(
        thresholds=HubThresholds(version=hub["version"], **hub["thresholds"]),
        lists=address_lists,
        thresholds_digest="fixture",
        lists_digest="fixture",
    )


def _cluster_config(expected: dict) -> ClusterConfig:
    cfg = dict(expected["config"])
    cfg["link_assets"] = tuple(cfg["link_assets"])
    cfg["evidence_weights"] = EvidenceWeights(**cfg["evidence_weights"])
    return ClusterConfig(**cfg, digest="fixture")


def _analyze(name: str):
    expected = load_cluster_expected(name)
    ingest = load_cluster_fixture(name)
    graph = GraphService(_hub_config(expected)).analyze(ingest)
    result = ClusterService(_cluster_config(expected)).analyze(graph, ingest)
    return expected, ingest, graph, result


@pytest.mark.parametrize("name", SCENARIOS)
def test_every_committed_scenario_matches_expected_json(name: str) -> None:
    expected, _, _, result = _analyze(name)
    assert to_dict(result) == expected["result"]


def test_inputs_are_not_mutated() -> None:
    expected = load_cluster_expected("c_two_clusters")
    ingest = load_cluster_fixture("c_two_clusters")
    graph = GraphService(_hub_config(expected)).analyze(ingest)
    before_ingest, before_graph = copy.deepcopy(ingest), copy.deepcopy(graph)
    ClusterService(_cluster_config(expected)).analyze(graph, ingest)
    assert ingest == before_ingest and graph == before_graph


def test_rejection_inputs_raise_type_error() -> None:
    from unmask.ingest.model import RejectKind
    expected = load_cluster_expected("c_single")
    ingest = load_cluster_fixture("c_single")
    graph = GraphService(_hub_config(expected)).analyze(ingest)
    service = ClusterService(_cluster_config(expected))
    rej = Rejection(kind=RejectKind.INVALID_ADDRESS, mint="x", detail="y")
    with pytest.raises(TypeError):
        service.analyze(graph, rej)
    with pytest.raises(TypeError):
        service.analyze(rej, ingest)


def test_mismatched_mint_raises_cluster_input_error() -> None:
    import dataclasses
    expected = load_cluster_expected("c_single")
    ingest = load_cluster_fixture("c_single")
    graph = GraphService(_hub_config(expected)).analyze(ingest)
    other = dataclasses.replace(ingest.metadata, mint="OtherMint11111111111111111111111111111")
    bad = dataclasses.replace(ingest, metadata=other)
    with pytest.raises(ClusterInputError):
        ClusterService(_cluster_config(expected)).analyze(graph, bad)
