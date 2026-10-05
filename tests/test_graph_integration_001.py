# verifies: FR-002-20
"""Інтеграція 002 зі справжнім виходом 001 (T-041; FR-002-20; research R-3, R-22; contracts/graph-service.md).

Усі інші тести 002 живуть на `tests/fixtures/graph/*/ingest.json` — результатах, зібраних генератором 002 напряму.
Тут вхід — те, що справді повертає `IngestService.collect` над записаними сценаріями 001 (`FixtureRpcSource`,
мережі немає), а оракул — `expected.json` самих сценаріїв 001, складений генератором 001 незалежно від графа:

- `basic` (N=5, depth=3): усі п'ять покупців у графі з рангами з еталона; кожен переказ еталона — рівно один `ref` рівно
  одного ребра, а ребра — це групи переказів за `(sender, receiver, asset)` з сумою, лічильником, слотами й часом,
  перерахованими тут заново (без `build_graph`);
- `hub` — три конфігурації з `expected.json.cases`: `hub_high_degree` (H відсічено критерієм `ingest_high_degree`, усі
  покупці на місці, поріг збору береться з результату — 5, а не 200 із `config/ingest.yaml`), `hub_signature_cap`
  (позначка `signature_cap` не є критерієм хаба — H лишається) і `hub_control` (H лишається: жоден інший критерій
  не спрацьовує, і це видно з ступеня й часток, порахованих з еталона);
- `corrupt`: неповнота 001 успадкована графом із тим самим `missing[]` (принцип V: «чисто» неможливе);
- журнал `FixtureRpcSource.calls` між `collect` і `analyze` не змінюється, а методи джерела на час `analyze` гучно
  падають (FR-002-20: усі дані — з результату збору);
- захист від хибних спрацювань `dust_fanout` (R-22) на справжньому виході 001: усі SOL-перекази `basic`/`hub` >= 1 SOL,
  тож жодного пилового запису ні у `pruned`, ні у `buyer_flags`;
- swap-and-send (T-046/T-047): `collect(swapsend)` → ребро `delegated_buy` A→B, B — не покупець, повнота `delegated`
  дзеркалить `buyers`; відмова джерела (і resume) дає неповний граф із тими самими причинами, що й у 001.

Конфіг відсікання — поставлений `config/hubs.yaml` + `config/hub_addresses.yaml` через `load_hub_config`, не заглушка.
"""

import copy
import dataclasses
import json
from collections import defaultdict
from pathlib import Path

import pytest

from unmask.graph.model import (
    EdgeKind,
    GraphCompletenessStatus,
    GraphResult,
    GraphWarning,
    HubCriterion,
    NodeRole,
)
from unmask.graph.service import GraphService
from unmask.hubs.config import load_hub_config
from unmask.ingest.budget import FakeClock
from unmask.ingest.cache import ResultCache
from unmask.ingest.config import load_config
from unmask.ingest.model import CompletenessStatus, IngestResult
from unmask.ingest.rpc.fixture import FailAfter, FailFor, FixtureRpcSource
from unmask.ingest.rpc.protocol import RpcRateLimited, RpcTimeout, RpcUnavailable
from unmask.ingest.service import IngestService

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = ROOT / "tests" / "fixtures" / "scenarios"
SHIPPED_INGEST = ROOT / "config" / "ingest.yaml"
SHIPPED_HUBS = ROOT / "config" / "hubs.yaml"
SHIPPED_LISTS = ROOT / "config" / "hub_addresses.yaml"

BASIC_EXPECTED = json.loads((SCENARIOS / "basic" / "expected.json").read_text(encoding="utf-8"))
HUB_EXPECTED = json.loads((SCENARIOS / "hub" / "expected.json").read_text(encoding="utf-8"))
SWAP_EXPECTED = json.loads((SCENARIOS / "swapsend" / "expected.json").read_text(encoding="utf-8"))
CORRUPT_CAST = json.loads((SCENARIOS / "corrupt" / "rpc.json").read_text(encoding="utf-8"))["_meta"]["cast"]
HUB_CASES = {c["name"]: c for c in HUB_EXPECTED["cases"]}
H = HUB_EXPECTED["wallets"]["H"]

