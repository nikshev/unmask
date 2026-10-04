# verifies: FR-002-09, FR-002-10
"""Відсікання хабів із захистом покупців (T-035; FR-002-09, FR-002-10, FR-002-22; research R-16, R-22;
contracts/graph-service.md §5; data-model «PruneRecord», «BuyerFlag», «PruneOutcome»).

Критична задача: помилка тут не ламає збірку, а тихо змінює граф кластеризації — відсічений покупець зникає з
аналізу, хаб без запису робить відсікання незворотним і непояснюваним. Тому, крім щасливого шляху:

- еталон — незалежний оракул генератора фікстур (`expected.json` усіх 12 сценаріїв: `prune.records`,
  `prune.buyer_flags`, `prune.after`), а не вихід `prune_hubs`; порівняння — повна рівність поле за полем;
- golden проти зафіксованих `config/hubs.yaml` (v2) і `config/hub_addresses.yaml` (v1) — принцип III: зміна
  порогу без перегенерації еталонів червона тут;
- межові випадки: покупець з усіма п'ятьма критеріями (лишається, позначка); ребро між двома хабами (в обох
  записах); `delegated_buy`-ребро хаба (у записі); покупець-джерело з ребром до хаба (ребро зникає, покупець
  лишається); порожній граф; `lists=None`; некоректний `ingest_counterparty_threshold` навіть на порожньому графі;
- інваріанти типів записів/позначок/результату (порожні критерії, чужі ребра, невпорядкованість, покупець у
  записах, хаб у графі) — гучно, а не тихо;
- чистота: вхідний граф не змінюється (глибоке порівняння), повтор дає той самий результат.
"""

import copy
import dataclasses
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
    HubCriterion,
    Node,
    NodeMeasures,
    NodeRole,
)
from unmask.hubs.config import ADDRESS_CATEGORIES, AddressLists, HubConfig, HubThresholds, load_hub_config
from unmask.hubs.criteria import CriterionHit
from unmask.hubs.prune import BuyerFlag, PruneOutcome, PruneRecord, prune_hubs
from unmask.ingest.model import AddressType

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = sorted(p.name for p in Path(GRAPH_FIXTURES).iterdir() if (p / "expected.json").is_file())
SHIPPED_HUBS = ROOT / "config" / "hubs.yaml"
SHIPPED_LISTS = ROOT / "config" / "hub_addresses.yaml"

MEASURED_CRITERIA = {"degree", "one_off_senders", "ingest_high_degree", "dust_fanout"}


# --- Дані сценаріїв ---------------------------------------------------------------------


def _expected(name: str) -> dict:
    return json.loads((Path(GRAPH_FIXTURES) / name / "expected.json").read_text(encoding="utf-8"))


def _lists(version: int = 1, **categories) -> AddressLists:
    parsed = {name: tuple(categories.get(name, ())) for name in ADDRESS_CATEGORIES}
    index = {address: name for name in ADDRESS_CATEGORIES for address in parsed[name]}
    return AddressLists(version=version, categories=MappingProxyType(parsed), index=MappingProxyType(index))


def _config(*, lists: AddressLists | None = None, **changes) -> HubConfig:
    kw = dict(
        version=2,
        degree_threshold=100,
        one_off_senders_share=0.8,
        one_off_min_senders=10,
        giant_component_warn_share=0.5,
        prune_off_curve=True,
        prune_ingest_high_degree=True,
        dust_amount_lamports=1_000_000,
        dust_min_fanout=5,
    )
    kw.update(changes)
    return HubConfig(
        thresholds=HubThresholds(**kw),
        lists=lists,
        thresholds_digest="0" * 64,
        lists_digest=None if lists is None else "1" * 64,
    )


def _scenario_config(expected: dict, *, with_lists: bool = True) -> HubConfig:
    """Конфіг сценарію рівно з `expected.config` (явні пороги й списки оракула)."""
    lists = expected["config"]["lists"]
    return _config(
        lists=_lists(lists["version"], **lists["categories"]) if with_lists else None,
        version=expected["config"]["version"],
        **expected["config"]["thresholds"],
    )


def _scenario(name: str, *, with_lists: bool = True):
    """(expected, повний граф, конфіг, поріг збору) сценарію; поріг збору — з метаданих 001 фікстури."""
    expected = _expected(name)
    result = load_ingest_fixture(name)
    # Поріг збору в еталоні й у метаданих 001 — одне значення (інакше еталон хітів `ingest_high_degree` хибний).
    assert result.metadata.counterparty_threshold == expected["ingest_counterparty_threshold"]
    return expected, build_graph(result), _scenario_config(expected, with_lists=with_lists), \
        result.metadata.counterparty_threshold


