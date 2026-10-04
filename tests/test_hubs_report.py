# verifies: FR-002-11
"""Звіт ефекту відсікання хабів (T-036; FR-002-11, SC-001, SC-009; research R-10, R-16, R-22;
contracts/graph-service.md §6; data-model «EffectSnapshot», «EffectReport»).

Критична задача: помилка тут не ламає збірку, а тихо змінює висновок — хибна частка найбільшої компоненти або
пропущене `giant_component` показує «розсипаний» граф там, де кластеризація дасть одну грудку (принцип V: звіт
ніколи не каже «чисто» і не мовчить про концентрацію). Тому, крім щасливого шляху:

- еталони незалежні від коду: розділ `report` у `expected.json` усіх 11 сценаріїв (оракул генератора — BFS) і
  власний BFS-оракул цього тесту над тими самими графами; порівняння — повна рівність поле за полем;
- межі: частка рівно на порозі (строго більше — `>`→`>=` червоний на 5/10), найбільша компонента — за кількістю
  ПОКУПЦІВ, а не вершин (50 джерел і 1 покупець проти 3 покупців), знаменник — усі покупці, включно з
  ізольованими, обидва види ребер з'єднують компоненти;
- golden: поріг попередження береться з конфігу (зафіксований `config/hubs.yaml` v2 — 0.5, принцип III);
- інваріанти типів звіту гучні; `after` не підграф `before` чи втрачений покупець — гучно, а не тихо;
- у звіті немає поля «ok»/«clean».

Попередження `address_lists_not_applied` (T-037), `empty_graph` і `all_sources_pruned` (T-038) у цій задачі не
перевіряються: звірка з `expected.json` порівнює решту попереджень повністю, а ці три — поза обсягом T-036.
"""

import ast
import copy
import dataclasses
import json
from collections import deque
from pathlib import Path
from types import MappingProxyType

import pytest

from conftest import GRAPH_FIXTURES, load_ingest_fixture
from unmask.graph.build import build_graph
from unmask.graph.model import (
    Edge,
    EdgeKind,
    EdgeRef,
    FundingGraph,
    GraphCompleteness,
    GraphWarning,
    Node,
    NodeMeasures,
    NodeRole,
)
from unmask.hubs import report as report_module
from unmask.hubs.config import ADDRESS_CATEGORIES, AddressLists, HubConfig, HubThresholds, load_hub_config
from unmask.hubs.prune import prune_hubs
from unmask.hubs.report import EffectReport, EffectSnapshot, effect_report
from unmask.ingest.model import AddressType

ROOT = Path(__file__).resolve().parents[1]
SHIPPED_HUBS = ROOT / "config" / "hubs.yaml"
SHIPPED_LISTS = ROOT / "config" / "hub_addresses.yaml"

# Зафіксований набір із 11 сценаріїв (а не «усе, що лежить у каталозі»): нові сценарії додаються сюди свідомо.
SCENARIOS = ["g_all_hubs", "g_basic", "g_buyer_hub", "g_dust", "g_dust_mixed", "g_empty",
             "g_financier", "g_hub", "g_incomplete", "g_known", "g_unexpanded"]

# Попередження, що з'являються в T-037/T-038 (поза обсягом T-036).
DEFERRED = {"address_lists_not_applied", "empty_graph", "all_sources_pruned"}

BUYER, FUNDER = NodeRole.BUYER, NodeRole.FUNDER
GIANT, DELEGATED_INCOMPLETE = GraphWarning.GIANT_COMPONENT, GraphWarning.DELEGATED_INCOMPLETE


# --- Конфіг і сценарії -------------------------------------------------------------------


def _lists(version: int = 1, **categories) -> AddressLists:
    parsed = {name: tuple(categories.get(name, ())) for name in ADDRESS_CATEGORIES}
    index = {address: name for name in ADDRESS_CATEGORIES for address in parsed[name]}
    return AddressLists(version=version, categories=MappingProxyType(parsed), index=MappingProxyType(index))


