# verifies: FR-002-11
"""Компоненти зв'язності графа фінансування (T-027, research R-10).

Критична задача: хибний підрахунок компонент тихо псує звіт ефекту відсікання
(SC-001) і попередження `giant_component`, не ламаючи збірку. Тому окрім
іменованих сценаріїв — перебір проти незалежного оракула (BFS по суміжності,
інша реалізація, ніж union-find у коді) на сотнях випадкових графів, інваріант
незалежності від порядку входу й перестановки адрес, межові випадки та
перевірка, що реалізація ітеративна (граф до сотень тисяч ребер).
"""

import ast
import inspect
import random
import time
from collections import deque

import pytest

from unmask.graph import components as components_module
from unmask.graph.components import Components, components
from unmask.graph.model import (
    Edge,
    EdgeKind,
    EdgeRef,
    FundingGraph,
    Node,
    NodeMeasures,
    NodeRole,
)
from unmask.ingest.model import AddressType

BUYER = NodeRole.BUYER
FUNDER = NodeRole.FUNDER
PAYER = NodeRole.DELEGATED_PAYER
RECEIVER = NodeRole.DELEGATED_RECEIVER
TRANSFER = EdgeKind.TRANSFER
DELEGATED = EdgeKind.DELEGATED_BUY
SPL = "spl:So11111111111111111111111111111111111111112"

_MEASURES = NodeMeasures(degree=0, unique_senders=0, one_off_senders=0, one_off_share=None)


# --- Будівельники ------------------------------------------------------------------


def _node(address, roles=(FUNDER,)):
    roles = frozenset(roles)
    is_buyer = BUYER in roles
    return Node(
        address=address,
        roles=roles,
        depth=0 if is_buyer else 1,
        buyer_rank=1 if is_buyer else None,
        address_type=AddressType.WALLET,
        unexpanded=None,
        measures=_MEASURES,
    )


def _buyer(address, roles=(BUYER,)):
    return _node(address, roles=roles)


_SIG = iter(range(10**9))


def _edge(sender, receiver, kind=TRANSFER, asset="sol"):
    sig = f"sig{next(_SIG)}"
    if kind is TRANSFER:
        return Edge(
            kind=TRANSFER, sender=sender, receiver=receiver, asset=asset, amount=1,
            decimals=6 if asset != "sol" else None, count=1, first_slot=1, last_slot=1,
            first_time=None, last_time=None, refs=(EdgeRef(sig, 1, "0"),),
        )
    return Edge(
        kind=DELEGATED, sender=sender, receiver=receiver, asset=None, amount=None,
        decimals=None, count=1, first_slot=1, last_slot=1, first_time=None, last_time=None,
        refs=(EdgeRef(sig, 1, None),),
    )


def _graph(nodes, edges=()):
    return FundingGraph(nodes=tuple(nodes), edges=tuple(edges))


def _partition(comps: Components) -> dict[str, tuple[str, ...]]:
    """Повний опис результату: id -> вершини (у порядку, який віддає код)."""
    return {cid: comps.members(cid) for cid in comps.ids()}


# --- Незалежний оракул: BFS по ненапрямленій суміжності ---------------------------


def _oracle(graph: FundingGraph):
    """Компоненти простим BFS. Повертає (partition, buyers_by_component).

    Не ділить жодного коду з `unmask.graph.components`: суміжність — множини,
    обхід — черга, id — `min()` по множині, порядок вершин — `sorted()`.
    """
    adjacency = {n.address: set() for n in graph.nodes}
    for e in graph.edges:
        adjacency[e.sender].add(e.receiver)
        adjacency[e.receiver].add(e.sender)
    buyers = {n.address for n in graph.nodes if BUYER in n.roles}
    seen = set()
    partition = {}
    for start in adjacency:
        if start in seen:
            continue
        group = {start}
        queue = deque([start])
        seen.add(start)
        while queue:
            cur = queue.popleft()
            for nxt in adjacency[cur]:
                if nxt not in seen:
                    seen.add(nxt)
                    group.add(nxt)
                    queue.append(nxt)
        partition[min(group)] = tuple(sorted(group))
    buyer_counts = {cid: sum(1 for a in members if a in buyers) for cid, members in partition.items()}
    return partition, buyer_counts


