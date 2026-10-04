# verifies: FR-002-01, FR-002-02, FR-002-18
"""Побудова ребер графа фінансування (T-028, research R-5).

Критична задача: хибна агрегація ребер (сума, кількість, межі за часом, refs)
тихо спотворює виміри, критерії хабів і докази кластерів, не ламаючи збірку.
Тому окрім названих у задачі тестів:

- golden-звірка ребер з `expected.json` **усіх** фікстур `tests/fixtures/graph/*`
  (еталон — незалежна агрегація словником у генераторі, R-18);
- 1:1 між refs ребер і переказами входу на кожній фікстурі (SC-006 001);
- межові випадки: `block_time` відсутній лише на одному кінці, розбіжні
  `decimals`, дубль `(signature, instruction_path)` у різних ребрах, порядок
  входу, не узгоджений зі слотами.

Вершини (ролі, глибина, тип адреси, `unexpanded`, виміри) — обсяг T-029/T-030;
`build_graph` мусить їх мати, бо `Node` без них не існує, тому їхня мінімальна
побудова тут перевірена однією golden-звіркою проти тих самих еталонів.
"""

import dataclasses
import json
from collections import Counter
from pathlib import Path

import pytest

from conftest import GRAPH_FIXTURES, load_ingest_fixture
from unmask.graph.build import build_graph
from unmask.graph.model import EdgeKind, FundingGraph, GraphInputError
from unmask.ingest.model import Transfer

SCENARIOS = sorted(p.name for p in GRAPH_FIXTURES.iterdir() if (p / "expected.json").is_file())

_BASIC = json.loads((GRAPH_FIXTURES / "g_basic" / "expected.json").read_text(encoding="utf-8"))
W = _BASIC["wallets"]
USDC = f"spl:{W['USDC']}"


def _expected(name: str) -> dict:
    return json.loads((Path(GRAPH_FIXTURES) / name / "expected.json").read_text(encoding="utf-8"))


def _edge_dict(e) -> dict:
    return {
        "kind": e.kind.value, "sender": e.sender, "receiver": e.receiver,
        "asset": None if e.asset is None else str(e.asset), "amount": e.amount,
        "decimals": e.decimals, "count": e.count, "first_slot": e.first_slot,
        "last_slot": e.last_slot, "first_time": e.first_time, "last_time": e.last_time,
        "refs": [{"signature": r.signature, "slot": r.slot, "instruction_path": r.instruction_path}
                 for r in e.refs],
    }


def _node_dict(n) -> dict:
    u = n.unexpanded
    m = n.measures
    return {
        "address": n.address, "roles": sorted(r.value for r in n.roles), "depth": n.depth,
        "buyer_rank": n.buyer_rank, "address_type": n.address_type.value,
        "unexpanded": None if u is None else {
            "reason": u.reason.value, "counterparties_seen": u.counterparties_seen,
            "signatures_seen": u.signatures_seen, "signatures_truncated": u.signatures_truncated,
        },
        "measures": {
            "degree": m.degree, "unique_senders": m.unique_senders,
            "one_off_senders": m.one_off_senders, "one_off_share": m.one_off_share,
            "buyer_fanout": m.buyer_fanout, "median_to_buyers": m.median_to_buyers,
        },
    }


def _t(sig, slot, sender, receiver, amount, *, path="0", asset="sol", decimals=None,
       block_time="auto", depth=1) -> Transfer:
    return Transfer(
        signature=sig, slot=slot,
        block_time=1_759_400_000 + slot if block_time == "auto" else block_time,
        instruction_path=path, sender=W[sender], receiver=W[receiver], asset=asset,
        amount=amount, decimals=decimals, depth=depth,
    )


def _result(*transfers):
    """Результат збору g_basic (ті самі покупці й метадані) з підміненими переказами."""
    return dataclasses.replace(load_ingest_fixture("g_basic"), transfers=tuple(transfers))


def _forced(*transfers):
    """Як `_result`, але в обхід перевірок `IngestResult` — імітує порушений контракт 001."""
    result = _result()
    object.__setattr__(result, "transfers", tuple(transfers))
    return result


