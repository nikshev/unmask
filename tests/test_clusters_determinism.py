# verifies: FR-003-12, FR-003-20
"""Детермінізм: перестановки входу, повторні прогони, PYTHONHASHSEED (T-084)."""

from __future__ import annotations

import dataclasses
import json
import random
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import CLUSTER_FIXTURES, load_cluster_expected
from test_clusters_service import _analyze, _cluster_config, _hub_config
from unmask.clusters.model import cluster_sort_key, diagnostic_sort_key, evidence_sort_key
from unmask.clusters.serialize import to_dict, to_json
from unmask.graph.service import GraphService
from unmask.clusters.service import ClusterService
from conftest import load_cluster_fixture

SCENARIOS = sorted(p.name for p in CLUSTER_FIXTURES.iterdir() if (p / "expected.json").is_file())
SEEDS = [101, 202, 303]


def _shuffled_ingest(ingest, seed: int):
    rng = random.Random(seed)
    buyers = list(ingest.buyers)
    transfers = list(ingest.transfers)
    unexpanded = list(ingest.unexpanded)
    rng.shuffle(buyers)
    rng.shuffle(transfers)
    rng.shuffle(unexpanded)
    links = list(ingest.delegated.links)
    unpaired = list(ingest.delegated.unpaired)
    rng.shuffle(links)
    rng.shuffle(unpaired)
    from unmask.ingest.model import DelegatedAnalysis
    delegated = DelegatedAnalysis.derive(links, unpaired, ingest.completeness.buyers)
    return dataclasses.replace(ingest, buyers=tuple(buyers), transfers=tuple(transfers),
                               unexpanded=tuple(unexpanded), delegated=delegated)


def _analyze_ingest(name: str, ingest):
    expected = load_cluster_expected(name)
    graph = GraphService(_hub_config(expected)).analyze(ingest)
    return ClusterService(_cluster_config(expected)).analyze(graph, ingest)


@pytest.mark.parametrize("name", SCENARIOS)
@pytest.mark.parametrize("seed", SEEDS)
def test_shuffled_input_tuples_give_identical_json(name: str, seed: int) -> None:
    expected, _, _, result = _analyze(name)
    ingest = load_cluster_fixture(name)
    assert to_json(_analyze_ingest(name, _shuffled_ingest(ingest, seed))) == to_json(result)


@pytest.mark.parametrize("name", SCENARIOS)
def test_reversed_input_gives_identical_json(name: str) -> None:
    expected, _, _, result = _analyze(name)
    ingest = load_cluster_fixture(name)
    from unmask.ingest.model import DelegatedAnalysis
    rev = dataclasses.replace(
        ingest, buyers=tuple(reversed(ingest.buyers)), transfers=tuple(reversed(ingest.transfers)),
        unexpanded=tuple(reversed(ingest.unexpanded)),
        delegated=DelegatedAnalysis.derive(list(reversed(ingest.delegated.links)),
                                           list(reversed(ingest.delegated.unpaired)),
                                           ingest.completeness.buyers))
    assert to_json(_analyze_ingest(name, rev)) == to_json(result)


@pytest.mark.parametrize("name", SCENARIOS)
def test_analyze_twice_gives_identical_bytes_and_equal_results(name: str) -> None:
    expected = load_cluster_expected(name)
    ingest = load_cluster_fixture(name)
    first = _analyze_ingest(name, ingest)
    second = _analyze_ingest(name, ingest)
    assert to_json(first) == to_json(second)
    assert first == second


def test_json_independent_of_pythonhashseed() -> None:
    lines = [
        "from conftest import load_cluster_fixture, load_cluster_expected",
        "from test_clusters_service import _hub_config, _cluster_config",
        "from unmask.graph.service import GraphService",
        "from unmask.clusters.service import ClusterService",
        "from unmask.clusters.serialize import to_json",
        "import json",
        "out = {}",
        "for name in ('c_two_clusters', 'c_behavior'):",
        "    expected = load_cluster_expected(name)",
        "    ingest = load_cluster_fixture(name)",
        "    graph = GraphService(_hub_config(expected)).analyze(ingest)",
        "    out[name] = to_json(ClusterService(_cluster_config(expected)).analyze(graph, ingest))",
        "print(json.dumps(out, sort_keys=True))",
    ]
    script = "\n".join(lines)
    root = Path(__file__).resolve().parents[1]
    import os
    outputs = set()
    for seed in ("0", "1", "12345"):
        env = dict(os.environ)
        env["PYTHONHASHSEED"] = seed
        env["PYTHONPATH"] = str(root / "src") + ":" + str(root / "tests")
        proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                              cwd=str(root), env=env)
        assert proc.returncode == 0, proc.stderr[-2000:]
        outputs.add(proc.stdout)
    assert len(outputs) == 1


def test_cluster_order_is_share_desc_then_id() -> None:
    from test_clusters_service import _analyze as _a
    for name in SCENARIOS:
        _, _, _, result = _a(name)
        assert list(result.clusters) == sorted(result.clusters, key=cluster_sort_key)


def test_evidence_members_diagnostics_follow_documented_keys() -> None:
    from test_clusters_service import _analyze as _a
    for name in SCENARIOS:
        _, _, _, result = _a(name)
        for cluster in result.clusters:
            assert list(cluster.evidence) == sorted(cluster.evidence, key=evidence_sort_key)
            ranks = [m.buyer_rank for m in cluster.members]
            assert ranks == sorted(ranks)
        assert list(result.diagnostics) == sorted(result.diagnostics, key=diagnostic_sort_key)


def test_no_set_or_dict_order_leaks_into_result_tuples() -> None:
    from test_clusters_service import _analyze as _a
    for name in SCENARIOS:
        _, _, _, result = _a(name)
        for cluster in result.clusters:
            wallets = [m.wallet for m in cluster.members]
            assert len(set(wallets)) == len(wallets)
            for ev in cluster.evidence:
                assert list(ev.wallets) == sorted(ev.wallets)
                assert list(ev.via) == sorted(ev.via)


def test_real_fixtures_identical_across_two_runs() -> None:
    from unmask.clusters.evaluate import evaluate
    root = Path(__file__).resolve().parents[1]
    real = root / "tests" / "fixtures" / "real"
    kwargs = dict(clusters_config=root / "config" / "clusters.yaml",
                  hubs_config=root / "config" / "hubs.yaml",
                  lists_config=root / "config" / "hub_addresses.yaml")
    assert evaluate(real, **kwargs) == evaluate(real, **kwargs)
