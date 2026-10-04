# verifies: FR-002-04
"""Детермінізм побудови графа (T-031, FR-002-04): той самий вхід -> побітово той самий граф.

Вхід `IngestResult` не нормалізує порядок своїх кортежів (`_tuple_of` лише перевіряє типи),
тож `dataclasses.replace` із переставленими кортежами справді подає `build_graph` інший
порядок. Результат не мусить від нього залежати.

Що доводять тести (і чим ловиться відповідна поломка):

- перестановки `buyers`/`transfers`/`unexpanded`/`missing` дають рівний граф і однаковий `repr`;
- порядок вершин і ребер дорівнює документованим ключам (ключі тут записані літерально,
  незалежно від `node_sort_key`/`edge_sort_key` моделі);
- порядок `refs` — `(slot, signature, шлях числово)`: "2" < "2.1" < "10";
- розрив нічиїх `first`/`last` — за тим самим ключем, а не за порядком входу чи `block_time`:
  перекази в ОДНОМУ слоті з різними `block_time` (у тому числі `None` на одному кінці),
  вхід подається в усіх перестановках, зокрема проти контрактного ключа. Без цього тесту
  `sorted(key=lambda pair: pair[0].slot)` (стабільне сортування -> порядок входу) проходив би
  всі решту тести, бо слот однозначно визначає ключ, коли слоти різні;
- `repr` і серіалізація графа однакові між запусками інтерпретатора з різним
  `PYTHONHASHSEED`: `frozenset` ролей вершини не має просочувати порядок хешів.
"""

import dataclasses
import hashlib
import itertools
import json
import os
import random
import re
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import GRAPH_FIXTURES, load_ingest_fixture
from unmask.graph.build import build_graph
from unmask.graph.model import (
    AddressType,
    FundingGraph,
    GraphCompleteness,
    Node,
    NodeMeasures,
    NodeRole,
)
from unmask.ingest.model import (
    Completeness,
    MissingHistory,
    MissingReason,
    Transfer,
)

SCENARIOS = sorted(p.name for p in GRAPH_FIXTURES.iterdir() if (p / "ingest.json").is_file())
SHUFFLED_FIXTURES = ("g_basic", "g_hub", "g_unexpanded")
PERMUTATIONS = 20
ROOT = Path(__file__).resolve().parent.parent

_BASIC = json.loads((GRAPH_FIXTURES / "g_basic" / "expected.json").read_text(encoding="utf-8"))
W = _BASIC["wallets"]


# --- Допоміжне -------------------------------------------------------------------------


def _t(sig, slot, *, path="0", block_time=None, sender="A", receiver="P1", amount=1) -> Transfer:
    return Transfer(
        signature=sig, slot=slot, block_time=block_time, instruction_path=path,
        sender=W[sender], receiver=W[receiver], asset="sol", amount=amount, decimals=None, depth=1,
    )


def _result(*transfers):
    """Результат збору g_basic (ті самі покупці й метадані) з підміненими переказами."""
    return dataclasses.replace(load_ingest_fixture("g_basic"), transfers=tuple(transfers))


def _shuffled_tuple(items, rng):
    items = list(items)
    rng.shuffle(items)
    return tuple(items)


def _shuffled(result, seed):
    rng = random.Random(seed)
    return dataclasses.replace(
        result,
        buyers=_shuffled_tuple(result.buyers, rng),
        transfers=_shuffled_tuple(result.transfers, rng),
        unexpanded=_shuffled_tuple(result.unexpanded, rng),
    )


def _with_missing_in_order(result, entries):
    """Копія результату з `completeness.missing` у заданому порядку, в обхід сортування 001."""
    completeness = Completeness.derive(entries, result.completeness.buyers)
    object.__setattr__(completeness, "missing", tuple(entries))
    return dataclasses.replace(result, completeness=completeness)


def _canonical_json(graph: FundingGraph) -> str:
    """Серіалізація графа для порівняння: ролі впорядковані явно, решта — у порядку графа."""
    def conv(obj):
        if isinstance(obj, frozenset):
            return sorted(str(x) for x in obj)
        raise TypeError(type(obj))

    return json.dumps(dataclasses.asdict(graph), default=conv, sort_keys=False)


