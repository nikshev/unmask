# verifies: FR-002-02, FR-002-03, FR-002-05, FR-002-06, FR-002-18, FR-002-22
"""Модель графа фінансування (T-025): інваріанти, похідна повнота, ключі порядку.

Критична задача: помилка тут не ламає збірку, а тихо спотворює граф, звіт і
кластеризацію далі. Тому окрім щасливого шляху перевіряється, що суперечливий
об'єкт неможливо сконструювати жодним прямим шляхом (конструктор,
`dataclasses.replace`), а похідні значення слідують за даними навіть після
примусового `object.__setattr__`.

T-055 (FR-002-22, research R-22) розширює модель критерієм `dust_fanout`,
вимірами `buyer_fanout`/`median_to_buyers` і пиловими порогами знімка; поля
без умовчань, тож усі конструктори нижче передають їх явно.
"""

import dataclasses
import inspect
import itertools
import json
import random
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from unmask.graph.model import (
    GRAPH_SCHEMA_VERSION,
    Edge,
    EdgeKind,
    EdgeRef,
    FundingGraph,
    GraphCompleteness,
    GraphCompletenessStatus,
    GraphInputError,
    GraphMetadata,
    GraphResult,
    GraphWarning,
    HubCriterion,
    MissingRef,
    Node,
    NodeMeasures,
    NodeRole,
    ThresholdsSnapshot,
    UnexpandedMark,
    edge_sort_key,
    node_sort_key,
    ref_sort_key,
)
from unmask.ingest.model import (
    AddressType,
    Asset,
    BuyersCompleteness,
    Completeness,
    CompletenessStatus,
    IngestResult,
    MissingHistory,
    MissingReason,
    RejectKind,
    Rejection,
    RunMetadata,
    UnexpandedReason,
)

A = "4Nd1mYtq3oG7Q2bKjv9b8yXbH9QJm6N3s1R5o2nZ8kPq"
B = "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin"
C = "7dHbWXmci3dT8UFYWYZweBLXgycu7Y3iL6trKn1Y7ARj"
D = "2wmVCSfPxGPjrnMMn7rchp4uaeoTqN39mXFC2zhPdri9"
H = "HubHubHubHubHubHubHubHubHubHubHubHubHubHub11"
MINT = "So11111111111111111111111111111111111111112"
SPL = "spl:" + MINT
SIG_A = "5" + "A" * 87
SIG_B = "5" + "B" * 87
SIG_C = "5" + "C" * 87

BUYER = NodeRole.BUYER
FUNDER = NodeRole.FUNDER
PAYER = NodeRole.DELEGATED_PAYER
RECEIVER = NodeRole.DELEGATED_RECEIVER
TRANSFER = EdgeKind.TRANSFER
DELEGATED = EdgeKind.DELEGATED_BUY


GRAPH_SCHEMA = json.loads(
    (Path(__file__).resolve().parents[1]
     / "specs/002-funding-graph-hub-pruning/contracts/graph-result.schema.json").read_text(encoding="utf-8")
)


def _schema_validator(definition):
    return Draft202012Validator({"$ref": f"#/$defs/{definition}", "$defs": GRAPH_SCHEMA["$defs"]})


# --- Будівельники ------------------------------------------------------------------


def _measures(degree=0, unique=0, one_off=0, share="auto", fanout=0, median="auto"):
    if share == "auto":
        share = None if unique == 0 else one_off / unique
    if median == "auto":
        median = None if fanout == 0 else 500_000
    return NodeMeasures(degree=degree, unique_senders=unique, one_off_senders=one_off, one_off_share=share,
                        buyer_fanout=fanout, median_to_buyers=median)


def _node(address, roles=(FUNDER,), depth=None, rank=None, **changes):
    roles = frozenset(roles)
    if depth is None:
        depth = 0 if BUYER in roles else 1
    if rank is None and BUYER in roles:
        rank = 1
    kw = dict(
        address=address,
        roles=roles,
        depth=depth,
        buyer_rank=rank,
        address_type=AddressType.WALLET,
        unexpanded=None,
        measures=_measures(),
    )
    kw.update(changes)
    return Node(**kw)


def _buyer(address, rank=1, roles=(BUYER,), **changes):
    return _node(address, roles=roles, depth=0, rank=rank, **changes)


def _ref(sig=SIG_A, slot=100, path="0"):
    return EdgeRef(signature=sig, slot=slot, instruction_path=path)


def _slot(fn, refs):
    slots = [r.slot for r in refs if isinstance(r, EdgeRef)]
    return fn(slots) if slots else 0


def _transfer(sender=B, receiver=A, refs=None, asset="sol", amount=1_000, decimals=None, **changes):
    refs = tuple(refs) if refs is not None else (_ref(),)
    kw = dict(
        kind=TRANSFER,
        sender=sender,
        receiver=receiver,
        asset=asset,
        amount=amount,
        decimals=decimals,
        count=len(refs),
        first_slot=_slot(min, refs),
        last_slot=_slot(max, refs),
        first_time=1_700_000_000,
        last_time=1_700_000_000,
        refs=refs,
    )
    kw.update(changes)
    return Edge(**kw)


def _delegated(sender=B, receiver=A, refs=None, **changes):
    refs = tuple(refs) if refs is not None else (_ref(path=None),)
    kw = dict(
        kind=DELEGATED,
        sender=sender,
        receiver=receiver,
        asset=None,
        amount=None,
        decimals=None,
        count=len(refs),
        first_slot=_slot(min, refs),
        last_slot=_slot(max, refs),
        first_time=None,
        last_time=None,
        refs=refs,
    )
    kw.update(changes)
    return Edge(**kw)


def _basic_graph():
    """A (покупець, rank 1), C (покупець-джерело, rank 2), B і D — джерела."""
    nodes = [
        _buyer(A, rank=1),
        _node(B, roles=(FUNDER,), depth=1),
        _buyer(C, rank=2, roles=(BUYER, FUNDER)),
        _node(D, roles=(FUNDER,), depth=2),
    ]
    edges = [
        _transfer(B, A, refs=[_ref(SIG_A, 100, "0")]),
        _transfer(C, A, refs=[_ref(SIG_B, 101, "1")], asset=SPL, decimals=6),
        _transfer(D, B, refs=[_ref(SIG_C, 90, "0")]),
        _delegated(B, C, refs=[_ref(SIG_C, 91, None)]),
    ]
    return FundingGraph(nodes=tuple(nodes), edges=tuple(edges))


def _run_metadata(wallets_analyzed):
    return RunMetadata(
        mint=MINT,
        analyzed_at=1_700_000_100,
        wallets_analyzed=wallets_analyzed,
        config_version=1,
        first_buyers_n=300,
        funding_depth=2,
        counterparty_threshold=200,
        max_signatures_per_wallet=300,
        collect_spl_inbound=True,
        time_budget_seconds=40.0,
        elapsed_seconds=1.5,
        rpc_calls=3,
        transactions_scanned=2,
        source="fixture:g_basic",
        resumed=False,
        served_from_cache=False,
    )


def _missing(wallet=A, depth=1, reason=MissingReason.TIMEOUT, detail="getSignaturesForAddress"):
    return MissingHistory(wallet=wallet, depth=depth, reason=reason, detail=detail)