def _oracle_largest(buyer_counts, partition):
    """Тай-брейк із документації `Components.largest_buyer_component`, наївно."""
    if not buyer_counts or max(buyer_counts.values()) == 0:
        return None
    best = max(buyer_counts.values())
    candidates = [cid for cid, b in buyer_counts.items() if b == best]
    most_nodes = max(len(partition[c]) for c in candidates)
    return min(c for c in candidates if len(partition[c]) == most_nodes)


_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _random_address(rng, used):
    while True:
        # Різні довжини: порівняння рядків, а не довжин, визначає мінімальну адресу.
        addr = "".join(rng.choice(_ALPHABET) for _ in range(rng.randint(1, 6)))
        if addr not in used:
            used.add(addr)
            return addr


def _random_case(rng: random.Random):
    """(nodes, edges) випадкового графа: розмір, щільність, частка покупців, види ребер — випадкові."""
    n = rng.choice([0, 1, 2, 3, rng.randint(4, 15), rng.randint(16, 60)])
    used = set()
    addresses = [_random_address(rng, used) for _ in range(n)]
    buyer_share = rng.choice([0.0, 0.1, 0.5, 1.0, rng.random()])
    nodes = []
    for a in addresses:
        if rng.random() < buyer_share:
            roles = rng.choice([(BUYER,), (BUYER, FUNDER), (BUYER, PAYER), (BUYER, RECEIVER)])
        else:
            roles = rng.choice([(FUNDER,), (PAYER,), (RECEIVER,), (FUNDER, PAYER)])
        nodes.append(_node(a, roles))
    density = rng.choice([0.0, 0.01, 0.03, 0.08, 0.2, rng.random() * 0.3])
    delegated_share = rng.choice([0.0, 0.3, 1.0, rng.random()])
    keys = set()
    edges = []
    if n >= 2:
        target = int(density * n * (n - 1))
        for _ in range(target):
            s, r = rng.sample(addresses, 2)
            if rng.random() < delegated_share:
                key = (DELEGATED, s, r, None)
            else:
                key = (TRANSFER, s, r, rng.choice(["sol", SPL]))
            if key in keys:
                continue
            keys.add(key)
            edges.append(key)
    return nodes, edges


def _build_edges(keys):
    return [_edge(s, r, kind, asset or "sol") for kind, s, r, asset in keys]


# --- Іменовані тести T-027 ---------------------------------------------------------


def test_components_match_bfs_oracle_on_random_graphs():
    """≥ 500 випадкових графів: розбиття, id, порядок вершин, покупці, найбільша компонента, частка."""
    rng = random.Random(20261004)
    nontrivial = 0
    for case in range(600):
        nodes, keys = _random_case(rng)
        graph = _graph(nodes, _build_edges(keys))
        comps = components(graph)
        partition, buyer_counts = _oracle(graph)

        assert _partition(comps) == partition, f"case {case}"
        assert comps.ids() == tuple(sorted(partition)), f"case {case}"
        assert comps.count == len(partition), f"case {case}"
        assert comps.buyers_by_component() == buyer_counts, f"case {case}"
        for cid, members in partition.items():
            for a in members:
                assert comps.of(a) == cid, f"case {case}"

        buyers_total = sum(1 for n in nodes if BUYER in n.roles)
        largest = _oracle_largest(buyer_counts, partition)
        assert comps.buyers_total == buyers_total, f"case {case}"
        assert comps.largest_buyer_component() == largest, f"case {case}"
        in_largest = 0 if largest is None else buyer_counts[largest]
        assert comps.buyers_in_largest_component() == in_largest, f"case {case}"
        expected_share = 0.0 if buyers_total == 0 else in_largest / buyers_total
        assert comps.largest_component_buyer_share() == expected_share, f"case {case}"
        if 1 < len(partition) < len(nodes):
            nontrivial += 1
    # Перебір має сенс, лише якщо серед графів є й «частково зв'язні».
    assert nontrivial >= 100


