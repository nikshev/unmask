# verifies: FR-003-01
"""Швидкодія кластеризації SC-006: 300 покупців / ~15 000 ребер < 1 с (T-086).

Годинникові тести — лише під маркером `perf` (виключені з типового прогону).
"""

from __future__ import annotations

import gc
import hashlib
import statistics
import time
from functools import lru_cache
from pathlib import Path

import pytest
from solders.keypair import Keypair
from solders.signature import Signature

from unmask.clusters.config import load_cluster_config
from unmask.clusters.serialize import to_dict, to_json
from unmask.clusters.service import ClusterService
from unmask.graph.service import GraphService
from unmask.hubs.config import load_hub_config
from unmask.ingest.serialize import from_dict

ROOT = Path(__file__).resolve().parents[1]
SHIPPED_CLUSTERS = ROOT / "config" / "clusters.yaml"
SHIPPED_HUBS = ROOT / "config" / "hubs.yaml"
SHIPPED_LISTS = ROOT / "config" / "hub_addresses.yaml"

SC_006_BUDGET_SECONDS = 1.0  # SC-006; поріг не послаблюється (R-17)
RUNS = 3
LINEAR_RATIO_LIMIT = 3.0

SOL = 10**9
BASE_TIME = 1_759_400_000
SEED = 20261005


def _address(label: str) -> str:
    seed = hashlib.sha256(f"unmask-clusters-perf/wallet/{label}".encode()).digest()
    return str(Keypair.from_seed(seed).pubkey())


def _signature(index: int) -> str:
    return str(Signature.from_bytes(hashlib.sha512(f"unmask-clusters-perf/tx/{index}".encode()).digest()))


@lru_cache(maxsize=None)
def synthetic(n_buyers: int) -> tuple:
    """`(IngestResult, GraphResult)` на `n_buyers` покупців. Кешується між тестами; таймер — лише стадія 003."""
    assert n_buyers % 10 == 0
    n_groups = n_buyers // 10
    buyers = [_address(f"B{i}") for i in range(n_buyers)]
    counter = [0]

    def transfer(sender: str, receiver: str, slot: int, amount: int, depth: int) -> dict:
        counter[0] += 1
        return {
            "signature": _signature(counter[0]), "slot": slot, "block_time": BASE_TIME + slot,
            "instruction_path": "0", "sender": sender, "receiver": receiver,
            "asset": "sol", "amount": amount, "decimals": None, "depth": depth,
        }

    transfers = []
    amount = [50_000_000]

    def next_amount(base: int) -> int:
        amount[0] += 1
        return base + amount[0]

    # Групи S: по 10 покупців у вікні; остання розбита 5+5 (дві групи) → n_groups + 1 груп проходу 1.
    for g in range(n_groups):
        funder = _address(f"S{g}")
        members = buyers[10 * g:10 * g + 10]
        if g == n_groups - 1:
            for j, buyer in enumerate(members[:5]):
                transfers.append(transfer(funder, buyer, 100 + j, next_amount(50_000_000), 1))
            for j, buyer in enumerate(members[5:]):
                transfers.append(transfer(funder, buyer, 5000 + j, next_amount(50_000_000), 1))
        else:
            for j, buyer in enumerate(members):
                transfers.append(transfer(funder, buyer, 100 + 20 * g + j, next_amount(50_000_000), 1))

    # Приватні фінансисти: 46 на покупця, усі суми різні.
    privates: dict[str, list[str]] = {}
    slot = 30_000
    for i, buyer in enumerate(buyers):
        funds = []
        for k in range(46):
            funder = _address(f"F{i}_{k}")
            funds.append(funder)
            slot += 1
            transfers.append(transfer(funder, buyer, slot, next_amount(10_000_000), 1))
        privates[buyer] = funds

    # Вершини C глибини 2: усі крім однієї — усередині однієї групи (зв'язку немає);
    # остання з'єднує дві групи (один непрямий зв'язок → одне злиття).
    n_c = n_buyers // 15
    for c in range(n_c - 1):
        g = c % n_groups
        for j in range(10):
            buyer = buyers[10 * g + j]
            transfers.append(transfer(_address(f"C{c}"), privates[buyer][j % 46], 60_000 + 20 * c + j,
                                      next_amount(20_000_000), 2))
    span = _address(f"C{n_c - 1}")
    for j in range(5):
        transfers.append(transfer(span, privates[buyers[j]][j], 70_000 + j, next_amount(20_000_000), 2))
    for j in range(5):
        transfers.append(transfer(span, privates[buyers[10 + j]][j], 70_010 + j, next_amount(20_000_000), 2))

    buyer_rows = []
    for i, buyer in enumerate(buyers):
        buyer_rows.append({
            "wallet": buyer, "rank": 0, "first_buy_signature": _signature(10_000_000 + i),
            "first_buy_slot": 100_000 + 3 * i, "first_buy_time": BASE_TIME + 100_000 + 3 * i,
            "received_amount": 1_000_000_000 + i, "spent": [{"asset": "sol", "amount": 600_000_000 + i}],
            "programs": [_address("DEX")], "address_type": "wallet",
        })
    buyer_rows.sort(key=lambda r: (r["first_buy_slot"], r["first_buy_signature"], r["wallet"]))
    for rank, row in enumerate(buyer_rows, start=1):
        row["rank"] = rank
    transfers.sort(key=lambda r: (r["slot"], r["signature"], r["instruction_path"]))

    ingest = from_dict({
        "metadata": {
            "mint": _address("MINT"), "analyzed_at": BASE_TIME + 200_000, "wallets_analyzed": n_buyers,
            "config_version": 2, "first_buyers_n": n_buyers, "funding_depth": 2,
            "counterparty_threshold": 200, "max_signatures_per_wallet": 300,
            "collect_spl_inbound": False, "time_budget_seconds": 40.0, "elapsed_seconds": 1.0,
            "rpc_calls": len(transfers), "transactions_scanned": len(transfers),
            "source": "http", "resumed": False, "served_from_cache": False,
        },
        "completeness": {"status": "complete", "missing": [],
                         "buyers": {"complete": True, "reason": None, "detail": ""}},
        "buyers": buyer_rows,
        "transfers": transfers,
        "unexpanded": [],
        "delegated": {"links": [], "unpaired": [], "complete": True, "reason": None, "detail": ""},
    })
    hub = load_hub_config(SHIPPED_HUBS, SHIPPED_LISTS)
    graph = GraphService(hub).analyze(ingest)
    assert graph.pruned == (), "synthetic graph must keep every vertex"
    return ingest, graph