# Golden: зафіксовані версії й пороги поставлених конфігів (принцип III; hubs.yaml — `## 3` у config/CHANGELOG.md,
# T-058: one_off_min_senders 10 → 50; ingest.yaml — `## 2`).
SHIPPED_HUB_VERSION, SHIPPED_LISTS_VERSION, SHIPPED_INGEST_VERSION = 3, 1, 2
SHIPPED_SNAPSHOT = {
    "degree_threshold": 100, "one_off_senders_share": 0.8, "one_off_min_senders": 50,
    "giant_component_warn_share": 0.5, "prune_off_curve": True, "prune_ingest_high_degree": True,
    "dust_amount_lamports": 1_000_000, "dust_min_fanout": 5,
}
ONE_SOL = 1_000_000_000

# id -> (каталог сценарію, значення конфігу збору, переказів еталона, покупців еталона)
CASES = {
    "basic": ("basic", BASIC_EXPECTED["config"], BASIC_EXPECTED["transfers"], BASIC_EXPECTED["buyers"]),
    **{
        name: ("hub", case["config"], case["transfers"], HUB_EXPECTED["buyers"])
        for name, case in HUB_CASES.items()
    },
}
MINTS = {"basic": BASIC_EXPECTED["mint"], "hub": HUB_EXPECTED["mint"], "corrupt": CORRUPT_CAST["M"],
         "swapsend": SWAP_EXPECTED["mint"]}
CORRUPT_VALUES = {"first_buyers_n": 3, "funding_depth": 1}
SWAP_VALUES = SWAP_EXPECTED["config"]


# --- Помічники -------------------------------------------------------------------------


def _hub_config():
    """Поставлений конфіг відсікання зі списками — як його завантажить продакшн-код."""
    return load_hub_config(SHIPPED_HUBS, SHIPPED_LISTS)


def _ingest_config(values: dict):
    return dataclasses.replace(load_config(SHIPPED_INGEST), **values)


def _service(directory: str, values: dict, *, failures=None, cache=None, source=None):
    clock = FakeClock()
    source = source or FixtureRpcSource(SCENARIOS / directory, failures=failures, clock=clock)
    return IngestService(_ingest_config(values), source, clock=clock, cache=cache), source


def _collect(directory: str, values: dict, *, failures=None):
    """Справжній `IngestService.collect` над записаним сценарієм -> (результат збору, джерело)."""
    service, source = _service(directory, values, failures=failures)
    outcome = service.collect(MINTS[directory])
    assert isinstance(outcome, IngestResult), outcome  # не `Rejection`
    return outcome, source


def _collect_case(case_id: str):
    directory, values, _transfers, _buyers = CASES[case_id]
    return _collect(directory, values)


def _analyze(ingest: IngestResult) -> GraphResult:
    result = GraphService(_hub_config()).analyze(ingest)
    assert isinstance(result, GraphResult)
    return result


def _ref_order(t: dict) -> tuple:
    """Порядок доказів: `(slot, signature, шлях числово)` — рахується тут, не береться з моделі графа."""
    return (t["slot"], t["signature"], tuple(int(p) for p in t["instruction_path"].split(".")))


def _all_edges(result: GraphResult) -> dict:
    """Ребра графа результату й усіх відсічених вершин (ребро між двома хабами — один раз)."""
    edges = {e.key: e for e in result.graph.edges}
    for record in result.pruned:
        for e in record.incident_edges:
            assert edges.setdefault(e.key, e) == e  # те саме ребро з двох боків — рівне
    return edges


def _assert_transfers_are_edges(result: GraphResult, transfers: list[dict]) -> None:
    """Кожен переказ еталона — рівно один `ref` рівно одного ребра виду `transfer`; ребра — групи переказів."""
    edges = _all_edges(result)
    groups = defaultdict(list)
    for t in transfers:
        groups[(t["sender"], t["receiver"], t["asset"])].append(t)

    transfer_edges = {k: e for k, e in edges.items() if e.kind is EdgeKind.TRANSFER}
    assert len(transfer_edges) == len(edges), "unexpected non-transfer edge in a result without delegated links"
    assert {(e.sender, e.receiver, str(e.asset)) for e in transfer_edges.values()} == set(groups)

    seen_refs = []
    for (sender, receiver, asset), ts in groups.items():
        edge = transfer_edges[(EdgeKind.TRANSFER, sender, receiver, asset)]
        ordered = sorted(ts, key=_ref_order)
        assert edge.count == len(ts) == len(edge.refs)
        assert edge.amount == sum(t["amount"] for t in ts)
        assert edge.decimals == ts[0]["decimals"]
        assert (edge.first_slot, edge.last_slot) == (ordered[0]["slot"], max(t["slot"] for t in ts))
        assert (edge.first_time, edge.last_time) == (ordered[0]["block_time"], ordered[-1]["block_time"])
        assert [(r.signature, r.slot, r.instruction_path) for r in edge.refs] == [
            (t["signature"], t["slot"], t["instruction_path"]) for t in ordered
        ]
        seen_refs += [(r.signature, r.instruction_path) for r in edge.refs]
    # 1:1: кожен переказ еталона — рівно один ref у всьому результаті, зайвих немає
    assert sorted(seen_refs) == sorted((t["signature"], t["instruction_path"]) for t in transfers)


