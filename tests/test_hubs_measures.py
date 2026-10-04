# verifies: FR-002-07, FR-002-22
"""Виміри вершин графа фінансування (T-032; research R-7, R-8, R-22; contracts/graph-service.md §2a).

Критична задача: хибний вимір не ламає збірку, а тихо змінює, які вершини
відсікаються як хаби (і отже які кластери покупців бачить 003). Тому окрім
іменованих сценаріїв — перебір проти незалежного оракула на 200 випадкових
графах (усі шість полів), еталони з `expected.json` (обчислені незалежним
генератором фікстур), незалежність від порядку входу та межові випадки.

Вершини в `compute` передаються з **нульовими** вимірами-заглушками: так
реалізація, що просто повертає `node.measures`, не пройде еталонні тести.
"""

import ast
import json
import random
from collections import namedtuple
from pathlib import Path

import pytest

from unmask.graph import measures as measures_module
from unmask.graph.measures import compute
from unmask.graph.model import Edge, EdgeKind, EdgeRef, Node, NodeMeasures, NodeRole
from unmask.ingest.model import AddressType

BUYER = NodeRole.BUYER
FUNDER = NodeRole.FUNDER
PAYER = NodeRole.DELEGATED_PAYER
RECEIVER = NodeRole.DELEGATED_RECEIVER
TRANSFER = EdgeKind.TRANSFER
DELEGATED = EdgeKind.DELEGATED_BUY
SOL = "sol"
SPL_A = "spl:So11111111111111111111111111111111111111112"
SPL_B = "spl:EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

FIXTURES = Path(__file__).parent / "fixtures" / "graph"
SCENARIOS = sorted(p.name for p in FIXTURES.iterdir() if (p / "expected.json").is_file())

DUST = 500_000  # лампорти; лише дані тестів, поріг критерію тут не застосовується
REAL = 500_000_000

_ZERO = NodeMeasures(degree=0, unique_senders=0, one_off_senders=0, one_off_share=None,
                     buyer_fanout=0, median_to_buyers=None)


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
        measures=_ZERO,
    )


def _buyer(address):
    return _node(address, (BUYER,))


_SIG = iter(range(10**9))


def _transfer(sender, receiver, asset=SOL, amount=1, count=1):
    refs = tuple(EdgeRef(f"sig{next(_SIG)}", 10 + i, "0") for i in range(count))
    return Edge(
        kind=TRANSFER, sender=sender, receiver=receiver, asset=asset, amount=amount,
        decimals=None if asset == SOL else 6, count=count, first_slot=10,
        last_slot=10 + count - 1, first_time=None, last_time=None, refs=refs,
    )


def _delegated(sender, receiver, count=1):
    refs = tuple(EdgeRef(f"sig{next(_SIG)}", 10 + i, None) for i in range(count))
    return Edge(
        kind=DELEGATED, sender=sender, receiver=receiver, asset=None, amount=None,
        decimals=None, count=count, first_slot=10, last_slot=10 + count - 1,
        first_time=None, last_time=None, refs=refs,
    )


def _m(degree, unique, one_off, fanout=0, median=None):
    share = None if unique == 0 else one_off / unique
    return NodeMeasures(degree=degree, unique_senders=unique, one_off_senders=one_off,
                        one_off_share=share, buyer_fanout=fanout, median_to_buyers=median)


# --- Еталони з expected.json -------------------------------------------------------


def _expected(name):
    return json.loads((FIXTURES / name / "expected.json").read_text(encoding="utf-8"))


def _from_expected(name):
    """(nodes з нульовими вимірами, edges, {address: очікувані NodeMeasures}, wallets)."""
    data = _expected(name)
    nodes, want = [], {}
    for raw in data["graph"]["nodes"]:
        nodes.append(Node(
            address=raw["address"],
            roles=frozenset(NodeRole(r) for r in raw["roles"]),
            depth=raw["depth"],
            buyer_rank=raw["buyer_rank"],
            address_type=AddressType(raw["address_type"]),
            unexpanded=None,
            measures=_ZERO,
        ))
        want[raw["address"]] = NodeMeasures(**raw["measures"])
    edges = []
    for raw in data["graph"]["edges"]:
        edges.append(Edge(
            kind=EdgeKind(raw["kind"]), sender=raw["sender"], receiver=raw["receiver"],
            asset=raw["asset"], amount=raw["amount"], decimals=raw["decimals"],
            count=raw["count"], first_slot=raw["first_slot"], last_slot=raw["last_slot"],
            first_time=raw["first_time"], last_time=raw["last_time"],
            refs=tuple(EdgeRef(r["signature"], r["slot"], r["instruction_path"])
                       for r in raw["refs"]),
        ))
    return nodes, edges, want, data["wallets"]