def _config(*, lists: AddressLists | None = None, **changes) -> HubConfig:
    kw = dict(version=2, degree_threshold=100, one_off_senders_share=0.8, one_off_min_senders=10,
              giant_component_warn_share=0.5, prune_off_curve=True, prune_ingest_high_degree=True,
              dust_amount_lamports=1_000_000, dust_min_fanout=5)
    kw.update(changes)
    return HubConfig(thresholds=HubThresholds(**kw), lists=lists, thresholds_digest="0" * 64,
                     lists_digest=None if lists is None else "1" * 64)


def _expected(name: str) -> dict:
    return json.loads((Path(GRAPH_FIXTURES) / name / "expected.json").read_text(encoding="utf-8"))


def _scenario(name: str):
    """(expected, before, outcome, config, delegated_complete) сценарію з конфігом рівно з `expected.config`."""
    expected = _expected(name)
    result = load_ingest_fixture(name)
    lists = expected["config"]["lists"]
    config = _config(lists=_lists(lists["version"], **lists["categories"]) if lists is not None else None,
                     version=expected["config"]["version"], **expected["config"]["thresholds"])
    before = build_graph(result)
    outcome = prune_hubs(before, config, ingest_counterparty_threshold=result.metadata.counterparty_threshold)
    return expected, before, outcome, config, GraphCompleteness.derive(result).delegated_complete


def _report(name: str) -> tuple[dict, FundingGraph, FundingGraph, EffectReport]:
    expected, before, outcome, config, delegated_complete = _scenario(name)
    report = effect_report(before, outcome.graph, config, lists_applied=outcome.lists_applied,
                           delegated_complete=delegated_complete)
    return expected, before, outcome.graph, report


# --- Відображення в форму expected.json (поле за полем, без asdict) ----------------------


def _snap_dict(s: EffectSnapshot) -> dict:
    return {"nodes": s.nodes, "edges": s.edges, "components": s.components, "buyers_total": s.buyers_total,
            "buyers_in_largest_component": s.buyers_in_largest_component,
            "largest_component_buyer_share": s.largest_component_buyer_share,
            "isolated_buyers": s.isolated_buyers}


def _report_dict(r: EffectReport) -> dict:
    return {"before": _snap_dict(r.before), "after": _snap_dict(r.after), "pruned_nodes": r.pruned_nodes,
            "pruned_edges": r.pruned_edges, "warn_share": r.warn_share,
            "warnings": [w.value for w in r.warnings]}


# --- Незалежний оракул: BFS (не ділить коду з unmask.graph.components) --------------------


def _oracle_snapshot(graph: FundingGraph) -> dict:
    adjacency = {n.address: set() for n in graph.nodes}
    for e in graph.edges:  # обидва види ребер, напрямок ігнорується
        adjacency[e.sender].add(e.receiver)
        adjacency[e.receiver].add(e.sender)
    buyers = {n.address for n in graph.nodes if BUYER in n.roles}
    seen, groups = set(), []
    for start in adjacency:
        if start in seen:
            continue
        seen.add(start)
        group, queue = {start}, deque([start])
        while queue:
            for nxt in adjacency[queue.popleft()]:
                if nxt not in seen:
                    seen.add(nxt)
                    group.add(nxt)
                    queue.append(nxt)
        groups.append(group)
    best = max((len(g & buyers) for g in groups), default=0)
    return {"nodes": len(adjacency), "edges": len(graph.edges), "components": len(groups),
            "buyers_total": len(buyers), "buyers_in_largest_component": best,
            "largest_component_buyer_share": best / len(buyers) if buyers else 0.0,
            "isolated_buyers": sum(1 for b in buyers if not adjacency[b])}


# --- Будівельники графів у пам'яті --------------------------------------------------------

_QUIET = NodeMeasures(degree=0, unique_senders=0, one_off_senders=0, one_off_share=None,
                      buyer_fanout=0, median_to_buyers=None)
_SIG = iter(range(10**9))