def _assert_buyers_are_expected(result: GraphResult, expected_buyers: list[dict]) -> None:
    """Усі покупці еталона — у графі результату з їхніми рангами; жодного зайвого (SC-003)."""
    nodes = {n.address: n for n in result.graph.nodes}
    for b in expected_buyers:
        node = nodes[b["wallet"]]
        assert NodeRole.BUYER in node.roles
        assert (node.buyer_rank, node.depth) == (b["rank"], 0)
    assert {n.address for n in result.graph.nodes if NodeRole.BUYER in n.roles} == {
        b["wallet"] for b in expected_buyers
    }
    assert result.metadata.wallets_analyzed == len(expected_buyers)
    assert result.report.before.buyers_total == result.report.after.buyers_total == len(expected_buyers)


# --- basic: справжній збір -> граф -----------------------------------------------------------


def test_basic_collect_result_builds_graph_with_all_five_buyers_and_expected_edges():
    ingest, _source = _collect_case("basic")
    assert ingest.completeness.status is CompletenessStatus.COMPLETE
    result = _analyze(ingest)

    _assert_buyers_are_expected(result, BASIC_EXPECTED["buyers"])
    assert len(BASIC_EXPECTED["buyers"]) == 5
    # збіг із `expected.json` 001: переказів 10 -> ребер 8 (два здвоєні переказа A->P2 і SPL S->P2 згорнуті), refs 10
    assert len(BASIC_EXPECTED["transfers"]) == 10 == len(ingest.transfers)
    _assert_transfers_are_edges(result, BASIC_EXPECTED["transfers"])
    assert len(result.graph.edges) == 8 and result.metadata.edges_total == 8
    assert sum(e.count for e in result.graph.edges) == 10
    # хабів немає; покупець 5 — PDA (`address_type: off_curve` в еталоні 001): позначений, але лишається (FR-002-10)
    assert result.pruned == ()
    off_curve = [b for b in BASIC_EXPECTED["buyers"] if b["address_type"] == "off_curve"]
    assert [b["rank"] for b in off_curve] == [5]
    assert [(f.address, f.buyer_rank) for f in result.buyer_flags] == [(b["wallet"], b["rank"]) for b in off_curve]
    (flag,) = result.buyer_flags
    assert [(h.criterion, h.detail) for h in flag.criteria] == [(HubCriterion.KNOWN_LIST, "address_type:off_curve")]

    # вершини: покупці ∪ відправники переказів; глибина не-покупця — найменша глибина переказів, що він шле
    expected_nodes = ({b["wallet"] for b in BASIC_EXPECTED["buyers"]}
                      | {t["sender"] for t in BASIC_EXPECTED["transfers"]})
    assert {n.address for n in result.graph.nodes} == expected_nodes
    assert result.metadata.nodes_total == len(expected_nodes)
    buyer_wallets = {b["wallet"] for b in BASIC_EXPECTED["buyers"]}
    for node in result.graph.nodes:
        if node.address in buyer_wallets:
            continue
        assert node.roles == frozenset({NodeRole.FUNDER}) and node.buyer_rank is None
        assert node.depth == min(t["depth"] for t in BASIC_EXPECTED["transfers"] if t["sender"] == node.address)
        assert 1 <= node.depth <= BASIC_EXPECTED["config"]["funding_depth"]
    assert result.completeness.status is GraphCompletenessStatus.COMPLETE
    assert result.completeness.missing == ()
    assert result.report.warnings == ()


# --- hub: три конфіги з expected.json.cases ---------------------------------------------------