def _prune(name: str, **kw) -> tuple[dict, FundingGraph, PruneOutcome]:
    expected, graph, config, threshold = _scenario(name, **kw)
    return expected, graph, prune_hubs(graph, config, ingest_counterparty_threshold=threshold)


def _w(expected: dict, name: str) -> str:
    return expected["wallets"][name]


# --- Відображення в форму expected.json (поле за полем, без asdict) ----------------------


def _hit_dict(h: CriterionHit) -> dict:
    return {"criterion": h.criterion.value, "measured": h.measured, "threshold": h.threshold,
            "detail": h.detail, "lists_version": h.lists_version}


def _measures_dict(m: NodeMeasures) -> dict:
    return {"degree": m.degree, "unique_senders": m.unique_senders, "one_off_senders": m.one_off_senders,
            "one_off_share": m.one_off_share, "buyer_fanout": m.buyer_fanout,
            "median_to_buyers": m.median_to_buyers}


def _edge_dict(e: Edge) -> dict:
    return {
        "kind": e.kind.value, "sender": e.sender, "receiver": e.receiver,
        "asset": None if e.asset is None else str(e.asset), "amount": e.amount,
        "decimals": e.decimals, "count": e.count, "first_slot": e.first_slot,
        "last_slot": e.last_slot, "first_time": e.first_time, "last_time": e.last_time,
        "refs": [{"signature": r.signature, "slot": r.slot, "instruction_path": r.instruction_path}
                 for r in e.refs],
    }


def _record_dict(r: PruneRecord) -> dict:
    return {"address": r.address, "criteria": [_hit_dict(h) for h in r.criteria],
            "incident_edges": [_edge_dict(e) for e in r.incident_edges],
            "measures": _measures_dict(r.measures), "config_version": r.config_version,
            "lists_version": r.lists_version}


def _flag_dict(f: BuyerFlag) -> dict:
    return {"address": f.address, "buyer_rank": f.buyer_rank, "criteria": [_hit_dict(h) for h in f.criteria],
            "measures": _measures_dict(f.measures)}


def _edge_key(e: Edge) -> list:
    return [e.kind.value, e.sender, e.receiver, "" if e.asset is None else str(e.asset)]


def _outcome_dict(o: PruneOutcome) -> dict:
    return {
        "lists_applied": o.lists_applied,
        "config_version": o.config_version,
        "lists_version": o.lists_version,
        "records": [_record_dict(r) for r in o.records],
        "buyer_flags": [_flag_dict(f) for f in o.buyer_flags],
        "after": {"node_addresses": [n.address for n in o.graph.nodes],
                  "edge_keys": [_edge_key(e) for e in o.graph.edges]},
    }


# --- Повна рівність з оракулом на всіх сценаріях ----------------------------------------


def test_fixture_set_is_the_twelve_scenarios():
    assert SCENARIOS == ["g_all_hubs", "g_basic", "g_buyer_hub", "g_delegated", "g_dust", "g_dust_mixed", "g_empty",
                         "g_financier", "g_hub", "g_incomplete", "g_known", "g_unexpanded"]


@pytest.mark.parametrize("name", SCENARIOS)
def test_outcome_equals_oracle_prune_section_on_every_scenario(name):
    expected, _, outcome = _prune(name)
    assert _outcome_dict(outcome) == expected["prune"]


@pytest.mark.parametrize("name", SCENARIOS)
def test_after_graph_nodes_and_edges_are_exactly_the_full_ones_minus_hubs(name):
    """Вершини й ребра після відсікання — ті самі об'єкти повного графа (виміри не перераховуються)."""
    expected, graph, outcome = _prune(name)
    pruned = {r["address"] for r in expected["prune"]["records"]}
    assert outcome.graph.nodes == tuple(n for n in graph.nodes if n.address not in pruned)
    assert outcome.graph.edges == tuple(e for e in graph.edges if e.sender not in pruned and e.receiver not in pruned)


def test_golden_against_shipped_config_files():
    """Принцип III: зафіксовані `config/hubs.yaml` v2 і `config/hub_addresses.yaml` v1 дають еталон оракула.

    Конфіг кожного сценарію, крім `g_all_hubs` (знижений `degree_threshold`, щоб кожне джерело мало свій
    критерій), — рівно комітований; зміна порогу чи списку без перегенерації еталонів червона тут.
    """
    shipped = load_hub_config(SHIPPED_HUBS, SHIPPED_LISTS)
    assert shipped.thresholds.version == 2
    assert shipped.lists is not None and shipped.lists.version == 1
    thresholds = {f.name: getattr(shipped.thresholds, f.name)
                  for f in dataclasses.fields(HubThresholds) if f.name != "version"}
    categories = {k: list(v) for k, v in shipped.lists.categories.items()}
    on_shipped = []
    for name in SCENARIOS:
        expected = _expected(name)
        if (expected["config"]["version"], expected["config"]["thresholds"], expected["config"]["lists"]) != (
            shipped.thresholds.version, thresholds, {"version": shipped.lists.version, "categories": categories}
        ):
            continue
        on_shipped.append(name)
        result = load_ingest_fixture(name)
        outcome = prune_hubs(build_graph(result), shipped,
                             ingest_counterparty_threshold=result.metadata.counterparty_threshold)
        assert _outcome_dict(outcome) == expected["prune"], name
    assert on_shipped == [n for n in SCENARIOS if n != "g_all_hubs"]


