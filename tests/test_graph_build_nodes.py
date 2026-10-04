# verifies: FR-002-03
"""Вершини графа фінансування (T-029, research R-6).

Ролі, мінімум глибини, `buyer_rank`, тип адреси — і гучні відмови на порушення
контракту 001 (`GraphInputError`, а не `ValueError` з конструктора `Node`).

Частина властивостей (ролі, глибина, тип адреси) реалізована вже в T-028, бо `Node`
не існує без них; для них «червоний перед кодом» недосяжний, тож їхні тести
перевірені мутаційно (звіт T-029). Реально нова поведінка T-029 — перевірки входу.
"""

import ast
import dataclasses
import itertools
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from solders.pubkey import Pubkey

from conftest import GRAPH_FIXTURES, load_ingest_fixture
from unmask.graph.build import build_graph
from unmask.graph.model import GraphInputError, NodeRole
from unmask.ingest.model import AddressType, Transfer

_BASIC = json.loads((GRAPH_FIXTURES / "g_basic" / "expected.json").read_text(encoding="utf-8"))
W = _BASIC["wallets"]
BUILD_PY = Path(__file__).resolve().parent.parent / "src" / "unmask" / "graph" / "build.py"


def _t(sig, slot, sender, receiver, *, depth=1, path="0", amount=1) -> Transfer:
    """Переказ SOL; `sender`/`receiver` — ключі гаманців g_basic або готова адреса."""
    return Transfer(
        signature=sig, slot=slot, block_time=1_759_400_000 + slot, instruction_path=path,
        sender=W.get(sender, sender), receiver=W.get(receiver, receiver), asset="sol",
        amount=amount, decimals=None, depth=depth,
    )


def _result(*transfers):
    """Результат збору g_basic (ті самі покупці P1..P5) з підміненими переказами."""
    return dataclasses.replace(load_ingest_fixture("g_basic"), transfers=tuple(transfers))


def _forced(result, **fields):
    """Підміна полів у обхід перевірок `IngestResult` — імітує порушений контракт 001."""
    for name, value in fields.items():
        object.__setattr__(result, name, value)
    return result


def _nodes(graph) -> dict:
    return {n.address: n for n in graph.nodes}


# --- ролі, глибина, ранг на g_basic -----------------------------------------------------


def test_g_basic_nodes_match_expected_roles_depth_rank():
    graph = build_graph(load_ingest_fixture("g_basic"))
    got = {
        n.address: (sorted(r.value for r in n.roles), n.depth, n.buyer_rank)
        for n in graph.nodes
    }
    want = {
        n["address"]: (n["roles"], n["depth"], n["buyer_rank"])
        for n in _BASIC["graph"]["nodes"]
    }
    assert got == want
    # Підстраховка від збігу двох однаково хибних еталонів: ключові значення явно.
    nodes = _nodes(graph)
    assert nodes[W["P1"]].roles == {NodeRole.BUYER} and nodes[W["P1"]].buyer_rank == 1
    assert nodes[W["A"]].roles == {NodeRole.FUNDER} and nodes[W["A"]].depth == 1
    assert nodes[W["A"]].buyer_rank is None
    assert nodes[W["C"]].depth == 3
    assert W["M"] not in nodes  # адреса, якої немає ні в покупцях, ні в переказах, вершини не має


def test_buyer_that_funds_another_buyer_is_one_node_with_two_roles_depth_zero():
    # g_basic: P3 -> P4 (глибина 1), обидва покупці.
    graph = build_graph(load_ingest_fixture("g_basic"))
    assert sum(1 for n in graph.nodes if n.address == W["P3"]) == 1
    p3, p4 = _nodes(graph)[W["P3"]], _nodes(graph)[W["P4"]]
    assert p3.roles == {NodeRole.BUYER, NodeRole.FUNDER}
    assert (p3.depth, p3.buyer_rank) == (0, 3)
    assert p4.roles == {NodeRole.BUYER} and p4.depth == 0


def test_buyer_depth_stays_zero_even_when_it_funds_at_higher_depth():
    # P1 надсилає переказ глибини 3: межа відправника 3, але покупець — завжди 0.
    graph = build_graph(_result(_t("s1", 10, "P1", "A", depth=3), _t("s2", 11, "A", "P2")))
    p1 = _nodes(graph)[W["P1"]]
    assert p1.depth == 0 and p1.roles == {NodeRole.BUYER, NodeRole.FUNDER}


@pytest.mark.parametrize("order", list(itertools.permutations(range(4))))
def test_depth_is_minimum_over_all_paths(order):
    # C надсилає на глибині 3 (C -> D) і на глибині 2 (C -> A): мінімум 2, у будь-якому порядку входу.
    base = [
        _t("s1", 10, "A", "P1", depth=1),
        _t("s2", 11, "C", "D", depth=3),
        _t("s3", 12, "C", "A", depth=2),
        _t("s4", 13, "D", "A", depth=2),
    ]
    graph = build_graph(_result(*(base[i] for i in order)))
    nodes = _nodes(graph)
    assert nodes[W["C"]].depth == 2
    assert nodes[W["D"]].depth == 2  # отримувач глибини 3: межа 3 - 1
    assert nodes[W["A"]].depth == 1