def _digest(graph: FundingGraph) -> tuple[str, str]:
    return (
        hashlib.sha256(repr(graph).encode()).hexdigest(),
        hashlib.sha256(_canonical_json(graph).encode()).hexdigest(),
    )


# --- Перестановки входу -----------------------------------------------------------------


@pytest.mark.parametrize("seed", range(PERMUTATIONS))
@pytest.mark.parametrize("name", SHUFFLED_FIXTURES)
def test_shuffled_input_tuples_give_identical_graph(name, seed):
    original = load_ingest_fixture(name)
    shuffled = _shuffled(original, seed)
    # Перестановка справді змінила вхід (інакше тест порожній): перекази g_* — 10…56 штук.
    assert shuffled.transfers != original.transfers
    baseline = build_graph(original)
    graph = build_graph(shuffled)
    assert graph == baseline
    assert graph.nodes == baseline.nodes and graph.edges == baseline.edges
    assert repr(graph) == repr(baseline)
    assert _canonical_json(graph) == _canonical_json(baseline)


@pytest.mark.parametrize("name", SCENARIOS)
def test_reversed_input_gives_identical_graph_for_every_fixture(name):
    original = load_ingest_fixture(name)
    reverse = dataclasses.replace(
        original,
        buyers=original.buyers[::-1], transfers=original.transfers[::-1],
        unexpanded=original.unexpanded[::-1],
    )
    assert repr(build_graph(reverse)) == repr(build_graph(original))


def test_missing_order_in_input_does_not_change_graph_completeness_or_graph():
    base = load_ingest_fixture("g_incomplete")
    entries = [
        MissingHistory(W["A"], 1, MissingReason.TIMEOUT, "t"),
        MissingHistory(W["P1"], 0, MissingReason.RATE_LIMITED, "r"),
        MissingHistory(W["P1"], 0, MissingReason.TIMEOUT, "t2"),
        MissingHistory(W["P2"], 2, MissingReason.UNAVAILABLE, ""),
    ]
    results = [_with_missing_in_order(base, list(p)) for p in itertools.permutations(entries)]
    assert len({r.completeness.missing for r in results}) == len(results), "перестановки не різні"
    expected = GraphCompleteness.derive(results[0])
    assert [(m.depth, m.wallet, m.reason.value) for m in expected.missing] == [
        (0, W["P1"], "rate_limited"), (0, W["P1"], "timeout"),
        (1, W["A"], "timeout"), (2, W["P2"], "unavailable"),
    ]
    graph = build_graph(results[0])
    for r in results:
        assert GraphCompleteness.derive(r) == expected
        assert build_graph(r) == graph


# --- Повторюваність ---------------------------------------------------------------------


@pytest.mark.parametrize("name", SCENARIOS)
def test_build_twice_is_equal_and_repr_identical(name):
    # Два незалежні розбори однієї фікстури: рівність не через ідентичність обʼєктів.
    first = build_graph(load_ingest_fixture(name))
    second = build_graph(load_ingest_fixture(name))
    assert first == second and first is not second
    assert repr(first) == repr(second)
    assert _canonical_json(first) == _canonical_json(second)
    assert first.nodes == second.nodes and first.edges == second.edges


# --- Документований порядок -------------------------------------------------------------


@pytest.mark.parametrize("name", SCENARIOS)
def test_edge_and_node_order_follow_documented_keys(name):
    original = load_ingest_fixture(name)
    # Вхід навмисно розвернутий: порядок графа не може бути порядком входу.
    graph = build_graph(dataclasses.replace(
        original, buyers=original.buyers[::-1], transfers=original.transfers[::-1],
    ))
    node_key = lambda n: n.address
    edge_key = lambda e: (e.kind.value, e.sender, e.receiver, e.asset or "")
    assert list(graph.nodes) == sorted(graph.nodes, key=node_key)
    assert list(graph.edges) == sorted(graph.edges, key=edge_key)
    assert len({node_key(n) for n in graph.nodes}) == len(graph.nodes)
    assert len({edge_key(e) for e in graph.edges}) == len(graph.edges)