@pytest.mark.parametrize("name", SCENARIOS)
def test_measures_equal_expected_on_every_scenario(name):
    nodes, edges, want, _ = _from_expected(name)
    got = compute(nodes, edges)
    assert set(got) == set(want)
    for address in want:
        assert got[address] == want[address], address


def test_scenarios_cover_all_eleven_fixtures():
    assert len(SCENARIOS) == 11


def test_g_hub_H_measures_equal_expected():
    nodes, edges, want, wallets = _from_expected("g_hub")
    got = compute(nodes, edges)
    hub = wallets["H"]
    assert got[hub] == want[hub]
    # Незалежна перевірка вигляду еталона: H — хаб за одноразовими відправниками.
    assert got[hub].unique_senders >= 10
    assert got[hub].one_off_share is not None and got[hub].one_off_share > 0.8


def test_g_dust_D_and_g_financier_R_measures_equal_expected():
    nodes, edges, want, wallets = _from_expected("g_dust")
    got = compute(nodes, edges)
    d = wallets["D"]
    assert got[d] == want[d]
    # 18 пилових ребер + одне на 5 SOL: верхня медіана — пил.
    assert got[d].buyer_fanout == 19
    assert got[d].median_to_buyers == 500_000

    nodes, edges, want, wallets = _from_expected("g_financier")
    got = compute(nodes, edges)
    r = wallets["R"]
    assert got[r] == want[r]
    assert got[r].buyer_fanout == 30
    assert got[r].median_to_buyers == 700_000_000


def test_g_dust_mixed_majority_rules_equal_expected():
    nodes, edges, want, wallets = _from_expected("g_dust_mixed")
    got = compute(nodes, edges)
    for label in ("V", "W", "X", "Y", "Z"):
        address = wallets[label]
        assert got[address] == want[address], label


# --- Незалежний оракул: перебір пар ------------------------------------------------


def _oracle(nodes, edges):
    """Виміри перебором пар вершин і повним скануванням списку ребер для кожної пари.

    Не ділить коду з `unmask.graph.measures`: жодних індексів суміжності, для кожної
    пари `(v, u)` ребра шукаються лінійно; медіана — через явний пошук елемента, у
    якого строго менших `< n//2 + 1` і не більших `>= n//2 + 1` (верхня медіана).
    """
    addresses = [n.address for n in nodes]
    buyers = [n.address for n in nodes if "buyer" in {str(r) for r in n.roles}]
    result = {}
    for v in addresses:
        degree = 0
        unique = 0
        one_off = 0
        for u in addresses:
            if u == v:
                continue
            linked = any((e.sender == u and e.receiver == v) or (e.sender == v and e.receiver == u)
                         for e in edges)
            if linked:
                degree += 1
            transfers_in = [e for e in edges
                            if e.kind == "transfer" and e.sender == u and e.receiver == v]
            if transfers_in:
                unique += 1
                if sum(e.count for e in transfers_in) == 1:
                    one_off += 1
        amounts = []
        for b in buyers:
            if b == v:
                continue
            to_b = [e for e in edges
                    if e.kind == "transfer" and e.asset == "sol" and e.sender == v and e.receiver == b]
            if to_b:
                amounts.append(sum(e.amount for e in to_b))
        median = None
        if amounts:
            need = len(amounts) // 2 + 1
            for x in amounts:
                below = sum(1 for y in amounts if y < x)
                not_above = sum(1 for y in amounts if y <= x)
                if below < need <= not_above:
                    median = x
                    break
        result[v] = NodeMeasures(
            degree=degree,
            unique_senders=unique,
            one_off_senders=one_off,
            one_off_share=None if unique == 0 else one_off / unique,
            buyer_fanout=len(amounts),
            median_to_buyers=median,
        )
    return result