BUYERS_OK = BuyersCompleteness(complete=True, reason=None, detail="")
BUYERS_CUT = BuyersCompleteness(complete=False, reason=MissingReason.BUDGET_EXHAUSTED, detail="cursor")


def _ingest(missing=(), buyers=BUYERS_OK, delegated="absent"):
    result = IngestResult(
        metadata=_run_metadata(0),
        completeness=Completeness.derive(missing, buyers),
        buyers=(),
        transfers=(),
        unexpanded=(),
    )
    if delegated != "absent":
        # До T-044 у IngestResult немає поля `delegated`; додаємо його так, як його
        # читатиме derive після T-044 (`.complete`, `.reason`).
        object.__setattr__(result, "delegated", delegated)
    return result


DELEGATED_OK = SimpleNamespace(complete=True, reason=None)
DELEGATED_TIMEOUT = SimpleNamespace(complete=False, reason=MissingReason.TIMEOUT)
DELEGATED_NOT_ANALYZED = SimpleNamespace(complete=False, reason="not_analyzed")


def _thresholds(**changes):
    kw = dict(
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
    return ThresholdsSnapshot(**kw)


def _metadata(wallets_analyzed=2, nodes_total=4, lists_version=1, lists_applied=True, **changes):
    kw = dict(
        mint=MINT,
        schema_version=GRAPH_SCHEMA_VERSION,
        ingest_analyzed_at=1_700_000_100,
        ingest_config_version=1,
        ingest_source="fixture:g_basic",
        wallets_analyzed=wallets_analyzed,
        hub_config_version=1,
        address_lists_version=lists_version,
        lists_applied=lists_applied,
        thresholds=_thresholds(),
        nodes_total=nodes_total,
        edges_total=4,
    )
    kw.update(changes)
    return GraphMetadata(**kw)


@dataclass(frozen=True)
class _Pruned:
    """Заглушка `PruneRecord` (hubs, T-035): GraphResult бачить `address`, `incident_edges` і версії (T-039)."""

    address: str
    incident_edges: tuple = ()
    config_version: int = 1  # == _metadata().hub_config_version
    lists_version: int | None = 1  # == _metadata().address_lists_version


@dataclass(frozen=True)
class _Snap:
    """Заглушка `EffectSnapshot`: GraphResult звіряє `nodes`, `edges`, `buyers_total` (T-039)."""

    nodes: int
    edges: int
    buyers_total: int


@dataclass(frozen=True)
class _Report:
    """Заглушка `EffectReport` (hubs, T-036): GraphResult бачить `warnings`, а з T-039 — і знімки та `pruned_nodes`.

    Знімки, не задані явно, `_result` заповнює узгоджено з метаданими й графом: ці тести перевіряють інші інваріанти.
    """

    warnings: tuple
    before: _Snap | None = None
    after: _Snap | None = None
    pruned_nodes: int | None = None


def _result(graph=None, pruned=(), metadata=None, report=None, completeness=None):
    graph = graph if graph is not None else _basic_graph()
    metadata = metadata if metadata is not None else _metadata(nodes_total=len(graph.nodes) + len(pruned))
    report = report if report is not None else _Report(warnings=())
    if report.before is None:
        report = dataclasses.replace(
            report,
            before=_Snap(metadata.nodes_total, metadata.edges_total, metadata.wallets_analyzed),
            after=_Snap(len(graph.nodes), len(graph.edges), metadata.wallets_analyzed),
            pruned_nodes=len(pruned),
        )
    return GraphResult(
        metadata=metadata,
        # Аналіз делегованих повний: інакше GraphResult (T-039) вимагає попередження `delegated_incomplete`.
        completeness=(completeness if completeness is not None
                      else GraphCompleteness.derive(_ingest(delegated=DELEGATED_OK))),
        graph=graph,
        pruned=tuple(pruned),
        buyer_flags=(),
        report=report,
    )


# --- Перелічення ------------------------------------------------------------------


def test_enum_values_exactly_as_data_model():
    assert {r.value for r in NodeRole} == {"buyer", "funder", "delegated_payer", "delegated_receiver"}
    assert {k.value for k in EdgeKind} == {"transfer", "delegated_buy"}
    assert {c.value for c in HubCriterion} == {
        "known_list", "degree", "one_off_senders", "ingest_high_degree", "dust_fanout",
    }
    assert {w.value for w in GraphWarning} == {
        "address_lists_not_applied", "giant_component", "empty_graph", "all_sources_pruned",
        "delegated_incomplete",
    }
    assert {s.value for s in GraphCompletenessStatus} == {"complete", "incomplete"}
    assert GRAPH_SCHEMA_VERSION == "002.1"
    assert issubclass(GraphInputError, Exception)


def test_hub_criterion_enum_has_five_values_including_dust_fanout():
    # FR-002-22: п'ятий критерій; порядок оголошення і значення — як у data-model та схемі.
    assert HubCriterion.DUST_FANOUT == "dust_fanout"
    assert HubCriterion("dust_fanout") is HubCriterion.DUST_FANOUT
    assert [c.value for c in HubCriterion] == [
        "known_list", "degree", "one_off_senders", "ingest_high_degree", "dust_fanout",
    ]
    assert len(HubCriterion) == 5
    hit = GRAPH_SCHEMA["$defs"]["criterionHit"]["properties"]["criterion"]["enum"]
    assert [c.value for c in HubCriterion] == hit


def test_criteria_sorted_as_strings_put_dust_fanout_second():
    # Порядок хітів — за рядковим значенням (data-model, graph-service): dust_fanout другий.
    expected = ["degree", "dust_fanout", "ingest_high_degree", "known_list", "one_off_senders"]
    assert [c.value for c in sorted(HubCriterion)] == expected
    assert [c.value for c in sorted(HubCriterion, key=lambda c: c.value)] == expected
    assert sorted(reversed(list(HubCriterion)))[1] is HubCriterion.DUST_FANOUT


# --- NodeMeasures -----------------------------------------------------------------


def test_measures_invariants_exhaustive():
    # Перебір решітки (unique, one_off, share): конструюється рівно узгоджене.
    for unique, one_off in itertools.product(range(4), range(4)):
        for share in (None, 0.0, 0.5, 1.0, "ratio"):
            value = (one_off / unique if unique else 0.0) if share == "ratio" else share
            ok = (
                one_off <= unique
                and ((value is None) == (unique == 0))
                and (value is None or value == one_off / unique)
            )
            build = lambda: NodeMeasures(degree=5, unique_senders=unique, one_off_senders=one_off,
                                         one_off_share=value, buyer_fanout=0, median_to_buyers=None)
            if ok:
                m = build()
                assert m.one_off_share == (None if unique == 0 else one_off / unique)
            else:
                with pytest.raises(ValueError):
                    build()


def test_measures_boundary_one_off_equals_unique_is_valid():
    m = _measures(degree=3, unique=3, one_off=3)
    assert m.one_off_share == 1.0
    with pytest.raises(ValueError):
        _measures(degree=3, unique=3, one_off=4, share=4 / 3)


@pytest.mark.parametrize(
    "field,bad",
    [("degree", -1), ("degree", 1.0), ("degree", True), ("unique_senders", -1), ("unique_senders", None),
     ("one_off_senders", -1), ("one_off_senders", "1"), ("one_off_share", "0.5"), ("one_off_share", True),
     ("one_off_share", 1), ("buyer_fanout", -1), ("buyer_fanout", True), ("buyer_fanout", 1.0),
     ("buyer_fanout", None), ("buyer_fanout", "1"), ("median_to_buyers", 0), ("median_to_buyers", -1),
     ("median_to_buyers", 1.0), ("median_to_buyers", True), ("median_to_buyers", "500000"),
     ("median_to_buyers", None)],
)
def test_measures_reject_wrong_types(field, bad):
    kw = dict(degree=2, unique_senders=1, one_off_senders=1, one_off_share=1.0, buyer_fanout=1,
              median_to_buyers=500_000)
    kw[field] = bad
    with pytest.raises((TypeError, ValueError)):
        NodeMeasures(**kw)


def _dust(fanout, median):
    return NodeMeasures(degree=7, unique_senders=0, one_off_senders=0, one_off_share=None,
                        buyer_fanout=fanout, median_to_buyers=median)


@pytest.mark.parametrize(
    "fanout,median,error",
    [(0, None, None), (0, 5, ValueError), (3, None, ValueError), (3, 0, ValueError), (3, 1, None),
     (3, True, TypeError),
     # межові й суміжні (FR-002-22, R-22)
     (1, 1, None), (1, 0, ValueError), (0, 0, ValueError), (0, 1, ValueError), (0, -1, ValueError),
     (3, -1, ValueError), (3, 10**18, None), (0, False, TypeError), (3, False, TypeError),
     (3, 1.0, TypeError), (3, "1", TypeError), (0, True, TypeError)],
)
def test_measures_median_none_iff_zero_fanout_and_at_least_one_otherwise(fanout, median, error):
    if error is None:
        m = _dust(fanout, median)
        assert (m.buyer_fanout, m.median_to_buyers) == (fanout, median)
        assert type(m.median_to_buyers) in (int, type(None))
    else:
        with pytest.raises(error):
            _dust(fanout, median)


_FANOUTS = (-1, 0, 1, 2, 5, 23)
_MEDIANS = (None, -1, 0, 1, 2, 999_999, 1_000_000, 10**18, True, False, 1.0, 0.5, "1")


def _dust_expected(fanout, median):
    """Еталон інваріанта, записаний незалежно від реалізації: None, TypeError або ValueError."""
    if fanout < 0:
        return ValueError
    if median is not None and (isinstance(median, bool) or not isinstance(median, int)):
        return TypeError
    if (median is None) != (fanout == 0):
        return ValueError
    if median is not None and median < 1:
        return ValueError
    return None


@pytest.mark.parametrize("unique,one_off", [(0, 0), (3, 1), (4, 4)])
def test_measures_dust_invariant_exhaustive(unique, one_off):
    # Перебір усіх комбінацій fanout × median, незалежно від гілки one_off_share
    # (unique == 0 і unique > 0): перевірка пилу не може бути пропущена ранім виходом.
    share = None if unique == 0 else one_off / unique
    for fanout, median in itertools.product(_FANOUTS, _MEDIANS):
        build = lambda: NodeMeasures(degree=9, unique_senders=unique, one_off_senders=one_off,
                                     one_off_share=share, buyer_fanout=fanout, median_to_buyers=median)
        expected = _dust_expected(fanout, median)
        if expected is None:
            m = build()
            assert (m.buyer_fanout, m.median_to_buyers) == (fanout, median)
        else:
            with pytest.raises(expected):
                build()


def test_measures_fields_have_no_defaults_and_order_as_data_model():
    fields = dataclasses.fields(NodeMeasures)
    assert [f.name for f in fields] == [
        "degree", "unique_senders", "one_off_senders", "one_off_share", "buyer_fanout", "median_to_buyers",
    ]
    assert all(f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING for f in fields)
    with pytest.raises(TypeError):
        NodeMeasures(degree=0, unique_senders=0, one_off_senders=0, one_off_share=None)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        NodeMeasures(degree=0, unique_senders=0, one_off_senders=0, one_off_share=None,  # type: ignore[call-arg]
                     buyer_fanout=0)


def test_measures_replace_cannot_produce_dust_contradiction():
    m = _measures(degree=6, fanout=6, median=1_500)
    with pytest.raises(ValueError):
        dataclasses.replace(m, buyer_fanout=0)
    with pytest.raises(ValueError):
        dataclasses.replace(m, median_to_buyers=None)
    with pytest.raises(ValueError):
        dataclasses.replace(m, median_to_buyers=0)
    with pytest.raises(TypeError):
        dataclasses.replace(m, median_to_buyers=True)
    zero = _measures()
    with pytest.raises(ValueError):
        dataclasses.replace(zero, median_to_buyers=1)
    with pytest.raises(ValueError):
        dataclasses.replace(zero, buyer_fanout=2)
    # Узгоджена заміна обох полів — дозволена в обидва боки.
    assert dataclasses.replace(m, buyer_fanout=0, median_to_buyers=None).median_to_buyers is None
    assert dataclasses.replace(zero, buyer_fanout=2, median_to_buyers=7).median_to_buyers == 7
    with pytest.raises(dataclasses.FrozenInstanceError):
        m.median_to_buyers = None  # type: ignore[misc]


def test_measures_forced_contradiction_does_not_survive_rebuild():
    # Примусовий object.__setattr__ обходить конструктор, але будь-яке перебудування
    # (replace, як у T-025) знову перевіряє інваріант — суперечність не поширюється.
    for field, value, error in [("buyer_fanout", 0, ValueError), ("median_to_buyers", None, ValueError),
                                ("median_to_buyers", 0, ValueError), ("median_to_buyers", True, TypeError),
                                ("buyer_fanout", -1, ValueError)]:
        m = _measures(fanout=4, median=10)
        object.__setattr__(m, field, value)
        with pytest.raises(error):
            dataclasses.replace(m)
    zero = _measures()
    object.__setattr__(zero, "median_to_buyers", 5)
    with pytest.raises(ValueError):
        dataclasses.replace(zero)


def test_measures_dust_fields_follow_schema_on_lattice():
    # Модель і контракт (`$defs/measures`) погоджуються на кожній цілочисловій комбінації.
    validator = _schema_validator("measures")
    for fanout, median in itertools.product((0, 1, 2, 5), (None, 0, 1, 2, 1_000_000)):
        doc = dict(degree=3, unique_senders=0, one_off_senders=0, one_off_share=None,
                   buyer_fanout=fanout, median_to_buyers=median)
        model_ok = _dust_expected(fanout, median) is None
        assert validator.is_valid(doc) is model_ok, (fanout, median)
        if model_ok:
            assert dataclasses.asdict(NodeMeasures(**doc)) == doc


# --- UnexpandedMark ---------------------------------------------------------------


def test_unexpanded_mark_both_reasons_and_validation():
    for reason in UnexpandedReason:
        m = UnexpandedMark(reason=reason.value, counterparties_seen=201, signatures_seen=300,
                           signatures_truncated=False)
        assert m.reason is reason
    assert {f.name for f in dataclasses.fields(UnexpandedMark)} == {
        "reason", "counterparties_seen", "signatures_seen", "signatures_truncated",
    }
    good = dict(reason="high_degree", counterparties_seen=1, signatures_seen=1, signatures_truncated=True)
    for field, bad in [("reason", "too_many"), ("counterparties_seen", -1), ("signatures_seen", 1.5),
                       ("signatures_truncated", "true"), ("signatures_truncated", 1)]:
        with pytest.raises((TypeError, ValueError)):
            UnexpandedMark(**{**good, field: bad})


def test_node_keeps_unexpanded_mark_as_attribute():
    mark = UnexpandedMark(reason=UnexpandedReason.SIGNATURE_CAP, counterparties_seen=3, signatures_seen=300,
                          signatures_truncated=True)
    n = _node(H, unexpanded=mark)
    assert n.unexpanded is mark
    with pytest.raises(TypeError):
        _node(H, unexpanded={"reason": "high_degree"})


# --- Node --------------------------------------------------------------------------


ALL_ROLE_SETS = [frozenset(c) for k in range(0, 5) for c in itertools.combinations(list(NodeRole), k)]


def test_node_buyer_role_iff_rank_and_depth_zero():
    for roles, rank, depth in itertools.product(ALL_ROLE_SETS, (None, 1, 7), (0, 1, 2)):
        ok = bool(roles) and ((BUYER in roles) == (rank is not None)) and (BUYER not in roles or depth == 0)
        build = lambda: Node(address=A, roles=roles, depth=depth, buyer_rank=rank,
                             address_type=AddressType.WALLET, unexpanded=None, measures=_measures())
        if ok:
            n = build()
            assert n.roles == roles
            assert (BUYER in n.roles) == (n.buyer_rank is not None)
        else:
            with pytest.raises(ValueError):
                build()


def test_node_replace_cannot_produce_contradiction():
    buyer = _buyer(A, rank=3)
    with pytest.raises(ValueError):
        dataclasses.replace(buyer, buyer_rank=None)
    with pytest.raises(ValueError):
        dataclasses.replace(buyer, depth=1)
    with pytest.raises(ValueError):
        dataclasses.replace(buyer, roles=frozenset({FUNDER}))
    with pytest.raises(ValueError):
        dataclasses.replace(buyer, roles=frozenset())
    funder = _node(B)
    with pytest.raises(ValueError):
        dataclasses.replace(funder, buyer_rank=1)
    # Узгоджена заміна — дозволена.
    assert dataclasses.replace(funder, roles={BUYER, FUNDER}, depth=0, buyer_rank=2).buyer_rank == 2


@pytest.mark.parametrize(
    "field,bad",
    [("address", ""), ("address", None), ("depth", -1), ("depth", 1.0), ("depth", True),
     ("buyer_rank", 0), ("buyer_rank", True), ("buyer_rank", 1.0), ("address_type", "pda"),
     ("roles", "buyer"), ("roles", {"whale"}), ("roles", None), ("measures", None),
     ("measures", {"degree": 0})],
)
def test_node_rejects_invalid_fields(field, bad):
    kw = dict(address=A, roles=frozenset({BUYER}), depth=0, buyer_rank=1, address_type=AddressType.WALLET,
              unexpanded=None, measures=_measures())
    kw[field] = bad
    with pytest.raises((TypeError, ValueError)):
        Node(**kw)


def test_node_normalises_roles_and_address_type():
    n = Node(address=A, roles=["buyer", "funder", "buyer"], depth=0, buyer_rank=1, address_type="off_curve",
             unexpanded=None, measures=_measures())
    assert isinstance(n.roles, frozenset)
    assert n.roles == {BUYER, FUNDER}
    assert all(isinstance(r, NodeRole) for r in n.roles)
    assert n.address_type is AddressType.OFF_CURVE
    with pytest.raises(AttributeError):
        n.depth = 3  # type: ignore[misc]


# --- EdgeRef ----------------------------------------------------------------------


@pytest.mark.parametrize("path", ["0", "12", "2.1", "10.11"])
def test_edge_ref_accepts_instruction_paths(path):
    assert _ref(path=path).instruction_path == path


@pytest.mark.parametrize(
    "field,bad",
    [("signature", ""), ("signature", None), ("slot", -1), ("slot", 1.0), ("slot", True),
     ("instruction_path", ""), ("instruction_path", "1.2.3"), ("instruction_path", "a"),
     ("instruction_path", 1)],
)
def test_edge_ref_rejects_invalid_fields(field, bad):
    kw = dict(signature=SIG_A, slot=1, instruction_path="0")
    kw[field] = bad
    with pytest.raises((TypeError, ValueError)):
        EdgeRef(**kw)


# --- Edge: види, агрегати, посилання -----------------------------------------------


def test_edge_kinds_never_share_a_key_and_delegated_carries_no_asset_or_amount():
    t = _transfer(B, A)
    d = _delegated(B, A)
    assert t.key == (TRANSFER, B, A, Asset.SOL)
    assert d.key == (DELEGATED, B, A, None)
    assert t.key != d.key
    # Та сама пара: переказ і делегована купівля співіснують як два ребра (FR-002-18).
    g = FundingGraph(nodes=(_buyer(A), _node(B, roles=(FUNDER, PAYER))), edges=(t, d))
    assert len(g.edges) == 2
    assert {e.kind for e in g.edges} == {TRANSFER, DELEGATED}

    # delegated_buy не несе ні активу, ні суми, ні decimals, ні шляху інструкції.
    for field, bad in [("asset", "sol"), ("asset", SPL), ("amount", 1), ("amount", 5_000), ("decimals", 0),
                       ("decimals", 9)]:
        with pytest.raises(ValueError):
            _delegated(**{field: bad})
    with pytest.raises(ValueError):
        _delegated(refs=[_ref(path="0")])
    with pytest.raises(ValueError):
        _delegated(refs=[_ref(SIG_A, 1, None), _ref(SIG_B, 2, "3")])
    with pytest.raises(ValueError):
        dataclasses.replace(d, asset=Asset.SOL)
    with pytest.raises(ValueError):
        dataclasses.replace(d, amount=1)
    # Зміна виду без зміни полів — суперечність в обидва боки.
    with pytest.raises(ValueError):
        dataclasses.replace(t, kind=DELEGATED)
    with pytest.raises(ValueError):
        dataclasses.replace(d, kind=TRANSFER)


def test_transfer_edge_requires_asset_amount_and_instruction_paths():
    assert _transfer().asset == Asset.SOL
    assert isinstance(_transfer(asset="sol").asset, Asset)
    assert _transfer(asset=SPL, decimals=6).decimals == 6
    assert _transfer(asset=SPL, decimals=None).decimals is None
    for field, bad in [("asset", None), ("asset", "usdc"), ("asset", "spl:"), ("amount", None),
                       ("amount", 0), ("amount", -1), ("amount", 1.0), ("amount", 1e3), ("amount", True),
                       ("amount", "1000"), ("decimals", -1), ("decimals", 256), ("decimals", 6.0)]:
        with pytest.raises((TypeError, ValueError)):
            _transfer(**{field: bad})
    # sol не має decimals.
    with pytest.raises(ValueError):
        _transfer(asset="sol", decimals=9)
    # Кожне посилання переказу несе шлях інструкції.
    with pytest.raises(ValueError):
        _transfer(refs=[_ref(path=None)])
    with pytest.raises(ValueError):
        _transfer(refs=[_ref(SIG_A, 1, "0"), _ref(SIG_B, 2, None)])
    assert _transfer(amount=1).amount == 1  # межа: amount ≥ 1


def test_edge_rejects_self_transfer_and_bad_endpoints():
    with pytest.raises(ValueError):
        _transfer(A, A)
    with pytest.raises(ValueError):
        _delegated(B, B)
    with pytest.raises(ValueError):
        dataclasses.replace(_transfer(B, A), receiver=B)
    for field, bad in [("sender", ""), ("receiver", ""), ("sender", None), ("kind", "swap"), ("kind", None)]:
        with pytest.raises((TypeError, ValueError)):
            _transfer(**{field: bad})
    assert _transfer(kind="transfer").kind is TRANSFER


def test_edge_count_equals_refs_and_refs_unique():
    refs = [_ref(SIG_A, 10, "0"), _ref(SIG_A, 10, "1"), _ref(SIG_B, 12, "0")]
    e = _transfer(refs=refs)
    assert e.count == len(e.refs) == 3
    for bad_count in (0, 2, 4, -1):
        with pytest.raises(ValueError):
            _transfer(refs=refs, count=bad_count)
    with pytest.raises(TypeError):
        _transfer(refs=refs, count=3.0)
    with pytest.raises(ValueError):
        dataclasses.replace(e, count=1)
    with pytest.raises(ValueError):
        dataclasses.replace(e, refs=e.refs[:2])
    # Порожні посилання — ребро без доказу (принцип V).
    with pytest.raises(ValueError):
        _transfer(refs=[], count=0)
    with pytest.raises(ValueError):
        _transfer(refs=[], count=1)
    # Дубль (signature, instruction_path) — заборонено; той самий підпис з іншим шляхом — ні.
    with pytest.raises(ValueError):
        _transfer(refs=[_ref(SIG_A, 10, "0"), _ref(SIG_A, 10, "0")])
    assert _transfer(refs=[_ref(SIG_A, 10, "0"), _ref(SIG_A, 10, "0.1")]).count == 2
    # delegated_buy: без дублів підпису.
    with pytest.raises(ValueError):
        _delegated(refs=[_ref(SIG_A, 10, None), _ref(SIG_A, 10, None)])
    assert _delegated(refs=[_ref(SIG_A, 10, None), _ref(SIG_B, 10, None)]).count == 2
    with pytest.raises(TypeError):
        _transfer(refs=[("sig", 1, "0")])


def test_edge_first_last_slot_come_from_refs():
    refs = [_ref(SIG_A, 30, "0"), _ref(SIG_B, 10, "0"), _ref(SIG_C, 20, "0")]
    e = _transfer(refs=refs)
    assert (e.first_slot, e.last_slot) == (10, 30)
    # first ≤ last; і обидва — рівно з посилань.
    with pytest.raises(ValueError):
        _transfer(refs=refs, first_slot=30, last_slot=10)
    with pytest.raises(ValueError):
        _transfer(refs=refs, first_slot=11)
    with pytest.raises(ValueError):
        _transfer(refs=refs, first_slot=9)
    with pytest.raises(ValueError):
        _transfer(refs=refs, last_slot=29)
    with pytest.raises(ValueError):
        _transfer(refs=refs, last_slot=31)
    # Одне посилання: first == last — валідна межа.
    single = _transfer(refs=[_ref(SIG_A, 7, "0")])
    assert single.first_slot == single.last_slot == 7
    for field, bad in [("first_time", -1), ("last_time", "1"), ("first_time", 1.5)]:
        with pytest.raises((TypeError, ValueError)):
            _transfer(**{field: bad})
    assert _transfer(first_time=None, last_time=None).first_time is None


def test_edge_refs_order_is_canonical_regardless_of_input_order():
    refs = [_ref(SIG_B, 5, "0"), _ref(SIG_A, 5, "10"), _ref(SIG_A, 5, "2"), _ref(SIG_A, 5, "2.1"),
            _ref(SIG_C, 4, "3")]
    expected = sorted(refs, key=ref_sort_key)
    rng = random.Random(7)
    for _ in range(20):
        shuffled = refs[:]
        rng.shuffle(shuffled)
        e = _transfer(refs=shuffled)
        assert e.refs == tuple(expected)
        assert isinstance(e.refs, tuple)
    assert [r.instruction_path for r in expected] == ["3", "2", "2.1", "10", "0"]


# --- FundingGraph -----------------------------------------------------------------


def test_graph_rejects_dangling_edge_and_duplicate_address():
    a, b = _buyer(A), _node(B)
    with pytest.raises(ValueError):  # отримувач не вершина
        FundingGraph(nodes=(b,), edges=(_transfer(B, A),))
    with pytest.raises(ValueError):  # відправник не вершина
        FundingGraph(nodes=(a,), edges=(_transfer(B, A),))
    with pytest.raises(ValueError):  # делегована — так само
        FundingGraph(nodes=(a,), edges=(_delegated(B, A),))
    with pytest.raises(ValueError):  # дубль адреси навіть з іншими ролями
        FundingGraph(nodes=(a, b, _node(B, roles=(PAYER,))), edges=())
    with pytest.raises(ValueError):  # дубль ключа (kind, sender, receiver, asset)
        FundingGraph(nodes=(a, b), edges=(_transfer(B, A, refs=[_ref(SIG_A)]),
                                          _transfer(B, A, refs=[_ref(SIG_B)])))
    with pytest.raises(ValueError):
        FundingGraph(nodes=(a, b), edges=(_delegated(B, A, refs=[_ref(SIG_A, path=None)]),
                                          _delegated(B, A, refs=[_ref(SIG_B, path=None)])))
    # Різні активи однієї пари — різні ключі.
    g = FundingGraph(nodes=(a, b), edges=(_transfer(B, A), _transfer(B, A, asset=SPL, decimals=6)))
    assert len(g.edges) == 2
    with pytest.raises(TypeError):
        FundingGraph(nodes=({"address": A},), edges=())
    with pytest.raises(TypeError):
        FundingGraph(nodes=(a,), edges=("edge",))
    g = _basic_graph()
    with pytest.raises(ValueError):
        dataclasses.replace(g, nodes=g.nodes[1:])  # A зникає — ребра до A висять
    with pytest.raises(ValueError):
        dataclasses.replace(g, edges=g.edges + g.edges[:1])


def test_empty_graph_is_valid():
    g = FundingGraph(nodes=(), edges=())
    assert g.nodes == () and g.edges == () and g.buyers() == ()
    assert FundingGraph(nodes=[], edges=[]) == g
    assert g.without(frozenset()) == g


def test_graph_sorts_canonically_regardless_of_input_order():
    g = _basic_graph()
    assert [n.address for n in g.nodes] == sorted(n.address for n in g.nodes)
    assert list(g.edges) == sorted(g.edges, key=edge_sort_key)
    rng = random.Random(3)
    nodes, edges = list(g.nodes), list(g.edges)
    for _ in range(30):
        rng.shuffle(nodes)
        rng.shuffle(edges)
        g2 = FundingGraph(nodes=nodes, edges=edges)
        assert g2 == g
        assert repr(g2) == repr(g)
        assert isinstance(g2.nodes, tuple) and isinstance(g2.edges, tuple)


def test_graph_node_incident_buyers():
    g = _basic_graph()
    assert g.node(B).address == B
    with pytest.raises(KeyError):
        g.node(H)
    # incident — обидва кінці, обидва види, канонічний порядок.
    inc = g.incident(B)
    assert {(e.kind, e.sender, e.receiver) for e in inc} == {
        (TRANSFER, B, A), (TRANSFER, D, B), (DELEGATED, B, C),
    }
    assert list(inc) == sorted(inc, key=edge_sort_key)
    assert isinstance(inc, tuple)
    with pytest.raises(KeyError):
        g.incident(H)
    assert [n.address for n in g.buyers()] == sorted([A, C])
    assert all(BUYER in n.roles for n in g.buyers())


def test_graph_lookups_follow_data_even_if_fields_are_forced():
    g = _basic_graph()
    g.node(A)  # прогріти можливий індекс
    object.__setattr__(g, "nodes", tuple(n for n in g.nodes if n.address != C))
    with pytest.raises(KeyError):
        g.node(C)
    assert [n.address for n in g.buyers()] == [A]


def test_graph_without_removes_nodes_and_incident_edges_only():
    g = _basic_graph()
    before = repr(g)
    g2 = g.without(frozenset({B}))
    assert {n.address for n in g2.nodes} == {A, C, D}
    # Зникли рівно ребра з B на будь-якому кінці (B відправник і B отримувач).
    assert set(g2.edges) == {e for e in g.edges if B not in (e.sender, e.receiver)}
    assert {(e.sender, e.receiver) for e in g2.edges} == {(C, A)}
    # Решта вершин — ті самі об'єкти даних, виміри не перераховані.
    for n in g2.nodes:
        assert n == g.node(n.address)
    # Вхідний граф незмінний.
    assert repr(g) == before
    # Порожня множина — тотожність; кілька адрес; всі адреси.
    assert g.without(frozenset()) == g
    g3 = g.without(frozenset({B, D}))
    assert {n.address for n in g3.nodes} == {A, C} and len(g3.edges) == 1
    assert g.without(frozenset(n.address for n in g.nodes)) == FundingGraph(nodes=(), edges=())
    # Видалення лише-отримувача теж прибирає його вхідні ребра.
    g4 = g.without(frozenset({A}))
    assert all(A not in (e.sender, e.receiver) for e in g4.edges)
    assert len(g4.edges) == 2
    # Невідома адреса — гучно; рядок замість множини — гучно (інакше ітерація по символах).
    with pytest.raises(KeyError):
        g.without(frozenset({H}))
    with pytest.raises(TypeError):
        g.without(B)
    assert g.without({B}) == g2


def test_graph_without_matches_brute_force_on_random_graphs():
    rng = random.Random(11)
    addrs = [f"Addr{i:02d}" + "x" * 30 for i in range(12)]
    for _ in range(40):
        nodes = [_node(a, roles=(FUNDER,)) for a in addrs]
        pairs = {(rng.choice(addrs), rng.choice(addrs)) for _ in range(25)}
        edges = []
        for i, (s, r) in enumerate(sorted(pairs)):
            if s != r:
                edges.append(_transfer(s, r, refs=[_ref(f"{i:04d}" + "S" * 80)]))
        g = FundingGraph(nodes=nodes, edges=edges)
        drop = frozenset(rng.sample(addrs, rng.randint(0, 5)))
        g2 = g.without(drop)
        assert {n.address for n in g2.nodes} == set(addrs) - drop
        assert set(g2.edges) == {e for e in g.edges if e.sender not in drop and e.receiver not in drop}


def test_graph_is_frozen():
    g = _basic_graph()
    with pytest.raises(AttributeError):
        g.nodes = ()  # type: ignore[misc]


# --- Ключі порядку ----------------------------------------------------------------


def test_sort_keys_are_data_only_and_path_compares_numerically():
    n = _node(B, measures=_measures(degree=9, unique=2, one_off=1))
    assert node_sort_key(n) == B
    # Ролі, глибина, виміри не впливають на позицію вершини.
    assert node_sort_key(dataclasses.replace(n, depth=3, measures=_measures())) == node_sort_key(n)

    e = _transfer(B, A, refs=[_ref(SIG_B, 50, "1")], amount=7)
    assert edge_sort_key(e) == ("transfer", B, A, "sol")
    d = _delegated(B, A)
    assert edge_sort_key(d) == ("delegated_buy", B, A, "")
    # Сума, кількість, слоти, посилання не впливають на позицію ребра.
    e2 = _transfer(B, A, refs=[_ref(SIG_A, 1, "0"), _ref(SIG_C, 99, "4")], amount=999_999)
    assert edge_sort_key(e2) == edge_sort_key(e)

    r = _ref(SIG_A, 5, "2.1")
    assert ref_sort_key(r) == (5, SIG_A, (2, 1))
    assert ref_sort_key(_ref(SIG_A, 5, None)) == (5, SIG_A, ())
    # Шлях порівнюється числово: "2" < "2.1" < "10" (рядково було б "10" < "2").
    paths = ["10", "2.1", "2", "0", "2.10", "2.9"]
    ordered = sorted((_ref(SIG_A, 5, p) for p in paths), key=ref_sort_key)
    assert [x.instruction_path for x in ordered] == ["0", "2", "2.1", "2.9", "2.10", "10"]
    # Спершу слот, потім підпис, потім шлях.
    mixed = [_ref(SIG_B, 5, "0"), _ref(SIG_A, 6, "0"), _ref(SIG_A, 5, "1")]
    assert [(x.slot, x.signature) for x in sorted(mixed, key=ref_sort_key)] == [
        (5, SIG_A), (5, SIG_B), (6, SIG_A),
    ]
    # Рівні дані -> рівні ключі -> порядок не залежить від входу.
    edges = [_transfer(C, A), _delegated(B, A), _transfer(B, A, asset=SPL, decimals=1), _transfer(B, A),
             _transfer(B, C)]
    expected = sorted(edges, key=edge_sort_key)
    assert [(x.kind.value, x.sender, x.receiver, x.asset or "") for x in expected] == sorted(
        (x.kind.value, x.sender, x.receiver, x.asset or "") for x in edges
    )
    rng = random.Random(5)
    for _ in range(20):
        rng.shuffle(edges)
        assert sorted(edges, key=edge_sort_key) == expected


# --- GraphCompleteness ------------------------------------------------------------


def test_completeness_derive_incomplete_when_ingest_incomplete_or_delegated_incomplete():
    lattice = itertools.product(
        ((), (_missing(),), (_missing(A), _missing(B, reason=MissingReason.CORRUPT_DATA))),
        (BUYERS_OK, BUYERS_CUT),
        ("absent", DELEGATED_OK, DELEGATED_TIMEOUT, DELEGATED_NOT_ANALYZED),
    )
    for missing, buyers, delegated in lattice:
        ingest = _ingest(missing, buyers, delegated)
        gc = GraphCompleteness.derive(ingest)
        ingest_complete = not missing and buyers.complete
        delegated_complete = delegated is DELEGATED_OK
        assert gc.ingest_status == ingest.completeness.status
        assert (gc.ingest_status is CompletenessStatus.COMPLETE) == ingest_complete
        assert gc.delegated_complete is delegated_complete
        expected = (
            GraphCompletenessStatus.COMPLETE
            if ingest_complete and delegated_complete
            else GraphCompletenessStatus.INCOMPLETE
        )
        assert gc.status is expected, (missing, buyers, delegated)


def test_completeness_without_delegated_analysis_is_not_analyzed_and_incomplete():
    gc = GraphCompleteness.derive(_ingest())
    assert gc.ingest_status is CompletenessStatus.COMPLETE
    assert gc.delegated_complete is False
    assert gc.delegated_reason == "not_analyzed"
    assert gc.status is GraphCompletenessStatus.INCOMPLETE


def test_completeness_derive_copies_missing_and_reasons_in_001_order():
    entries = [_missing(C, 2), _missing(B, 1, MissingReason.CORRUPT_DATA, "tx"), _missing(A, 2)]
    ingest = _ingest(entries, BUYERS_CUT, DELEGATED_TIMEOUT)
    gc = GraphCompleteness.derive(ingest)
    assert [(m.wallet, m.depth, m.reason, m.detail) for m in gc.missing] == [
        (m.wallet, m.depth, m.reason, m.detail) for m in ingest.completeness.missing
    ]
    assert all(isinstance(m, MissingRef) for m in gc.missing)
    assert gc.buyers_complete is False
    assert gc.buyers_reason == "budget_exhausted"
    assert gc.delegated_reason == "timeout"
    assert isinstance(gc.missing, tuple)
    gc_ok = GraphCompleteness.derive(_ingest((), BUYERS_OK, DELEGATED_OK))
    assert (gc_ok.buyers_complete, gc_ok.buyers_reason, gc_ok.delegated_reason) == (True, None, None)
    assert gc_ok.missing == ()


def test_completeness_missing_order_is_001_key_even_for_unsorted_input():
    # Порядок missing — ключ 001 (depth, wallet, reason) з даних, а не порядок входу.
    entries = (_missing(C, 2), _missing(B, 1, MissingReason.CORRUPT_DATA), _missing(B, 1), _missing(A, 2))
    ingest = _ingest(entries, BUYERS_OK, DELEGATED_OK)
    object.__setattr__(ingest.completeness, "missing", entries)  # примусово невпорядковано
    gc = GraphCompleteness.derive(ingest)
    assert [(m.depth, m.wallet, m.reason.value) for m in gc.missing] == sorted(
        (m.depth, m.wallet, m.reason.value) for m in entries
    )


def test_completeness_derive_rejects_non_ingest_input():
    with pytest.raises(TypeError):
        GraphCompleteness.derive(Rejection(kind=RejectKind.TOKEN_NOT_FOUND, mint=MINT, detail=""))
    with pytest.raises(TypeError):
        GraphCompleteness.derive(SimpleNamespace(completeness=None))
    with pytest.raises(TypeError):
        GraphCompleteness.derive(_ingest(delegated=SimpleNamespace(complete="true", reason=None)))
    with pytest.raises(ValueError):
        GraphCompleteness.derive(_ingest(delegated=SimpleNamespace(complete=True, reason="timeout")))
    with pytest.raises(ValueError):
        GraphCompleteness.derive(_ingest(delegated=SimpleNamespace(complete=False, reason=None)))
    with pytest.raises(ValueError):
        GraphCompleteness.derive(_ingest(delegated=SimpleNamespace(complete=False, reason="whatever")))


def test_direct_construction_of_completeness_rejected():
    # status (і ingest_status) — не поля: їх неможливо передати, ані зберегти суперечливими.
    names = {f.name for f in dataclasses.fields(GraphCompleteness)}
    assert "status" not in names and "ingest_status" not in names
    assert "status" not in inspect.signature(GraphCompleteness).parameters
    with pytest.raises(TypeError):
        GraphCompleteness(  # type: ignore[call-arg]
            status=GraphCompletenessStatus.COMPLETE, missing=(), buyers_complete=True, buyers_reason=None,
            delegated_complete=True, delegated_reason=None,
        )
    # Прямий конструктор закритий навіть з узгодженими полями: єдиний вхід — derive.
    with pytest.raises(TypeError):
        GraphCompleteness(missing=(), buyers_complete=True, buyers_reason=None, delegated_complete=True,
                          delegated_reason=None)
    with pytest.raises(TypeError):
        GraphCompleteness((), True, None, True, None)
    # replace — теж обхід derive: заборонений (інакше «повний» підробляється без збору).
    gc = GraphCompleteness.derive(_ingest((_missing(),), BUYERS_CUT, DELEGATED_TIMEOUT))
    for change in ({"missing": ()}, {"delegated_complete": True, "delegated_reason": None},
                   {"buyers_complete": True, "buyers_reason": None}, {}):
        with pytest.raises(TypeError):
            dataclasses.replace(gc, **change)
    with pytest.raises(AttributeError):
        gc.status = GraphCompletenessStatus.COMPLETE  # type: ignore[misc]
    with pytest.raises(AttributeError):
        gc.missing = ()  # type: ignore[misc]


def test_completeness_status_follows_data_even_if_fields_are_forced():
    for field, value in [("missing", (MissingRef(wallet=A, depth=1, reason=MissingReason.TIMEOUT, detail=""),)),
                         ("buyers_complete", False), ("delegated_complete", False)]:
        gc = GraphCompleteness.derive(_ingest((), BUYERS_OK, DELEGATED_OK))
        assert gc.status is GraphCompletenessStatus.COMPLETE
        object.__setattr__(gc, field, value)
        assert gc.status is GraphCompletenessStatus.INCOMPLETE, field


def test_missing_ref_validation():
    m = MissingRef(wallet=A, depth=0, reason="rate_limited", detail="")
    assert m.reason is MissingReason.RATE_LIMITED
    for field, bad in [("wallet", ""), ("depth", -1), ("reason", "glitch"), ("detail", None)]:
        with pytest.raises((TypeError, ValueError)):
            MissingRef(**{**dict(wallet=A, depth=0, reason="timeout", detail=""), field: bad})


# --- GraphMetadata ----------------------------------------------------------------


def test_metadata_lists_applied_iff_lists_version():
    assert _metadata(lists_version=1, lists_applied=True).lists_applied is True
    assert _metadata(lists_version=None, lists_applied=False).address_lists_version is None
    with pytest.raises(ValueError):
        _metadata(lists_version=None, lists_applied=True)
    with pytest.raises(ValueError):
        _metadata(lists_version=2, lists_applied=False)
    with pytest.raises(TypeError):
        _metadata(lists_version=None, lists_applied=0)


@pytest.mark.parametrize(
    "field,bad",
    [("schema_version", "002.2"), ("schema_version", "1.0"), ("mint", ""), ("ingest_analyzed_at", -1),
     ("ingest_config_version", 0), ("ingest_source", ""), ("wallets_analyzed", -1),
     ("hub_config_version", 0), ("address_lists_version", 0), ("nodes_total", -1), ("edges_total", 1.0),
     ("thresholds", {"degree_threshold": 100, "one_off_senders_share": 0.8, "one_off_min_senders": 10,
                     "giant_component_warn_share": 0.5, "prune_off_curve": True,
                     "prune_ingest_high_degree": True, "dust_amount_lamports": 1_000_000,
                     "dust_min_fanout": 5})],
)
def test_metadata_rejects_invalid_fields(field, bad):
    with pytest.raises((TypeError, ValueError)):
        _metadata(**{field: bad})


@pytest.mark.parametrize(
    "field,bad",
    [("degree_threshold", 0), ("degree_threshold", 1.5), ("one_off_senders_share", -0.1),
     ("one_off_senders_share", 1.1), ("one_off_min_senders", 1), ("giant_component_warn_share", 0),
     ("giant_component_warn_share", 1.01), ("prune_off_curve", "true"), ("prune_ingest_high_degree", 1),
     ("dust_amount_lamports", 0), ("dust_amount_lamports", -1), ("dust_amount_lamports", 1_000_000.0),
     ("dust_amount_lamports", None), ("dust_min_fanout", 1), ("dust_min_fanout", 0),
     ("dust_min_fanout", 5.0), ("dust_min_fanout", "5")],
)
def test_thresholds_snapshot_bounds(field, bad):
    with pytest.raises((TypeError, ValueError)):
        _thresholds(**{field: bad})


def test_thresholds_snapshot_boundaries_valid():
    assert _thresholds(one_off_senders_share=0, giant_component_warn_share=1).giant_component_warn_share == 1
    assert _thresholds(one_off_senders_share=1.0, one_off_min_senders=2, degree_threshold=1).degree_threshold == 1
    low = _thresholds(dust_amount_lamports=1, dust_min_fanout=2)
    assert (low.dust_amount_lamports, low.dust_min_fanout) == (1, 2)


@pytest.mark.parametrize(
    "field,value,error",
    [("dust_amount_lamports", 0, ValueError), ("dust_amount_lamports", 1, None),
     ("dust_min_fanout", 1, ValueError), ("dust_min_fanout", 2, None),
     ("dust_amount_lamports", True, TypeError), ("dust_min_fanout", True, TypeError),
     ("dust_amount_lamports", False, TypeError), ("dust_min_fanout", False, TypeError),
     # межові й суміжні
     ("dust_amount_lamports", -1, ValueError), ("dust_amount_lamports", 2, None),
     ("dust_amount_lamports", 10**18, None), ("dust_amount_lamports", 1.0, TypeError),
     ("dust_amount_lamports", None, TypeError), ("dust_amount_lamports", "1000000", TypeError),
     ("dust_min_fanout", 0, ValueError), ("dust_min_fanout", 3, None), ("dust_min_fanout", 2.0, TypeError),
     ("dust_min_fanout", None, TypeError)],
)
def test_thresholds_snapshot_dust_bounds(field, value, error):
    if error is None:
        assert getattr(_thresholds(**{field: value}), field) == value
    else:
        with pytest.raises(error):
            _thresholds(**{field: value})


def test_thresholds_snapshot_has_eight_required_fields_matching_schema():
    fields = dataclasses.fields(ThresholdsSnapshot)
    names = [f.name for f in fields]
    assert names == [
        "degree_threshold", "one_off_senders_share", "one_off_min_senders", "giant_component_warn_share",
        "prune_off_curve", "prune_ingest_high_degree", "dust_amount_lamports", "dust_min_fanout",
    ]
    assert all(f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING for f in fields)
    assert names == GRAPH_SCHEMA["$defs"]["thresholds"]["required"]
    with pytest.raises(TypeError):
        ThresholdsSnapshot(degree_threshold=100, one_off_senders_share=0.8, one_off_min_senders=10,  # type: ignore[call-arg]
                           giant_component_warn_share=0.5, prune_off_curve=True, prune_ingest_high_degree=True)
    t = _thresholds()
    with pytest.raises(ValueError):
        dataclasses.replace(t, dust_min_fanout=1)
    with pytest.raises(ValueError):
        dataclasses.replace(t, dust_amount_lamports=0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        t.dust_min_fanout = 1  # type: ignore[misc]
    assert _schema_validator("thresholds").is_valid(dataclasses.asdict(t))


# --- GraphResult ------------------------------------------------------------------


def test_graph_result_requires_all_buyers_present_and_disjoint_pruned():
    g = _basic_graph()  # 2 покупці, 4 вершини
    r = _result(g, pruned=[_Pruned(H)])
    assert r.metadata.nodes_total == 5
    # Покупців у графі менше/більше за wallets_analyzed (SC-003) — неможливо.
    for wallets in (0, 1, 3):
        with pytest.raises(ValueError):
            _result(g, metadata=_metadata(wallets_analyzed=wallets, nodes_total=4))
    with pytest.raises(ValueError):
        _result(g.without(frozenset({C})), metadata=_metadata(wallets_analyzed=2, nodes_total=3))
    # Відсічена адреса, що лишилась у графі, — неможливо.
    with pytest.raises(ValueError):
        _result(g, pruned=[_Pruned(B)], metadata=_metadata(nodes_total=5))
    with pytest.raises(ValueError):
        _result(g, pruned=[_Pruned(H), _Pruned(A)], metadata=_metadata(nodes_total=6))
    # nodes_total == len(graph.nodes) + len(pruned).
    for total in (4, 6):
        with pytest.raises(ValueError):
            _result(g, pruned=[_Pruned(H)], metadata=_metadata(nodes_total=total))
    with pytest.raises(ValueError):
        dataclasses.replace(r, pruned=())
    with pytest.raises(ValueError):
        dataclasses.replace(r, graph=g.without(frozenset({A})))
    # Порожній граф без покупців — валідний результат.
    empty = FundingGraph(nodes=(), edges=())
    assert _result(empty, metadata=_metadata(wallets_analyzed=0, nodes_total=0, edges_total=0)).graph == empty


def test_graph_result_lists_not_applied_requires_warning():
    g = _basic_graph()
    md = _metadata(lists_version=None, lists_applied=False)
    with pytest.raises(ValueError):
        _result(g, metadata=md, report=_Report(warnings=()))
    with pytest.raises(ValueError):
        _result(g, metadata=md, report=_Report(warnings=(GraphWarning.GIANT_COMPONENT,)))
    ok = _result(g, metadata=md, report=_Report(warnings=(GraphWarning.ADDRESS_LISTS_NOT_APPLIED,)))
    assert ok.metadata.lists_applied is False
    # Зі списками попередження не обов'язкове.
    assert _result(g, report=_Report(warnings=())).metadata.lists_applied is True


def test_graph_result_type_checks():
    g = _basic_graph()
    with pytest.raises(TypeError):
        _result(g, completeness=SimpleNamespace(status="complete"))
    with pytest.raises(TypeError):
        GraphResult(metadata={"mint": MINT}, completeness=GraphCompleteness.derive(_ingest()), graph=g,
                    pruned=(), buyer_flags=(), report=_Report(()))
    with pytest.raises(TypeError):
        GraphResult(metadata=_metadata(), completeness=GraphCompleteness.derive(_ingest()),
                    graph={"nodes": []}, pruned=(), buyer_flags=(), report=_Report(()))
    with pytest.raises(TypeError):
        _result(g, pruned=[H], metadata=_metadata(nodes_total=5))
    r = _result(g, pruned=[_Pruned(H)])
    assert isinstance(r.pruned, tuple) and isinstance(r.buyer_flags, tuple)


@pytest.mark.parametrize("field", ["one_off_senders_share", "giant_component_warn_share"])
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_thresholds_snapshot_rejects_non_finite_shares(field, bad):
    """NaN порівнюється хибно з усім: поріг-NaN мовчки вимкнув би критерій, тож знімок його не приймає (T-040)."""
    with pytest.raises(ValueError):
        _thresholds(**{field: bad})