def _edges_by_key(graph: FundingGraph) -> dict:
    return {(e.kind.value, e.sender, e.receiver, e.asset): e for e in graph.edges}


# --- golden ---------------------------------------------------------------------------


def test_g_basic_matches_expected_golden():
    graph = build_graph(load_ingest_fixture("g_basic"))
    assert isinstance(graph, FundingGraph)
    assert [_edge_dict(e) for e in graph.edges] == _expected("g_basic")["graph"]["edges"]


@pytest.mark.parametrize("name", SCENARIOS)
def test_edges_match_expected_golden_for_every_fixture(name):
    graph = build_graph(load_ingest_fixture(name))
    assert [_edge_dict(e) for e in graph.edges] == _expected(name)["graph"]["edges"]


@pytest.mark.parametrize("name", SCENARIOS)
def test_nodes_built_for_edges_match_expected_golden_for_every_fixture(name):
    """Мінімальна побудова вершин (обсяг T-029/T-030), без якої граф не існує, і виклик `compute`."""
    graph = build_graph(load_ingest_fixture(name))
    assert [_node_dict(n) for n in graph.nodes] == _expected(name)["graph"]["nodes"]


def test_fixture_set_is_the_documented_one():
    assert SCENARIOS == ["g_all_hubs", "g_basic", "g_buyer_hub", "g_delegated", "g_dust", "g_dust_mixed", "g_empty",
                         "g_financier", "g_hub", "g_incomplete", "g_known", "g_unexpanded"]


def test_empty_result_gives_empty_graph():
    assert build_graph(load_ingest_fixture("g_empty")) == FundingGraph((), ())


# --- агрегація ------------------------------------------------------------------------


def test_two_transfers_same_pair_same_asset_collapse_into_one_edge_with_sum_count_first_last_and_all_refs():
    # Вхід навмисно не в порядку слотів: перший/останній мають братися зі слотів, а не з порядку входу.
    graph = build_graph(_result(
        _t("sigLate", 300, "A", "P1", 7, path="1"),
        _t("sigEarly", 100, "A", "P1", 5),
        _t("sigMid", 200, "A", "P1", 11, path="2.1"),
    ))
    assert len(graph.edges) == 1
    (edge,) = graph.edges
    assert (edge.kind, edge.sender, edge.receiver, edge.asset) == (EdgeKind.TRANSFER, W["A"], W["P1"], "sol")
    assert edge.amount == 5 + 11 + 7
    assert edge.count == 3
    assert (edge.first_slot, edge.last_slot) == (100, 300)
    assert (edge.first_time, edge.last_time) == (1_759_400_100, 1_759_400_300)
    assert edge.decimals is None
    assert [(r.signature, r.slot, r.instruction_path) for r in edge.refs] == [
        ("sigEarly", 100, "0"), ("sigMid", 200, "2.1"), ("sigLate", 300, "1"),
    ]


def test_first_and_last_time_follow_slots_not_magnitude_or_input_order():
    # block_time ідуть проти слотів (штучно): first_time — час найранішого за слотом переказу.
    graph = build_graph(_result(
        _t("s2", 20, "A", "P1", 1, block_time=1_000),
        _t("s1", 10, "A", "P1", 1, block_time=5_000),
        _t("s3", 30, "A", "P1", 1, block_time=3_000),
    ))
    (edge,) = graph.edges
    assert (edge.first_time, edge.last_time) == (5_000, 3_000)


def test_two_transfers_in_one_transaction_are_two_refs_of_one_edge():
    graph = build_graph(_result(
        _t("sigX", 50, "A", "P2", 3, path="0"),
        _t("sigX", 50, "A", "P2", 4, path="1"),
    ))
    (edge,) = graph.edges
    assert (edge.amount, edge.count) == (7, 2)
    assert [r.instruction_path for r in edge.refs] == ["0", "1"]