_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _random_case(rng):
    n = rng.choice([0, 1, 2, 3, rng.randint(4, 10), rng.randint(11, 25)])
    used = set()
    addresses = []
    while len(addresses) < n:
        a = "".join(rng.choice(_ALPHABET) for _ in range(rng.randint(1, 5)))
        if a not in used:
            used.add(a)
            addresses.append(a)
    buyer_share = rng.choice([0.0, 0.3, 0.7, 1.0, rng.random()])
    nodes = []
    for a in addresses:
        if rng.random() < buyer_share:
            roles = rng.choice([(BUYER,), (BUYER, FUNDER), (BUYER, PAYER), (BUYER, RECEIVER)])
        else:
            roles = rng.choice([(FUNDER,), (PAYER,), (RECEIVER,), (FUNDER, PAYER)])
        nodes.append(_node(a, roles))
    # Суми з малого набору — щоб часто були рівні значення й межі медіани.
    pool = rng.choice([[1, 2, 3], [DUST, REAL], [DUST, DUST + 1, REAL, 5 * 10**9],
                       [1, 10**18, 2**62 + 1]])
    edges, keys = [], set()
    if n >= 2:
        target = int(rng.choice([0.0, 0.05, 0.2, 0.5, rng.random()]) * n * (n - 1))
        for _ in range(target):
            s, r = rng.sample(addresses, 2)
            kind = rng.choice([TRANSFER, TRANSFER, TRANSFER, DELEGATED])
            asset = None if kind is DELEGATED else rng.choice([SOL, SOL, SPL_A, SPL_B])
            key = (kind, s, r, asset)
            if key in keys:
                continue
            keys.add(key)
            count = rng.choice([1, 1, 1, 2, 3])
            if kind is DELEGATED:
                edges.append(_delegated(s, r, count))
            else:
                edges.append(_transfer(s, r, asset, rng.choice(pool), count))
    return nodes, edges


@pytest.mark.parametrize("name", SCENARIOS)
def test_oracle_itself_agrees_with_independent_expected(name):
    """Оракул тесту і генератор фікстур — дві незалежні реалізації; вони мають збігатись."""
    nodes, edges, want, _ = _from_expected(name)
    assert _oracle(nodes, edges) == want


def test_measures_match_brute_force_oracle_on_random_graphs():
    rng = random.Random(20261004)
    stats = {"fanout>=2": 0, "even_fanout": 0, "one_off_mixed": 0, "delegated": 0}
    for case in range(200):
        nodes, edges = _random_case(rng)
        got = compute(nodes, edges)
        want = _oracle(nodes, edges)
        assert got == want, f"case {case}"
        for m in got.values():
            stats["fanout>=2"] += m.buyer_fanout >= 2
            stats["even_fanout"] += m.buyer_fanout >= 2 and m.buyer_fanout % 2 == 0
            stats["one_off_mixed"] += 0 < m.one_off_senders < m.unique_senders
        stats["delegated"] += any(e.kind is DELEGATED for e in edges)
    # Перебір не вироджений: межові конфігурації справді траплялись.
    assert all(v >= 20 for v in stats.values()), stats


def test_result_does_not_depend_on_input_order():
    rng = random.Random(7)
    for _ in range(50):
        nodes, edges = _random_case(rng)
        base = compute(nodes, edges)
        for _ in range(3):
            n2, e2 = nodes[:], edges[:]
            rng.shuffle(n2)
            rng.shuffle(e2)
            assert compute(n2, e2) == base
            assert list(compute(n2, e2)) == list(base)  # навіть порядок ключів словника


# --- Іменовані тести T-032 ---------------------------------------------------------


def test_degree_counts_counterparty_once_across_assets_and_directions():
    nodes = [_node("v"), _node("u"), _node("w")]
    edges = [
        _transfer("u", "v", SOL), _transfer("u", "v", SPL_A), _transfer("v", "u", SOL),
        _delegated("u", "v"), _delegated("v", "u"),
    ]
    got = compute(nodes, edges)
    assert got["v"].degree == 1
    assert got["u"].degree == 1
    assert got["w"].degree == 0
    edges.append(_transfer("w", "v", SPL_B))
    assert compute(nodes, edges)["v"].degree == 2


def test_two_transfers_from_same_sender_make_it_not_one_off():
    nodes = [_node("v"), _node("a"), _node("b"), _node("c")]
    # count=2 по одному активу
    got = compute(nodes, [_transfer("a", "v", SOL, count=2), _transfer("c", "v", SOL)])
    assert got["v"] == _m(2, 2, 1)
    # count=1+1 по двох активах — теж не одноразовий (сума по всіх активах)
    got = compute(nodes, [_transfer("b", "v", SOL), _transfer("b", "v", SPL_A),
                          _transfer("c", "v", SOL)])
    assert got["v"] == _m(2, 2, 1)
    assert got["v"].one_off_share == 0.5


def test_share_is_none_without_senders_and_one_when_all_one_off():
    nodes = [_node("v"), _node("a"), _node("b"), _node("c")]
    got = compute(nodes, [_transfer("v", "a"), _delegated("b", "v")])
    assert got["v"].unique_senders == 0
    assert got["v"].one_off_share is None
    got = compute(nodes, [_transfer("a", "v"), _transfer("b", "v", SPL_A), _transfer("c", "v")])
    assert got["v"] == _m(3, 3, 3)
    assert got["v"].one_off_share == 1.0
    assert isinstance(got["v"].one_off_share, float)


