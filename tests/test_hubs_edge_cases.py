# verifies: FR-002-09, FR-002-11
"""Крайові випадки відсікання хабів і звіту ефекту (T-038; FR-002-09, FR-002-11; research R-16;
contracts/graph-service.md §6; data-model «EffectReport»; конституція V).

Принцип V: порожній результат ніколи не виглядає «чистим». Тому:

- порожній результат збору (0 покупців) -> порожній граф, знімки з нулями й попередження `empty_graph`;
  інваріант `before.buyers_total == 0` ⇔ `empty_graph ∈ warnings` гучний у `EffectReport`;
- усі джерела відсічені -> граф лише з покупців (можливо, з ребрами між ними), попередження
  `all_sources_pruned` ⇔ у `after` немає вершин без ролі покупця, а в `before` вони були; кожне відсічене джерело
  має запис із критерієм і інцидентними ребрами (FR-002-09), навіть коли відсічено всіх;
- вершина — і джерело, і покупець, що відповідає критерію: лишається з позначкою, а її ребра до відсічених
  хабів зникають разом із хабами (ребра до решти вершин лишаються);
- жоден з 12 сценаріїв (з наявними й відсутніми списками) не піднімає виняток;
- порядок попереджень — за рядковим значенням: address_lists_not_applied < all_sources_pruned <
  delegated_incomplete < empty_graph < giant_component.

Еталони — `expected.json` (оракул генератора фікстур), а не вихід коду, що тестується.
"""

import json
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
from unmask.hubs.config import ADDRESS_CATEGORIES, AddressLists, HubConfig, HubThresholds
from unmask.hubs.prune import PruneOutcome, prune_hubs
from unmask.hubs.report import EffectReport, EffectSnapshot, effect_report
from unmask.ingest.model import AddressType

SCENARIOS = ["g_all_hubs", "g_basic", "g_buyer_hub", "g_delegated", "g_dust", "g_dust_mixed", "g_empty",
             "g_financier", "g_hub", "g_incomplete", "g_known", "g_unexpanded"]

W = GraphWarning
BUYER, FUNDER = NodeRole.BUYER, NodeRole.FUNDER


# --- Сценарії -----------------------------------------------------------------------------


def _expected(name: str) -> dict:
    return json.loads((Path(GRAPH_FIXTURES) / name / "expected.json").read_text(encoding="utf-8"))


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


def _scenario_config(expected: dict, *, with_lists: bool = True) -> HubConfig:
    lists = expected["config"]["lists"]
    return _config(lists=_lists(lists["version"], **lists["categories"]) if with_lists and lists else None,
                   version=expected["config"]["version"], **expected["config"]["thresholds"])


def _run_scenario(name: str, *, with_lists: bool = True):
    """(expected, before, outcome, report) сценарію через `build_graph` -> `prune_hubs` -> `effect_report`."""
    expected = _expected(name)
    result = load_ingest_fixture(name)
    config = _scenario_config(expected, with_lists=with_lists)
    before = build_graph(result)
    outcome = prune_hubs(before, config, ingest_counterparty_threshold=result.metadata.counterparty_threshold)
    report = effect_report(before, outcome.graph, config, lists_applied=outcome.lists_applied,
                           delegated_complete=GraphCompleteness.derive(result).delegated_complete)
    return expected, before, outcome, report


def _zero_snapshot() -> EffectSnapshot:
    return EffectSnapshot(nodes=0, edges=0, components=0, buyers_total=0, buyers_in_largest_component=0,
                          largest_component_buyer_share=0.0, isolated_buyers=0)


# --- Будівельники графів у пам'яті ---------------------------------------------------------

_QUIET = NodeMeasures(degree=1, unique_senders=1, one_off_senders=1, one_off_share=1.0,
                      buyer_fanout=0, median_to_buyers=None)
_SIG = iter(range(10**9))


def _node(address: str, *roles: NodeRole, rank: int | None = None, hub: bool = False) -> Node:
    """`hub=True` — вершина поза кривою: відповідає критерію `known_list` (`address_type:off_curve`)."""
    roles = frozenset(roles or (FUNDER,))
    is_buyer = BUYER in roles
    return Node(address=address, roles=roles, depth=0 if is_buyer else 1,
                buyer_rank=(rank or 1) if is_buyer else None,
                address_type=AddressType.OFF_CURVE if hub else AddressType.WALLET, unexpanded=None,
                measures=_QUIET)