def test_same_pair_different_assets_are_separate_edges():
    graph = build_graph(_result(
        _t("s1", 10, "A", "P2", 1_000),
        _t("s2", 11, "A", "P2", 2_000),
        _t("s3", 12, "A", "P2", 50, asset=USDC, decimals=6),
    ))
    edges = _edges_by_key(graph)
    assert set(edges) == {("transfer", W["A"], W["P2"], "sol"), ("transfer", W["A"], W["P2"], USDC)}
    sol, usdc = edges[("transfer", W["A"], W["P2"], "sol")], edges[("transfer", W["A"], W["P2"], USDC)]
    assert (sol.amount, sol.count, sol.decimals) == (3_000, 2, None)
    assert (usdc.amount, usdc.count, usdc.decimals) == (50, 1, 6)


def test_opposite_directions_are_separate_edges():
    graph = build_graph(_result(
        _t("s1", 10, "A", "D", 1, depth=2),
        _t("s2", 11, "D", "A", 2, depth=2),
        _t("s0", 9, "A", "P1", 3),
    ))
    edges = _edges_by_key(graph)
    assert edges[("transfer", W["A"], W["D"], "sol")].amount == 1
    assert edges[("transfer", W["D"], W["A"], "sol")].amount == 2


def test_spl_edge_keeps_common_decimals():
    graph = build_graph(_result(
        _t("s1", 10, "A", "P2", 5, asset=USDC, decimals=6),
        _t("s2", 11, "A", "P2", 6, asset=USDC, decimals=6),
    ))
    (edge,) = graph.edges
    assert (edge.asset, edge.decimals, edge.amount) == (USDC, 6, 11)


def test_only_transfer_edges_are_built_from_transfers():
    graph = build_graph(load_ingest_fixture("g_basic"))
    assert graph.edges and all(e.kind is EdgeKind.TRANSFER for e in graph.edges)


@pytest.mark.parametrize("name", SCENARIOS)
def test_edge_refs_lead_to_every_source_signature_and_slot(name):
    """SC-006 001: кожен ref ребра `transfer` — рівно один переказ входу і навпаки, з тими самими кінцями й
    активом; кожен ref ребра `delegated_buy` — рівно один зв'язок `delegated.links` і навпаки (T-047)."""
    result = load_ingest_fixture(name)
    graph = build_graph(result)
    assert {e.kind for e in graph.edges} <= {EdgeKind.TRANSFER, EdgeKind.DELEGATED_BUY}
    from_refs = Counter(
        (r.signature, r.slot, r.instruction_path, e.sender, e.receiver, str(e.asset))
        for e in graph.edges if e.kind is EdgeKind.TRANSFER for r in e.refs
    )
    from_input = Counter(
        (t.signature, t.slot, t.instruction_path, t.sender, t.receiver, str(t.asset))
        for t in result.transfers
    )
    assert from_refs == from_input
    transfer_edges = [e for e in graph.edges if e.kind is EdgeKind.TRANSFER]
    assert sum(e.count for e in transfer_edges) == len(result.transfers)
    for e in transfer_edges:
        mine = [t for t in result.transfers if (t.sender, t.receiver, t.asset) == (e.sender, e.receiver, e.asset)]
        assert e.amount == sum(t.amount for t in mine)
    delegated_edges = [e for e in graph.edges if e.kind is EdgeKind.DELEGATED_BUY]
    from_delegated_refs = Counter(
        (r.signature, r.slot, r.instruction_path, e.sender, e.receiver, e.asset)
        for e in delegated_edges for r in e.refs
    )
    from_links = Counter(
        (l.signature, l.slot, None, l.payer, l.receiver, None) for l in result.delegated.links
    )
    assert from_delegated_refs == from_links
    assert sum(e.count for e in delegated_edges) == len(result.delegated.links)
    assert all(e.amount is None for e in delegated_edges)


# --- block_time ------------------------------------------------------------------------


def test_block_time_none_propagates_as_none_not_zero():
    graph = build_graph(_result(
        _t("s1", 10, "A", "P1", 1, block_time=None),
        _t("s2", 20, "A", "P1", 1, block_time=None),
    ))
    (edge,) = graph.edges
    assert edge.first_time is None and edge.last_time is None