def _node(address: str, *roles: NodeRole, rank: int | None = None) -> Node:
    roles = frozenset(roles or (FUNDER,))
    is_buyer = BUYER in roles
    return Node(address=address, roles=roles, depth=0 if is_buyer else 1,
                buyer_rank=(rank or 1) if is_buyer else None, address_type=AddressType.WALLET,
                unexpanded=None, measures=_QUIET)


def _buyer(address: str, rank: int = 1) -> Node:
    return _node(address, BUYER, rank=rank)


def _edge(sender: str, receiver: str, kind: EdgeKind = EdgeKind.TRANSFER) -> Edge:
    sig = f"sig{next(_SIG)}"
    if kind is EdgeKind.TRANSFER:
        return Edge(kind=kind, sender=sender, receiver=receiver, asset="sol", amount=1, decimals=None, count=1,
                    first_slot=1, last_slot=1, first_time=None, last_time=None, refs=(EdgeRef(sig, 1, "0"),))
    return Edge(kind=kind, sender=sender, receiver=receiver, asset=None, amount=None, decimals=None, count=1,
                first_slot=1, last_slot=1, first_time=None, last_time=None, refs=(EdgeRef(sig, 1, None),))


def _graph(nodes, edges=()) -> FundingGraph:
    return FundingGraph(nodes=tuple(nodes), edges=tuple(edges))


def _fan(k: int, total: int) -> FundingGraph:
    """`total` покупців; фінансист F фінансує перших `k`, решта ізольовані."""
    buyers = [_buyer(f"B{i:02d}", rank=i + 1) for i in range(total)]
    return _graph([_node("F"), *buyers], [_edge("F", b.address) for b in buyers[:k]])


def _same(graph: FundingGraph, config: HubConfig | None = None, *, delegated_complete: bool = True,
          lists_applied: bool = True) -> EffectReport:
    """Звіт без відсікання (`after` — той самий граф)."""
    return effect_report(graph, graph, config or _config(), lists_applied=lists_applied,
                         delegated_complete=delegated_complete)


# --- Звірка з незалежними еталонами --------------------------------------------------------


def test_fixture_set_is_present():
    for name in SCENARIOS:
        assert (Path(GRAPH_FIXTURES) / name / "expected.json").is_file(), name


@pytest.mark.parametrize("name", SCENARIOS)
def test_snapshots_match_bfs_oracle_from_expected_json(name):
    expected, before, after, report = _report(name)
    assert _snap_dict(report.before) == _oracle_snapshot(before) == expected["report"]["before"]
    assert _snap_dict(report.after) == _oracle_snapshot(after) == expected["report"]["after"]


@pytest.mark.parametrize("name", SCENARIOS)
def test_report_equals_expected_json_report_section(name):
    expected, _, _, report = _report(name)
    got, want = _report_dict(report), dict(expected["report"])
    # Попередження T-037/T-038 — поза обсягом; решта попереджень — повна рівність, разом із порядком.
    assert [w for w in got.pop("warnings") if w not in DEFERRED] == \
        [w for w in want.pop("warnings") if w not in DEFERRED]
    assert got == want


@pytest.mark.parametrize("name", SCENARIOS)
def test_pruned_counts_equal_differences(name):
    expected, before, outcome, config, delegated_complete = _scenario(name)
    report = effect_report(before, outcome.graph, config, lists_applied=outcome.lists_applied,
                           delegated_complete=delegated_complete)
    assert report.pruned_nodes == report.before.nodes - report.after.nodes == len(outcome.records)
    removed = {e.key for r in outcome.records for e in r.incident_edges}  # ребро між хабами — в обох записах
    assert report.pruned_edges == report.before.edges - report.after.edges == len(removed)
    assert report.before.buyers_total == report.after.buyers_total  # покупці не відсікаються (SC-003)


# --- SC-001, SC-009, фінансист ------------------------------------------------------------


def test_g_hub_before_share_above_0_9_after_below_warn_threshold():
    _, _, _, report = _report("g_hub")
    assert report.before.components == 1 and report.before.largest_component_buyer_share > 0.9
    assert report.after.largest_component_buyer_share < report.warn_share == 0.5
    assert (report.after.buyers_in_largest_component, report.after.buyers_total) == (3, 20)
    assert report.after.isolated_buyers == 17
    assert GIANT not in report.warnings