def _services():
    return ClusterService(load_cluster_config(SHIPPED_CLUSTERS))


def _seconds(n_buyers: int) -> float:
    ingest, graph = synthetic(n_buyers)
    service = _services()
    gc.collect()
    best = None
    for _ in range(RUNS):
        start = time.perf_counter()
        result = service.analyze(graph, ingest)
        to_json(result)
        elapsed = time.perf_counter() - start
        best = elapsed if best is None else min(best, elapsed)
    return best


@pytest.mark.perf
def test_300_buyers_15k_edges_analyze_and_to_json_under_one_second() -> None:
    _, graph = synthetic(300)
    service = _services()
    ingest, _ = synthetic(300)
    times = []
    for _ in range(RUNS):
        gc.collect()
        start = time.perf_counter()
        result = service.analyze(graph, ingest)
        to_json(result)
        times.append(time.perf_counter() - start)
    assert statistics.median(times) < SC_006_BUDGET_SECONDS


@pytest.mark.perf
def test_runtime_grows_roughly_linearly_with_edges() -> None:
    assert _seconds(600) < LINEAR_RATIO_LIMIT * _seconds(300)


def test_synthetic_graph_has_about_15000_edges_and_300_buyers() -> None:
    _, graph = synthetic(300)
    assert graph.metadata.wallets_analyzed == 300
    assert 14_000 <= len(graph.graph.edges) <= 16_000


def test_synthetic_clusters_found_and_no_false_artifact() -> None:
    _, graph = synthetic(300)
    result = _services().analyze(graph, synthetic(300)[0])
    assert len(result.clusters) >= 30
    for cluster in result.clusters:
        assert any(e.type.value == "shared_funder" for e in cluster.evidence)
        assert "possible_pruning_artifact" not in [w.value for w in cluster.warnings]
    assert any(e.type.value == "indirect_link" for c in result.clusters for e in c.evidence)


def test_synthetic_result_validates_against_schema() -> None:
    import json as _json
    import jsonschema
    from jsonschema import Draft202012Validator
    schema = _json.loads((ROOT / "specs" / "003-wallet-clusters-risk" / "contracts"
                          / "cluster-result.schema.json").read_text(encoding="utf-8"))
    _, graph = synthetic(300)
    Draft202012Validator(schema).validate(to_dict(_services().analyze(graph, synthetic(300)[0])))