def test_block_time_missing_on_one_end_only_stays_none_on_that_end():
    # Не підставляємо час сусіднього переказу: відсутній час першого переказу — None, а не час другого.
    graph = build_graph(_result(
        _t("s1", 10, "A", "P1", 1, block_time=None),
        _t("s2", 20, "A", "P1", 1, block_time=1_234),
    ))
    (edge,) = graph.edges
    assert (edge.first_time, edge.last_time) == (None, 1_234)
    graph = build_graph(_result(
        _t("s1", 10, "A", "P1", 1, block_time=1_000),
        _t("s2", 20, "A", "P1", 1, block_time=None),
    ))
    (edge,) = graph.edges
    assert (edge.first_time, edge.last_time) == (1_000, None)


def test_block_time_zero_is_kept_as_zero():
    graph = build_graph(_result(_t("s1", 10, "A", "P1", 1, block_time=0)))
    (edge,) = graph.edges
    assert (edge.first_time, edge.last_time) == (0, 0)


# --- перевірки входу ---------------------------------------------------------------------


def test_self_transfer_in_input_raises_graph_input_error():
    with pytest.raises(GraphInputError, match="self"):
        build_graph(_result(_t("s1", 10, "A", "P1", 1), _t("s2", 11, "A", "A", 1, depth=2)))


def test_duplicate_signature_path_raises_graph_input_error():
    # Той самий ключ ребра.
    with pytest.raises(GraphInputError, match="duplicate"):
        build_graph(_forced(_t("s1", 10, "A", "P1", 1), _t("s1", 10, "A", "P1", 2)))
    # Різні ребра: дубль не зловила б перевірка всередині одного ребра.
    with pytest.raises(GraphInputError, match="duplicate"):
        build_graph(_forced(_t("s1", 10, "A", "P1", 1), _t("s1", 10, "B", "P2", 2)))


def test_same_signature_different_paths_is_not_a_duplicate():
    graph = build_graph(_result(_t("s1", 10, "A", "P1", 1, path="0"), _t("s1", 10, "B", "P2", 2, path="0.1")))
    assert len(graph.edges) == 2


def test_mismatched_decimals_of_one_asset_raise_graph_input_error():
    with pytest.raises(GraphInputError, match="decimals"):
        build_graph(_result(
            _t("s1", 10, "A", "P2", 5, asset=USDC, decimals=6),
            _t("s2", 11, "A", "P2", 6, asset=USDC, decimals=9),
        ))


def test_mismatched_decimals_across_different_edges_are_allowed():
    # Межа перевірки — ребро: у різних пар той самий mint з тими самими decimals, інший mint — свої.
    other = f"spl:{W['DEX']}"
    graph = build_graph(_result(
        _t("s1", 10, "A", "P2", 5, asset=USDC, decimals=6),
        _t("s2", 11, "A", "P2", 6, asset=other, decimals=9),
    ))
    assert {(e.asset, e.decimals) for e in graph.edges} == {(USDC, 6), (other, 9)}


def test_graph_input_error_is_raised_before_any_partial_graph():
    with pytest.raises(GraphInputError):
        build_graph(_result(_t("s1", 10, "A", "P1", 1), _t("s2", 11, "C", "C", 1, depth=2)))


# --- перевірки входу мінімальної побудови вершин (див. шапку) ------------------------


def test_non_buyer_address_that_is_not_a_public_key_raises_graph_input_error():
    bad = dataclasses.replace(_t("s1", 10, "A", "P1", 1), sender="not-a-key")
    with pytest.raises(GraphInputError, match="public key"):
        build_graph(_result(bad))


def test_unexpanded_record_for_address_outside_graph_raises_graph_input_error():
    result = load_ingest_fixture("g_unexpanded")
    stray = dataclasses.replace(result.unexpanded[0], wallet=W["C"])
    with pytest.raises(GraphInputError, match="not a graph node"):
        build_graph(dataclasses.replace(result, unexpanded=result.unexpanded + (stray,)))


def test_duplicate_unexpanded_record_raises_graph_input_error():
    result = load_ingest_fixture("g_unexpanded")
    with pytest.raises(GraphInputError, match="duplicate unexpanded"):
        build_graph(dataclasses.replace(result, unexpanded=result.unexpanded + result.unexpanded[:1]))