def test_edge_order_is_not_the_order_of_first_appearance_in_input():
    # Не тавтологія: на g_hub порядок першої появи пари у вході відрізняється від ключа.
    original = load_ingest_fixture("g_hub")
    appearance = list(dict.fromkeys((t.sender, t.receiver) for t in original.transfers))
    graph = build_graph(original)
    assert [(e.sender, e.receiver) for e in graph.edges if e.asset == "sol"] != appearance
    assert appearance != sorted(appearance)


def test_refs_order_is_slot_signature_path_numeric():
    # Один слот і одна підпис: шляхи "10", "2.1", "2" -> числово "2" < "2.1" < "10".
    # Різні слоти 9 і 10: слот числовий, а не рядковий. Підписи в слоті 10 — лексично.
    graph = build_graph(_result(
        _t("sigM", 10, path="10"),
        _t("sigM", 10, path="2.1"),
        _t("sigB", 10, path="3"),
        _t("sigM", 10, path="2"),
        _t("sigZ", 9, path="0"),
    ))
    (edge,) = graph.edges
    assert [(r.signature, r.slot, r.instruction_path) for r in edge.refs] == [
        ("sigZ", 9, "0"),
        ("sigB", 10, "3"),
        ("sigM", 10, "2"),
        ("sigM", 10, "2.1"),
        ("sigM", 10, "10"),
    ]


# --- Розрив нічиїх first/last: слот, підпис, шлях ---------------------------------------

# Кожен сценарій: перекази (у порядку, який перебирається у всіх перестановках), очікувані
# (first_slot, last_slot, first_time, last_time) за контрактним ключем (slot, signature, path).
_TIE_SCENARIOS = {
    # Один слот, різні підписи: "sA" < "sB"; перший має block_time None.
    "same_slot_none_on_first": (
        [_t("sB", 100, block_time=2_000), _t("sA", 100, block_time=None)],
        (100, 100, None, 2_000),
    ),
    # Те саме, None на другому кінці.
    "same_slot_none_on_last": (
        [_t("sB", 100, block_time=None), _t("sA", 100, block_time=2_000)],
        (100, 100, 2_000, None),
    ),
    # Ключ іде проти block_time: першим за підписом стоїть найпізніший час.
    "same_slot_time_against_signature": (
        [_t("sB", 100, block_time=1), _t("sA", 100, block_time=9)],
        (100, 100, 9, 1),
    ),
    # Одна підпис, один слот, шляхи числово: "2" < "2.1" < "10", час проти шляху.
    "same_signature_numeric_paths": (
        [
            _t("sX", 100, path="10", block_time=3_000),
            _t("sX", 100, path="2", block_time=1_000),
            _t("sX", 100, path="2.1", block_time=None),
        ],
        (100, 100, 1_000, 3_000),
    ),
    # Нічия лише в останньому слоті: найменший слот один, у найбільшому слоті розрив за підписом.
    "tie_only_in_last_slot": (
        [
            _t("sQ", 100, block_time=500),
            _t("sZ", 200, block_time=None),
            _t("sY", 200, block_time=3_000),
        ],
        (100, 200, 500, None),
    ),
    # Нічия лише в першому слоті.
    "tie_only_in_first_slot": (
        [
            _t("sK", 100, block_time=7_000),
            _t("sJ", 100, block_time=None),
            _t("sM", 300, block_time=4_000),
        ],
        (100, 300, None, 4_000),
    ),
    # Нічия й за слотом, і за підписом: розриває лише числовий шлях; решта — інший слот.
    "tie_by_slot_and_signature_then_path": (
        [
            _t("sP", 50, path="1.10", block_time=10),
            _t("sP", 50, path="1.9", block_time=20),
            _t("sA", 60, path="0", block_time=30),
        ],
        (50, 60, 20, 30),
    ),
}


