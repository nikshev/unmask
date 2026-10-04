# verifies: FR-002-04, FR-002-13, FR-002-21, FR-002-22
"""Серіалізація результату фічі 002 за контрактом (T-040): `graph.serialize.to_dict` / `to_json`.

Еталон форми — `contracts/graph-result.schema.json` (`Draft202012Validator`, схема не послаблюється).

Що доводять тести:

- `to_dict` валідний проти схеми на всіх 12 сценаріях, містить лише точні JSON-типи (жодних `frozenset`,
  `tuple`, `StrEnum`, `Asset`) і рівно ті ключі верхнього рівня, що вимагає схема;
- golden: розділи `completeness`, `report`, `pruned`, `buyer_flags`, `graph` і `metadata` дорівнюють НЕЗАЛЕЖНОМУ
  еталону — `expected.json` (генератор `build_graph_fixtures.py`) та `ingest.json`, а не іншій ділянці `serialize`;
  порівняння — канонічним JSON, тож `1` проти `1.0` теж ловиться;
- сама схема — частина контракту: ручні документи, що порушують SC-002 (запис без критеріїв), FR-002-05 (повний
  статус над неповним збором), FR-002-12 (списки не застосовано без попередження), FR-002-22 (пилові числа,
  виміри, пороги), відхиляються з очікуваної причини (шлях помилки), а контрольний документ валідний;
- SC-004: `to_json` двічі побайтово однаковий, обертається без втрат, не залежить від порядку входу
  `IngestResult` і від `PYTHONHASHSEED` (ролі — відсортований список, а не порядок `frozenset`);
- `schema_version` результату `002.1` не залежить від `$id`/версії схеми 001.

Паритети попереджень виражені в `allOf` схеми (T-040): `address_lists_not_applied` ⇔ `lists_applied == false`,
`delegated_incomplete` ⇔ `delegated_complete == false`, `all_sources_pruned` ⇔ записи відсікання є й кожна вершина
графа результату — покупець (рівносильно `before.nodes - buyers_total >= 1` і `after.nodes - buyers_total == 0` за
інваріантами `GraphResult`). Кожну умову закріплює тест-відмова на ручному документі.
"""

import copy
import dataclasses
import hashlib
import json
import math
import os
import random
import subprocess
import sys
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from conftest import GRAPH_FIXTURES, load_ingest_fixture
from unmask.graph.model import (
    GRAPH_SCHEMA_VERSION,
    GraphResult,
    GraphWarning,
    NodeRole,
    ThresholdsSnapshot,
)
from unmask.graph.serialize import to_dict, to_json
from unmask.graph.service import GraphService
from unmask.hubs.config import ADDRESS_CATEGORIES, AddressLists, HubConfig, HubThresholds, load_hub_config
from unmask.ingest.model import (
    BuyersCompleteness,
    Completeness,
    DelegatedAnalysis,
    MissingHistory,
    MissingReason,
    Rejection,
    RejectKind,
)

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "specs" / "002-funding-graph-hub-pruning" / "contracts" / "graph-result.schema.json"
SCHEMA_001_PATH = ROOT / "specs" / "001-onchain-data-ingest" / "contracts" / "ingest-result.schema.json"
SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
SCHEMA_001 = json.loads(SCHEMA_001_PATH.read_text(encoding="utf-8"))
VALIDATOR = Draft202012Validator(SCHEMA)
SHIPPED_HUBS = ROOT / "config" / "hubs.yaml"
SHIPPED_LISTS = ROOT / "config" / "hub_addresses.yaml"

# Зафіксований набір із 12 сценаріїв: нові сценарії додаються сюди свідомо.
SCENARIOS = ["g_all_hubs", "g_basic", "g_buyer_hub", "g_delegated", "g_dust", "g_dust_mixed", "g_empty",
             "g_financier", "g_hub", "g_incomplete", "g_known", "g_unexpanded"]

JSON_TYPES = (dict, list, str, int, float, bool, type(None))


# --- Помічники -------------------------------------------------------------------------------


def _expected(name: str) -> dict:
    return json.loads((GRAPH_FIXTURES / name / "expected.json").read_text(encoding="utf-8"))


def _ingest_doc(name: str) -> dict:
    doc = json.loads((GRAPH_FIXTURES / name / "ingest.json").read_text(encoding="utf-8"))
    return doc.get("result", doc)


def _lists(version: int = 1, **categories) -> AddressLists:
    parsed = {name: tuple(categories.get(name, ())) for name in ADDRESS_CATEGORIES}
    index = {address: name for name in ADDRESS_CATEGORIES for address in parsed[name]}
    return AddressLists(version=version, categories=MappingProxyType(parsed), index=MappingProxyType(index))


def _scenario_config(expected: dict, *, no_lists: bool = False) -> HubConfig:
    lists = None if no_lists else expected["config"]["lists"]
    return HubConfig(
        thresholds=HubThresholds(version=expected["config"]["version"], **expected["config"]["thresholds"]),
        lists=None if lists is None else _lists(lists["version"], **lists["categories"]),
        thresholds_digest="0" * 64, lists_digest=None if lists is None else "1" * 64,
    )


def _analyze(name: str, *, no_lists: bool = False, ingest=None) -> GraphResult:
    config = _scenario_config(_expected(name), no_lists=no_lists)
    return GraphService(config).analyze(ingest if ingest is not None else load_ingest_fixture(name))


def _errors(instance) -> list[str]:
    return [f"{'/'.join(map(str, e.absolute_path))}: {e.message}" for e in VALIDATOR.iter_errors(instance)]


def _assert_invalid_at(instance, path_fragment: str) -> None:
    """Невалідний саме там, де очікується: шлях помилки містить `path_fragment`."""
    errors = _errors(instance)
    assert errors, "the hand-made document must be rejected by the schema"
    assert any(path_fragment in e for e in errors), (path_fragment, errors)