def _buyer(address: str, rank: int = 1, *, hub: bool = False) -> Node:
    return _node(address, BUYER, rank=rank, hub=hub)


def _edge(sender: str, receiver: str, kind: EdgeKind = EdgeKind.TRANSFER) -> Edge:
    slot = next(_SIG)
    if kind is EdgeKind.TRANSFER:
        return Edge(kind, sender, receiver, "sol", 10, None, 1, slot, slot, None, None,
                    (EdgeRef(f"sig{slot}", slot, "0"),))
    return Edge(kind, sender, receiver, None, None, None, 1, slot, slot, None, None,
                (EdgeRef(f"dsig{slot}", slot, None),))


def _prune(graph: FundingGraph, config: HubConfig | None = None) -> PruneOutcome:
    return prune_hubs(graph, config or _config(), ingest_counterparty_threshold=200)


def _report(before: FundingGraph, outcome: PruneOutcome, config: HubConfig | None = None, *,
            lists_applied: bool = True, delegated_complete: bool = True) -> EffectReport:
    return effect_report(before, outcome.graph, config or _config(), lists_applied=lists_applied,
                         delegated_complete=delegated_complete)


# --- 1. Порожній результат ------------------------------------------------------------------


def test_empty_ingest_yields_empty_graph_zero_snapshots_and_empty_graph_warning():
    expected, before, outcome, report = _run_scenario("g_empty")
    assert expected["graph"] == {"nodes": [], "edges": []}  # еталон оракула: порожній граф
    assert before.nodes == () and before.edges == ()
    assert outcome.graph.nodes == () and outcome.graph.edges == ()
    assert outcome.records == () and outcome.buyer_flags == ()
    assert report.before == report.after == _zero_snapshot()
    assert (report.pruned_nodes, report.pruned_edges) == (0, 0)
    assert report.warnings == (W.EMPTY_GRAPH,) == tuple(W(w) for w in expected["report"]["warnings"])
    assert report.warnings != ()  # принцип V: порожній результат ніколи не «чистий»


def test_empty_graph_warning_combines_in_value_order_with_the_other_warnings():
    _, before, outcome, _ = _run_scenario("g_empty")
    report = _report(before, outcome, lists_applied=False, delegated_complete=False)
    assert report.warnings == (W.ADDRESS_LISTS_NOT_APPLIED, W.DELEGATED_INCOMPLETE, W.EMPTY_GRAPH)


def test_zero_buyers_with_sources_still_warns_empty_graph():
    # 0 покупців, але вершини є: частка 0.0, звіт однаково не «чистий» (інваріант — за покупцями, не за вершинами).
    graph = FundingGraph(nodes=(_node("A"), _node("C")), edges=(_edge("A", "C"),))
    report = _report(graph, _prune(graph))
    assert report.before.buyers_total == 0 and report.before.nodes == 2
    assert report.warnings == (W.EMPTY_GRAPH,)


def test_single_buyer_is_not_an_empty_graph():
    # Межа «0 покупців»: рівно один покупець — граф не порожній, попередження `empty_graph` немає.
    graph = FundingGraph(nodes=(_node("S"), _buyer("B0", 1)), edges=(_edge("S", "B0"),))
    report = _report(graph, _prune(graph))
    assert report.before.buyers_total == 1
    assert W.EMPTY_GRAPH not in report.warnings
    lone = FundingGraph(nodes=(_buyer("B0", 1),), edges=())
    assert W.EMPTY_GRAPH not in _report(lone, _prune(lone)).warnings


def test_zero_buyers_and_every_node_pruned_warns_both_empty_graph_and_all_sources_pruned():
    graph = FundingGraph(nodes=(_node("H1", hub=True), _node("H2", hub=True)), edges=(_edge("H1", "H2"),))
    outcome = _prune(graph)
    assert outcome.graph.nodes == () and len(outcome.records) == 2
    assert _report(graph, outcome).warnings == (W.ALL_SOURCES_PRUNED, W.EMPTY_GRAPH)