def test_hub_high_degree_case_prunes_H_via_ingest_high_degree():
    ingest, _source = _collect_case("hub_high_degree")
    case = HUB_CASES["hub_high_degree"]
    (mark,) = case["unexpanded"]
    assert (mark["wallet"], mark["reason"]) == (H, "high_degree")
    # поріг прогону збору (5) — не поріг поставленого config/ingest.yaml (200): аналіз бере його з результату
    assert ingest.metadata.counterparty_threshold == case["config"]["counterparty_threshold"] == 5
    assert load_config(SHIPPED_INGEST).counterparty_threshold == 200
    result = _analyze(ingest)

    # рівно один хаб H, рівно з одним критерієм ingest_high_degree і виміряним значенням збору
    (record,) = result.pruned
    assert record.address == H
    (hit,) = record.criteria
    assert hit.criterion is HubCriterion.INGEST_HIGH_DEGREE
    assert (hit.measured, hit.threshold) == (mark["counterparties_seen"], 5) == (6, 5)
    assert hit.detail == "unexpanded:high_degree"
    assert (record.config_version, record.lists_version) == (SHIPPED_HUB_VERSION, SHIPPED_LISTS_VERSION)

    # H зник з графа; усі покупці (SC-003) і їхні ранги на місці; покупець Q1 лишився без фінансиста (чесно)
    assert H not in {n.address for n in result.graph.nodes}
    _assert_buyers_are_expected(result, HUB_EXPECTED["buyers"])
    # ребра H — у записі відсікання: рівно ті, що торкаються H у групах переказів еталона
    touching = [t for t in case["transfers"] if H in (t["sender"], t["receiver"])]
    assert {(e.sender, e.receiver) for e in record.incident_edges} == {(t["sender"], t["receiver"]) for t in touching}
    assert not any(H in (e.sender, e.receiver) for e in result.graph.edges)
    # жоден переказ не загубився: граф ∪ запис відсікання = усі перекази еталона (1:1 на refs)
    _assert_transfers_are_edges(result, case["transfers"])
    assert (result.report.pruned_nodes, result.report.pruned_edges) == (1, len(record.incident_edges))
    assert result.report.before.edges - result.report.after.edges == len(record.incident_edges)
    assert result.completeness.status is GraphCompletenessStatus.COMPLETE  # нерозгорнутість — не неповнота


def test_hub_control_case_without_high_degree_keeps_H_unless_other_criteria():
    ingest, _source = _collect_case("hub_control")
    case = HUB_CASES["hub_control"]
    assert case["unexpanded"] == []  # у контролі збір H не позначав
    result = _analyze(ingest)

    # «якщо інші критерії не спрацьовують»: перевіряємо це числами з еталона, а не віримо відсутності запису
    senders = defaultdict(int)
    receivers = set()
    for t in case["transfers"]:
        if t["receiver"] == H:
            senders[t["sender"]] += 1
        if t["sender"] == H:
            receivers.add(t["receiver"])
    degree = len(set(senders) | receivers)
    one_off = sum(1 for n in senders.values() if n == 1)
    thresholds = SHIPPED_SNAPSHOT
    assert degree == 7 <= thresholds["degree_threshold"]                       # degree: не більше порога
    assert len(senders) == 6 < thresholds["one_off_min_senders"]               # one_off_senders: передумова не виконана
    assert len(senders) < 10  # …і не виконана б навіть за історичної v2 (10): висновок не залежить від T-058
    assert one_off / len(senders) <= thresholds["one_off_senders_share"]       # і частка все одно під порогом
    assert len(receivers & {b["wallet"] for b in HUB_EXPECTED["buyers"]}) < thresholds["dust_min_fanout"]  # dust
    assert HUB_EXPECTED["wallets"]["H"] not in ingest.unexpanded and not ingest.unexpanded

    # висновок: H — вершина графа, не відсічена, без позначок; усі його ребра в графі
    assert result.pruned == () and result.buyer_flags == ()
    node = next(n for n in result.graph.nodes if n.address == H)
    assert node.roles == frozenset({NodeRole.FUNDER}) and node.unexpanded is None and node.depth == 1
    assert node.measures.degree == degree
    assert (node.measures.unique_senders, node.measures.one_off_senders) == (len(senders), one_off)
    _assert_buyers_are_expected(result, HUB_EXPECTED["buyers"])
    _assert_transfers_are_edges(result, case["transfers"])
    assert all(e in result.graph.edges for e in _all_edges(result).values())
    assert (result.report.pruned_nodes, result.report.pruned_edges) == (0, 0)


def test_hub_signature_cap_case_keeps_H_marked_but_not_pruned():
    """`signature_cap` — позначка нерозгорнутості, а не критерій хаба: H лишається в графі з позначкою."""
    ingest, _source = _collect_case("hub_signature_cap")
    case = HUB_CASES["hub_signature_cap"]
    (mark,) = case["unexpanded"]
    assert (mark["wallet"], mark["reason"], mark["signatures_truncated"]) == (H, "signature_cap", True)
    result = _analyze(ingest)

    assert result.pruned == ()
    node = next(n for n in result.graph.nodes if n.address == H)
    assert node.unexpanded is not None and node.unexpanded.reason.value == "signature_cap"
    assert node.unexpanded.signatures_truncated is True
    _assert_buyers_are_expected(result, HUB_EXPECTED["buyers"])
    _assert_transfers_are_edges(result, case["transfers"])
    # нерозгорнутість — атрибут, не неповнота: збір повний
    assert result.completeness.status is GraphCompletenessStatus.COMPLETE