def test_result_independent_of_edge_order():
    """Перестановки ребер і вершин (і перейменування зі збереженням порядку адрес) → той самий результат."""
    rng = random.Random(7)
    for case in range(120):
        nodes, keys = _random_case(rng)
        reference = components(_graph(nodes, _build_edges(keys)))
        for _ in range(4):
            shuffled_nodes = list(nodes)
            shuffled_keys = list(keys)
            rng.shuffle(shuffled_nodes)
            rng.shuffle(shuffled_keys)
            # Розвернути частину ребер: напрямок не впливає на слабку зв'язність.
            flipped = [
                (k, r, s, a) if rng.random() < 0.5 and (k, r, s, a) not in set(shuffled_keys) else (k, s, r, a)
                for k, s, r, a in shuffled_keys
            ]
            if len(set(flipped)) != len(flipped):
                flipped = shuffled_keys
            for variant in (shuffled_keys, flipped):
                comps = components(_graph(shuffled_nodes, _build_edges(variant)))
                assert _partition(comps) == _partition(reference), f"case {case}"
                assert comps.buyers_by_component() == reference.buyers_by_component()
                assert comps.largest_buyer_component() == reference.largest_buyer_component()
                assert comps.largest_component_buyer_share() == reference.largest_component_buyer_share()


def test_isolated_nodes_are_singleton_components():
    graph = _graph(
        [_buyer("b1"), _buyer("b2"), _node("f1"), _node("f2"), _node("lonely")],
        [_edge("f1", "b1")],
    )
    comps = components(graph)
    assert comps.count == 4
    assert comps.ids() == ("b1", "b2", "f2", "lonely")
    assert comps.members("b1") == ("b1", "f1")
    assert comps.members("b2") == ("b2",)
    assert comps.members("lonely") == ("lonely",)
    assert comps.of("lonely") == "lonely"
    assert comps.buyers_by_component() == {"b1": 1, "b2": 1, "f2": 0, "lonely": 0}
    # Ізольований покупець — у знаменнику частки (R-10: знаменник — усі покупці).
    assert comps.buyers_total == 2
    assert comps.largest_component_buyer_share() == 0.5


def test_delegated_edges_connect_components_too():
    nodes = [_buyer("b1"), _buyer("b2"), _node("p", (PAYER,))]
    without = components(_graph(nodes, [_edge("p", "b1", DELEGATED)]))
    assert without.count == 2
    with_delegated = components(_graph(nodes, [_edge("p", "b1", DELEGATED), _edge("p", "b2", DELEGATED)]))
    assert with_delegated.count == 1
    assert with_delegated.members("b1") == ("b1", "b2", "p")
    assert with_delegated.largest_component_buyer_share() == 1.0
    # Змішаний ланцюжок: transfer + delegated_buy складаються в одну компоненту.
    mixed = components(_graph(nodes, [_edge("p", "b1", TRANSFER), _edge("b2", "p", DELEGATED)]))
    assert mixed.count == 1


def test_buyers_by_component_counts_only_buyer_role():
    graph = _graph(
        [
            _buyer("a"),
            _buyer("b", (BUYER, FUNDER)),
            _node("c", (FUNDER,)),
            _node("d", (PAYER,)),
            _node("e", (RECEIVER,)),
            _node("f", (FUNDER, PAYER)),
        ],
        [_edge("c", "a"), _edge("b", "a"), _edge("d", "e", DELEGATED), _edge("f", "e")],
    )
    comps = components(graph)
    assert comps.buyers_by_component() == {"a": 2, "d": 0}
    assert comps.members("d") == ("d", "e", "f")
    assert comps.buyers_total == 2
    assert comps.largest_buyer_component() == "a"
    assert comps.buyers_in_largest_component() == 2
    assert comps.largest_component_buyer_share() == 1.0