def test_g_dust_before_share_above_0_9_after_below_warn_threshold():
    # SC-009: пилове джерело склеювало покупців так само, як хаб.
    _, _, _, report = _report("g_dust")
    assert report.before.components == 1 and report.before.largest_component_buyer_share > 0.9
    assert report.after.largest_component_buyer_share < report.warn_share
    assert report.pruned_nodes == 1
    assert GIANT not in report.warnings


def test_g_financier_report_before_equals_after_and_warns_giant_component_honestly():
    # Жодного відсікання; звіт не мовчить про концентрацію; R-22: нових попереджень критерій не додає.
    _, _, _, report = _report("g_financier")
    assert report.before == report.after
    assert (report.pruned_nodes, report.pruned_edges) == (0, 0)
    assert report.after.largest_component_buyer_share == 1.0
    assert report.warnings == (GIANT,)


def test_g_dust_mixed_still_warns_after_pruning():
    # Відсікання відбулося, але покупці лишились в одній компоненті — попередження, а не «чисто».
    _, _, _, report = _report("g_dust_mixed")
    assert report.pruned_nodes == 2 and report.after.largest_component_buyer_share == 1.0
    assert GIANT in report.warnings


# --- Правила обчислення --------------------------------------------------------------------


def test_largest_component_is_by_buyer_count_not_node_count():
    # Компонента 1: 50 джерел -> S -> B0 (52 вершини, 1 покупець). Компонента 2: F -> B1, B2, B3 (3 покупці).
    sources = [_node(f"S{i:02d}") for i in range(50)]
    buyers = [_buyer(f"B{i}", rank=i + 1) for i in range(4)]
    edges = [_edge(s.address, "S") for s in sources] + [_edge("S", "B0")] + \
        [_edge("F", f"B{i}") for i in (1, 2, 3)]
    graph = _graph([*sources, _node("S"), _node("F"), *buyers], edges)
    snap = _same(graph).before
    assert snap.components == 2
    assert snap.buyers_in_largest_component == 3
    assert snap.largest_component_buyer_share == 0.75
    assert GIANT in _same(graph).warnings  # 0.75 > 0.5; за кількістю вершин було б 1/4 — без попередження


def test_share_denominator_is_all_buyers_including_isolated():
    # F -> B00, B01; B02..B04 ізольовані. Частка 2/5, а не 2/2.
    graph = _fan(2, 5)
    snap = _same(graph).before
    assert (snap.buyers_total, snap.buyers_in_largest_component, snap.isolated_buyers) == (5, 2, 3)
    assert snap.largest_component_buyer_share == 0.4
    assert snap.components == 4
    assert GIANT not in _same(graph).warnings


@pytest.mark.parametrize("k, warns", [(4, False), (5, False), (6, True)], ids=["4/10", "5/10", "6/10"])
def test_giant_component_warning_below_at_and_above_warn_share(k, warns):
    report = _same(_fan(k, 10), _config(giant_component_warn_share=0.5))
    assert report.after.largest_component_buyer_share == k / 10
    assert (GIANT in report.warnings) is warns


@pytest.mark.parametrize("warn_share, warns", [(0.3, False), (0.29, True), (1.0, False)])
def test_giant_component_threshold_comes_from_config_and_is_strict(warn_share, warns):
    # 3/10 рівно на порозі 0.3 — без попередження (строго більше); поріг 1.0 — ніколи.
    graph = _fan(3, 10) if warn_share != 1.0 else _fan(10, 10)
    report = _same(graph, _config(giant_component_warn_share=warn_share))
    assert report.warn_share == warn_share
    assert (GIANT in report.warnings) is warns


def test_giant_component_judged_on_after_not_before():
    before = _fan(10, 10)
    after = before.without(["F"])
    report = effect_report(before, after, _config(), lists_applied=True, delegated_complete=True)
    assert report.before.largest_component_buyer_share == 1.0
    assert report.after.largest_component_buyer_share == 0.1
    assert GIANT not in report.warnings
    # і навпаки: «до» без концентрації не скасовує попередження «після» (те саме «після» — тут без відсікання)
    assert GIANT in _same(before).warnings