# --- метадані з поставленим конфігом -----------------------------------------------------------


@pytest.mark.parametrize("case_id", list(CASES))
def test_shipped_configs_are_reflected_in_metadata_of_basic_and_hub_results(case_id):
    directory, values, transfers, buyers = CASES[case_id]
    ingest, _source = _collect_case(case_id)
    result = _analyze(ingest)
    md = result.metadata
    assert md.mint == MINTS[directory]
    assert md.schema_version == "002.1"
    assert (md.hub_config_version, md.address_lists_version, md.lists_applied) == (
        SHIPPED_HUB_VERSION, SHIPPED_LISTS_VERSION, True)
    assert dataclasses.asdict(md.thresholds) == SHIPPED_SNAPSHOT
    assert md.ingest_config_version == SHIPPED_INGEST_VERSION == ingest.metadata.config_version
    assert md.ingest_source == f"fixture:{directory}"
    assert md.wallets_analyzed == len(buyers) == ingest.metadata.wallets_analyzed
    assert md.nodes_total == len(result.graph.nodes) + len(result.pruned)
    # лише відомі попередження: списки застосовано, делеговані проаналізовано, гігантської компоненти немає
    assert result.report.warnings == ()
    assert GraphWarning.ADDRESS_LISTS_NOT_APPLIED not in result.report.warnings
    # конфіг збору прогону лишається видимим у метаданих збору (а не лише в `config/ingest.yaml`)
    assert ingest.metadata.counterparty_threshold == values["counterparty_threshold"]


# --- corrupt: неповнота успадкована ---------------------------------------------------------


def test_corrupt_scenario_inherits_incomplete_with_same_missing():
    ingest, _source = _collect("corrupt", CORRUPT_VALUES)
    assert ingest.completeness.status is CompletenessStatus.INCOMPLETE
    assert len(ingest.completeness.missing) == 2  # K2: null-транзакція, K3: без meta
    result = _analyze(ingest)

    assert result.completeness.status is GraphCompletenessStatus.INCOMPLETE  # не «чисто»
    assert result.completeness.ingest_status is CompletenessStatus.INCOMPLETE
    got = [(m.wallet, m.depth, m.reason.value, m.detail) for m in result.completeness.missing]
    want = [(m.wallet, m.depth, m.reason.value, m.detail) for m in ingest.completeness.missing]
    assert got == want                                                  # те саме missing, у тому самому порядку
    assert {(w, r) for w, _d, r, _detail in got} == {(CORRUPT_CAST["K2"], "unavailable"),
                                                    (CORRUPT_CAST["K3"], "corrupt_data")}
    assert all(detail for *_rest, detail in got)                        # підпис проблемної транзакції збережено
    # покупці перелічені повністю — неповнота лише у фінансуванні; делеговані дзеркалять покупців
    assert result.completeness.buyers_complete is True and result.completeness.buyers_reason is None
    assert result.completeness.delegated_complete is True

    # здорова частина графа не загублена: усі три покупці, переказ F1->K1 глибини 1
    buyers = {n.address: n for n in result.graph.nodes if NodeRole.BUYER in n.roles}
    assert set(buyers) == {CORRUPT_CAST["K1"], CORRUPT_CAST["K2"], CORRUPT_CAST["K3"]}
    assert [(e.sender, e.receiver) for e in result.graph.edges] == [(CORRUPT_CAST["F1"], CORRUPT_CAST["K1"])]
    assert result.metadata.wallets_analyzed == 3


# --- жодних звернень до джерела з 002 ---------------------------------------------------------

SOURCE_METHODS = ("get_account_info", "get_signatures_for_address", "get_transactions", "get_token_accounts_by_owner")
ALL_RUNS = [
    *[pytest.param(case_id, id=case_id) for case_id in CASES],
    pytest.param("corrupt", id="corrupt"),
    pytest.param("swapsend", id="swapsend"),
]


def _collect_any(case_id: str):
    if case_id in CASES:
        return _collect_case(case_id)
    if case_id == "corrupt":
        return _collect("corrupt", CORRUPT_VALUES)
    return _collect("swapsend", SWAP_VALUES)