# --- Сценарії з тексту задачі -----------------------------------------------------------


def test_g_hub_prunes_H_with_record_matching_oracle_and_keeps_F():
    expected, graph, outcome = _prune("g_hub")
    H, F = _w(expected, "H"), _w(expected, "F")
    assert [r.address for r in outcome.records] == [H]
    (record,) = outcome.records
    (oracle,) = expected["prune"]["records"]
    assert _record_dict(record) == oracle
    assert [h.criterion for h in record.criteria] == [HubCriterion.ONE_OFF_SENDERS]
    assert record.measures is graph.node(H).measures
    assert outcome.graph.node(F) is graph.node(F)
    assert outcome.buyer_flags == ()


@pytest.mark.parametrize("name", SCENARIOS)
def test_pruned_graph_has_no_hub_node_and_no_incident_edge_and_record_holds_exactly_those_edges(name):
    _, graph, outcome = _prune(name)
    after = {n.address for n in outcome.graph.nodes}
    for record in outcome.records:
        assert record.address not in after
        assert all(record.address not in (e.sender, e.receiver) for e in outcome.graph.edges)
        assert record.incident_edges == graph.incident(record.address)


@pytest.mark.parametrize("name", SCENARIOS)
def test_every_pruned_node_has_at_least_one_criterion_with_measured_and_threshold(name):
    """SC-002: жодного відсікання без пояснення. Виміряний критерій несе число і поріг; `known_list` — джерело
    (`list:<категорія>` з версією списку або `address_type:off_curve`)."""
    _, graph, outcome = _prune(name)
    assert len(outcome.records) == len(graph.nodes) - len(outcome.graph.nodes)
    for item in [*outcome.records, *outcome.buyer_flags]:
        assert len(item.criteria) >= 1
        for hit in item.criteria:
            if hit.criterion.value in MEASURED_CRITERIA:
                assert isinstance(hit.measured, (int, float)) and isinstance(hit.threshold, (int, float))
            else:
                assert hit.criterion is HubCriterion.KNOWN_LIST
                assert hit.detail == "address_type:off_curve" or (
                    hit.detail.startswith("list:") and hit.lists_version == outcome.lists_version)


def test_buyer_matching_all_criteria_is_flagged_not_pruned():
    expected, graph, outcome = _prune("g_buyer_hub")
    X = _w(expected, "X")
    assert outcome.records == ()
    (flag,) = outcome.buyer_flags
    assert flag.address == X and flag.buyer_rank == graph.node(X).buyer_rank == 1
    assert [h.criterion.value for h in flag.criteria] == [
        "degree", "dust_fanout", "ingest_high_degree", "known_list", "one_off_senders"]
    assert flag.measures is graph.node(X).measures
    assert outcome.graph.node(X) is graph.node(X)
    assert outcome.graph == graph  # нічого не відсічено: усі 101 ребро X лишились


@pytest.mark.parametrize("name", SCENARIOS)
def test_all_buyers_present_after_pruning_on_every_scenario(name):
    """SC-003, FR-002-10: кожен покупець входу — у графі після відсікання, той самий об'єкт; жодного в записах."""
    _, graph, outcome = _prune(name)
    assert outcome.graph.buyers() == graph.buyers()
    buyers = {b.address for b in graph.buyers()}
    assert not buyers & {r.address for r in outcome.records}
    assert {f.address for f in outcome.buyer_flags} <= buyers


def test_g_dust_prunes_D_by_dust_fanout_with_median_fanout_and_thresholds_in_record_and_keeps_E_and_F():
    """SC-009. Інцидентні ребра D — усі на будь-якому кінці: 19 вихідних до покупців B01…B19 і 2 вхідних від
    відправників D (ступінь 21); `expected.json` оракула фіксує саме 21."""
    expected, graph, outcome = _prune("g_dust")
    D, E, F = _w(expected, "D"), _w(expected, "E"), _w(expected, "F")
    (record,) = outcome.records
    assert record.address == D
    (hit,) = record.criteria
    assert hit.criterion is HubCriterion.DUST_FANOUT and hit.detail == "measured"
    assert hit.measured < 1_000_000 and hit.threshold == 1_000_000
    assert record.measures.buyer_fanout == 19 and record.measures.median_to_buyers == hit.measured
    buyers = {b.address for b in graph.buyers()}
    to_buyers = [e for e in record.incident_edges if e.sender == D and e.receiver in buyers]
    assert len(to_buyers) == 19
    assert len(record.incident_edges) == 21 == len(graph.incident(D))
    # Один великий переказ серед пилу лишається в записі як доказ (5 SOL до B19).
    assert max(e.amount for e in to_buyers) == 5_000_000_000
    assert outcome.graph.node(E) is graph.node(E) and outcome.graph.node(F) is graph.node(F)
    assert outcome.buyer_flags == ()