def test_delegated_buy_edges_join_components():
    # R-10: компоненти — по ребрах обох видів. Платник P -> B0 (делегована купівля), P -> B1 (переказ).
    graph = _graph([_node("P", NodeRole.DELEGATED_PAYER, FUNDER), _buyer("B0", 1), _buyer("B1", 2)],
                   [_edge("P", "B0", EdgeKind.DELEGATED_BUY), _edge("P", "B1")])
    snap = _same(graph).before
    assert (snap.components, snap.buyers_in_largest_component, snap.isolated_buyers) == (1, 2, 0)


def test_isolated_buyer_counts_only_buyers_without_any_edge():
    # Покупець-джерело B0 -> B1 (обидва не ізольовані); ізольований не-покупець X не рахується.
    graph = _graph([_buyer("B0", 1), _buyer("B1", 2), _buyer("B2", 3), _node("X")], [_edge("B0", "B1")])
    snap = _same(graph).before
    assert (snap.isolated_buyers, snap.components, snap.buyers_in_largest_component) == (1, 3, 2)


def test_zero_buyers_share_is_zero_without_division():
    graph = _graph([_node("A"), _node("C")], [_edge("A", "C")])
    snap = _same(graph).before
    assert (snap.buyers_total, snap.buyers_in_largest_component, snap.largest_component_buyer_share) == (0, 0, 0.0)
    assert GIANT not in _same(graph).warnings
    empty = _same(_graph([])).before
    assert _snap_dict(empty) == _oracle_snapshot(_graph([]))


# --- Попередження ---------------------------------------------------------------------------


@pytest.mark.parametrize("delegated_complete", [True, False])
def test_delegated_incomplete_warning_iff_not_delegated_complete(delegated_complete):
    report = _same(_fan(2, 10), delegated_complete=delegated_complete)
    assert (DELEGATED_INCOMPLETE in report.warnings) is (not delegated_complete)
    assert GIANT not in report.warnings


def test_warnings_are_sorted_unique_and_combine_independently():
    report = _same(_fan(10, 10), delegated_complete=False)
    assert report.warnings == (DELEGATED_INCOMPLETE, GIANT)  # порядок — за рядковим значенням
    assert all(isinstance(w, GraphWarning) for w in report.warnings)
    _, _, _, fin = _report("g_financier")
    _, before, outcome, config, _ = _scenario("g_financier")
    fin_incomplete = effect_report(before, outcome.graph, config, lists_applied=True, delegated_complete=False)
    assert fin_incomplete.warnings == (DELEGATED_INCOMPLETE, GIANT)
    assert fin_incomplete.before == fin.before and fin_incomplete.after == fin.after


def test_report_has_no_clean_flag_field():
    names = {f.name for f in dataclasses.fields(EffectReport)}
    assert not names & {"ok", "clean", "is_clean", "is_ok", "status", "verdict"}
    assert names == {"before", "after", "pruned_nodes", "pruned_edges", "warn_share", "warnings"}
    assert {f.name for f in dataclasses.fields(EffectSnapshot)} == {
        "nodes", "edges", "components", "buyers_total", "buyers_in_largest_component",
        "largest_component_buyer_share", "isolated_buyers"}


# --- Golden проти зафіксованого конфігу (принцип III) ---------------------------------------


def test_golden_warn_share_from_shipped_config():
    shipped = load_hub_config(SHIPPED_HUBS, SHIPPED_LISTS)
    assert shipped.thresholds.giant_component_warn_share == 0.5
    for name in SCENARIOS:
        assert _expected(name)["report"]["warn_share"] == shipped.thresholds.giant_component_warn_share, name
    # Звіт бере поріг із конфігу, а не з константи: зафіксований конфіг на g_financier — те саме попередження.
    _, before, outcome, _, _ = _scenario("g_financier")
    report = effect_report(before, outcome.graph, shipped, lists_applied=True, delegated_complete=True)
    assert report.warn_share == 0.5 and report.warnings == (GIANT,)
    looser = effect_report(before, outcome.graph, _config(giant_component_warn_share=1.0),
                           lists_applied=True, delegated_complete=True)
    assert looser.warn_share == 1.0 and GIANT not in looser.warnings