@pytest.mark.parametrize("case_id", ALL_RUNS)
def test_analyze_makes_zero_source_calls(case_id):
    """Журнал звернень джерела між `collect` і `analyze` не змінюється (дві послідовні `analyze` — теж)."""
    ingest, source = _collect_any(case_id)
    journal = copy.deepcopy(source.calls)
    assert journal and ingest.metadata.rpc_calls == len(journal)  # журнал — вся правда про звернення збору
    service = GraphService(_hub_config())
    first = service.analyze(ingest)
    assert source.calls == journal
    second = service.analyze(ingest)  # без стану між викликами
    assert first == second
    assert source.calls == journal and len(source.calls) == ingest.metadata.rpc_calls


@pytest.mark.parametrize("case_id", ALL_RUNS)
def test_analyze_never_even_attempts_a_source_call(case_id):
    """Навіть перехоплена спроба звернення — провал: методи джерела на час `analyze` фіксують спробу й падають,
    тож `except Exception` десь усередині не сховає звернення (журнал джерела такої спроби не бачить)."""
    ingest, source = _collect_any(case_id)
    attempts: list[str] = []

    def guard(name):
        def forbidden(*_args, **_kwargs):
            attempts.append(name)
            raise AssertionError(f"analyze touched the RPC source: {name}")
        return forbidden

    with pytest.MonkeyPatch.context() as patch:  # локальний патч: гард мережі з conftest лишається
        for method in SOURCE_METHODS:
            patch.setattr(source, method, guard(method))
        result = GraphService(_hub_config()).analyze(ingest)
    assert attempts == []
    assert isinstance(result, GraphResult)


# --- R-22 на справжньому виході 001 ----------------------------------------------------------


@pytest.mark.parametrize("case_id", list(CASES))
def test_no_dust_fanout_record_on_001_fixtures_whose_sol_amounts_are_at_least_one_sol(case_id):
    directory, _values, transfers, _buyers = CASES[case_id]
    ingest, _source = _collect_case(case_id)
    # передумова тесту — властивість фікстур 001, перевірена і на еталоні, і на справжньому виході збору
    sol_expected = [t["amount"] for t in transfers if t["asset"] == "sol"]
    sol_actual = [t.amount for t in ingest.transfers if str(t.asset) == "sol"]
    assert sol_expected and min(sol_expected) >= ONE_SOL and min(sol_actual) >= ONE_SOL
    assert sorted(sol_actual) == sorted(sol_expected)

    result = _analyze(ingest)
    criteria = [hit.criterion for record in result.pruned for hit in record.criteria]
    criteria += [hit.criterion for flag in result.buyer_flags for hit in flag.criteria]
    assert HubCriterion.DUST_FANOUT not in criteria
    # і не лише «немає запису»: виміряна медіана сум кожної вершини з SOL-фінансуванням покупців — не пил
    measures = [n.measures for n in result.graph.nodes]
    measures += [r.measures for r in result.pruned] + [f.measures for f in result.buyer_flags]
    medians = [m.median_to_buyers for m in measures if m.median_to_buyers is not None]
    assert all(median >= ONE_SOL for median in medians)
    assert medians or case_id == "hub_high_degree"  # на hub_high_degree фінансистів покупців майже не лишилось


# --- swap-and-send: справжній збір swapsend -> ребро delegated_buy ----------------------------