def test_one_off_share_is_exact_ratio_of_integers():
    senders = [f"s{i}" for i in range(7)]
    nodes = [_node("v")] + [_node(s) for s in senders]
    edges = [_transfer(s, "v", count=1 if i < 3 else 2) for i, s in enumerate(senders)]
    got = compute(nodes, edges)["v"]
    assert (got.unique_senders, got.one_off_senders) == (7, 3)
    assert got.one_off_share == 3 / 7


def test_delegated_edges_count_toward_degree_but_not_senders_or_fanout():
    nodes = [_node("p"), _buyer("b1"), _buyer("b2")]
    edges = [_delegated("p", "b1"), _delegated("p", "b2", count=2), _delegated("b2", "b1")]
    got = compute(nodes, edges)
    assert got["p"] == _m(2, 0, 0)
    assert got["b1"] == _m(2, 0, 0)  # delegated_buy не робить відправника
    assert got["b2"] == _m(2, 0, 0)


def test_buyer_fanout_counts_distinct_buyers_over_sol_transfer_edges_only():
    nodes = [_node("v"), _buyer("b1"), _buyer("b2"), _node("f")]
    # два SOL-перекази одному покупцю -> 1
    got = compute(nodes, [_transfer("v", "b1", SOL, amount=7, count=2)])
    assert (got["v"].buyer_fanout, got["v"].median_to_buyers) == (1, 7)
    # SPL до покупця -> 0
    got = compute(nodes, [_transfer("v", "b1", SPL_A, amount=7)])
    assert (got["v"].buyer_fanout, got["v"].median_to_buyers) == (0, None)
    # SOL до не-покупця -> 0
    got = compute(nodes, [_transfer("v", "f", SOL, amount=7)])
    assert (got["v"].buyer_fanout, got["v"].median_to_buyers) == (0, None)
    # вхідне SOL-ребро від покупця не є fan-out вершини
    got = compute(nodes, [_transfer("b1", "v", SOL, amount=7)])
    assert got["v"].buyer_fanout == 0
    # SOL + SPL тому самому покупцю -> 1, медіана — лише SOL-сума
    got = compute(nodes, [_transfer("v", "b1", SOL, amount=7), _transfer("v", "b1", SPL_A, amount=9),
                          _transfer("v", "b2", SPL_A, amount=9)])
    assert (got["v"].buyer_fanout, got["v"].median_to_buyers) == (1, 7)


def test_buyer_with_several_roles_and_buyer_to_buyer_edges_count():
    nodes = [_node("b1", (BUYER, FUNDER)), _buyer("b2"), _node("b3", (BUYER, PAYER))]
    got = compute(nodes, [_transfer("b1", "b2", amount=3), _transfer("b1", "b3", amount=5)])
    assert (got["b1"].buyer_fanout, got["b1"].median_to_buyers) == (2, 5)


def _fanout_graph(amounts, counts=None):
    counts = counts or [1] * len(amounts)
    buyers = [f"b{i:02d}" for i in range(len(amounts))]
    nodes = [_node("v")] + [_buyer(b) for b in buyers]
    edges = [_transfer("v", b, SOL, amount=a, count=c) for b, a, c in zip(buyers, amounts, counts)]
    return nodes, edges


def test_median_is_upper_median_of_per_buyer_edge_sums():
    d, r = DUST, REAL
    # парне n: [d, d, r, r] -> r (верхня медіана; нижня дала б d)
    got = compute(*_fanout_graph([r, d, r, d]))["v"]
    assert (got.buyer_fanout, got.median_to_buyers) == (4, r)
    # непарне n: [d, d, r] -> d
    got = compute(*_fanout_graph([r, d, d]))["v"]
    assert (got.buyer_fanout, got.median_to_buyers) == (3, d)
    # один переказ 5 SOL серед 18 пилових -> пил
    got = compute(*_fanout_graph([d] * 18 + [5 * 10**9]))["v"]
    assert (got.buyer_fanout, got.median_to_buyers) == (19, d)
    # 20 дрібних доплат одному покупцю не перехиляють медіану — агрегат на ребрі:
    # 10 покупців по 0.5 SOL; одному з них ребро несе 21 переказ (сума 0.5 SOL + 20 пилинок).
    amounts = [r] * 10
    counts = [1] * 10
    amounts[0] = r + 20 * 1_000
    counts[0] = 21
    got = compute(*_fanout_graph(amounts, counts))["v"]
    assert (got.buyer_fanout, got.median_to_buyers) == (10, r)
    # 3 пилових + 3 справжніх (n=6) -> справжня; 4 + 3 (n=7) -> пил
    assert compute(*_fanout_graph([d, d, d, r, r, r]))["v"].median_to_buyers == r
    assert compute(*_fanout_graph([d, d, d, d, r, r, r]))["v"].median_to_buyers == d