def test_g_financier_prunes_nothing_and_flags_nothing():
    """SC-009: справжній фінансист 30 покупців (ступінь 32, один пиловий переказ) — 0 хибних відсікань."""
    expected, graph, outcome = _prune("g_financier")
    assert outcome.records == () and outcome.buyer_flags == ()
    assert outcome.graph == graph
    assert outcome.graph.node(_w(expected, "R")) is graph.node(_w(expected, "R"))


def test_g_dust_mixed_prunes_X_and_W_only_and_keeps_real_edges_in_records():
    expected, graph, outcome = _prune("g_dust_mixed")
    X, W = _w(expected, "X"), _w(expected, "W")
    assert sorted(r.address for r in outcome.records) == sorted([X, W])
    for name in ("Y", "Z", "V", "U"):
        assert outcome.graph.node(_w(expected, name)) is graph.node(_w(expected, name))
    rx = next(r for r in outcome.records if r.address == X)
    amounts = sorted(e.amount for e in rx.incident_edges if e.sender == X)
    # Строга більшість пилу (4 із 7) — відсічений, але три справжні 0,5 SOL лишаються в записі як доказ (R-22).
    assert amounts.count(500_000_000) == 3 and sum(a < 1_000_000 for a in amounts) == 4
    assert rx.incident_edges == graph.incident(X)


def test_record_carries_config_version_and_lists_version():
    config = _scenario_config(_expected("g_hub"))
    config = dataclasses.replace(
        config,
        thresholds=dataclasses.replace(config.thresholds, version=7),
        lists=_lists(5, **{k: list(v) for k, v in config.lists.categories.items()}),
    )
    result = load_ingest_fixture("g_known")
    outcome = prune_hubs(build_graph(result), config,
                         ingest_counterparty_threshold=result.metadata.counterparty_threshold)
    assert (outcome.config_version, outcome.lists_version, outcome.lists_applied) == (7, 5, True)
    assert outcome.records
    for record in outcome.records:
        assert (record.config_version, record.lists_version) == (7, 5)
        for hit in record.criteria:
            if hit.detail.startswith("list:"):
                assert hit.lists_version == 5


def test_lists_none_gives_lists_applied_false_null_version_and_no_list_hit():
    expected, graph, outcome = _prune("g_known", with_lists=False)
    assert (outcome.lists_applied, outcome.lists_version, outcome.config_version) == (False, None, 2)
    assert [r.address for r in outcome.records] == [_w(expected, "PDA_SRC")]  # off_curve працює далі
    assert all(r.lists_version is None for r in outcome.records)
    assert outcome.graph.node(_w(expected, "PUMP")) is graph.node(_w(expected, "PUMP"))


def test_input_graph_is_not_mutated_and_output_is_deterministic():
    for name in SCENARIOS:
        expected, graph, config, threshold = _scenario(name)
        snapshot = copy.deepcopy(graph)
        first = prune_hubs(graph, config, ingest_counterparty_threshold=threshold)
        second = prune_hubs(graph, config, ingest_counterparty_threshold=threshold)
        assert graph == snapshot and repr(graph) == repr(snapshot)
        assert first == second and repr(first) == repr(second)
        # Незалежна побудова того самого входу дає той самий результат.
        again = prune_hubs(build_graph(load_ingest_fixture(name)), config, ingest_counterparty_threshold=threshold)
        assert again == first and repr(again) == repr(first)


def test_second_pass_prunes_nothing_and_keeps_the_same_flags():
    """Виміри вершин не перераховуються після відсікання (data-model `without`), тож рішення стабільне."""
    for name in SCENARIOS:
        _, _, config, threshold = _scenario(name)
        first = prune_hubs(_scenario(name)[1], config, ingest_counterparty_threshold=threshold)
        second = prune_hubs(first.graph, config, ingest_counterparty_threshold=threshold)
        assert second.records == ()
        assert second.buyer_flags == first.buyer_flags
        assert second.graph == first.graph


