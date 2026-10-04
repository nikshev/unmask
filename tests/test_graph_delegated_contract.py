# verifies: FR-002-19, FR-002-21
"""Делеговані зв'язки у контракті й звіті фічі 002 (T-048; FR-002-19 неповнота, FR-002-21 контракт).

Що доводить цей файл (структура ребер у моделі — T-047, `test_graph_delegated_edges.py`; переказова частина
серіалізації — T-040, `test_graph_serialize_contract.py`; модельні перевірки справжнього збору — T-041):

- СЕРІАЛІЗОВАНИЙ результат `g_delegated` валідний проти `graph-result.schema.json`, байт-у-байт детермінований,
  несе ребра `delegated_buy` без активу/суми/decimals і без шляху в доказах, а пара з переказом І делегованою
  купівлею дає в JSON ДВА ребра;
- вимір `degree` враховує делеговані ребра, `unique_senders` / `one_off_senders` — ні (перерахунок з `ingest.json`
  незалежно від коду графа, для кожної вершини документа);
- FR-002-19: вхід з неповним аналізом (`delegated.complete = false`) і вхід `not_analyzed` дають у контракті
  `delegated_complete = false` з причиною, `status = incomplete`, попередження `delegated_incomplete`; знайдені
  зв'язки не губляться; схема забороняє «повний» статус над неповним аналізом;
- схема відхиляє ребро `delegated_buy` з активом/сумою/decimals/шляхом у доказі й `transfer` без них та приймає
  `transfer` з активом; делеговане ребро всередині запису відсікання теж проходить схему;
- справжній `IngestService` на `scenarios/swapsend` → `analyze` → `to_dict`/`to_json` валідні проти схеми, ребро
  A→B, B — не покупець, кандидатів без пари в документі немає; resume після відмови дає документ, байт-у-байт
  рівний свіжому.
"""

import copy
import dataclasses
import json
from pathlib import Path
from types import MappingProxyType

import pytest
from jsonschema import Draft202012Validator

from conftest import GRAPH_FIXTURES, load_ingest_fixture
from unmask.graph.model import GraphCompletenessStatus, GraphWarning
from unmask.graph.serialize import to_dict, to_json
from unmask.graph.service import GraphService
from unmask.hubs.config import ADDRESS_CATEGORIES, AddressLists, HubConfig, load_hub_config
from unmask.ingest.budget import FakeClock
from unmask.ingest.cache import ResultCache
from unmask.ingest.config import load_config
from unmask.ingest.model import (
    NOT_ANALYZED_REASON,
    BuyersCompleteness,
    Completeness,
    DelegatedAnalysis,
    IngestResult,
    MissingReason,
)
from unmask.ingest.rpc.fixture import FailAfter, FailFor, FixtureRpcSource
from unmask.ingest.rpc.protocol import RpcUnavailable
from unmask.ingest.service import IngestService

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "specs" / "002-funding-graph-hub-pruning" / "contracts" / "graph-result.schema.json"
VALIDATOR = Draft202012Validator(json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))
SHIPPED_INGEST = ROOT / "config" / "ingest.yaml"
SHIPPED_HUBS = ROOT / "config" / "hubs.yaml"
SHIPPED_LISTS = ROOT / "config" / "hub_addresses.yaml"
SCENARIOS = ROOT / "tests" / "fixtures" / "scenarios"

NAME = "g_delegated"
_INGEST = json.loads((GRAPH_FIXTURES / NAME / "ingest.json").read_text(encoding="utf-8"))
_EXPECTED = json.loads((GRAPH_FIXTURES / NAME / "expected.json").read_text(encoding="utf-8"))
W = _EXPECTED["wallets"]
SWAP = json.loads((SCENARIOS / "swapsend" / "expected.json").read_text(encoding="utf-8"))
SWAP_VALUES = SWAP["config"]
(LINK,) = SWAP["delegated"]["links"]


# --- Помічники -------------------------------------------------------------------------------


def _config(lists=True) -> HubConfig:
    return load_hub_config(SHIPPED_HUBS, SHIPPED_LISTS if lists else None)