@pytest.mark.parametrize("scenario", sorted(_TIE_SCENARIOS))
def test_first_last_tie_break_follows_slot_signature_path_for_every_input_order(scenario):
    transfers, expected = _TIE_SCENARIOS[scenario]
    for order in itertools.permutations(transfers):
        (edge,) = build_graph(_result(*order)).edges
        assert (edge.first_slot, edge.last_slot, edge.first_time, edge.last_time) == expected, (
            f"input order {[(t.signature, t.instruction_path) for t in order]}"
        )
        assert edge.count == len(transfers)
        assert edge.amount == len(transfers)


@pytest.mark.parametrize("scenario", sorted(_TIE_SCENARIOS))
def test_refs_follow_the_same_key_as_first_last_in_tie_scenarios(scenario):
    transfers, _ = _TIE_SCENARIOS[scenario]
    expected = [
        (t.signature, t.slot, t.instruction_path)
        for t in sorted(
            transfers,
            key=lambda t: (t.slot, t.signature, tuple(int(x) for x in t.instruction_path.split("."))),
        )
    ]
    for order in itertools.permutations(transfers):
        (edge,) = build_graph(_result(*order)).edges
        assert [(r.signature, r.slot, r.instruction_path) for r in edge.refs] == expected


# --- Між запусками інтерпретатора ---------------------------------------------------------

_SUBPROCESS_SCRIPT = r"""
import dataclasses, hashlib, json, sys
from pathlib import Path
from unmask.graph.build import build_graph
from unmask.ingest.serialize import from_dict

def conv(obj):
    if isinstance(obj, frozenset):
        return sorted(str(x) for x in obj)
    raise TypeError(type(obj))

out = {}
for path in sorted(Path(sys.argv[1]).glob("*/ingest.json")):
    graph = build_graph(from_dict(json.loads(path.read_text(encoding="utf-8"))))
    out[path.parent.name] = [
        hashlib.sha256(repr(graph).encode()).hexdigest(),
        hashlib.sha256(json.dumps(dataclasses.asdict(graph), default=conv).encode()).hexdigest(),
    ]
print(json.dumps(out, sort_keys=True))
"""

HASH_SEEDS = ("0", "1", "2", "3", "4")


def _run_with_hashseed(seed: str) -> dict:
    env = dict(os.environ, PYTHONHASHSEED=seed, PYTHONPATH=str(ROOT / "src"), PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run(
        [sys.executable, "-c", _SUBPROCESS_SCRIPT, str(GRAPH_FIXTURES)],
        env=env, capture_output=True, text=True, check=False, timeout=120, cwd=ROOT,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_repr_and_serialization_do_not_depend_on_pythonhashseed():
    runs = {seed: _run_with_hashseed(seed) for seed in HASH_SEEDS}
    reference = runs[HASH_SEEDS[0]]
    assert set(reference) == set(SCENARIOS)
    for seed, run in runs.items():
        assert run == reference, f"graph repr/serialization differs for PYTHONHASHSEED={seed}"
    # Між запусками й усередині цього процесу (його сід довільний) — теж те саме.
    for name in SCENARIOS:
        assert list(_digest(build_graph(load_ingest_fixture(name)))) == reference[name]


def test_node_repr_lists_roles_in_sorted_order_whatever_the_hash_seed():
    roles = frozenset(NodeRole)
    node = Node(
        address=W["P1"], roles=roles, depth=0, buyer_rank=1, address_type=AddressType.WALLET,
        unexpanded=None,
        measures=NodeMeasures(
            degree=0, unique_senders=0, one_off_senders=0, one_off_share=None,
            buyer_fanout=0, median_to_buyers=None,
        ),
    )
    shown = re.findall(r"NodeRole\.(\w+)", repr(node))
    assert shown == [r.name for r in sorted(NodeRole)]
    assert len(shown) == len(NodeRole) == 4


def test_no_set_or_dict_order_leaks_into_graph_tuples():
    # Усі колекції графа — кортежі; єдина множина (ролі) — frozenset, що не входить у порядок.
    graph = build_graph(load_ingest_fixture("g_hub"))
    assert isinstance(graph.nodes, tuple) and isinstance(graph.edges, tuple)
    assert all(isinstance(e.refs, tuple) for e in graph.edges)
    assert all(isinstance(n.roles, frozenset) for n in graph.nodes)