@pytest.mark.parametrize("name", SCENARIOS)
def test_union_of_pruned_graph_and_records_reconstructs_full_graph(name):
    _, graph, outcome = _prune(name)
    nodes = {n.address for n in outcome.graph.nodes} | {r.address for r in outcome.records}
    assert nodes == {n.address for n in graph.nodes}
    edges = set(outcome.graph.edges)
    for record in outcome.records:
        edges |= set(record.incident_edges)
        assert record.measures == graph.node(record.address).measures
    assert sorted(edges, key=_edge_key) == sorted(graph.edges, key=_edge_key)
    rebuilt = FundingGraph(nodes=graph.nodes, edges=tuple(edges))
    assert rebuilt == graph


# --- Синтетичні межові випадки ----------------------------------------------------------

_ADDR = [
    "4Nd1mYtq3oG7Q2bKjv9b8yXbH9QJm6N3s1R5o2nZ8kPq",
    "5dGLBimVFRmBiwQK4zV6rEsDCW4GTfi5XVsCipgJBR8n",
    "6WzTKVcxeNRhvesvAZRTuidQA42utf2KB5tUgLERcShh",
    "8B7NyiiYRSVGS5RZSb1wvEAj1Fk8UrHAF94BBF2cqumY",
    "BA41neM4vZngAdcq3VupzXU8KBezcs4SweZL8L9WE53z",
    "CGhP3Yu7nfFaSFGSi1jn65kSbvpdGrUeanNVzLTkWVSh",
]
_QUIET = NodeMeasures(degree=1, unique_senders=1, one_off_senders=1, one_off_share=1.0,
                      buyer_fanout=0, median_to_buyers=None)
_sig = iter(range(10_000))


def _node(address, *, roles=(NodeRole.FUNDER,), rank=None, hub=False) -> Node:
    """`hub=True` — вершина поза кривою (критерій `known_list` за типом); решта вимірів далеко від порогів."""
    roles = frozenset(roles)
    is_buyer = NodeRole.BUYER in roles
    return Node(address=address, roles=roles, depth=0 if is_buyer else 1,
                buyer_rank=(rank or 1) if is_buyer else None,
                address_type=AddressType.OFF_CURVE if hub else AddressType.WALLET,
                unexpanded=None, measures=_QUIET)


def _transfer(sender, receiver, amount=10) -> Edge:
    slot = next(_sig)
    return Edge(EdgeKind.TRANSFER, sender, receiver, "sol", amount, None, 1, slot, slot, None, None,
                (EdgeRef(f"sig{slot}", slot, "0"),))


def _delegated(payer, receiver) -> Edge:
    slot = next(_sig)
    return Edge(EdgeKind.DELEGATED_BUY, payer, receiver, None, None, None, 1, slot, slot, None, None,
                (EdgeRef(f"dsig{slot}", slot, None),))


def _run(graph: FundingGraph, config: HubConfig | None = None) -> PruneOutcome:
    return prune_hubs(graph, config or _config(), ingest_counterparty_threshold=200)


def test_edge_between_two_hubs_is_kept_in_both_records():
    h1, h2, b = _ADDR[0], _ADDR[1], _ADDR[2]
    e12, e1b = _transfer(h1, h2), _transfer(h1, b)
    graph = FundingGraph(nodes=(_node(h1, hub=True), _node(h2, hub=True),
                                _node(b, roles=(NodeRole.BUYER,))), edges=(e12, e1b))
    outcome = _run(graph)
    records = {r.address: r for r in outcome.records}
    assert records[h1].incident_edges == graph.incident(h1) == tuple(sorted((e12, e1b), key=_edge_key))
    assert records[h2].incident_edges == (e12,)
    assert outcome.graph.edges == () and [n.address for n in outcome.graph.nodes] == [b]


def test_delegated_buy_edge_of_hub_is_pruned_and_kept_in_record():
    hub, b1, b2 = _ADDR[0], _ADDR[1], _ADDR[2]
    d, t = _delegated(hub, b1), _transfer(b1, b2)
    graph = FundingGraph(
        nodes=(_node(hub, roles=(NodeRole.DELEGATED_PAYER,), hub=True),
               _node(b1, roles=(NodeRole.BUYER, NodeRole.DELEGATED_RECEIVER), rank=1),
               _node(b2, roles=(NodeRole.BUYER,), rank=2)),
        edges=(d, t))
    outcome = _run(graph)
    (record,) = outcome.records
    assert record.address == hub and record.incident_edges == (d,)
    assert outcome.graph.edges == (t,)