def test_swapsend_collect_then_analyze_has_delegated_edge_A_to_B_and_B_is_not_a_buyer():
    (link,) = SWAP_EXPECTED["delegated"]["links"]
    payer, receiver = link["payer"], link["receiver"]
    ingest, _source = _collect("swapsend", SWAP_VALUES)
    assert [(l.payer, l.receiver) for l in ingest.delegated.links] == [(payer, receiver)]
    result = _analyze(ingest)

    # покупці — точно еталон 001; отримувач B серед них немає (FR-002-17)
    expected_buyers = SWAP_EXPECTED["buyers"]
    buyer_nodes = {n.address: n for n in result.graph.nodes if NodeRole.BUYER in n.roles}
    assert {w: n.buyer_rank for w, n in buyer_nodes.items()} == {b["wallet"]: b["rank"] for b in expected_buyers}
    assert receiver not in buyer_nodes and payer not in buyer_nodes
    assert result.metadata.wallets_analyzed == len(expected_buyers)

    # ребро A->B: єдине, виду delegated_buy, без активу/суми/decimals, з доказом (підпис, слот, без шляху)
    (edge,) = [e for e in result.graph.edges if e.kind is EdgeKind.DELEGATED_BUY]
    assert (edge.sender, edge.receiver) == (payer, receiver)
    assert (edge.asset, edge.amount, edge.decimals, edge.count) == (None, None, None, 1)
    assert [(r.signature, r.slot, r.instruction_path) for r in edge.refs] == [(link["signature"], link["slot"], None)]
    assert (edge.first_slot, edge.last_slot) == (link["slot"], link["slot"])
    assert (edge.first_time, edge.last_time) == (link["block_time"], link["block_time"])
    assert [e for e in result.graph.edges if e.kind is EdgeKind.TRANSFER] == []  # переказів у сценарії немає

    nodes = {n.address: n for n in result.graph.nodes}
    assert nodes[payer].roles == frozenset({NodeRole.DELEGATED_PAYER}) and nodes[payer].buyer_rank is None
    assert nodes[receiver].roles == frozenset({NodeRole.DELEGATED_RECEIVER}) and nodes[receiver].buyer_rank is None
    assert result.pruned == ()  # делегований платник не хаб

    # кандидати без пари лишаються в результаті 001: у графі їх немає (пару не вгадуємо)
    unpaired = {c["wallet"] for c in SWAP_EXPECTED["delegated"]["unpaired"]}
    assert unpaired and unpaired.isdisjoint(nodes) and unpaired.isdisjoint({r.address for r in result.pruned})

    # повнота: delegated дзеркалить buyers (обидва повні), попередження немає
    assert ingest.delegated.complete and ingest.completeness.buyers.complete
    assert result.completeness.delegated_complete is True and result.completeness.delegated_reason is None
    assert result.completeness.status is GraphCompletenessStatus.COMPLETE
    assert GraphWarning.DELEGATED_INCOMPLETE not in result.report.warnings


# --- відмова джерела та resume: граф успадковує причини ----------------------------------------

RPC_FAILURES = {
    "unavailable": lambda: RpcUnavailable("node down"),
    "rate_limited": lambda: RpcRateLimited(retry_after=1.0),
    "timeout": lambda: RpcTimeout("slow"),
}
(LINK,) = SWAP_EXPECTED["delegated"]["links"]
FAILURE_POINTS = [
    pytest.param(lambda reason, k=k: [FailAfter(k, RPC_FAILURES[reason]())], id=f"after-{k}")
    for k in (1, 2, 3, 5, 8)
] + [
    pytest.param(lambda reason: [FailFor(LINK["signature"], RPC_FAILURES[reason](), times=1)], id="link-tx-once"),
]


def _assert_graph_inherits_incompleteness(ingest: IngestResult, result: GraphResult, reason: str) -> None:
    """Повнота графа — дзеркало повноти 001 поле за полем; `reason` — клас відмови, який підклали джерелу."""
    c = ingest.completeness
    assert c.status is CompletenessStatus.INCOMPLETE
    g = result.completeness
    assert g.status is GraphCompletenessStatus.INCOMPLETE                      # неповний збір — не «чисто»
    assert g.ingest_status is CompletenessStatus.INCOMPLETE
    assert [(m.wallet, m.depth, m.reason.value, m.detail) for m in g.missing] == [
        (m.wallet, m.depth, m.reason.value, m.detail) for m in c.missing]
    b, d = c.buyers, ingest.delegated
    assert (g.buyers_complete, g.buyers_reason) == (b.complete, None if b.reason is None else b.reason.value)
    assert d.complete == b.complete                                            # правило 001: delegated дзеркалить buyers
    assert (g.delegated_complete, g.delegated_reason) == (d.complete, None if d.reason is None else d.reason.value)
    assert (GraphWarning.DELEGATED_INCOMPLETE in result.report.warnings) == (not d.complete)
    # усі причини в графі — саме класу відмови, що підклали; неповнота не лишилась без причини
    reasons = {m.reason.value for m in g.missing} | ({g.buyers_reason} if g.buyers_reason else set())
    assert reasons == {reason}
    assert result.metadata.wallets_analyzed == ingest.metadata.wallets_analyzed