def test_empty_graph_has_zero_components():
    comps = components(_graph([]))
    assert comps.count == 0
    assert comps.ids() == ()
    assert comps.buyers_by_component() == {}
    assert comps.buyers_total == 0
    assert comps.largest_buyer_component() is None
    assert comps.buyers_in_largest_component() == 0
    # R-10 / data-model: нуль покупців -> частка 0.0, без ділення на нуль.
    share = comps.largest_component_buyer_share()
    assert share == 0.0 and isinstance(share, float)


# --- Межові випадки -----------------------------------------------------------------


def test_only_non_buyers_share_is_zero_and_no_largest():
    comps = components(_graph([_node("x"), _node("y"), _node("z")], [_edge("x", "y")]))
    assert comps.count == 2
    assert comps.buyers_total == 0
    assert comps.largest_buyer_component() is None
    assert comps.largest_component_buyer_share() == 0.0


def test_component_id_is_min_address_not_first_seen_or_buyer():
    # Ребро з "z" (перший відправник) до "m"; мінімальна адреса — "a" через інший ланцюжок.
    graph = _graph([_node("z"), _buyer("m"), _node("a")], [_edge("z", "m"), _edge("a", "z")])
    comps = components(graph)
    assert comps.ids() == ("a",)
    assert comps.of("m") == "a"
    assert comps.members("a") == ("a", "m", "z")


def test_id_uses_string_order_not_length():
    graph = _graph([_buyer("B"), _node("AAAAAA"), _node("a")], [_edge("AAAAAA", "B")])
    comps = components(graph)
    assert comps.ids() == ("AAAAAA", "a")


def test_direction_is_ignored_star_in_and_out():
    # Хаб лише отримує (in-star) і хаб лише відправляє (out-star) — обидва склеюють.
    in_star = _graph([_node("h")] + [_buyer(f"b{i}") for i in range(5)], [_edge(f"b{i}", "h") for i in range(5)])
    out_star = _graph([_node("h")] + [_buyer(f"b{i}") for i in range(5)], [_edge("h", f"b{i}") for i in range(5)])
    for g in (in_star, out_star):
        comps = components(g)
        assert comps.count == 1
        assert comps.largest_component_buyer_share() == 1.0


def test_two_way_and_multi_asset_edges_between_same_pair_count_once():
    graph = _graph(
        [_buyer("a"), _node("b")],
        [_edge("a", "b"), _edge("b", "a"), _edge("b", "a", asset=SPL), _edge("b", "a", DELEGATED)],
    )
    comps = components(graph)
    assert comps.count == 1
    assert comps.members("a") == ("a", "b")


def test_largest_buyer_component_is_by_buyers_not_by_size():
    # Велика компонента без покупців-більшості (1 покупець, 5 джерел) проти малої з 2 покупцями.
    nodes = [_buyer("p"), _buyer("q"), _buyer("r")] + [_node(f"s{i}") for i in range(5)]
    edges = [_edge(f"s{i}", "p") for i in range(5)] + [_edge("q", "r")]
    comps = components(_graph(nodes, edges))
    assert comps.largest_buyer_component() == "q"
    assert comps.buyers_in_largest_component() == 2
    assert comps.largest_component_buyer_share() == 2 / 3


def test_largest_tie_break_more_nodes_then_min_id():
    # Однакова кількість покупців (по 1): перемагає компонента з більшою кількістю вершин...
    nodes = [_buyer("a"), _buyer("b"), _node("f1"), _node("f2")]
    comps = components(_graph(nodes, [_edge("f1", "b"), _edge("f2", "b")]))
    assert comps.largest_buyer_component() == "b"
    # ...а за рівності й вершин — мінімальний id.
    nodes = [_buyer("c"), _buyer("d"), _node("x"), _node("y")]
    comps = components(_graph(nodes, [_edge("x", "d"), _edge("y", "c")]))
    assert comps.largest_buyer_component() == "c"
    # Компонента без покупців ніколи не «найбільша серед покупців», хоч і більша.
    nodes = [_buyer("z")] + [_node(f"n{i}") for i in range(4)]
    comps = components(_graph(nodes, [_edge("n0", "n1"), _edge("n1", "n2"), _edge("n2", "n3")]))
    assert comps.largest_buyer_component() == "z"