# --- 2. Усі джерела відсічені ----------------------------------------------------------------


def test_all_sources_pruned_keeps_only_buyers_and_warns():
    expected, before, outcome, report = _run_scenario("g_all_hubs")
    buyers = {n.address for n in before.buyers()}
    assert buyers == {expected["wallets"][f"B{i}"] for i in range(1, 7)}  # B1..B6 — усі покупці сценарію
    assert {n.address for n in outcome.graph.nodes} == buyers  # лише покупці, усі на місці (SC-003)
    assert all(BUYER in n.roles for n in outcome.graph.nodes)
    assert outcome.graph.edges == ()  # у цьому сценарії всі ребра — від джерел
    assert (report.after.nodes, report.after.buyers_total) == (6, 6)
    assert report.before.nodes > report.before.buyers_total  # джерела були
    assert report.warnings == (W.ALL_SOURCES_PRUNED,) == tuple(W(w) for w in expected["report"]["warnings"])
    assert W.GIANT_COMPONENT not in report.warnings and W.EMPTY_GRAPH not in report.warnings


def test_all_sources_pruned_with_edges_between_remaining_buyers():
    h1, h2 = _node("H1", hub=True), _node("H2", hub=True)
    graph = FundingGraph(nodes=(h1, h2, _buyer("B0", 1), _buyer("B1", 2)),
                         edges=(_edge("H1", "B0"), _edge("H2", "B1"), _edge("B0", "B1")))
    outcome = _prune(graph)
    assert [n.address for n in outcome.graph.nodes] == ["B0", "B1"] and len(outcome.graph.edges) == 1
    # частка після = 2/2 > 0.5: поруч із all_sources_pruned — giant_component, порядок за значенням
    assert _report(graph, outcome).warnings == (W.ALL_SOURCES_PRUNED, W.GIANT_COMPONENT)


@pytest.mark.parametrize("name", ["g_hub", "g_known", "g_dust", "g_dust_mixed", "g_unexpanded"])
def test_no_all_sources_pruned_when_some_source_survives(name):
    # Відсікання було (є хаби), але вцілілі джерела лишились -> попередження немає.
    _, before, outcome, report = _run_scenario(name)
    assert outcome.records
    assert any(BUYER not in n.roles for n in outcome.graph.nodes)
    assert W.ALL_SOURCES_PRUNED not in report.warnings


def test_no_all_sources_pruned_when_there_were_no_sources_to_prune():
    graph = FundingGraph(nodes=(_buyer("B0", 1), _buyer("B1", 2)), edges=(_edge("B0", "B1"),))
    report = _report(graph, _prune(graph))
    assert report.before.nodes == report.before.buyers_total == 2
    assert W.ALL_SOURCES_PRUNED not in report.warnings and W.EMPTY_GRAPH not in report.warnings


def test_all_sources_pruned_still_records_each_hub_with_criteria():
    expected, before, outcome, _ = _run_scenario("g_all_hubs")
    want = expected["prune"]["records"]
    assert len(want) == 4 and len(outcome.records) == 4  # по запису на кожне джерело
    sources = {n.address for n in before.nodes if BUYER not in n.roles}
    assert {r.address for r in outcome.records} == sources == {r["address"] for r in want}
    for record, oracle in zip(outcome.records, want):
        assert record.address == oracle["address"]
        assert record.criteria, record.address  # SC-002: без пояснення відсікання немає
        assert [(h.criterion.value, h.detail, h.measured, h.threshold) for h in record.criteria] == \
            [(h["criterion"], h["detail"], h["measured"], h["threshold"]) for h in oracle["criteria"]]
        assert record.incident_edges  # хаб-джерело фінансує покупців
        assert [[e.kind.value, e.sender, e.receiver, "" if e.asset is None else str(e.asset)]
                for e in record.incident_edges] == \
            [[e["kind"], e["sender"], e["receiver"], e["asset"] or ""] for e in oracle["incident_edges"]]
        assert (record.config_version, record.lists_version) == (2, 1)
    # кожне ребро повного графа — у записі свого хаба: нічого не зникло безслідно (FR-002-09)
    in_records = {e.key for r in outcome.records for e in r.incident_edges}
    assert in_records == {e.key for e in before.edges}