@pytest.mark.parametrize("order", [(0, 1, 2, 3), (3, 2, 1, 0), (2, 0, 3, 1)])
def test_depth_takes_the_receiver_bound_when_it_is_lower_than_the_sender_bound(order):
    # Y отримує на глибині 2 (межа 1) і надсилає на глибині 3 (межа 3): мінімум 1.
    y = str(Pubkey.from_bytes(bytes([7]) * 32))
    base = [
        _t("s1", 10, "A", "P1"),
        _t("s2", 11, "C", y, depth=2),
        _t("s3", 12, y, "D", depth=3),
        _t("s4", 13, "D", "A", depth=2),
    ]
    graph = build_graph(_result(*(base[i] for i in order)))
    nodes = _nodes(graph)
    assert nodes[y].depth == 1
    assert nodes[W["C"]].depth == 2 and nodes[W["D"]].depth == 2


# --- тип адреси ---------------------------------------------------------------------


def test_non_buyer_pda_address_type_is_off_curve_and_wallet_is_wallet():
    pda, _bump = Pubkey.find_program_address([b"unmask", b"t029"], Pubkey.from_string(W["DEX"]))
    assert not pda.is_on_curve()
    wallet = Pubkey.from_string(W["A"])
    assert wallet.is_on_curve()
    graph = build_graph(_result(
        _t("s1", 10, "A", "P1"), _t("s2", 11, str(pda), "A", depth=2),
    ))
    nodes = _nodes(graph)
    assert nodes[str(pda)].address_type is AddressType.OFF_CURVE
    assert nodes[W["A"]].address_type is AddressType.WALLET
    assert nodes[W["P1"]].address_type is AddressType.WALLET


def test_buyer_address_type_taken_from_ingest_not_recomputed():
    result = load_ingest_fixture("g_basic")
    assert Pubkey.from_string(W["P1"]).is_on_curve()  # перерахунок дав би wallet
    buyers = tuple(
        dataclasses.replace(b, address_type=AddressType.OFF_CURVE) if b.wallet == W["P1"] else b
        for b in result.buyers
    )
    graph = build_graph(dataclasses.replace(result, buyers=buyers))
    nodes = _nodes(graph)
    assert nodes[W["P1"]].address_type is AddressType.OFF_CURVE
    assert nodes[W["P2"]].address_type is AddressType.WALLET


def test_build_does_not_import_ingest_addresses():
    # Статично: жоден імпорт у build.py не веде до unmask.ingest.addresses.
    tree = ast.parse(BUILD_PY.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            imported.add(base)
            imported.update(f"{base}.{a.name}" for a in node.names)
    assert imported and not {m for m in imported if m.endswith("ingest.addresses")}
    # Динамічно: у свіжому інтерпретаторі після імпорту build_graph модуль не завантажено.
    code = (
        "import sys; import unmask.graph.build; "
        "sys.exit(1 if 'unmask.ingest.addresses' in sys.modules else 0)"
    )
    env = {**os.environ, "PYTHONPATH": str(BUILD_PY.parents[2])}
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr


# --- перевірки входу (R-6): GraphInputError --------------------------------------------


def test_buyer_without_rank_raises_graph_input_error():
    result = load_ingest_fixture("g_basic")
    for bad in (None, 0, -1, True, False, 1.5, "1"):
        buyers = tuple(
            dataclasses.replace(b) if b.wallet != W["P2"] else _with_rank(b, bad)
            for b in result.buyers
        )
        with pytest.raises(GraphInputError, match="rank"):
            build_graph(_forced(dataclasses.replace(result), buyers=buyers))


def _with_rank(buyer, rank):
    """Копія покупця з порушеним рангом (обхід `Buyer.__post_init__`)."""
    clone = dataclasses.replace(buyer)
    object.__setattr__(clone, "rank", rank)
    return clone


def test_two_buyers_with_the_same_address_raise_graph_input_error():
    result = load_ingest_fixture("g_basic")
    dup = dataclasses.replace(result.buyers[0], rank=9)
    with pytest.raises(GraphInputError, match="duplicate buyer"):
        build_graph(_forced(dataclasses.replace(result), buyers=result.buyers + (dup,)))


def test_contradictory_depth_raises_graph_input_error():
    # Y отримує на глибині 1 (межа 0 — рівень покупців), але не покупець.
    y = str(Pubkey.from_bytes(bytes([9]) * 32))
    with pytest.raises(GraphInputError, match="depth"):
        build_graph(_result(_t("s1", 10, "A", y, depth=1), _t("s2", 11, y, "P1", depth=1)))


def test_receiver_of_depth_one_transfer_that_is_not_a_buyer_raises_graph_input_error():
    with pytest.raises(GraphInputError, match="depth"):
        build_graph(_result(_t("s1", 10, "A", "C", depth=1), _t("s2", 11, "C", "A", depth=2)))


def test_receiver_without_a_role_raises_graph_input_error_not_value_error():
    # Y принесений переказом глибини 2, але більше ніде не з'являється: ролі нема ні `buyer`, ні `funder`.
    y = str(Pubkey.from_bytes(bytes([5]) * 32))
    with pytest.raises(GraphInputError, match="role"):
        build_graph(_result(_t("s1", 10, "A", "P1"), _t("s2", 11, "A", y, depth=2)))


def test_consistent_depths_are_not_flagged():
    # Негативний контроль: ті самі форми без суперечності будуються.
    graph = build_graph(_result(_t("s1", 10, "A", "P1"), _t("s2", 11, "C", "A", depth=2)))
    assert {n.address for n in graph.nodes} == {W["A"], W["C"], *(W[f"P{i}"] for i in range(1, 6))}