def _canon(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _assert_json_types_only(value, path="$") -> None:
    """Точні JSON-типи (не підкласи): `StrEnum`/`Asset`/`tuple`/`frozenset` не просочуються."""
    assert type(value) in JSON_TYPES, f"{path}: {type(value)!r}"
    if isinstance(value, dict):
        for k, v in value.items():
            assert type(k) is str, f"{path}: key {k!r} is {type(k)!r}"
            _assert_json_types_only(v, f"{path}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _assert_json_types_only(v, f"{path}[{i}]")


def _doc(name: str, **kwargs) -> dict:
    """Незалежна глибока копія документа: ручні правки не торкаються моделі."""
    return copy.deepcopy(to_dict(_analyze(name, **kwargs)))


# --- Валідність схеми й форма ---------------------------------------------------------------


def test_schema_itself_is_valid_draft_2020_12():
    Draft202012Validator.check_schema(SCHEMA)


@pytest.mark.parametrize("name", SCENARIOS)
def test_result_dict_validates_against_schema_on_every_scenario(name):
    doc = to_dict(_analyze(name))
    assert _errors(doc) == []
    _assert_json_types_only(doc)
    assert set(doc) == set(SCHEMA["required"]) == {"metadata", "completeness", "graph", "pruned", "buyer_flags",
                                                   "report"}


def test_scenarios_list_covers_every_fixture_directory():
    on_disk = sorted(p.name for p in GRAPH_FIXTURES.iterdir() if (p / "ingest.json").is_file())
    assert on_disk == SCENARIOS


@pytest.mark.parametrize("name", SCENARIOS)
def test_result_dict_matches_independent_expected_json(name):
    """Незалежний еталон: `expected.json` (генератор) і `ingest.json`, а не `serialize`."""
    expected, doc = _expected(name), to_dict(_analyze(name))

    assert _canon(doc["completeness"]) == _canon(expected["completeness"])
    assert _canon(doc["report"]) == _canon(expected["report"])
    assert _canon(doc["pruned"]) == _canon(expected["prune"]["records"])
    assert _canon(doc["buyer_flags"]) == _canon(expected["prune"]["buyer_flags"])

    # `expected["graph"]` — ПОВНИЙ граф до відсікання; граф результату — його підмножина за `prune.after`.
    keep_nodes = set(expected["prune"]["after"]["node_addresses"])
    keep_edges = {tuple(k) for k in expected["prune"]["after"]["edge_keys"]}
    nodes = [n for n in expected["graph"]["nodes"] if n["address"] in keep_nodes]
    edges = [e for e in expected["graph"]["edges"]
             if (e["kind"], e["sender"], e["receiver"], e["asset"] or "") in keep_edges]
    assert len(nodes) == len(keep_nodes) and len(edges) == len(keep_edges)
    assert _canon(doc["graph"]) == _canon({"nodes": nodes, "edges": edges})

    meta, config = _ingest_doc(name)["metadata"], expected["config"]
    lists = config["lists"]
    assert _canon(doc["metadata"]) == _canon({
        "mint": meta["mint"], "schema_version": "002.1", "ingest_analyzed_at": meta["analyzed_at"],
        "ingest_config_version": meta["config_version"], "ingest_source": meta["source"],
        "wallets_analyzed": meta["wallets_analyzed"], "hub_config_version": config["version"],
        "address_lists_version": None if lists is None else lists["version"], "lists_applied": lists is not None,
        "thresholds": config["thresholds"],
        "nodes_total": len(expected["graph"]["nodes"]), "edges_total": len(expected["graph"]["edges"]),
    })


def _distinct_config(*, off_curve: bool, high_degree: bool, lists_version: int = 11) -> HubConfig:
    lists = _expected("g_known")["config"]["lists"]["categories"]
    return HubConfig(
        thresholds=HubThresholds(
            version=7, degree_threshold=17, one_off_senders_share=0.61, one_off_min_senders=6,
            giant_component_warn_share=0.37, prune_off_curve=off_curve, prune_ingest_high_degree=high_degree,
            dust_amount_lamports=123_456, dust_min_fanout=3),
        lists=_lists(lists_version, **lists), thresholds_digest="0" * 64, lists_digest="1" * 64)


@pytest.mark.parametrize("off_curve, high_degree", [(False, True), (True, False)])
def test_metadata_fields_come_from_their_own_sources_with_pairwise_distinct_values(off_curve, high_degree):
    """У фікстурах версії збору й хаба рівні (2 і 2), а обидва перемикачі `true`: підміну полів golden не ловить.
    Тут кожне значення своє, тож будь-яка підміна `serialize` падає."""
    base = load_ingest_fixture("g_basic")
    ingest = dataclasses.replace(base, metadata=dataclasses.replace(
        base.metadata, config_version=31, analyzed_at=1_759_999_999, source="fixture:custom_src"))
    result = GraphService(_distinct_config(off_curve=off_curve, high_degree=high_degree)).analyze(ingest)
    doc = to_dict(result)
    assert _errors(doc) == []
    expected_full = _expected("g_basic")["graph"]  # повний граф: лічильники не залежать від порогів
    assert (len(expected_full["nodes"]), len(expected_full["edges"]), base.metadata.wallets_analyzed) == (9, 8, 5)
    assert doc["metadata"] == {
        "mint": base.metadata.mint, "schema_version": "002.1", "ingest_analyzed_at": 1_759_999_999,
        "ingest_config_version": 31, "ingest_source": "fixture:custom_src", "wallets_analyzed": 5,
        "hub_config_version": 7, "address_lists_version": 11, "lists_applied": True,
        "thresholds": {
            "degree_threshold": 17, "one_off_senders_share": 0.61, "one_off_min_senders": 6,
            "giant_component_warn_share": 0.37, "prune_off_curve": off_curve, "prune_ingest_high_degree": high_degree,
            "dust_amount_lamports": 123_456, "dust_min_fanout": 3,
        },
        "nodes_total": 9, "edges_total": 8,
    }
    assert doc["report"]["warn_share"] == 0.37


def test_versions_reach_prune_records_and_list_hits_from_the_config_not_the_ingest():
    base = load_ingest_fixture("g_known")
    ingest = dataclasses.replace(base, metadata=dataclasses.replace(base.metadata, config_version=31))
    doc = to_dict(GraphService(_distinct_config(off_curve=True, high_degree=True)).analyze(ingest))
    assert _errors(doc) == [] and len(doc["pruned"]) == 2
    assert doc["metadata"]["ingest_config_version"] == 31 and doc["metadata"]["hub_config_version"] == 7
    for record in doc["pruned"]:
        assert (record["config_version"], record["lists_version"]) == (7, 11)
    by_detail = {h["detail"]: h for r in doc["pruned"] for h in r["criteria"]}
    assert by_detail["list:launchpads"]["lists_version"] == 11
    assert by_detail["address_type:off_curve"]["lists_version"] is None


def test_expected_sections_are_nontrivial_somewhere():
    """Golden-перевірка не порожня: у наборі є записи, позначки покупців, обидва види ребер, ролі, `null`-вимір."""
    docs = [to_dict(_analyze(n)) for n in SCENARIOS]
    assert any(d["pruned"] for d in docs) and any(d["buyer_flags"] for d in docs)
    kinds = {e["kind"] for d in docs for e in d["graph"]["edges"]}
    assert kinds == {"transfer", "delegated_buy"}
    assert any(d["completeness"]["missing"] for d in docs)
    assert any(n["unexpanded"] is not None for d in docs for n in d["graph"]["nodes"])
    assert any(r["incident_edges"] for d in docs for r in d["pruned"])


# --- ролі: відсортований список -------------------------------------------------------------


@pytest.mark.parametrize("name", SCENARIOS)
def test_roles_are_sorted_lists_in_every_node(name):
    for node in to_dict(_analyze(name))["graph"]["nodes"]:
        roles = node["roles"]
        assert type(roles) is list and roles and roles == sorted(set(roles))
        assert set(roles) <= {r.value for r in NodeRole}


def test_multi_role_nodes_serialize_roles_in_alphabetical_order():
    def multi(name):
        return [n["roles"] for n in to_dict(_analyze(name))["graph"]["nodes"] if len(n["roles"]) > 1]

    assert multi("g_basic") == [["buyer", "funder"]]
    delegated = multi("g_delegated")
    # Порядок вершин не перевіряється (залежить від адрес фікстури): кожен список — відсортований, набір — відомий.
    assert all(roles == sorted(roles) for roles in delegated)
    assert sorted(delegated) == [["buyer", "delegated_payer"], ["buyer", "delegated_receiver"],
                                 ["delegated_payer", "funder"], ["delegated_payer", "funder"]]


# --- ручні документи: схема відхиляє порушення контракту ------------------------------------


def test_schema_rejects_pruned_record_without_criteria():
    """SC-002: запис відсікання без пояснення не існує — навіть як ручний документ."""
    doc = _doc("g_hub")
    assert _errors(doc) == [] and doc["pruned"]
    doc["pruned"][0]["criteria"] = []
    _assert_invalid_at(doc, "pruned/0/criteria")

    flagged = _doc("g_buyer_hub")
    assert _errors(flagged) == [] and flagged["buyer_flags"]
    flagged["buyer_flags"][0]["criteria"] = []
    _assert_invalid_at(flagged, "buyer_flags/0/criteria")


def test_schema_rejects_complete_status_over_incomplete_ingest():
    """FR-002-05: граф над неповним збором ніколи не `complete`; `complete` вимагає всіх повнот."""
    base = _doc("g_incomplete")
    assert _errors(base) == [] and base["completeness"]["status"] == "incomplete"
    over_ingest = copy.deepcopy(base)
    over_ingest["completeness"]["status"] = "complete"
    _assert_invalid_at(over_ingest, "completeness")

    complete = _doc("g_basic")
    assert _errors(complete) == [] and complete["completeness"]["status"] == "complete"
    for label, mutate in {
        "ingest_status": lambda c: c.update(ingest_status="incomplete"),
        "missing": lambda c: c.update(missing=[{"wallet": complete["graph"]["nodes"][0]["address"], "depth": 1,
                                                "reason": "timeout", "detail": "x"}]),
        "buyers_complete": lambda c: c.update(buyers_complete=False, buyers_reason="timeout"),
        "delegated_complete": lambda c: c.update(delegated_complete=False, delegated_reason="timeout"),
    }.items():
        broken = copy.deepcopy(complete)
        mutate(broken["completeness"])
        assert _errors(broken), label
        assert any("completeness" in e for e in _errors(broken)), (label, _errors(broken))

    # Зворотне: неповний делегований аналіз над `incomplete`-статусом — валідний лише з `incomplete`.
    delegated_incomplete = copy.deepcopy(complete)
    delegated_incomplete["completeness"].update(delegated_complete=False, delegated_reason="timeout")
    delegated_incomplete["report"]["warnings"] = ["delegated_incomplete"]
    _assert_invalid_at(delegated_incomplete, "completeness")  # status лишився complete
    delegated_incomplete["completeness"]["status"] = "incomplete"
    assert _errors(delegated_incomplete) == []


def test_schema_rejects_lists_applied_false_without_warning():
    """FR-002-12: відсутність списків — лише з попередженням, і без жодного `list:*` у записах."""
    no_lists = _doc("g_known", no_lists=True)
    assert _errors(no_lists) == []
    assert no_lists["metadata"]["lists_applied"] is False
    assert no_lists["report"]["warnings"].count("address_lists_not_applied") == 1

    silent = copy.deepcopy(no_lists)
    silent["report"]["warnings"].remove("address_lists_not_applied")
    _assert_invalid_at(silent, "report/warnings")

    # Той самий документ зі списками, ручно переведений у «lists_applied=false» без попередження.
    applied = _doc("g_known")
    assert _errors(applied) == [] and applied["metadata"]["lists_applied"] is True
    forged = copy.deepcopy(applied)
    forged["metadata"].update(lists_applied=False, address_lists_version=None)
    assert any("report/warnings" in e for e in _errors(forged)), _errors(forged)
    # З попередженням, але зі збереженим `list:*` у записі — теж невалідно (FR-002-12, друга умова).
    forged["report"]["warnings"] = sorted(forged["report"]["warnings"] + ["address_lists_not_applied"])
    assert any("pruned" in e and "detail" in e for e in _errors(forged)), _errors(forged)

    # lists_applied ⇔ версія списків є.
    for applied_flag, version in ((True, None), (False, 1)):
        mismatch = copy.deepcopy(applied)
        mismatch["metadata"].update(lists_applied=applied_flag, address_lists_version=version)
        assert any("address_lists_version" in e for e in _errors(mismatch)), (applied_flag, _errors(mismatch))


def _dust_doc() -> dict:
    doc = _doc("g_dust")
    assert _errors(doc) == []
    record = doc["pruned"][0]
    assert record["criteria"][0]["criterion"] == "dust_fanout" and record["measures"]["buyer_fanout"] > 0
    return doc


def _node_with(doc: dict, predicate) -> dict:
    return next(n for n in doc["graph"]["nodes"] if predicate(n["measures"]))


@pytest.mark.parametrize("label, mutate, where", [
    ("measured null", lambda d: d["pruned"][0]["criteria"][0].update(measured=None), "criteria/0/measured"),
    ("threshold null", lambda d: d["pruned"][0]["criteria"][0].update(threshold=None), "criteria/0/threshold"),
    ("measured fractional", lambda d: d["pruned"][0]["criteria"][0].update(measured=500000.5),
     "criteria/0/measured"),
    ("measured zero", lambda d: d["pruned"][0]["criteria"][0].update(measured=0), "criteria/0/measured"),
    ("threshold fractional", lambda d: d["pruned"][0]["criteria"][0].update(threshold=1000000.5),
     "criteria/0/threshold"),
    ("detail not 'measured'", lambda d: d["pruned"][0]["criteria"][0].update(detail="unexpanded:high_degree"),
     "criteria/0/detail"),
    ("measures without buyer_fanout", lambda d: d["pruned"][0]["measures"].pop("buyer_fanout"),
     "pruned/0/measures"),
    ("measures without median_to_buyers", lambda d: d["pruned"][0]["measures"].pop("median_to_buyers"),
     "pruned/0/measures"),
    ("node measures without buyer_fanout", lambda d: d["graph"]["nodes"][0]["measures"].pop("buyer_fanout"),
     "graph/nodes/0/measures"),
    ("buyer_fanout 0 with numeric median",
     lambda d: _node_with(d, lambda m: m["buyer_fanout"] == 0)["measures"].update(median_to_buyers=5),
     "measures/median_to_buyers"),
    ("negative buyer_fanout", lambda d: d["pruned"][0]["measures"].update(buyer_fanout=-1), "buyer_fanout"),
    ("fanout positive with null median", lambda d: d["pruned"][0]["measures"].update(median_to_buyers=None),
     "median_to_buyers"),
    ("zero median with fanout", lambda d: d["pruned"][0]["measures"].update(median_to_buyers=0),
     "median_to_buyers"),
    ("thresholds without dust_amount_lamports",
     lambda d: d["metadata"]["thresholds"].pop("dust_amount_lamports"), "metadata/thresholds"),
    ("thresholds without dust_min_fanout", lambda d: d["metadata"]["thresholds"].pop("dust_min_fanout"),
     "metadata/thresholds"),
    ("dust_min_fanout below 2", lambda d: d["metadata"]["thresholds"].update(dust_min_fanout=1),
     "thresholds/dust_min_fanout"),
    ("dust_amount_lamports zero", lambda d: d["metadata"]["thresholds"].update(dust_amount_lamports=0),
     "thresholds/dust_amount_lamports"),
    ("unknown extra threshold", lambda d: d["metadata"]["thresholds"].update(unexpected=1), "metadata/thresholds"),
])
def test_schema_rejects_dust_hit_without_numbers_and_measures_without_dust_fields(label, mutate, where):
    """FR-002-22: ручні документи з порушеними пиловими числами, вимірами чи порогами невалідні."""
    doc = _dust_doc()
    mutate(doc)
    _assert_invalid_at(doc, where)


def _hit_in(doc: dict, criterion: str, detail: str) -> dict:
    return next(h for r in doc["pruned"] for h in r["criteria"]
                if (h["criterion"], h["detail"]) == (criterion, detail))


@pytest.mark.parametrize("label, mutate, where, scenario", [
    ("degree hit with null measured", lambda d: _hit_in(d, "degree", "measured").update(measured=None), "measured", "g_all_hubs"),
    ("one_off hit with null threshold",
     lambda d: _hit_in(d, "one_off_senders", "measured").update(threshold=None), "threshold", "g_hub"),
    ("ingest_high_degree hit with null measured",
     lambda d: _hit_in(d, "ingest_high_degree", "unexpanded:high_degree").update(measured=None), "measured", "g_all_hubs"),
    ("off_curve known_list with numeric measured",
     lambda d: _hit_in(d, "known_list", "address_type:off_curve").update(measured=3), "measured", "g_all_hubs"),
    ("list known_list with numeric threshold",
     lambda d: _hit_in(d, "known_list", "list:launchpads").update(threshold=3), "threshold", "g_all_hubs"),
    ("list hit without lists_version",
     lambda d: _hit_in(d, "known_list", "list:launchpads").update(lists_version=None), "lists_version", "g_all_hubs"),
    ("off_curve hit with lists_version",
     lambda d: _hit_in(d, "known_list", "address_type:off_curve").update(lists_version=1), "lists_version", "g_all_hubs"),
    ("degree hit with lists_version", lambda d: _hit_in(d, "degree", "measured").update(lists_version=1),
     "lists_version", "g_all_hubs"),
    ("known_list with detail 'measured'",
     lambda d: _hit_in(d, "known_list", "address_type:off_curve").update(detail="measured"), "detail", "g_all_hubs"),
    ("unknown criterion", lambda d: _hit_in(d, "degree", "measured").update(criterion="signature_cap"), "criterion", "g_all_hubs"),
    ("share with zero senders",
     lambda d: next(n for n in d["graph"]["nodes"] if n["measures"]["unique_senders"] == 0)["measures"].update(
         one_off_share=0.5), "one_off_share", "g_hub"),
    ("null share with senders",
     lambda d: next(n for n in d["graph"]["nodes"] if n["measures"]["unique_senders"] > 0)["measures"].update(
         one_off_share=None), "one_off_share", "g_hub"),
])
def test_schema_rejects_null_where_a_number_is_required_and_a_number_where_null_is_required(
        label, mutate, where, scenario):
    """`None` — лише там, де схема його дозволяє: ручні документи з `null` не на місці (і числом не на місці)."""
    doc = _doc(scenario)
    assert _errors(doc) == []
    mutate(doc)
    _assert_invalid_at(doc, where)


def _warnings(doc: dict) -> list[str]:
    return doc["report"]["warnings"]


def test_schema_rejects_inverted_all_sources_pruned_warning():
    """FR-002-11 (R-16): `all_sources_pruned` ⇔ записи відсікання є, а в графі лишились самі покупці."""
    # Усі джерела відсічені: попередження обовʼязкове.
    pruned_all = _doc("g_all_hubs")
    assert _errors(pruned_all) == [] and "all_sources_pruned" in _warnings(pruned_all)
    assert pruned_all["pruned"] and all("buyer" in n["roles"] for n in pruned_all["graph"]["nodes"])
    missing = copy.deepcopy(pruned_all)
    _warnings(missing).remove("all_sources_pruned")
    _assert_invalid_at(missing, "report/warnings")

    # Усі джерела цілі (нічого не відсічено): попередження зайве.
    nothing = _doc("g_basic")
    assert _errors(nothing) == [] and not nothing["pruned"] and "all_sources_pruned" not in _warnings(nothing)
    spurious = copy.deepcopy(nothing)
    _warnings(spurious).append("all_sources_pruned")
    _assert_invalid_at(spurious, "report/warnings")

    # Частину джерел відсічено, частина лишилась: попередження зайве.
    partial = _doc("g_hub")
    assert _errors(partial) == [] and partial["pruned"]
    assert any("buyer" not in n["roles"] for n in partial["graph"]["nodes"])
    wrongly = copy.deepcopy(partial)
    _warnings(wrongly).append("all_sources_pruned")
    _assert_invalid_at(wrongly, "report/warnings")

    # Одна вціліла вершина без ролі покупця скасовує умову (перевіряється `items`, а не лише `minItems`).
    survivor = copy.deepcopy(pruned_all)
    extra = copy.deepcopy(survivor["graph"]["nodes"][0])
    extra.update(address=survivor["pruned"][0]["address"], roles=["funder"], buyer_rank=None, depth=1)
    survivor["graph"]["nodes"].append(extra)
    _assert_invalid_at(survivor, "report/warnings")  # попередження лишилось, а джерело вціліло
    _warnings(survivor).remove("all_sources_pruned")
    assert not any("report/warnings" in e for e in _errors(survivor))

    # Порожній граф без записів: попередження зайве (`empty_graph` — окреме).
    empty = _doc("g_empty")
    assert _errors(empty) == [] and _warnings(empty) == ["empty_graph"] and not empty["pruned"]
    _warnings(empty).append("all_sources_pruned")
    _assert_invalid_at(empty, "report/warnings")

    # Усі джерела відсічені й покупців немає: записи є, graph.nodes порожній -> обовʼязкове (поруч з empty_graph).
    no_buyers = copy.deepcopy(pruned_all)
    no_buyers["graph"]["nodes"], no_buyers["graph"]["edges"] = [], []
    assert "all_sources_pruned" in _warnings(no_buyers)
    assert not any("report/warnings" in e for e in _errors(no_buyers))
    _warnings(no_buyers).remove("all_sources_pruned")
    _assert_invalid_at(no_buyers, "report/warnings")


def test_schema_rejects_address_lists_not_applied_warning_when_lists_are_applied():
    """FR-002-12, зворотний напрям: списки застосовано -> попередження про їх відсутність неправдиве."""
    applied = _doc("g_known")
    assert _errors(applied) == [] and applied["metadata"]["lists_applied"] is True
    forged = copy.deepcopy(applied)
    forged["report"]["warnings"] = sorted(_warnings(forged) + ["address_lists_not_applied"])
    _assert_invalid_at(forged, "report/warnings")
    # Контроль: у документа без списків це попередження обовʼязкове (перший напрям, вже в схемі).
    assert "address_lists_not_applied" in _warnings(_doc("g_known", no_lists=True))


def test_schema_rejects_delegated_incomplete_warning_out_of_sync_with_delegated_complete():
    """FR-002-19: `delegated_incomplete` у попередженнях ⇔ `delegated_complete == false`."""
    complete = _doc("g_basic")
    assert _errors(complete) == [] and complete["completeness"]["delegated_complete"] is True
    spurious = copy.deepcopy(complete)
    _warnings(spurious).append("delegated_incomplete")
    _assert_invalid_at(spurious, "report/warnings")

    incomplete = to_dict(_analyze("g_basic", ingest=_with_incomplete_delegated("g_basic")))
    assert _errors(incomplete) == [] and incomplete["completeness"]["delegated_complete"] is False
    assert "delegated_incomplete" in _warnings(incomplete)
    silent = copy.deepcopy(incomplete)
    _warnings(silent).remove("delegated_incomplete")
    _assert_invalid_at(silent, "report/warnings")

    # Ручне перемикання `delegated_complete` у валідному документі без зміни попереджень.
    flipped = copy.deepcopy(complete)
    flipped["completeness"].update(delegated_complete=False, delegated_reason="timeout", status="incomplete")
    _assert_invalid_at(flipped, "report/warnings")


def test_schema_rejects_malformed_documents_for_every_new_dust_requirement_together():
    """Контроль: контрольний документ валідний, а кожна група вимог FR-002-22 справді перевіряється окремо."""
    doc = _dust_doc()
    assert doc["metadata"]["thresholds"]["dust_amount_lamports"] == 1_000_000
    assert doc["metadata"]["thresholds"]["dust_min_fanout"] == 5
    assert {"buyer_fanout", "median_to_buyers"} <= set(SCHEMA["$defs"]["measures"]["required"])
    assert {"dust_amount_lamports", "dust_min_fanout"} <= set(SCHEMA["$defs"]["thresholds"]["required"])
    assert "dust_fanout" in SCHEMA["$defs"]["criterionHit"]["properties"]["criterion"]["enum"]


# --- Повнота: варіанти причин ---------------------------------------------------------------


def _with_incomplete_delegated(name: str):
    base = load_ingest_fixture(name)
    buyers = BuyersCompleteness(False, MissingReason.TIMEOUT, "listing stopped")
    return dataclasses.replace(
        base, completeness=Completeness.derive(base.completeness.missing, buyers),
        delegated=DelegatedAnalysis.derive(base.delegated.links, base.delegated.unpaired, buyers),
    )


def test_incomplete_delegated_and_not_analyzed_serialize_reasons_and_validate():
    mirrored = to_dict(_analyze("g_basic", ingest=_with_incomplete_delegated("g_basic")))
    assert _errors(mirrored) == []
    assert mirrored["completeness"]["status"] == "incomplete"
    assert (mirrored["completeness"]["buyers_complete"], mirrored["completeness"]["buyers_reason"]) == (False, "timeout")
    assert (mirrored["completeness"]["delegated_complete"],
            mirrored["completeness"]["delegated_reason"]) == (False, "timeout")
    assert GraphWarning.DELEGATED_INCOMPLETE.value in mirrored["report"]["warnings"]

    base = load_ingest_fixture("g_basic")
    not_analyzed = to_dict(_analyze("g_basic", ingest=dataclasses.replace(base, delegated=DelegatedAnalysis.NOT_ANALYZED)))
    assert _errors(not_analyzed) == []
    assert not_analyzed["completeness"]["delegated_reason"] == "not_analyzed"
    assert not_analyzed["completeness"]["delegated_complete"] is False
    assert not_analyzed["completeness"]["status"] == "incomplete"
    _assert_json_types_only(not_analyzed)


# --- SC-004: детермінізм ---------------------------------------------------------------------


@pytest.mark.parametrize("name", SCENARIOS)
def test_to_json_twice_is_byte_identical_and_round_trips(name):
    # Два незалежні розбори однієї фікстури й два виклики: рівність не через ідентичність обʼєктів.
    first, second = _analyze(name), _analyze(name)
    text = to_json(first)
    assert isinstance(text, str) and text == to_json(first) == to_json(second)
    assert text.encode("utf-8") == to_json(second).encode("utf-8")
    assert json.loads(text) == to_dict(first)
    assert _errors(json.loads(text)) == []
    # Канонічна форма: ключі відсортовані, без пробілів-роздільників, без NaN.
    assert text == json.dumps(to_dict(first), sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                              allow_nan=False)
    assert text == json.dumps(json.loads(text), sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def test_to_json_is_compact_sorted_and_keeps_cyrillic_unescaped():
    base = load_ingest_fixture("g_incomplete")
    wallet = _expected("g_basic")["wallets"]["A"]
    detail = "таймаут після 2 повторів"
    missing = (MissingHistory(wallet, 1, MissingReason.TIMEOUT, detail),)
    ingest = dataclasses.replace(base, completeness=Completeness.derive(missing, base.completeness.buyers))
    result = _analyze("g_incomplete", ingest=ingest)
    text = to_json(result)
    assert detail in text and "\\u" not in text  # ensure_ascii=False
    assert json.loads(text)["completeness"]["missing"][0]["detail"] == detail
    assert ", " not in text.replace(detail, "") and '":' in text and '": ' not in text.replace(detail, "")
    assert text.startswith('{"buyer_flags":[') and text.index('"completeness"') < text.index('"graph"') \
        < text.index('"metadata"') < text.index('"pruned"') < text.index('"report"')
    keys = list(json.loads(text)["metadata"])
    assert keys == sorted(keys)


def test_to_json_rejects_non_finite_numbers():
    # `ThresholdsSnapshot` сам відхиляє NaN (див. нижче), тож отруюємо вже створений знімок в обхід `__post_init__`.
    result = _analyze("g_basic")
    object.__setattr__(result.metadata.thresholds, "one_off_senders_share", math.nan)
    assert math.isnan(to_dict(result)["metadata"]["thresholds"]["one_off_senders_share"])  # to_dict не фільтрує
    with pytest.raises(ValueError):
        to_json(result)


@pytest.mark.parametrize("field", ["one_off_senders_share", "giant_component_warn_share"])
def test_nan_threshold_never_reaches_a_result(field):
    """NaN-поріг, створений в обхід `load_hub_config`, не вимикає критерій мовчки: `analyze` падає `ValueError`
    (помилка конфігу, не входу — тому не `GraphInputError`), а знімок порогів NaN не приймає."""
    base = _scenario_config(_expected("g_hub"))
    config = dataclasses.replace(base, thresholds=dataclasses.replace(base.thresholds, **{field: math.nan}))
    with pytest.raises(ValueError):
        GraphService(config).analyze(load_ingest_fixture("g_hub"))
    shares = dataclasses.asdict(_analyze("g_hub").metadata.thresholds)
    with pytest.raises(ValueError):
        ThresholdsSnapshot(**{**shares, field: math.nan})


def test_equal_shares_serialize_to_the_same_bytes_whether_int_or_float():
    """`1 == 1.0`, тож рівні результати мають давати однакові байти: частки завжди серіалізуються як float."""
    result = _analyze("g_basic")
    as_float = dataclasses.replace(result.metadata.thresholds, giant_component_warn_share=1.0,
                                   one_off_senders_share=1.0)
    as_int = dataclasses.replace(result.metadata.thresholds, giant_component_warn_share=1, one_off_senders_share=1)
    assert as_float == as_int
    floats = dataclasses.replace(result, metadata=dataclasses.replace(result.metadata, thresholds=as_float))
    ints = dataclasses.replace(result, metadata=dataclasses.replace(result.metadata, thresholds=as_int))
    assert to_json(floats) == to_json(ints)
    thresholds = json.loads(to_json(ints))["metadata"]["thresholds"]
    assert type(thresholds["giant_component_warn_share"]) is float
    assert type(thresholds["one_off_senders_share"]) is float


def _shuffled(ingest, seed):
    rng = random.Random(seed)

    def mixed(items):
        items = list(items)
        rng.shuffle(items)
        return tuple(items)

    shuffled = dataclasses.replace(
        ingest, buyers=mixed(ingest.buyers), transfers=mixed(ingest.transfers), unexpanded=mixed(ingest.unexpanded),
    )
    completeness = Completeness.derive(ingest.completeness.missing, ingest.completeness.buyers)
    object.__setattr__(completeness, "missing", mixed(ingest.completeness.missing))  # в обхід сортування 001
    object.__setattr__(shuffled, "completeness", completeness)
    return shuffled


@pytest.mark.parametrize("seed", range(5))
@pytest.mark.parametrize("name", ["g_basic", "g_hub", "g_unexpanded", "g_delegated", "g_dust", "g_incomplete"])
def test_to_json_identical_for_shuffled_input(name, seed):
    original = load_ingest_fixture(name)
    shuffled = _shuffled(original, seed)
    assert (shuffled.buyers, shuffled.transfers, shuffled.unexpanded) != \
        (original.buyers, original.transfers, original.unexpanded), "перестановка не змінила вхід"
    assert to_json(_analyze(name, ingest=shuffled)) == to_json(_analyze(name, ingest=original))


def test_missing_entries_in_reverse_order_do_not_change_the_json():
    base = load_ingest_fixture("g_incomplete")
    wallets = _expected("g_basic")["wallets"]
    entries = [MissingHistory(wallets["A"], 1, MissingReason.TIMEOUT, "t"),
               MissingHistory(wallets["P1"], 0, MissingReason.RATE_LIMITED, "r"),
               MissingHistory(wallets["P2"], 2, MissingReason.UNAVAILABLE, "")]
    texts = set()
    for order in (entries, entries[::-1], [entries[1], entries[2], entries[0]]):
        completeness = Completeness.derive(order, base.completeness.buyers)
        object.__setattr__(completeness, "missing", tuple(order))  # в обхід сортування 001
        texts.add(to_json(_analyze("g_incomplete", ingest=dataclasses.replace(base, completeness=completeness))))
    assert len(texts) == 1
    missing = json.loads(texts.pop())["completeness"]["missing"]
    assert [(m["depth"], m["wallet"]) for m in missing] == [(0, wallets["P1"]), (1, wallets["A"]), (2, wallets["P2"])]


_HASHSEED_SCRIPT = """
import hashlib, json, sys
from pathlib import Path
from unmask.graph.serialize import to_json
from unmask.graph.service import GraphService
from unmask.hubs.config import load_hub_config
from unmask.ingest.serialize import from_dict

root = Path(sys.argv[1])
config = load_hub_config(root / "config" / "hubs.yaml", root / "config" / "hub_addresses.yaml")
out = {}
for directory in sorted((root / "tests" / "fixtures" / "graph").iterdir()):
    ingest = from_dict(json.loads((directory / "ingest.json").read_text(encoding="utf-8")))
    out[directory.name] = hashlib.sha256(to_json(GraphService(config).analyze(ingest)).encode("utf-8")).hexdigest()
print(json.dumps(out))
"""
HASH_SEEDS = ("0", "1", "2", "3", "4")


def _run_with_hashseed(seed: str) -> dict:
    env = dict(os.environ, PYTHONHASHSEED=seed, PYTHONPATH=str(ROOT / "src"), PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run([sys.executable, "-c", _HASHSEED_SCRIPT, str(ROOT)], env=env, capture_output=True,
                          text=True, check=False, timeout=180, cwd=ROOT)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_to_json_does_not_depend_on_pythonhashseed():
    """Ролі — `frozenset` рядків: їхній порядок залежить від хешу; у JSON він не має просочуватись."""
    runs = {seed: _run_with_hashseed(seed) for seed in HASH_SEEDS}
    reference = runs[HASH_SEEDS[0]]
    assert set(reference) == set(SCENARIOS)
    for seed, run in runs.items():
        assert run == reference, f"to_json differs for PYTHONHASHSEED={seed}"
    config = load_hub_config(SHIPPED_HUBS, SHIPPED_LISTS)
    for name in SCENARIOS:
        text = to_json(GraphService(config).analyze(load_ingest_fixture(name)))
        assert hashlib.sha256(text.encode("utf-8")).hexdigest() == reference[name], name


# --- Версія схеми ----------------------------------------------------------------------------


def test_schema_version_is_002_1_and_independent_of_001_schema_id():
    doc = to_dict(_analyze("g_basic"))
    assert doc["metadata"]["schema_version"] == GRAPH_SCHEMA_VERSION == "002.1"
    assert SCHEMA["$defs"]["metadata"]["properties"]["schema_version"] == {"const": "002.1"}
    assert json.loads(to_json(_analyze("g_basic")))["metadata"]["schema_version"] == "002.1"

    # Контракти версіонуються окремо (FR-002-21): інший `$id`, інша версія, інша гілка шляху.
    assert SCHEMA["$id"] == "https://unmask.local/schemas/002/graph-result-1.json"
    assert SCHEMA_001["$id"] == "https://unmask.local/schemas/001/ingest-result-1.1.json"
    assert SCHEMA["$id"] != SCHEMA_001["$id"]
    assert "/002/" in SCHEMA["$id"] and "/001/" in SCHEMA_001["$id"]
    for version in ("1.0", "1.1", "002.0", "002.2", "002.1 ", ""):
        forged = copy.deepcopy(doc)
        forged["metadata"]["schema_version"] = version
        _assert_invalid_at(forged, "metadata/schema_version")

    # Документ 001 не є результатом 002 і навпаки.
    assert _errors({"metadata": {"schema_version": "1.1"}}) != []


# --- FR-002-22: пиловий запис ----------------------------------------------------------------


def test_g_dust_result_serializes_dust_record_with_integer_median_and_threshold():
    result = _analyze("g_dust")
    doc, text = to_dict(result), to_json(result)
    assert [r["address"] for r in doc["pruned"]] == [r.address for r in result.pruned]
    assert len(doc["pruned"]) == 1
    record = doc["pruned"][0]
    hit = record["criteria"][0]
    assert record["criteria"] == [{"criterion": "dust_fanout", "measured": 500_000, "threshold": 1_000_000,
                                   "detail": "measured", "lists_version": None}]
    assert type(hit["measured"]) is int and type(hit["threshold"]) is int
    assert record["measures"]["buyer_fanout"] == 19 and record["measures"]["median_to_buyers"] == 500_000
    assert type(record["measures"]["buyer_fanout"]) is int and type(record["measures"]["median_to_buyers"]) is int
    thresholds = doc["metadata"]["thresholds"]
    assert (thresholds["dust_amount_lamports"], thresholds["dust_min_fanout"]) == (1_000_000, 5)
    assert type(thresholds["dust_amount_lamports"]) is int and type(thresholds["dust_min_fanout"]) is int
    # У тексті ті самі числа без десяткової частини.
    assert '"measured":500000,"threshold":1000000}' in text
    assert '"median_to_buyers":500000' in text and "500000.0" not in text and "1000000.0" not in text
    # Вершини без SOL-роздавання несуть `null`-медіану й нуль fan-out.
    assert any(n["measures"] == {**n["measures"], "buyer_fanout": 0, "median_to_buyers": None}
               for n in doc["graph"]["nodes"])


# --- Невідомі типи й ізоляція ---------------------------------------------------------------


@pytest.mark.parametrize("bad", [None, {}, "result", 42, Rejection(RejectKind.TOKEN_NOT_FOUND, "m", "d")])
def test_to_dict_and_to_json_reject_non_results_with_type_error(bad):
    with pytest.raises(TypeError):
        to_dict(bad)
    with pytest.raises(TypeError):
        to_json(bad)


@pytest.mark.parametrize("field, value", [
    ("report", None), ("report", SimpleNamespace(before=None)), ("pruned", ("not a record",)),
    ("buyer_flags", (42,)),
])
def test_to_dict_rejects_foreign_objects_inside_a_result_with_type_error(field, value):
    result = _analyze("g_hub")
    broken = dataclasses.replace(result)
    object.__setattr__(broken, field, value)  # в обхід інваріантів `GraphResult`
    with pytest.raises(TypeError):
        to_dict(broken)


def test_to_dict_rejects_a_criterion_that_is_not_a_hub_criterion():
    result = _analyze("g_hub")
    hit = result.pruned[0].criteria[0]
    object.__setattr__(hit, "criterion", str(hit.criterion))  # звичайний рядок, не `HubCriterion`
    with pytest.raises(TypeError):
        to_dict(result)


def test_to_dict_returns_a_fresh_structure_independent_of_the_model():
    result = _analyze("g_hub")
    first = to_dict(result)
    first["graph"]["nodes"].clear()
    first["pruned"][0]["criteria"].clear()
    first["metadata"]["thresholds"]["degree_threshold"] = -1
    second = to_dict(result)
    assert second is not first and second["graph"]["nodes"] and second["pruned"][0]["criteria"]
    assert second["metadata"]["thresholds"]["degree_threshold"] != -1
    assert _errors(second) == []


class _FancyInt(int):
    pass


class _FancyStr(str):
    pass


@pytest.mark.parametrize("label, corrupt", [
    ("int: bool", lambda r: object.__setattr__(r.metadata, "wallets_analyzed", True)),
    ("int: str", lambda r: object.__setattr__(r.metadata, "wallets_analyzed", "3")),
    ("float: bool", lambda r: object.__setattr__(r.metadata.thresholds, "giant_component_warn_share", True)),
    ("float: str", lambda r: object.__setattr__(r.metadata.thresholds, "giant_component_warn_share", "x")),
    ("bool: int", lambda r: object.__setattr__(r.metadata, "lists_applied", 1)),
    ("str: int", lambda r: object.__setattr__(r.metadata, "mint", 5)),
    ("collection: str", lambda r: object.__setattr__(r, "pruned", "abc")),
    ("collection: empty str", lambda r: object.__setattr__(r, "pruned", "")),  # `tuple("") == ()` без перевірки
    ("collection: empty bytes", lambda r: object.__setattr__(r, "buyer_flags", b"")),
    ("collection: int", lambda r: object.__setattr__(r, "buyer_flags", 42)),
    ("warning: bare str", lambda r: object.__setattr__(r.report, "warnings", ("giant_component",))),
    ("node: not a Node", lambda r: object.__setattr__(r.graph, "nodes", ("x",))),
    ("edge: not an Edge", lambda r: object.__setattr__(r.graph, "edges", ("x",))),
    ("incident edge: not an Edge", lambda r: object.__setattr__(r.pruned[0], "incident_edges", ("x",))),
    ("measures: not NodeMeasures", lambda r: object.__setattr__(r.graph.nodes[0], "measures", object())),
    ("unexpanded: not a mark", lambda r: object.__setattr__(r.graph.nodes[0], "unexpanded", object())),
    ("thresholds: not a snapshot", lambda r: object.__setattr__(r.metadata, "thresholds", object())),
])
def test_to_dict_rejects_wrong_typed_values_inside_a_result_with_type_error(label, corrupt):
    result = _analyze("g_hub")
    assert _errors(to_dict(result)) == []
    corrupt(result)
    with pytest.raises(TypeError):
        to_dict(result)


def test_to_dict_emits_exact_json_types_for_int_and_str_subclasses():
    """`int`/`str`-підкласи (`IntEnum`, `StrEnum`, `Asset`) не просочуються у вихід: типи точні."""
    result = _analyze("g_hub")
    object.__setattr__(result.metadata, "ingest_analyzed_at", _FancyInt(result.metadata.ingest_analyzed_at))
    object.__setattr__(result.metadata, "mint", _FancyStr(result.metadata.mint))
    object.__setattr__(result.graph.nodes[0].measures, "degree", _FancyInt(result.graph.nodes[0].measures.degree))
    doc = to_dict(result)
    _assert_json_types_only(doc)
    assert type(doc["metadata"]["ingest_analyzed_at"]) is int and type(doc["metadata"]["mint"]) is str
    assert type(doc["graph"]["nodes"][0]["measures"]["degree"]) is int


def test_several_flags_and_warnings_keep_the_model_order():
    # Низький поріг ступеня: у g_dust кілька покупців стають позначками (за рангом, не за порядком хешів чи адрес).
    expected = _expected("g_dust")
    base = _scenario_config(expected)
    config = dataclasses.replace(base, thresholds=dataclasses.replace(base.thresholds, degree_threshold=1))
    result = GraphService(config).analyze(load_ingest_fixture("g_dust"))
    doc = to_dict(result)
    assert _errors(doc) == []
    ranks = [f["buyer_rank"] for f in doc["buyer_flags"]]
    assert len(ranks) >= 2 and ranks == sorted(set(ranks))
    by_rank = {n["buyer_rank"]: n["address"] for n in doc["graph"]["nodes"] if n["buyer_rank"] is not None}
    assert [f["address"] for f in doc["buyer_flags"]] == [by_rank[r] for r in ranks]
    for flag in doc["buyer_flags"]:
        keys = [(h["criterion"], h["detail"]) for h in flag["criteria"]]
        assert keys == sorted(keys) and keys

    # Два попередження одночасно — за алфавітом значень.
    both = to_dict(_analyze("g_buyer_hub", no_lists=True))
    assert _errors(both) == []
    assert both["report"]["warnings"] == ["address_lists_not_applied", "giant_component"]


def test_int_thresholds_serialize_as_float_in_hits_snapshot_and_report():
    """`one_off_senders_share=0` (int з конфігу) дає int-поріг у хіті; `giant_component_warn_share=1` — int `warn_share`.
    Рівні значення мають мати однакові байти: усе — `float`."""
    base = _scenario_config(_expected("g_hub"))
    config = dataclasses.replace(base, thresholds=dataclasses.replace(
        base.thresholds, one_off_senders_share=0, giant_component_warn_share=1))
    result = GraphService(config).analyze(load_ingest_fixture("g_hub"))
    hit = result.pruned[0].criteria[0]
    assert hit.criterion.value == "one_off_senders" and type(hit.threshold) is int  # саме int у моделі
    doc, text = to_dict(result), to_json(result)
    assert _errors(doc) == []
    out = doc["pruned"][0]["criteria"][0]
    assert out["threshold"] == 0 and type(out["threshold"]) is float and type(out["measured"]) is float
    assert type(doc["report"]["warn_share"]) is float and doc["report"]["warn_share"] == 1.0
    assert type(doc["metadata"]["thresholds"]["one_off_senders_share"]) is float
    assert '"threshold":0.0' in text and '"warn_share":1.0' in text


@pytest.mark.parametrize("owner, attribute", [
    ("record", "lists_version"), ("record", "config_version"), ("record", "measures"),
    ("hit", "measured"), ("hit", "threshold"), ("hit", "lists_version"), ("hit", "detail"),
    ("flag", "buyer_rank"), ("report", "warn_share"), ("report", "pruned_edges"),
])
def test_absent_attribute_of_a_hubs_object_is_a_type_error_even_when_none_would_be_allowed(owner, attribute):
    """Відсутнє поле ≠ `None`: опційні поля (`lists_version`, `measured`, `threshold`) не мовчать, а падають `TypeError`."""
    result = _analyze("g_buyer_hub")
    flag = result.buyer_flags[0]
    hit_source = flag.criteria[0]

    def without(obj, name):
        return SimpleNamespace(**{k: getattr(obj, k) for k in vars(obj) if k != name})

    if owner == "record":
        record = _analyze("g_hub").pruned[0]
        result = _analyze("g_hub")
        object.__setattr__(result, "pruned", (without(record, attribute),))
    elif owner == "hit":
        stripped = without(hit_source, attribute)
        object.__setattr__(result, "buyer_flags", (SimpleNamespace(
            address=flag.address, buyer_rank=flag.buyer_rank, criteria=(stripped,), measures=flag.measures),))
    elif owner == "flag":
        object.__setattr__(result, "buyer_flags", (without(flag, attribute),))
    else:
        object.__setattr__(result, "report", without(result.report, attribute))
    with pytest.raises(TypeError):
        to_dict(result)