def _analyze_doc(ingest: IngestResult, config: HubConfig | None = None):
    result = GraphService(config or _config()).analyze(ingest)
    return result, to_dict(result)


def _errors(instance) -> list[str]:
    return [f"{'/'.join(map(str, e.absolute_path))}: {e.message}" for e in VALIDATOR.iter_errors(instance)]


def _assert_invalid_at(instance, fragment: str) -> None:
    """Документ відхилено саме там, де очікується: шлях помилки містить `fragment`."""
    errors = _errors(instance)
    assert errors, "the hand-made document must be rejected by the schema"
    assert any(fragment in e for e in errors), (fragment, errors)


def _g_delegated_doc() -> dict:
    return copy.deepcopy(_analyze_doc(load_ingest_fixture(NAME))[1])


def _edges(doc: dict, kind=None, sender=None, receiver=None) -> list[dict]:
    return [e for e in doc["graph"]["edges"]
            if (kind is None or e["kind"] == kind)
            and (sender is None or e["sender"] == W[sender])
            and (receiver is None or e["receiver"] == W[receiver])]


def _incomplete_ingest(name: str, reason: MissingReason = MissingReason.TIMEOUT) -> IngestResult:
    """Валідний результат з неповним переліченням покупців: `delegated` дзеркалить його (правило 001)."""
    base = load_ingest_fixture(name)
    buyers = BuyersCompleteness(False, reason, "listing stopped")
    return dataclasses.replace(
        base, completeness=Completeness.derive(base.completeness.missing, buyers),
        delegated=DelegatedAnalysis.derive(base.delegated.links, base.delegated.unpaired, buyers),
    )


def _ingest_service(failures=None, cache=None):
    clock = FakeClock()
    source = FixtureRpcSource(SCENARIOS / "swapsend", failures=failures, clock=clock)
    config = dataclasses.replace(load_config(SHIPPED_INGEST), **SWAP_VALUES)
    return IngestService(config, source, clock=clock, cache=cache)


def _collect_swapsend(failures=None, cache=None) -> IngestResult:
    outcome = _ingest_service(failures, cache).collect(SWAP["mint"])
    assert isinstance(outcome, IngestResult), outcome  # не `Rejection`
    return outcome


# --- g_delegated: валідність, детермінізм, форма ребер ---------------------------------------