# --- 3. Жоден сценарій не піднімає виняток ----------------------------------------------------


def test_scenario_list_is_the_twelve_fixture_directories():
    on_disk = sorted(p.name for p in Path(GRAPH_FIXTURES).iterdir() if (p / "expected.json").is_file())
    assert on_disk == SCENARIOS and len(SCENARIOS) == 12


@pytest.mark.parametrize("with_lists", [True, False], ids=["lists", "no-lists"])
@pytest.mark.parametrize("name", SCENARIOS)
def test_no_exception_for_any_scenario(name, with_lists):
    expected, before, outcome, report = _run_scenario(name, with_lists=with_lists)
    # покупці лишаються завжди, а порожній результат ніколи не «чистий»
    assert report.before.buyers_total == report.after.buyers_total
    assert {n.address for n in before.buyers()} == {n.address for n in outcome.graph.buyers()}
    assert (report.before.buyers_total == 0) == (W.EMPTY_GRAPH in report.warnings)
    assert (W.ADDRESS_LISTS_NOT_APPLIED in report.warnings) is (not with_lists)
    if with_lists:  # еталон складено з увімкненими списками: попередження збігаються повністю
        assert [w.value for w in report.warnings] == expected["report"]["warnings"]


# --- 4. Вершина — і джерело, і покупець ------------------------------------------------------


def test_buyer_source_node_keeps_edges_to_remaining_nodes_only():
    hub, x, b2, b3 = "Hub", "X", "B2", "B3"
    to_x, to_b2, x_to_hub, x_to_b3, delegated = (_edge(hub, x), _edge(hub, b2), _edge(x, hub), _edge(x, b3),
                                                 _edge(hub, x, EdgeKind.DELEGATED_BUY))
    graph = FundingGraph(
        nodes=(_node(hub, hub=True), _node(x, BUYER, FUNDER, rank=1, hub=True), _buyer(b2, 2), _buyer(b3, 3)),
        edges=(to_x, to_b2, x_to_hub, x_to_b3, delegated))
    outcome = _prune(graph)
    # хаб відсічено разом з усіма своїми ребрами (обох видів, обох напрямків) і записано
    (record,) = outcome.records
    assert record.address == hub
    assert {e.key for e in record.incident_edges} == {to_x.key, to_b2.key, x_to_hub.key, delegated.key}
    # покупець-джерело X відповідає критерію, але лишається з позначкою; його ребро до b3 — теж
    (flag,) = outcome.buyer_flags
    assert (flag.address, flag.buyer_rank) == (x, 1) and flag.criteria
    assert {n.address for n in outcome.graph.nodes} == {x, b2, b3}
    assert [(e.sender, e.receiver) for e in outcome.graph.edges] == [(x, b3)]
    # звіт: у `after` лишилися лише покупці -> попередження; покупців не стало менше
    report = _report(graph, outcome)
    assert report.before.buyers_total == report.after.buyers_total == 3
    assert (report.pruned_nodes, report.pruned_edges) == (1, 4)
    assert W.ALL_SOURCES_PRUNED in report.warnings


def test_g_buyer_hub_fixture_keeps_flagged_buyer_and_every_edge():
    expected, before, outcome, report = _run_scenario("g_buyer_hub")
    (flag,) = outcome.buyer_flags
    assert {h.criterion.value for h in flag.criteria} == {"degree", "dust_fanout", "ingest_high_degree",
                                                          "known_list", "one_off_senders"}
    assert outcome.records == ()
    assert {n.address for n in outcome.graph.nodes} == {n.address for n in before.nodes}
    assert outcome.graph.edges == before.edges
    assert (report.pruned_nodes, report.pruned_edges) == (0, 0)
    assert W.ALL_SOURCES_PRUNED not in report.warnings  # відсікання не було: джерела не «всі відсічені»


# --- 5. Інваріанти EffectReport (гучно, а не тихо) --------------------------------------------