def test_buyer_source_keeps_edges_to_remaining_nodes_only_and_buyer_hub_is_flagged():
    """Покупець-джерело, що сам відповідає критерію, лишається з позначкою; його ребро до хаба зникає разом з
    хабом, ребро до іншого покупця — лишається."""
    hub, buyer_src, b2 = _ADDR[0], _ADDR[1], _ADDR[2]
    to_hub, to_b2 = _transfer(buyer_src, hub), _transfer(buyer_src, b2)
    graph = FundingGraph(
        nodes=(_node(hub, hub=True),
               _node(buyer_src, roles=(NodeRole.BUYER, NodeRole.FUNDER), rank=2, hub=True),
               _node(b2, roles=(NodeRole.BUYER,), rank=1)),
        edges=(to_hub, to_b2))
    outcome = _run(graph)
    assert [r.address for r in outcome.records] == [hub]
    assert outcome.records[0].incident_edges == (to_hub,)
    (flag,) = outcome.buyer_flags
    assert (flag.address, flag.buyer_rank) == (buyer_src, 2)
    assert outcome.graph.node(buyer_src) is graph.node(buyer_src)
    assert outcome.graph.edges == (to_b2,)


def test_non_buyer_matching_all_criteria_gets_one_record_with_all_six_hits():
    """Запис несе **усі** хіти (до шести: п'ять критеріїв, `known_list` з двох джерел), а не перший."""
    from unmask.graph.model import UnexpandedMark
    from unmask.ingest.model import UnexpandedReason

    hub, buyer = _ADDR[0], _ADDR[1]
    loud = NodeMeasures(degree=101, unique_senders=12, one_off_senders=12, one_off_share=1.0,
                        buyer_fanout=89, median_to_buyers=500_000)
    node = dataclasses.replace(_node(hub, hub=True), measures=loud,
                               unexpanded=UnexpandedMark(UnexpandedReason.HIGH_DEGREE, 201, 201, False))
    edge = _transfer(hub, buyer)
    graph = FundingGraph(nodes=(node, _node(buyer, roles=(NodeRole.BUYER,))), edges=(edge,))
    outcome = _run(graph, _config(lists=_lists(3, exchanges=[hub])))
    (record,) = outcome.records
    assert [(h.criterion.value, h.detail) for h in record.criteria] == [
        ("degree", "measured"), ("dust_fanout", "measured"), ("ingest_high_degree", "unexpanded:high_degree"),
        ("known_list", "address_type:off_curve"), ("known_list", "list:exchanges"), ("one_off_senders", "measured")]
    assert record.criteria[4].lists_version == 3 == record.lists_version
    assert record.measures is loud and record.incident_edges == (edge,)


def test_flags_ordered_by_rank_and_records_by_address():
    addrs = sorted(_ADDR)
    # Ранги навмисно в порядку, зворотному до адрес: впорядкування позначок — за рангом, не за адресою.
    buyers = [_node(a, roles=(NodeRole.BUYER,), rank=len(addrs) - i, hub=True) for i, a in enumerate(addrs[:3])]
    hubs = [_node(a, hub=True) for a in reversed(addrs[3:])]
    outcome = _run(FundingGraph(nodes=(*hubs, *buyers), edges=()))
    assert [r.address for r in outcome.records] == addrs[3:]
    assert [f.buyer_rank for f in outcome.buyer_flags] == sorted(f.buyer_rank for f in outcome.buyer_flags)
    assert [f.address for f in outcome.buyer_flags] == list(reversed(addrs[:3]))


def test_empty_graph_gives_empty_outcome():
    outcome = _run(FundingGraph((), ()))
    assert outcome.graph == FundingGraph((), ())
    assert (outcome.records, outcome.buyer_flags) == ((), ())
    assert (outcome.lists_applied, outcome.config_version, outcome.lists_version) == (False, 2, None)


@pytest.mark.parametrize("threshold, exc", [(0, ValueError), (-1, ValueError), (True, TypeError),
                                            (1.0, TypeError), ("200", TypeError)])
def test_bad_ingest_threshold_is_rejected_even_on_empty_graph(threshold, exc):
    with pytest.raises(exc):
        prune_hubs(FundingGraph((), ()), _config(), ingest_counterparty_threshold=threshold)


def test_wrong_argument_types_raise_type_error():
    with pytest.raises(TypeError):
        prune_hubs(object(), _config(), ingest_counterparty_threshold=200)
    with pytest.raises(TypeError):
        prune_hubs(FundingGraph((), ()), object(), ingest_counterparty_threshold=200)


# --- Інваріанти типів --------------------------------------------------------------------

_OFF = CriterionHit(HubCriterion.KNOWN_LIST, None, None, "address_type:off_curve", None)
_DEG = CriterionHit(HubCriterion.DEGREE, 101, 100, "measured", None)
_LIST = CriterionHit(HubCriterion.KNOWN_LIST, None, None, "list:launchpads", 1)


def _record(**kw) -> PruneRecord:
    a, b = _ADDR[0], _ADDR[1]
    base = dict(address=a, criteria=(_DEG, _OFF), incident_edges=(_transfer(a, b),), measures=_QUIET,
                config_version=2, lists_version=1)
    base.update(kw)
    return PruneRecord(**base)