# --- Гучні відмови й інваріанти -------------------------------------------------------------


def test_after_must_be_a_pruning_of_before():
    before = _fan(3, 4)
    with pytest.raises(ValueError):  # покупця втрачено
        effect_report(before, before.without(["B00"]), _config(), lists_applied=True, delegated_complete=True)
    with pytest.raises(ValueError):  # зайва вершина в after
        effect_report(before, _graph([*before.nodes, _node("Z")], before.edges), _config(),
                      lists_applied=True, delegated_complete=True)
    with pytest.raises(ValueError):  # ребро, якого не було в before
        effect_report(before, _graph(before.nodes, [*before.edges, _edge("F", "B03")]), _config(),
                      lists_applied=True, delegated_complete=True)
    with pytest.raises(ValueError):  # обидва графи — не одне й те саме: after на місці before
        effect_report(before.without(["F"]), before, _config(), lists_applied=True, delegated_complete=True)
    # Підміни з тими самими кількостями: різниці лічильників тут нічого не виявляють — лише перевірка підмножин.
    swapped_source = _graph([_node("G"), *before.buyers()], [_edge("G", f"B{i:02d}") for i in range(3)])
    with pytest.raises(ValueError):  # інше джерело замість F
        effect_report(before, swapped_source, _config(), lists_applied=True, delegated_complete=True)
    swapped_edge = _graph(before.nodes, [*before.edges[:2], _edge("F", "B03")])
    with pytest.raises(ValueError):  # інше ребро замість F -> B02
        effect_report(before, swapped_edge, _config(), lists_applied=True, delegated_complete=True)
    swapped_buyer = _graph([_node("F"), *before.buyers()[:3], _buyer("B99", 4)],
                           [_edge("F", f"B{i:02d}") for i in range(3)])
    with pytest.raises(ValueError):  # інший покупець замість B03
        effect_report(before, swapped_buyer, _config(), lists_applied=True, delegated_complete=True)


def test_argument_types_are_checked():
    g, c = _fan(1, 2), _config()
    with pytest.raises(TypeError):
        effect_report("graph", g, c, lists_applied=True, delegated_complete=True)
    with pytest.raises(TypeError):
        effect_report(g, None, c, lists_applied=True, delegated_complete=True)
    with pytest.raises(TypeError):
        effect_report(g, g, c.thresholds, lists_applied=True, delegated_complete=True)
    with pytest.raises(TypeError):
        effect_report(g, g, c, lists_applied=1, delegated_complete=True)
    with pytest.raises(TypeError):
        effect_report(g, g, c, lists_applied=True, delegated_complete=None)
    with pytest.raises(TypeError):
        effect_report(g, g, c, True, True)  # прапорці — лише іменовані


def _snap(**changes) -> EffectSnapshot:
    kw = dict(nodes=5, edges=2, components=3, buyers_total=4, buyers_in_largest_component=2,
              largest_component_buyer_share=0.5, isolated_buyers=1)
    kw.update(changes)
    return EffectSnapshot(**kw)


def test_snapshot_invariants_are_loud():
    _snap()
    with pytest.raises(ValueError):
        _snap(largest_component_buyer_share=0.4)  # не дорівнює buyers_in_largest / buyers_total
    with pytest.raises(ValueError):
        _snap(buyers_in_largest_component=5, largest_component_buyer_share=1.25)
    with pytest.raises(ValueError):
        _snap(buyers_total=0, buyers_in_largest_component=0, largest_component_buyer_share=0.1, isolated_buyers=0)
    with pytest.raises(ValueError):
        _snap(isolated_buyers=5)
    with pytest.raises(ValueError):
        _snap(nodes=-1)
    with pytest.raises(ValueError):
        _snap(components=6)  # компонент більше, ніж вершин
    with pytest.raises(ValueError):
        _snap(buyers_in_largest_component=0, largest_component_buyer_share=0.0)  # покупці є, а в найбільшій — 0
    with pytest.raises(TypeError):
        _snap(nodes=True)
    with pytest.raises(TypeError):
        _snap(largest_component_buyer_share=1)
    _snap(buyers_total=0, buyers_in_largest_component=0, largest_component_buyer_share=0.0, isolated_buyers=0)