def _snap(**changes) -> EffectSnapshot:
    kw = dict(nodes=5, edges=2, components=3, buyers_total=4, buyers_in_largest_component=2,
              largest_component_buyer_share=0.5, isolated_buyers=1)
    kw.update(changes)
    return EffectSnapshot(**kw)


def _zero_buyers(nodes: int = 1) -> EffectSnapshot:
    return _snap(nodes=nodes, edges=0, components=nodes, buyers_total=0, buyers_in_largest_component=0,
                 largest_component_buyer_share=0.0, isolated_buyers=0)


def _rep(**changes) -> EffectReport:
    kw = dict(before=_snap(), after=_snap(), pruned_nodes=0, pruned_edges=0, warn_share=0.5, warnings=())
    kw.update(changes)
    return EffectReport(**kw)


def test_report_requires_empty_graph_warning_iff_zero_buyers():
    _rep()
    with pytest.raises(ValueError):  # 0 покупців без попередження — «тихий чистий» випадок
        _rep(before=_zero_buyers(), after=_zero_buyers())
    ok = _rep(before=_zero_buyers(), after=_zero_buyers(), warnings=(W.EMPTY_GRAPH,))
    assert ok.warnings == (W.EMPTY_GRAPH,)
    with pytest.raises(ValueError):  # попередження без підстави
        _rep(warnings=(W.EMPTY_GRAPH,))
    ok_empty = _rep(before=_zero_buyers(0), after=_zero_buyers(0), warnings=(W.EMPTY_GRAPH,))
    assert ok_empty.before == ok_empty.after == EffectSnapshot(0, 0, 0, 0, 0, 0.0, 0)


def test_report_requires_all_sources_pruned_iff_after_has_no_non_buyer_but_before_had():
    def only_buyers(nodes=4):
        return _snap(nodes=nodes, buyers_total=nodes, buyers_in_largest_component=2,
                     largest_component_buyer_share=2 / nodes, isolated_buyers=1, components=3)
    before = _snap(nodes=6, buyers_total=4, components=3)  # у `before` є 2 не-покупці
    with pytest.raises(ValueError):  # усі не-покупці зникли, попередження немає
        _rep(before=before, after=only_buyers(), pruned_nodes=2)
    rep = _rep(before=before, after=only_buyers(), pruned_nodes=2, warnings=(W.ALL_SOURCES_PRUNED,))
    assert rep.warnings == (W.ALL_SOURCES_PRUNED,)
    with pytest.raises(ValueError):  # попередження, хоч не-покупці лишилися
        _rep(warnings=(W.ALL_SOURCES_PRUNED,))
    with pytest.raises(ValueError):  # джерел і не було -> відсікати нічого, попередження недоречне
        _rep(before=only_buyers(), after=only_buyers(), warnings=(W.ALL_SOURCES_PRUNED,))
    _rep(before=only_buyers(), after=only_buyers())  # без джерел і без відсікання — без попередження


def test_report_warning_order_is_by_value():
    before = _snap(nodes=6, buyers_total=4, components=3)
    after = _snap(nodes=4, buyers_total=4, buyers_in_largest_component=3, largest_component_buyer_share=0.75,
                  isolated_buyers=1, components=2)
    ordered = (W.ADDRESS_LISTS_NOT_APPLIED, W.ALL_SOURCES_PRUNED, W.DELEGATED_INCOMPLETE, W.GIANT_COMPONENT)
    assert [w.value for w in ordered] == sorted(w.value for w in ordered)
    assert _rep(before=before, after=after, pruned_nodes=2, warnings=ordered).warnings == ordered
    with pytest.raises(ValueError):
        _rep(before=before, after=after, pruned_nodes=2, warnings=tuple(reversed(ordered)))
    with pytest.raises(ValueError):  # empty_graph — після delegated_incomplete, перед giant_component
        _rep(before=_zero_buyers(), after=_zero_buyers(), warnings=(W.EMPTY_GRAPH, W.DELEGATED_INCOMPLETE))
    all_values = [w.value for w in W]
    assert sorted(all_values) == ["address_lists_not_applied", "all_sources_pruned", "delegated_incomplete",
                                  "empty_graph", "giant_component"]