def test_record_accepts_valid_and_normalises_collections_to_tuples():
    r = _record(criteria=[_DEG, _OFF])
    assert isinstance(r.criteria, tuple) and isinstance(r.incident_edges, tuple)


@pytest.mark.parametrize("changes, exc", [
    (dict(criteria=()), ValueError),                                   # SC-002: без пояснення не відсікається
    (dict(criteria=(_OFF, _DEG)), ValueError),                         # не впорядковано за (criterion, detail)
    (dict(criteria=(_DEG, _DEG)), ValueError),                         # дубль хіта
    (dict(criteria=("degree",)), TypeError),
    (dict(incident_edges=(_transfer(_ADDR[2], _ADDR[3]),)), ValueError),  # чуже ребро
    (dict(incident_edges=("edge",)), TypeError),
    (dict(address=""), ValueError),
    (dict(measures=None), TypeError),
    (dict(config_version=0), ValueError),
    (dict(config_version=True), TypeError),
    (dict(lists_version=0), ValueError),
    (dict(criteria=(_LIST,), lists_version=None), ValueError),         # хіт за списком без версії списку
    (dict(criteria=(_LIST,), lists_version=2), ValueError),            # версія хіта != версія запису
])
def test_record_rejects_broken_state(changes, exc):
    with pytest.raises(exc):
        _record(**changes)


def test_record_rejects_unsorted_or_duplicate_incident_edges():
    a = _ADDR[0]
    e1, e2 = sorted((_transfer(a, _ADDR[1]), _transfer(_ADDR[2], a)), key=_edge_key)
    assert _record(incident_edges=(e1, e2)).incident_edges == (e1, e2)
    with pytest.raises(ValueError):
        _record(incident_edges=(e2, e1))
    with pytest.raises(ValueError):
        _record(incident_edges=(e1, e1))


@pytest.mark.parametrize("changes, exc", [
    (dict(criteria=()), ValueError),
    (dict(criteria=(_OFF, _DEG)), ValueError),
    (dict(buyer_rank=None), TypeError),
    (dict(buyer_rank=0), ValueError),
    (dict(address=""), ValueError),
    (dict(measures=None), TypeError),
])
def test_buyer_flag_rejects_broken_state(changes, exc):
    base = dict(address=_ADDR[0], buyer_rank=1, criteria=(_DEG,), measures=_QUIET)
    BuyerFlag(**base)
    base.update(changes)
    with pytest.raises(exc):
        BuyerFlag(**base)


def _outcome_parts():
    hub, buyer = _ADDR[0], _ADDR[1]
    edge = _transfer(hub, buyer)
    full = FundingGraph(nodes=(_node(hub, hub=True), _node(buyer, roles=(NodeRole.BUYER,), rank=1, hub=True)),
                        edges=(edge,))
    record = PruneRecord(hub, (_OFF,), (edge,), _QUIET, 2, 1)
    flag = BuyerFlag(buyer, 1, (_OFF,), _QUIET)
    return full, record, flag


def test_outcome_accepts_consistent_state():
    full, record, flag = _outcome_parts()
    outcome = PruneOutcome(full.without({record.address}), [record], [flag], True, 2, 1)
    assert outcome.records == (record,) and outcome.buyer_flags == (flag,)


def test_outcome_rejects_broken_state():
    full, record, flag = _outcome_parts()
    after = full.without({record.address})
    with pytest.raises(ValueError):  # хаб досі в графі
        PruneOutcome(full, (record,), (flag,), True, 2, 1)
    with pytest.raises(ValueError):  # позначка для вершини поза графом
        PruneOutcome(after, (record,), (BuyerFlag(_ADDR[5], 1, (_OFF,), _QUIET),), True, 2, 1)
    hub_node = full.node(record.address)
    with pytest.raises(ValueError):  # позначка для не-покупця
        PruneOutcome(full, (), (BuyerFlag(hub_node.address, 1, (_OFF,), _QUIET),), True, 2, 1)
    with pytest.raises(ValueError):  # ранг позначки != ранг вершини
        PruneOutcome(after, (record,), (BuyerFlag(flag.address, 2, (_OFF,), _QUIET),), True, 2, 1)
    with pytest.raises(ValueError):  # lists_applied без версії
        PruneOutcome(after, (dataclasses.replace(record, lists_version=None),), (flag,), True, 2, None)
    with pytest.raises(ValueError):  # версія конфігу запису != версія результату
        PruneOutcome(after, (record,), (flag,), True, 3, 1)
    with pytest.raises(ValueError):  # версія списків запису != версія результату
        PruneOutcome(after, (dataclasses.replace(record, lists_version=4),), (flag,), True, 2, 1)
    with pytest.raises(ValueError):  # списки не застосовано, а хіт за списком є
        PruneOutcome(after, (), (BuyerFlag(flag.address, 1, (_LIST,), _QUIET),), False, 2, None)
    with pytest.raises(ValueError):  # дубль запису
        PruneOutcome(after, (record, record), (flag,), True, 2, 1)
    with pytest.raises(TypeError):
        PruneOutcome(after, ("record",), (flag,), True, 2, 1)
    with pytest.raises(TypeError):
        PruneOutcome(after, (record,), (flag,), "yes", 2, 1)