@pytest.mark.parametrize("reason", list(RPC_FAILURES))
@pytest.mark.parametrize("failures", FAILURE_POINTS)
def test_failed_collect_then_resume_gives_incomplete_then_complete_graph_equal_to_fresh(failures, reason):
    cache = ResultCache()
    service, source = _service("swapsend", SWAP_VALUES, failures=failures(reason), cache=cache)
    first = service.collect(MINTS["swapsend"])
    assert isinstance(first, IngestResult)
    first_journal = copy.deepcopy(source.calls)
    first_graph = _analyze(first)
    assert source.calls == first_journal
    _assert_graph_inherits_incompleteness(first, first_graph, reason)
    assert first.metadata.resumed is False

    # resume: той самий кеш, здорове джерело — доотримується лише недоотримане (001), граф стає повним
    resumed_service, resumed_source = _service("swapsend", SWAP_VALUES, cache=cache)
    second = resumed_service.collect(MINTS["swapsend"])
    assert isinstance(second, IngestResult) and second.metadata.resumed is True
    journal = copy.deepcopy(resumed_source.calls)
    second_graph = _analyze(second)
    assert resumed_source.calls == journal  # analyze не звертається до джерела й після resume
    assert second_graph.completeness.status is GraphCompletenessStatus.COMPLETE
    assert second_graph.completeness.delegated_complete is True

    fresh, _fresh_source = _collect("swapsend", SWAP_VALUES)
    assert second_graph == _analyze(fresh)  # resume == свіжий прогін і на рівні графа
    (edge,) = [e for e in second_graph.graph.edges if e.kind is EdgeKind.DELEGATED_BUY]
    assert (edge.sender, edge.receiver) == (LINK["payer"], LINK["receiver"])


@pytest.mark.parametrize("reason", list(RPC_FAILURES))
def test_resume_that_fails_again_keeps_graph_incomplete_with_the_same_reasons(reason):
    """Джерело, що не відпускає й при resume: граф лишається неповним з причинами ТОГО прогону, не «чистим»."""
    cache = ResultCache()
    first_service, _src = _service("swapsend", SWAP_VALUES, failures=[FailAfter(3, RPC_FAILURES[reason]())],
                                   cache=cache)
    first = first_service.collect(MINTS["swapsend"])
    _assert_graph_inherits_incompleteness(first, _analyze(first), reason)

    # другий прогін — інший клас відмови, щоб причина не могла «просочитись» з першого результату
    other = "timeout" if reason != "timeout" else "unavailable"
    second_service, second_source = _service("swapsend", SWAP_VALUES, failures=[FailAfter(2, RPC_FAILURES[other]())],
                                             cache=cache)
    second = second_service.collect(MINTS["swapsend"])
    assert isinstance(second, IngestResult) and second.metadata.resumed is True
    journal = copy.deepcopy(second_source.calls)
    second_graph = _analyze(second)
    assert second_source.calls == journal
    _assert_graph_inherits_incompleteness(second, second_graph, other)


def test_source_down_at_the_start_gives_empty_incomplete_graph_with_delegated_warning():
    """Збій на першому ж зверненні (`_unverified`): покупців немає, але граф не «порожній-чистий»."""
    service, _source = _service("swapsend", SWAP_VALUES, failures=[FailAfter(1, RpcUnavailable("node down"))])
    ingest = service.collect(MINTS["swapsend"])
    assert isinstance(ingest, IngestResult) and ingest.buyers == () and ingest.completeness.buyers.complete is False
    result = _analyze(ingest)
    assert result.graph.nodes == () and result.graph.edges == () and result.metadata.wallets_analyzed == 0
    assert result.completeness.status is GraphCompletenessStatus.INCOMPLETE
    assert (result.completeness.buyers_complete, result.completeness.buyers_reason) == (False, "unavailable")
    assert (result.completeness.delegated_complete, result.completeness.delegated_reason) == (False, "unavailable")
    assert {GraphWarning.EMPTY_GRAPH, GraphWarning.DELEGATED_INCOMPLETE} <= set(result.report.warnings)


def test_funding_failure_after_buyers_gives_incomplete_graph_with_complete_delegated():
    """Збій у фінансуванні, коли покупці вже перелічені: `delegated` повний (дзеркало `buyers`), граф — неповний
    через `missing[]`, і попередження `delegated_incomplete` немає — причини не вигадуються."""
    service, _source = _service("swapsend", SWAP_VALUES, failures=[FailAfter(8, RpcUnavailable("node down"))])
    ingest = service.collect(MINTS["swapsend"])
    assert isinstance(ingest, IngestResult) and ingest.completeness.buyers.complete is True
    result = _analyze(ingest)
    assert result.completeness.missing and result.completeness.status is GraphCompletenessStatus.INCOMPLETE
    assert result.completeness.delegated_complete is True and result.completeness.delegated_reason is None
    assert GraphWarning.DELEGATED_INCOMPLETE not in result.report.warnings