def test_median_of_two_is_the_larger_and_of_one_is_itself():
    assert compute(*_fanout_graph([3, 9]))["v"].median_to_buyers == 9
    assert compute(*_fanout_graph([9, 3]))["v"].median_to_buyers == 9
    assert compute(*_fanout_graph([4]))["v"].median_to_buyers == 4


def test_median_stays_exact_integer_for_huge_amounts():
    big = 2**62 + 1  # за межами точності float
    got = compute(*_fanout_graph([big, big + 2, 1]))["v"]
    assert got.median_to_buyers == big
    assert isinstance(got.median_to_buyers, int)


def test_median_none_iff_zero_fanout():
    nodes = [_node("v"), _buyer("b"), _node("f")]
    for edges in ([], [_transfer("v", "f")], [_transfer("v", "b", SPL_A)], [_delegated("v", "b")],
                  [_transfer("b", "v")]):
        got = compute(nodes, edges)["v"]
        assert got.buyer_fanout == 0 and got.median_to_buyers is None
    got = compute(nodes, [_transfer("v", "b", amount=1)])["v"]
    assert got.buyer_fanout == 1 and got.median_to_buyers == 1


# --- Межові випадки й контракт входу -----------------------------------------------


def test_empty_input_gives_empty_mapping():
    assert compute([], []) == {}


def test_isolated_node_has_zero_measures():
    assert compute([_node("v")], []) == {"v": _ZERO}


def test_every_node_gets_measures_and_keys_are_sorted_addresses():
    nodes = [_node("c"), _buyer("a"), _node("b")]
    got = compute(nodes, [_transfer("c", "a")])
    assert list(got) == ["a", "b", "c"]
    assert all(isinstance(m, NodeMeasures) for m in got.values())


def test_accepts_one_shot_iterators():
    nodes = [_node("v"), _buyer("b"), _node("s")]
    edges = [_transfer("v", "b", amount=5), _transfer("s", "v")]
    assert compute(iter(nodes), iter(edges)) == compute(nodes, edges)


def test_accepts_role_carrying_nodes_without_measures():
    """`build_graph` (T-028) викликає `compute` до того, як вершини мають виміри."""
    Pending = namedtuple("Pending", "address roles")
    nodes = [Pending("v", frozenset({FUNDER})), Pending("b", frozenset({BUYER}))]
    got = compute(nodes, [_transfer("v", "b", amount=5)])
    assert got["v"] == _m(1, 0, 0, fanout=1, median=5)
    assert got["b"] == _m(1, 1, 1)


def test_edge_endpoint_not_among_nodes_fails_loudly():
    with pytest.raises(ValueError, match="not a node"):
        compute([_node("v")], [_transfer("v", "ghost")])
    with pytest.raises(ValueError, match="not a node"):
        compute([_node("v")], [_transfer("ghost", "v")])


def test_duplicate_node_address_fails_loudly():
    with pytest.raises(ValueError, match="duplicate"):
        compute([_node("v"), _buyer("v")], [])


def test_duplicate_edge_key_fails_loudly():
    nodes = [_node("v"), _buyer("b")]
    with pytest.raises(ValueError, match="duplicate"):
        compute(nodes, [_transfer("v", "b", amount=1), _transfer("v", "b", amount=2)])


def test_non_edge_input_is_rejected():
    with pytest.raises(TypeError):
        compute([_node("v")], [("transfer", "v", "b", "sol")])


def test_inputs_are_not_mutated():
    nodes, edges = _fanout_graph([3, 1, 2])
    nodes_before, edges_before = list(nodes), list(edges)
    compute(nodes, edges)
    assert nodes == nodes_before and edges == edges_before


# --- Межі модуля (принцип IV, plan «Правило залежностей») --------------------------


def test_measures_module_imports_only_graph_model_and_stdlib():
    tree = ast.parse(Path(measures_module.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative import"
            imported.add(node.module)
    unmask = {m for m in imported if m.split(".")[0] == "unmask"}
    assert unmask <= {"unmask.graph.model"}, unmask