def test_g_delegated_result_validates_against_schema_and_is_byte_deterministic():
    # Два незалежні розбори однієї фікстури й два незалежні сервіси: рівність не через ідентичність обʼєктів.
    first_result, first_doc = _analyze_doc(load_ingest_fixture(NAME))
    second_result, second_doc = _analyze_doc(load_ingest_fixture(NAME))
    assert _errors(first_doc) == []
    assert first_doc == second_doc
    text = to_json(first_result)
    assert text == to_json(second_result) == to_json(first_result)
    assert json.loads(text) == first_doc and _errors(json.loads(text)) == []
    assert text == json.dumps(first_doc, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    assert first_doc["metadata"]["schema_version"] == "002.1"

    # Делеговані ребра в документі — рівно ті, що в незалежному еталоні; жодне не несе актив/суму/шлях.
    delegated = _edges(first_doc, kind="delegated_buy")
    assert delegated == [e for e in _EXPECTED["graph"]["edges"] if e["kind"] == "delegated_buy"]
    assert sorted(e["count"] for e in delegated) == [1, 1, 2]
    for e in delegated:
        assert (e["asset"], e["amount"], e["decimals"]) == (None, None, None)
        assert e["count"] == len(e["refs"]) >= 1
        assert all(r["instruction_path"] is None for r in e["refs"])
    # Завершеність: повний збір і повний аналіз, без попередження.
    c = first_doc["completeness"]
    assert (c["status"], c["delegated_complete"], c["delegated_reason"]) == ("complete", True, None)
    assert "delegated_incomplete" not in first_doc["report"]["warnings"]


def test_same_pair_transfer_and_delegated_buy_are_both_present_in_the_json_document():
    doc = json.loads(to_json(_analyze_doc(load_ingest_fixture(NAME))[0]))
    pair = _edges(doc, sender="A", receiver="R")
    assert sorted(e["kind"] for e in pair) == ["delegated_buy", "transfer"]
    transfer = next(e for e in pair if e["kind"] == "transfer")
    delegated = next(e for e in pair if e["kind"] == "delegated_buy")
    assert (transfer["asset"], transfer["amount"], transfer["count"]) == ("sol", 10**9, 1)
    assert [r["instruction_path"] for r in transfer["refs"]] == ["0"]
    assert (delegated["asset"], delegated["amount"], delegated["count"]) == (None, None, 1)
    assert {r["signature"] for r in transfer["refs"]}.isdisjoint({r["signature"] for r in delegated["refs"]})
    # ключ `(kind, sender, receiver, asset)` унікальний у всьому документі
    keys = [(e["kind"], e["sender"], e["receiver"], e["asset"]) for e in doc["graph"]["edges"]]
    assert len(keys) == len(set(keys))


def test_measures_in_json_count_delegated_edges_in_degree_but_not_in_unique_senders():
    """Незалежний перерахунок з `ingest.json` для КОЖНОЇ вершини документа (не з коду графа)."""
    doc = _g_delegated_doc()
    contacts: dict[str, set[str]] = {}
    senders: dict[str, set[str]] = {}
    for t in _INGEST["transfers"]:
        contacts.setdefault(t["sender"], set()).add(t["receiver"])
        contacts.setdefault(t["receiver"], set()).add(t["sender"])
        senders.setdefault(t["receiver"], set()).add(t["sender"])
    for link in _INGEST["delegated"]["links"]:  # делегована купівля — контрагент, але не відправник
        contacts.setdefault(link["payer"], set()).add(link["receiver"])
        contacts.setdefault(link["receiver"], set()).add(link["payer"])
    assert {n["address"] for n in doc["graph"]["nodes"]} == set(contacts) | {
        b["wallet"] for b in _INGEST["buyers"]}
    for node in doc["graph"]["nodes"]:
        m = node["measures"]
        assert m["degree"] == len(contacts.get(node["address"], ())), node["address"]
        assert m["unique_senders"] == len(senders.get(node["address"], ())), node["address"]
    nodes = {n["address"]: n["measures"] for n in doc["graph"]["nodes"]}
    # вершини, чий єдиний зв'язок — делегована купівля: degree = 1, відправників немає
    for wallet in ("V", "W"):
        assert nodes[W[wallet]]["degree"] == 1
        assert (nodes[W[wallet]]["unique_senders"], nodes[W[wallet]]["one_off_senders"],
                nodes[W[wallet]]["one_off_share"]) == (0, 0, None)
    # A: R (переказ І делегована купівля — один контрагент), P1, Q
    assert nodes[W["A"]]["degree"] == 3
    # R: пара з двох ребер — degree 1, відправник один (лише переказ)
    assert (nodes[W["R"]]["degree"], nodes[W["R"]]["unique_senders"]) == (1, 1)


# --- FR-002-19: неповний аналіз і «не аналізовано» -------------------------------------------


def test_incomplete_delegated_yields_incomplete_graph_status_and_warning():
    ingest = _incomplete_ingest(NAME)
    assert ingest.delegated.complete is False and len(ingest.delegated.links) == 4  # вхід: delegated.complete=false
    result, doc = _analyze_doc(ingest)
    assert _errors(doc) == []
    c = doc["completeness"]
    assert (c["delegated_complete"], c["delegated_reason"]) == (False, "timeout")
    assert c["status"] == "incomplete" and result.completeness.status is GraphCompletenessStatus.INCOMPLETE
    assert GraphWarning.DELEGATED_INCOMPLETE in result.report.warnings
    assert "delegated_incomplete" in doc["report"]["warnings"]
    # Неповнота — у completeness, а не у втраті даних: знайдені зв'язки лишаються ребрами (принцип V).
    assert len(_edges(doc, kind="delegated_buy")) == 3
    # Контроль: повний вхід — повний статус, жодного попередження.
    complete = _g_delegated_doc()
    assert complete["completeness"]["status"] == "complete"
    assert "delegated_incomplete" not in complete["report"]["warnings"]


@pytest.mark.parametrize("reason", list(MissingReason))
def test_incomplete_delegated_reason_reaches_the_contract_for_every_missing_reason(reason):
    _result, doc = _analyze_doc(_incomplete_ingest(NAME, reason))
    assert _errors(doc) == []
    assert doc["completeness"]["delegated_reason"] == reason.value == doc["completeness"]["buyers_reason"]
    assert doc["completeness"]["delegated_complete"] is False


def test_not_analyzed_input_yields_incomplete_with_reason_not_analyzed_in_contract():
    base = load_ingest_fixture(NAME)
    assert base.delegated.complete is True and base.delegated.links  # контроль: у вході є що губити
    ingest = dataclasses.replace(base, delegated=DelegatedAnalysis.NOT_ANALYZED)
    result, doc = _analyze_doc(ingest)
    assert _errors(doc) == []
    c = doc["completeness"]
    assert c["delegated_reason"] == NOT_ANALYZED_REASON == "not_analyzed"
    assert c["delegated_complete"] is False
    assert c["status"] == "incomplete" and c["ingest_status"] == "complete"  # збір повний, аналізу не було
    assert (c["buyers_complete"], c["buyers_reason"]) == (True, None)
    assert "delegated_incomplete" in doc["report"]["warnings"]
    # аналізу не було — делегованих ребер немає, і це не «відсутність зв'язків»: статус це каже
    assert _edges(doc, kind="delegated_buy") == []
    assert result.completeness.status is GraphCompletenessStatus.INCOMPLETE
    assert '"delegated_reason":"not_analyzed"' in to_json(result)


def test_schema_rejects_complete_status_and_silent_warnings_over_incomplete_delegated_analysis():
    _result, doc = _analyze_doc(_incomplete_ingest(NAME))
    assert _errors(doc) == []
    forged = copy.deepcopy(doc)
    forged["completeness"]["status"] = "complete"  # «чисто» над неповним аналізом — хибне «чисто»
    _assert_invalid_at(forged, "completeness")
    silent = copy.deepcopy(doc)
    silent["report"]["warnings"].remove("delegated_incomplete")
    _assert_invalid_at(silent, "report/warnings")
    reasonless = copy.deepcopy(doc)
    reasonless["completeness"]["delegated_reason"] = None
    _assert_invalid_at(reasonless, "completeness")


# --- Схема: форма ребра за видом -------------------------------------------------------------


def _edge_index(doc: dict, kind: str) -> int:
    return next(i for i, e in enumerate(doc["graph"]["edges"]) if e["kind"] == kind)


def _set_edge(which, **changes):
    def mutate(doc):
        edge = doc["graph"]["edges"][_edge_index(doc, which)]
        for key, value in changes.items():
            edge[key] = value
    return mutate


def _set_ref_path(kind, path):
    def mutate(doc):
        doc["graph"]["edges"][_edge_index(doc, kind)]["refs"][0]["instruction_path"] = path
    return mutate


MALFORMED_EDGES = {
    "delegated-with-asset": _set_edge("delegated_buy", asset="sol"),
    "delegated-with-amount": _set_edge("delegated_buy", amount=5),
    "delegated-with-decimals": _set_edge("delegated_buy", decimals=6),
    "delegated-with-asset-and-amount": _set_edge("delegated_buy", asset="sol", amount=10**9),
    "delegated-with-instruction-path": _set_ref_path("delegated_buy", "0"),
    "transfer-without-asset": _set_edge("transfer", asset=None),
    "transfer-without-amount": _set_edge("transfer", amount=None),
    "transfer-without-instruction-path": _set_ref_path("transfer", None),
    "transfer-relabelled-as-delegated": _set_edge("transfer", kind="delegated_buy"),
    "delegated-relabelled-as-transfer": _set_edge("delegated_buy", kind="transfer"),
}


def test_schema_rejects_delegated_edge_with_asset_or_amount():
    control = _g_delegated_doc()
    assert _errors(control) == []
    for label, mutate in MALFORMED_EDGES.items():
        doc = copy.deepcopy(control)
        mutate(doc)
        errors = _errors(doc)
        assert errors, f"{label}: the schema must reject the document"
        assert any("graph/edges" in e for e in errors), (label, errors)


def test_schema_accepts_transfer_edge_with_asset_and_rejects_nothing_in_the_control_document():
    doc = _g_delegated_doc()
    transfer = doc["graph"]["edges"][_edge_index(doc, "transfer")]
    assert transfer["asset"] == "sol" and transfer["amount"] and _errors(doc) == []
    spl = copy.deepcopy(doc)  # SPL-актив з decimals — теж валідний `transfer`
    edge = spl["graph"]["edges"][_edge_index(spl, "transfer")]
    edge.update(asset=f"spl:{SWAP['mint']}", decimals=6)
    assert _errors(spl) == []
    delegated = copy.deepcopy(doc)  # те саме значення активу на `delegated_buy` — відхилено
    delegated["graph"]["edges"][_edge_index(delegated, "delegated_buy")].update(asset=f"spl:{SWAP['mint']}", decimals=6)
    _assert_invalid_at(delegated, "graph/edges")


def test_delegated_edge_inside_a_prune_record_validates_and_is_checked_by_the_same_edge_schema():
    """Хаб-платник зі списку відсікається разом з делегованим ребром: запис відсікання несе його в `incident_edges`."""
    lists = {name: () for name in ADDRESS_CATEGORIES}
    lists["exchanges"] = (W["A"],)
    index = {a: name for name, addresses in lists.items() for a in addresses}
    config = dataclasses.replace(_config(lists=False), lists=AddressLists(
        version=1, categories=MappingProxyType(lists), index=MappingProxyType(index)), lists_digest="1" * 64)
    result, doc = _analyze_doc(load_ingest_fixture(NAME), config)
    assert _errors(doc) == []
    (record,) = [r for r in doc["pruned"] if r["address"] == W["A"]]
    delegated = [e for e in record["incident_edges"] if e["kind"] == "delegated_buy"]
    assert [(e["receiver"], e["count"]) for e in delegated] == [(W["R"], 1)]
    assert (delegated[0]["asset"], delegated[0]["amount"], delegated[0]["decimals"]) == (None, None, None)
    # ребра запису відсікання перевіряються тим самим визначенням `edge`
    forged = copy.deepcopy(doc)
    next(r for r in forged["pruned"] if r["address"] == W["A"])["incident_edges"][
        record["incident_edges"].index(delegated[0])]["amount"] = 7
    _assert_invalid_at(forged, "pruned")
    assert to_json(result) == to_json(GraphService(config).analyze(load_ingest_fixture(NAME)))


# --- Справжній збір swapsend -----------------------------------------------------------------


def test_end_to_end_swapsend_collect_then_analyze_has_delegated_edge_A_to_B():
    payer, receiver = LINK["payer"], LINK["receiver"]
    ingest = _collect_swapsend()
    result = GraphService(_config()).analyze(ingest)
    doc, text = to_dict(result), to_json(result)
    assert _errors(doc) == [] and json.loads(text) == doc

    # ребро A->B у документі: одне, delegated_buy, з первинним посиланням, без активу/суми/шляху
    (edge,) = [e for e in doc["graph"]["edges"] if e["kind"] == "delegated_buy"]
    assert (edge["sender"], edge["receiver"]) == (payer, receiver)
    assert (edge["asset"], edge["amount"], edge["decimals"], edge["count"]) == (None, None, None, 1)
    assert edge["refs"] == [{"signature": LINK["signature"], "slot": LINK["slot"], "instruction_path": None}]
    assert (edge["first_time"], edge["last_time"]) == (LINK["block_time"], LINK["block_time"])
    assert not [e for e in doc["graph"]["edges"] if e["kind"] == "transfer"]  # переказів у сценарії немає

    # B — не покупець; покупці — рівно P1..P3 еталона 001
    nodes = {n["address"]: n for n in doc["graph"]["nodes"]}
    assert nodes[receiver]["roles"] == ["delegated_receiver"] and nodes[receiver]["buyer_rank"] is None
    assert nodes[payer]["roles"] == ["delegated_payer"] and nodes[payer]["buyer_rank"] is None
    buyers = {a: n["buyer_rank"] for a, n in nodes.items() if "buyer" in n["roles"]}
    assert buyers == {b["wallet"]: b["rank"] for b in SWAP["buyers"]}
    assert receiver not in buyers and doc["metadata"]["wallets_analyzed"] == len(SWAP["buyers"])
    # вимір: відправником делегована купівля не робить
    assert (nodes[receiver]["measures"]["degree"], nodes[receiver]["measures"]["unique_senders"]) == (1, 0)

    # кандидатів без пари в документі немає ніде: ні вершини, ні ребра, ні запису відсікання
    unpaired = {c["wallet"] for c in SWAP["delegated"]["unpaired"]}
    assert unpaired
    in_edges = {e[side] for e in doc["graph"]["edges"] for side in ("sender", "receiver")}
    in_pruned = {r["address"] for r in doc["pruned"]}
    assert unpaired.isdisjoint(nodes) and unpaired.isdisjoint(in_edges) and unpaired.isdisjoint(in_pruned)

    # повнота
    c = doc["completeness"]
    assert (c["status"], c["delegated_complete"], c["delegated_reason"]) == ("complete", True, None)
    assert "delegated_incomplete" not in doc["report"]["warnings"]
    # два незалежні збори й аналізи — один і той самий документ байт-у-байт
    assert text == to_json(GraphService(_config()).analyze(_collect_swapsend()))


FAILURES = [
    pytest.param(lambda: [FailAfter(1, RpcUnavailable("node down"))], id="down-at-start"),
    pytest.param(lambda: [FailAfter(3, RpcUnavailable("node down"))], id="after-3"),
    pytest.param(lambda: [FailFor(LINK["signature"], RpcUnavailable("node down"), times=1)], id="link-tx-once"),
]


@pytest.mark.parametrize("failures", FAILURES)
def test_resume_after_failure_gives_complete_document_byte_equal_to_a_fresh_one(failures):
    cache = ResultCache()
    first = _collect_swapsend(failures=failures(), cache=cache)
    first_result, first_doc = _analyze_doc(first)
    # відмова: документ валідний, неповний, причина видима; делегований аналіз дзеркалить покупців
    assert _errors(first_doc) == []
    c = first_doc["completeness"]
    assert c["status"] == "incomplete"
    assert c["delegated_complete"] == c["buyers_complete"]
    assert (c["delegated_reason"] is not None) == (not c["delegated_complete"])
    assert ("delegated_incomplete" in first_doc["report"]["warnings"]) == (not c["delegated_complete"])

    # resume: той самий кеш, здорове джерело — граф повний і рівний свіжому на рівні `to_json`
    resumed = _collect_swapsend(cache=cache)
    assert resumed.metadata.resumed is True
    resumed_result, resumed_doc = _analyze_doc(resumed)
    assert _errors(resumed_doc) == []
    assert resumed_doc["completeness"]["status"] == "complete"
    assert resumed_doc["completeness"]["delegated_complete"] is True
    assert "delegated_incomplete" not in resumed_doc["report"]["warnings"]
    fresh_result = GraphService(_config()).analyze(_collect_swapsend())
    assert to_json(resumed_result) == to_json(fresh_result)
    (edge,) = [e for e in resumed_doc["graph"]["edges"] if e["kind"] == "delegated_buy"]
    assert (edge["sender"], edge["receiver"]) == (LINK["payer"], LINK["receiver"])
    assert to_json(first_result) != to_json(resumed_result)  # неповний і повний результати розрізнювані