def _rep(**changes) -> EffectReport:
    kw = dict(before=_snap(), after=_snap(), pruned_nodes=0, pruned_edges=0, warn_share=0.5, warnings=())
    kw.update(changes)
    return EffectReport(**kw)


def test_report_invariants_are_loud():
    _rep()
    with pytest.raises(ValueError):  # частка 0.75 > 0.5 без giant_component
        _rep(after=_snap(buyers_in_largest_component=3, largest_component_buyer_share=0.75, isolated_buyers=0))
    with pytest.raises(ValueError):  # giant_component без підстави
        _rep(warnings=(GIANT,))
    with pytest.raises(ValueError):  # pruned_nodes != before.nodes - after.nodes
        _rep(pruned_nodes=1)
    with pytest.raises(ValueError):
        _rep(pruned_edges=1)
    with pytest.raises(ValueError):  # покупців стало менше
        _rep(after=_snap(nodes=4, buyers_total=3, buyers_in_largest_component=2,
                         largest_component_buyer_share=2 / 3), pruned_nodes=1)
    with pytest.raises(ValueError):  # не впорядковано
        _rep(warnings=(GraphWarning.EMPTY_GRAPH, DELEGATED_INCOMPLETE))
    with pytest.raises(ValueError):  # дубль
        _rep(warnings=(DELEGATED_INCOMPLETE, DELEGATED_INCOMPLETE))
    with pytest.raises(ValueError):
        _rep(warn_share=0.0)
    with pytest.raises(ValueError):
        _rep(warn_share=1.5)
    with pytest.raises((TypeError, ValueError)):
        _rep(warnings=("no_such_warning",))
    with pytest.raises(TypeError):
        _rep(before={"nodes": 5})
    rep = _rep(warnings=["delegated_incomplete"])  # рядок значення приводиться до GraphWarning, кортеж
    assert rep.warnings == (DELEGATED_INCOMPLETE,) and isinstance(rep.warnings[0], GraphWarning)


# --- Чистота, детермінізм, межі модулів, швидкодія ------------------------------------------


def test_report_is_pure_and_deterministic():
    _, before, outcome, config, _ = _scenario("g_hub")
    before_copy, after_copy = copy.deepcopy(before), copy.deepcopy(outcome.graph)
    r1 = effect_report(before, outcome.graph, config, lists_applied=True, delegated_complete=True)
    r2 = effect_report(before, outcome.graph, config, lists_applied=True, delegated_complete=True)
    assert r1 == r2
    assert before == before_copy and outcome.graph == after_copy
    # Порядок вершин і ребер на вході не має значення.
    shuffled = FundingGraph(nodes=tuple(reversed(before.nodes)), edges=tuple(reversed(before.edges)))
    assert effect_report(shuffled, outcome.graph, config, lists_applied=True, delegated_complete=True) == r1


def test_report_uses_graph_components_api_and_respects_module_boundaries():
    tree = ast.parse(Path(report_module.__file__).read_text(encoding="utf-8"))
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    assert "unmask.graph.components" in imports  # існуючий union-find (T-027), а не копія
    defined = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    assert not defined & {"find", "union", "bfs", "components"}
    forbidden = {f"unmask.ingest.{m}" for m in
                 ("rpc", "collector", "buyers", "funding", "cache", "service", "budget", "addresses")}
    forbidden |= {"unmask.graph.service", "unmask.graph.build", "socket", "httpx", "requests"}
    for name in imports:
        assert not any(name == f or name.startswith(f + ".") for f in forbidden), name