def test_share_is_exact_ratio_without_rounding():
    nodes = [_buyer(f"b{i}") for i in range(7)] + [_node("h")]
    edges = [_edge("h", f"b{i}") for i in range(3)]
    comps = components(_graph(nodes, edges))
    assert comps.buyers_total == 7
    assert comps.buyers_in_largest_component() == 3
    assert comps.largest_component_buyer_share() == 3 / 7


def test_share_denominator_is_all_buyers_including_isolated():
    # 18 з 20 покупців під хабом, 2 ізольовані: 0.9, а не 1.0 (SC-001-подібна форма).
    nodes = [_buyer(f"b{i:02d}") for i in range(20)] + [_node("H")]
    edges = [_edge("H", f"b{i:02d}") for i in range(18)]
    comps = components(_graph(nodes, edges))
    assert comps.largest_component_buyer_share() == 0.9
    after = components(_graph(nodes, edges).without(["H"]))
    assert after.count == 20
    assert after.largest_component_buyer_share() == 1 / 20


def test_unknown_address_and_unknown_component_raise():
    comps = components(_graph([_buyer("a"), _node("b")], [_edge("b", "a")]))
    with pytest.raises(KeyError):
        comps.of("zzz")
    with pytest.raises(KeyError):
        comps.members("b")  # "b" — вершина, але не id компоненти


def test_rejects_non_graph_input():
    with pytest.raises(TypeError):
        components([_buyer("a")])


def test_returned_collections_do_not_leak_internal_state():
    comps = components(_graph([_buyer("a"), _node("b")], [_edge("b", "a")]))
    counts = comps.buyers_by_component()
    counts["a"] = 99
    assert comps.buyers_by_component() == {"a": 1}
    assert isinstance(comps.members("a"), tuple)
    with pytest.raises(Exception):
        comps.buyers_total = 5  # frozen


def test_long_chain_is_handled_iteratively():
    """Ланцюжок 30 000 вершин: рекурсивний find/обхід упав би на ліміті рекурсії."""
    n = 30_000
    names = [f"v{i:06d}" for i in range(n)]
    nodes = [_buyer(names[0])] + [_node(a) for a in names[1:]]
    # Ребра у «поганому» порядку для злиття: від кінця до початку.
    edges = [_edge(names[i + 1], names[i]) for i in reversed(range(n - 1))]
    comps = components(_graph(nodes, edges))
    assert comps.count == 1
    assert comps.of(names[-1]) == names[0]
    assert len(comps.members(names[0])) == n


def test_module_has_no_recursion():
    """Статично: жодна функція модуля не викликає саму себе (ітеративність — вимога задачі)."""
    tree = ast.parse(inspect.getsource(components_module))
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for call in ast.walk(fn):
                if isinstance(call, ast.Call):
                    target = call.func
                    name = target.id if isinstance(target, ast.Name) else getattr(target, "attr", None)
                    assert name != fn.name, f"recursive call in {fn.name}"


def test_performance_300_buyers_20k_edges():
    """SC-007-масштаб: 300 покупців, ~20 000 ребер. Поріг щедрий (2 с при очікуваних десятках мс)."""
    rng = random.Random(42)
    buyers = [f"B{i:04d}" for i in range(300)]
    funders = [f"F{i:05d}" for i in range(6000)]
    nodes = [_buyer(a) for a in buyers] + [_node(a) for a in funders]
    everyone = buyers + funders
    keys = set()
    while len(keys) < 20_000:
        s, r = rng.sample(everyone, 2)
        keys.add((TRANSFER if rng.random() < 0.9 else DELEGATED, s, r))
    edges = [_edge(s, r, k) for k, s, r in sorted(keys)]
    graph = _graph(nodes, edges)
    start = time.perf_counter()
    comps = components(graph)
    elapsed = time.perf_counter() - start
    partition, buyer_counts = _oracle(graph)
    assert _partition(comps) == partition
    assert comps.buyers_by_component() == buyer_counts
    assert elapsed < 2.0, f"components() took {elapsed:.3f}s"