_LIST_DEX = CriterionHit(HubCriterion.KNOWN_LIST, None, None, "list:dex_routers", 1)


def test_record_and_flag_reject_two_list_categories_for_one_address():
    # Інваріант «адреса — не більше ніж в одній категорії списку»: два хіти `list:*` з різними `detail` проходять
    # перевірку порядку й дублів, тож їх ловить лише окреме правило.
    a, b = _ADDR[0], _ADDR[1]
    assert (_LIST_DEX.criterion.value, _LIST_DEX.detail) < (_LIST.criterion.value, _LIST.detail)  # порядок гаразд
    with pytest.raises(ValueError, match="at most one address list category"):
        PruneRecord(a, (_LIST_DEX, _LIST), (_transfer(a, b),), _QUIET, 2, 1)
    with pytest.raises(ValueError, match="at most one address list category"):
        BuyerFlag(b, 1, (_LIST_DEX, _LIST), _QUIET)
    # хіт за списком + `address_type:off_curve` — це два різні джерела `known_list`, не дві категорії: гаразд
    assert PruneRecord(a, (_OFF, _LIST), (_transfer(a, b),), _QUIET, 2, 1).criteria == (_OFF, _LIST)


def test_outcome_rejects_flag_list_hit_with_other_lists_version_even_when_lists_applied():
    full, record, flag = _outcome_parts()
    after = full.without({record.address})
    stale = CriterionHit(HubCriterion.KNOWN_LIST, None, None, "list:launchpads", 2)  # версія 2, у результату — 1
    with pytest.raises(ValueError, match="list hit lists_version != outcome"):
        PruneOutcome(after, (record,), (BuyerFlag(flag.address, 1, (stale,), _QUIET),), True, 2, 1)
    ok = PruneOutcome(after, (record,), (BuyerFlag(flag.address, 1, (_LIST,), _QUIET),), True, 2, 1)
    assert ok.buyer_flags[0].criteria == (_LIST,)


def test_list_hit_without_applied_lists_cannot_be_built_in_record_or_flag_at_all():
    # Хіт за списком вимагає lists_version >= 1; результат без списків має lists_version=None, тож ні запис, ні
    # позначка з таким хітом не проходять власних перевірок (окремої гілки в `PruneOutcome` для цього немає).
    full, record, flag = _outcome_parts()
    after = full.without({record.address})
    with pytest.raises(ValueError):
        dataclasses.replace(record, criteria=(_LIST,), lists_version=None)
    with pytest.raises(ValueError):
        PruneOutcome(after, (), (BuyerFlag(flag.address, 1, (_LIST,), _QUIET),), False, 2, None)
    with pytest.raises(ValueError):
        CriterionHit(HubCriterion.KNOWN_LIST, None, None, "list:launchpads", None)


def test_outcome_rejects_unordered_records_and_flags():
    h1, h2, b1, b2 = sorted(_ADDR[:4])
    graph = FundingGraph(nodes=(_node(b1, roles=(NodeRole.BUYER,), rank=1),
                                _node(b2, roles=(NodeRole.BUYER,), rank=2)), edges=())
    r1, r2 = PruneRecord(h1, (_OFF,), (), _QUIET, 2, None), PruneRecord(h2, (_OFF,), (), _QUIET, 2, None)
    f1, f2 = BuyerFlag(b1, 1, (_OFF,), _QUIET), BuyerFlag(b2, 2, (_OFF,), _QUIET)
    PruneOutcome(graph, (r1, r2), (f1, f2), False, 2, None)
    with pytest.raises(ValueError):
        PruneOutcome(graph, (r2, r1), (f1, f2), False, 2, None)
    with pytest.raises(ValueError):
        PruneOutcome(graph, (r1, r2), (f2, f1), False, 2, None)


def test_hubs_prune_imports_no_ingest_internals():
    import ast
    source = (ROOT / "src" / "unmask" / "hubs" / "prune.py").read_text(encoding="utf-8")
    forbidden = {f"unmask.ingest.{m}" for m in
                 ("rpc", "collector", "buyers", "funding", "cache", "service", "budget", "addresses")}
    forbidden |= {"socket", "httpx", "requests"}
    for node in ast.walk(ast.parse(source)):
        names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
            [node.module] if isinstance(node, ast.ImportFrom) and node.module else []
        for name in names:
            assert not any(name == f or name.startswith(f + ".") for f in forbidden), name
