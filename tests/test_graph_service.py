# verifies: FR-002-13, FR-002-20, FR-002-04, FR-002-19
"""Публічний сервіс фічі 002 і версії в метаданих (T-039; FR-002-13, FR-002-20, FR-002-04, FR-002-19;
contracts/graph-service.md §7; data-model «GraphMetadata», «GraphResult»; research R-14, R-16, R-22).

Критична задача: помилка тут не ламає збірку, а тихо робить два результати «порівнянними», хоча їх отримано з різних
версій порогів чи списків (принцип III), або ховає неповноту (принцип V). Тому, крім щасливого шляху:

- golden: знімок порогів і версії звіряються з ЛІТЕРАЛАМИ зафіксованого `config/hubs.yaml` v2 і
  `config/hub_addresses.yaml` v1, а не лише з тим самим обʼєктом конфігу;
- незалежні еталони: метадані — з `ingest.json` і розділу `config` у `expected.json` (оракул генератора), повнота,
  звіт і відсікання — з `expected.json` усіх 12 сценаріїв поле за полем;
- парність `lists_applied` ⇔ `address_lists_not_applied` і `delegated_complete` ⇔ немає `delegated_incomplete` — в
  обидва боки, на рівні типу `GraphResult` (не лише сервісу);
- вхід, що суперечить контракту 001 (позначка `high_degree` на порозі, `signature_cap` понад поріг, глибина поза
  `funding_depth`), — `GraphInputError`, а не голий `ValueError` і не тихий результат;
- межі модулів: `ast` імпортів усіх файлів `graph` і `hubs`; жодного годинника, файлів чи мережі в `analyze`.
"""

import ast
import builtins
import dataclasses
import json
import socket
import time
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

from conftest import GRAPH_FIXTURES, load_ingest_fixture
from unmask.graph import service as service_module
from unmask.graph.model import (
    GRAPH_SCHEMA_VERSION,
    FundingGraph,
    GraphCompleteness,
    GraphCompletenessStatus,
    GraphInputError,
    GraphMetadata,
    GraphResult,
    GraphWarning,
    HubCriterion,
    MissingRef,
    NodeRole,
    ThresholdsSnapshot,
)
from unmask.graph.service import GraphService
from unmask.hubs.config import ADDRESS_CATEGORIES, AddressLists, HubConfig, HubThresholds, load_hub_config
from unmask.ingest.model import (
    Completeness,
    DelegatedAnalysis,
    IngestResult,
    MissingHistory,
    MissingReason,
    RejectKind,
    Rejection,
    UnexpandedReason,
)

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "unmask"
SHIPPED_HUBS = ROOT / "config" / "hubs.yaml"
SHIPPED_LISTS = ROOT / "config" / "hub_addresses.yaml"

# Зафіксований набір із 12 сценаріїв: нові сценарії додаються сюди свідомо.
SCENARIOS = ["g_all_hubs", "g_basic", "g_buyer_hub", "g_delegated", "g_dust", "g_dust_mixed", "g_empty",
             "g_financier", "g_hub", "g_incomplete", "g_known", "g_unexpanded"]

NOT_APPLIED, DELEGATED_INCOMPLETE = GraphWarning.ADDRESS_LISTS_NOT_APPLIED, GraphWarning.DELEGATED_INCOMPLETE

# Golden: зафіксовані значення `config/hubs.yaml` v2 (принцип III; запис `## 2` у config/CHANGELOG.md).
SHIPPED_HUBS_VERSION = 2
SHIPPED_LISTS_VERSION = 1
SHIPPED_SNAPSHOT = {
    "degree_threshold": 100, "one_off_senders_share": 0.8, "one_off_min_senders": 10,
    "giant_component_warn_share": 0.5, "prune_off_curve": True, "prune_ingest_high_degree": True,
    "dust_amount_lamports": 1_000_000, "dust_min_fanout": 5,
}


# --- Конфіг і сценарії -------------------------------------------------------------------


def _lists(version: int = 1, **categories) -> AddressLists:
    parsed = {name: tuple(categories.get(name, ())) for name in ADDRESS_CATEGORIES}
    index = {address: name for name in ADDRESS_CATEGORIES for address in parsed[name]}
    return AddressLists(version=version, categories=MappingProxyType(parsed), index=MappingProxyType(index))


def _config(*, lists: AddressLists | None = None, **changes) -> HubConfig:
    kw = dict(version=2, **SHIPPED_SNAPSHOT)
    kw.update(changes)
    return HubConfig(thresholds=HubThresholds(**kw), lists=lists, thresholds_digest="0" * 64,
                     lists_digest=None if lists is None else "1" * 64)


def _expected(name: str) -> dict:
    return json.loads((Path(GRAPH_FIXTURES) / name / "expected.json").read_text(encoding="utf-8"))


def _ingest_doc(name: str) -> dict:
    doc = json.loads((Path(GRAPH_FIXTURES) / name / "ingest.json").read_text(encoding="utf-8"))
    return doc.get("result", doc)


def _scenario_config(expected: dict) -> HubConfig:
    lists = expected["config"]["lists"]
    return _config(lists=_lists(lists["version"], **lists["categories"]) if lists is not None else None,
                   version=expected["config"]["version"], **expected["config"]["thresholds"])


def _shipped() -> HubConfig:
    return load_hub_config(SHIPPED_HUBS, SHIPPED_LISTS)


def _analyze(name: str, config: HubConfig | None = None) -> GraphResult:
    config = config if config is not None else _scenario_config(_expected(name))
    return GraphService(config).analyze(load_ingest_fixture(name))


# --- Відображення у форму expected.json (поле за полем) -------------------------------------


def _completeness_dict(gc: GraphCompleteness) -> dict:
    return {
        "status": gc.status.value, "ingest_status": gc.ingest_status.value,
        "missing": [{"wallet": m.wallet, "depth": m.depth, "reason": m.reason.value, "detail": m.detail}
                    for m in gc.missing],
        "buyers_complete": gc.buyers_complete, "buyers_reason": gc.buyers_reason,
        "delegated_complete": gc.delegated_complete, "delegated_reason": gc.delegated_reason,
    }


def _snap_dict(s) -> dict:
    return {"nodes": s.nodes, "edges": s.edges, "components": s.components, "buyers_total": s.buyers_total,
            "buyers_in_largest_component": s.buyers_in_largest_component,
            "largest_component_buyer_share": s.largest_component_buyer_share,
            "isolated_buyers": s.isolated_buyers}


def _report_dict(r) -> dict:
    return {"before": _snap_dict(r.before), "after": _snap_dict(r.after), "pruned_nodes": r.pruned_nodes,
            "pruned_edges": r.pruned_edges, "warn_share": r.warn_share,
            "warnings": [w.value for w in r.warnings]}


def _hit_dict(h) -> dict:
    return {"criterion": h.criterion.value, "measured": h.measured, "threshold": h.threshold, "detail": h.detail,
            "lists_version": h.lists_version}


def _edge_key(e) -> list:
    return [e.kind.value, e.sender, e.receiver, e.asset or ""]


def _measures_dict(m) -> dict:
    return {"degree": m.degree, "unique_senders": m.unique_senders, "one_off_senders": m.one_off_senders,
            "one_off_share": m.one_off_share, "buyer_fanout": m.buyer_fanout,
            "median_to_buyers": m.median_to_buyers}


def _record_view(r) -> dict:
    return {"address": r.address, "criteria": [_hit_dict(h) for h in r.criteria],
            "incident_edges": [_edge_key(e) for e in r.incident_edges], "measures": _measures_dict(r.measures),
            "config_version": r.config_version, "lists_version": r.lists_version}


def _expected_record_view(r: dict) -> dict:
    return {"address": r["address"], "criteria": r["criteria"],
            "incident_edges": [[e["kind"], e["sender"], e["receiver"], e["asset"] or ""] for e in r["incident_edges"]],
            "measures": r["measures"], "config_version": r["config_version"], "lists_version": r["lists_version"]}


def _flag_view(f) -> dict:
    return {"address": f.address, "buyer_rank": f.buyer_rank, "criteria": [_hit_dict(h) for h in f.criteria],
            "measures": _measures_dict(f.measures)}


def _expected_metadata(name: str, expected: dict) -> dict:
    """Метадані з НЕЗАЛЕЖНИХ джерел: `ingest.json` (вхід) і `expected.json` (конфіг і повний граф оракула)."""
    meta = _ingest_doc(name)["metadata"]
    lists = expected["config"]["lists"]
    return {
        "mint": meta["mint"], "schema_version": "002.1", "ingest_analyzed_at": meta["analyzed_at"],
        "ingest_config_version": meta["config_version"], "ingest_source": meta["source"],
        "wallets_analyzed": meta["wallets_analyzed"], "hub_config_version": expected["config"]["version"],
        "address_lists_version": None if lists is None else lists["version"], "lists_applied": lists is not None,
        "thresholds": expected["config"]["thresholds"],
        "nodes_total": len(expected["graph"]["nodes"]), "edges_total": len(expected["graph"]["edges"]),
    }


def _metadata_dict(m: GraphMetadata) -> dict:
    return {
        "mint": m.mint, "schema_version": m.schema_version, "ingest_analyzed_at": m.ingest_analyzed_at,
        "ingest_config_version": m.ingest_config_version, "ingest_source": m.ingest_source,
        "wallets_analyzed": m.wallets_analyzed, "hub_config_version": m.hub_config_version,
        "address_lists_version": m.address_lists_version, "lists_applied": m.lists_applied,
        "thresholds": {f.name: getattr(m.thresholds, f.name) for f in dataclasses.fields(m.thresholds)},
        "nodes_total": m.nodes_total, "edges_total": m.edges_total,
    }


# --- FR-002-13: версії й знімок порогів у метаданих (принцип III) -------------------------


def test_metadata_carries_both_versions_and_threshold_snapshot():
    config = _shipped()
    result = GraphService(config).analyze(load_ingest_fixture("g_basic"))
    md = result.metadata
    # Обидві версії — з того конфігу, з яким прогнано, і дорівнюють зафіксованим файлам (golden).
    assert md.hub_config_version == config.thresholds.version == SHIPPED_HUBS_VERSION
    assert md.address_lists_version == config.lists.version == SHIPPED_LISTS_VERSION
    assert md.lists_applied is True
    assert md.schema_version == GRAPH_SCHEMA_VERSION == "002.1"
    # Знімок — рівно вісім полів HubThresholds без `version`, кожне дорівнює значенню конфігу і golden-літералу.
    snapshot_fields = [f.name for f in dataclasses.fields(ThresholdsSnapshot)]
    threshold_fields = [f.name for f in dataclasses.fields(HubThresholds) if f.name != "version"]
    assert sorted(snapshot_fields) == sorted(threshold_fields) == sorted(SHIPPED_SNAPSHOT)
    assert len(snapshot_fields) == 8
    assert isinstance(md.thresholds, ThresholdsSnapshot)
    for name in snapshot_fields:
        value = getattr(md.thresholds, name)
        assert value == getattr(config.thresholds, name) == SHIPPED_SNAPSHOT[name], name
        assert type(value) is type(SHIPPED_SNAPSHOT[name]), name
    # Пилові пороги (R-22) — у знімку, тож два прогони з різним пилом видно з метаданих без читання коду.
    assert (md.thresholds.dust_amount_lamports, md.thresholds.dust_min_fanout) == (1_000_000, 5)
    # Поля 001 — з метаданих результату збору (не з годинника й не з config/ingest.yaml).
    meta = _ingest_doc("g_basic")["metadata"]
    assert (md.mint, md.ingest_analyzed_at, md.ingest_config_version, md.ingest_source, md.wallets_analyzed) == (
        meta["mint"], meta["analyzed_at"], meta["config_version"], meta["source"], meta["wallets_analyzed"])
    # nodes_total/edges_total — повний граф ДО відсікання (оракул генератора).
    exp = _expected("g_basic")
    assert (md.nodes_total, md.edges_total) == (len(exp["graph"]["nodes"]), len(exp["graph"]["edges"]))


def test_snapshot_follows_each_threshold_independently():
    """Кожне з восьми полів знімка береться зі «свого» поля конфігу (а не з сусіднього чи константи)."""
    base = load_ingest_fixture("g_basic")
    changed = dict(degree_threshold=101, one_off_senders_share=0.75, one_off_min_senders=11,
                   giant_component_warn_share=0.55, prune_off_curve=False, prune_ingest_high_degree=False,
                   dust_amount_lamports=999_999, dust_min_fanout=6)
    assert set(changed) == set(SHIPPED_SNAPSHOT)
    for name, value in changed.items():
        md = GraphService(_config(lists=_lists(), **{name: value})).analyze(base).metadata
        for other in SHIPPED_SNAPSHOT:
            want = value if other == name else SHIPPED_SNAPSHOT[other]
            assert getattr(md.thresholds, other) == want, (name, other)


def test_two_configs_differing_only_in_version_give_same_graph_but_different_metadata():
    base = load_ingest_fixture("g_hub")  # є запис відсікання — версія потрапляє і в записи
    lists = _lists(1)
    r2 = GraphService(_config(lists=lists, version=2)).analyze(base)
    r7 = GraphService(_config(lists=lists, version=7)).analyze(base)
    assert r2.pruned, "сценарій має відсікати хоч одну вершину"
    # Той самий граф, позначки, звіт і повнота — висновок не залежить від номера версії…
    assert r7.graph == r2.graph
    assert r7.buyer_flags == r2.buyer_flags
    assert r7.report == r2.report
    assert r7.completeness == r2.completeness
    assert [dataclasses.replace(r, config_version=2) for r in r7.pruned] == list(r2.pruned)
    # …але результати не виглядають однаковими: метадані відрізняються рівно версією порогів.
    assert r7.metadata != r2.metadata
    assert (r2.metadata.hub_config_version, r7.metadata.hub_config_version) == (2, 7)
    assert dataclasses.replace(r7.metadata, hub_config_version=2) == r2.metadata
    assert {r.config_version for r in r7.pruned} == {7}
    # Те саме для версії списків (FR-002-13: дві версії, а не одна).
    l1 = GraphService(_config(lists=_lists(1))).analyze(base)
    l4 = GraphService(_config(lists=_lists(4))).analyze(base)
    assert (l4.graph, l4.report) == (l1.graph, l1.report)
    assert (l1.metadata.address_lists_version, l4.metadata.address_lists_version) == (1, 4)
    assert dataclasses.replace(l4.metadata, address_lists_version=1) == l1.metadata
    assert {r.lists_version for r in l4.pruned} == {4}


# --- FR-002-12/13: списки недоступні — видно в метаданих і попередженні ------------------------


def test_lists_none_gives_lists_applied_false_and_null_version():
    config = load_hub_config(SHIPPED_HUBS, None)
    assert config.lists is None
    for name in SCENARIOS:
        result = GraphService(config).analyze(load_ingest_fixture(name))
        md = result.metadata
        assert md.lists_applied is False, name
        assert md.address_lists_version is None, name
        assert md.hub_config_version == SHIPPED_HUBS_VERSION, name
        assert NOT_APPLIED in result.report.warnings, name
        assert all(not h.detail.startswith("list:") for r in result.pruned for h in r.criteria), name


def test_lists_present_gives_lists_applied_true_and_no_not_applied_warning():
    config = _shipped()
    for name in SCENARIOS:
        result = GraphService(config).analyze(load_ingest_fixture(name))
        assert result.metadata.lists_applied is True, name
        assert result.metadata.address_lists_version == SHIPPED_LISTS_VERSION, name
        assert NOT_APPLIED not in result.report.warnings, name


def test_lists_applied_is_taken_from_prune_outcome_not_recomputed(monkeypatch):
    """Сервіс передає у звіт `outcome.lists_applied` — те саме значення, що потрапляє в метадані."""
    seen = {}
    original = service_module.effect_report

    def spy(before, after, config, *, lists_applied, delegated_complete):
        seen["lists_applied"] = lists_applied
        seen["delegated_complete"] = delegated_complete
        return original(before, after, config, lists_applied=lists_applied, delegated_complete=delegated_complete)

    monkeypatch.setattr(service_module, "effect_report", spy)
    result = GraphService(_config(lists=None)).analyze(load_ingest_fixture("g_basic"))
    assert seen == {"lists_applied": False, "delegated_complete": True}
    assert result.metadata.lists_applied is False
    result = GraphService(_config(lists=_lists())).analyze(load_ingest_fixture("g_basic"))
    assert seen["lists_applied"] is True and result.metadata.lists_applied is True


# --- GraphResult: інваріанти типу (обидва напрямки парностей) --------------------------------


def _valid(name: str = "g_basic", lists: bool = True) -> GraphResult:
    return GraphService(_config(lists=_lists() if lists else None)).analyze(load_ingest_fixture(name))


def test_graph_result_lists_parity_holds_in_both_directions():
    with_lists = _valid(lists=True)
    without = _valid(lists=False)
    assert NOT_APPLIED not in with_lists.report.warnings and NOT_APPLIED in without.report.warnings
    # lists_applied=False без попередження — неможливо.
    stripped = dataclasses.replace(without.report, warnings=tuple(w for w in without.report.warnings
                                                                  if w is not NOT_APPLIED))
    with pytest.raises(ValueError, match="address_lists_not_applied"):
        dataclasses.replace(without, report=stripped)
    # lists_applied=True з попередженням «не застосовано» — теж неможливо (зворотний напрямок).
    added = dataclasses.replace(with_lists.report,
                                warnings=tuple(sorted((*with_lists.report.warnings, NOT_APPLIED), key=str)))
    with pytest.raises(ValueError, match="address_lists_not_applied"):
        dataclasses.replace(with_lists, report=added)


def test_graph_result_delegated_parity_holds_in_both_directions():
    ok = _valid()
    assert ok.completeness.delegated_complete is True and DELEGATED_INCOMPLETE not in ok.report.warnings
    added = dataclasses.replace(ok.report, warnings=tuple(sorted((*ok.report.warnings, DELEGATED_INCOMPLETE),
                                                                 key=str)))
    with pytest.raises(ValueError, match="delegated_incomplete"):
        dataclasses.replace(ok, report=added)
    # Над NOT_ANALYZED: повнота неповна, попередження є; без нього — неможливо.
    blind = GraphService(_config(lists=_lists())).analyze(
        dataclasses.replace(load_ingest_fixture("g_basic"), delegated=DelegatedAnalysis.NOT_ANALYZED))
    assert blind.completeness.delegated_complete is False
    assert blind.completeness.status is GraphCompletenessStatus.INCOMPLETE
    assert DELEGATED_INCOMPLETE in blind.report.warnings
    stripped = dataclasses.replace(blind.report, warnings=tuple(w for w in blind.report.warnings
                                                                if w is not DELEGATED_INCOMPLETE))
    with pytest.raises(ValueError, match="delegated_incomplete"):
        dataclasses.replace(blind, report=stripped)


def test_graph_result_report_counts_must_match_metadata_and_graph():
    r = _valid("g_hub")
    before, after = r.report.before, r.report.after
    assert before.buyers_total == after.buyers_total == r.metadata.wallets_analyzed
    assert (before.nodes, before.edges) == (r.metadata.nodes_total, r.metadata.edges_total)
    assert (after.nodes, after.edges) == (len(r.graph.nodes), len(r.graph.edges))
    # Звіт, що рахує інших покупців, ніж метадані (buyers_total != wallets_analyzed), — гучно. `EffectSnapshot` сам
    # не дає змінити лише `buyers_total` (частка перевіряється), тож звіт — копія полями (GraphResult бачить поля).
    for side in ("before", "after"):
        snap = getattr(r.report, side)
        wrong = SimpleNamespace(**{f.name: getattr(snap, f.name) for f in dataclasses.fields(snap)})
        wrong.buyers_total += 1
        fake = SimpleNamespace(**{f.name: getattr(r.report, f.name) for f in dataclasses.fields(r.report)})
        setattr(fake, side, wrong)
        with pytest.raises(ValueError, match="buyers_total"):
            dataclasses.replace(r, report=fake)
    # Кожне число звіту, що не відповідає метаданим чи графу результату, — гучно, окремо (інші поля узгоджені).
    for side, field, pattern in (("before", "nodes", "report.before"), ("before", "edges", "report.before"),
                                 ("after", "nodes", "report.after"), ("after", "edges", "report.after")):
        snap = getattr(r.report, side)
        wrong = SimpleNamespace(**{f.name: getattr(snap, f.name) for f in dataclasses.fields(snap)})
        setattr(wrong, field, getattr(wrong, field) + 1)
        fake = SimpleNamespace(**{f.name: getattr(r.report, f.name) for f in dataclasses.fields(r.report)})
        setattr(fake, side, wrong)
        with pytest.raises(ValueError, match=pattern):
            dataclasses.replace(r, report=fake)
    fake = SimpleNamespace(**{f.name: getattr(r.report, f.name) for f in dataclasses.fields(r.report)})
    fake.pruned_nodes += 1
    with pytest.raises(ValueError, match="pruned_nodes"):
        dataclasses.replace(r, report=fake)
    # Звіт «до» не про повний граф метаданих — гучно.
    with pytest.raises(ValueError, match="nodes_total|edges_total"):
        dataclasses.replace(r, metadata=dataclasses.replace(r.metadata, edges_total=r.metadata.edges_total + 1))
    # Звіт «після» не про граф результату — гучно.
    smaller = r.graph.without({next(n.address for n in r.graph.nodes if NodeRole.BUYER not in n.roles)})
    with pytest.raises(ValueError):
        dataclasses.replace(r, graph=smaller,
                            metadata=dataclasses.replace(r.metadata, nodes_total=r.metadata.nodes_total - 1))


def test_graph_result_pruned_records_must_carry_metadata_versions_and_all_removed_edges():
    r = _valid("g_hub")
    assert r.pruned
    record = r.pruned[0]
    with pytest.raises(ValueError, match="version"):
        dataclasses.replace(r, pruned=(dataclasses.replace(record, config_version=record.config_version + 1),
                                       *r.pruned[1:]))
    with pytest.raises(ValueError, match="version"):
        dataclasses.replace(r, pruned=(dataclasses.replace(record, lists_version=record.lists_version + 1),
                                       *r.pruned[1:]))
    # `graph ∪ pruned` відтворює всі ребра повного графа: запис без частини інцидентних ребер — гучно.
    assert len(record.incident_edges) >= 2
    with pytest.raises(ValueError, match="edges"):
        dataclasses.replace(r, pruned=(dataclasses.replace(record, incident_edges=record.incident_edges[1:]),
                                       *r.pruned[1:]))


def test_graph_result_buyer_flags_must_point_at_buyers_in_graph():
    r = _valid("g_buyer_hub")
    assert r.buyer_flags
    flag = r.buyer_flags[0]
    non_buyer = next(n for n in r.graph.nodes if NodeRole.BUYER not in n.roles)
    with pytest.raises(ValueError, match="buyer_flags"):
        dataclasses.replace(r, buyer_flags=(dataclasses.replace(flag, address=non_buyer.address),
                                            *r.buyer_flags[1:]))
    # Покупець той самий, але ранг чужий — теж гучно («з тим самим рангом»).
    with pytest.raises(ValueError, match="buyer_flags"):
        dataclasses.replace(r, buyer_flags=(dataclasses.replace(flag, buyer_rank=flag.buyer_rank + 1000),
                                            *r.buyer_flags[1:]))


def _fake_report(report, **snapshot_deltas):
    """Копія звіту полями (SimpleNamespace) зі зсувом чисел: GraphResult бачить лише поля."""
    fake = SimpleNamespace(**{f.name: getattr(report, f.name) for f in dataclasses.fields(report)})
    for side in ("before", "after"):
        snap = getattr(report, side)
        setattr(fake, side, SimpleNamespace(**{f.name: getattr(snap, f.name) for f in dataclasses.fields(snap)}))
    for path, delta in snapshot_deltas.items():
        target, field = (fake, path) if "__" not in path else (getattr(fake, path.split("__")[0]), path.split("__")[1])
        setattr(target, field, getattr(target, field) + delta)
    return fake


def _fake_record(record, incident_edges) -> SimpleNamespace:
    """Копія запису полями: `PruneRecord` сам відкидає чуже/невпорядковане ребро, а GraphResult (що не імпортує
    `hubs`) має стерегти це й для будь-якого обʼєкта з полями запису."""
    fake = SimpleNamespace(**{f.name: getattr(record, f.name) for f in dataclasses.fields(record)})
    fake.incident_edges = tuple(incident_edges)
    return fake


def _other_pruned(r: GraphResult, index: int = 0) -> tuple:
    return r.pruned[:index] + r.pruned[index + 1:]


def test_graph_result_rejects_duplicate_pruned_address():
    r = _valid("g_hub")
    record = r.pruned[0]
    # Дубль запису з узгодженими лічильниками (nodes_total, before.nodes, pruned_nodes +1; ребра ті самі):
    # ловить лише перевірка дублікату адреси.
    md = dataclasses.replace(r.metadata, nodes_total=r.metadata.nodes_total + 1)
    report = _fake_report(r.report, before__nodes=1, pruned_nodes=1)
    with pytest.raises(ValueError, match="duplicate address"):
        dataclasses.replace(r, metadata=md, report=report, pruned=(record, *r.pruned))


def _free_pair(r: GraphResult) -> tuple[str, str]:
    """Дві вершини графа результату без ребра `transfer`/sol між ними (ключ нового ребра не зайнятий)."""
    taken = {e.key for e in r.graph.edges} | {e.key for p in r.pruned for e in p.incident_edges}
    nodes = [n.address for n in r.graph.nodes]
    for a in nodes:
        for b in nodes:
            if a != b and (r.graph.edges[0].kind, a, b, r.pruned[0].incident_edges[0].asset) not in taken:
                return a, b
    raise AssertionError("no free pair")


def test_graph_result_rejects_foreign_edge_in_pruned_record():
    r = _valid("g_hub")
    record = r.pruned[0]
    edge = record.incident_edges[0]
    a, b = _free_pair(r)
    foreign = dataclasses.replace(edge, sender=a, receiver=b)  # обидва кінці — вершини графа, H не кінець
    # Заміна «одне на одне»: кількість ребер і кінці узгоджені — ловить лише перевірка «чужого» ребра.
    bad = _fake_record(record, (foreign, *record.incident_edges[1:]))
    with pytest.raises(ValueError, match="foreign edge"):
        dataclasses.replace(r, pruned=(bad, *_other_pruned(r)))


def test_graph_result_rejects_pruned_edge_with_unknown_endpoint():
    r = _valid("g_hub")
    record = r.pruned[0]
    edge = next(e for e in record.incident_edges if e.receiver == record.address)
    stranger = _expected("g_hub")["mint"]  # валідна адреса, не вершина і не відсічена
    assert stranger not in {n.address for n in r.graph.nodes} | {p.address for p in r.pruned}
    bad = _fake_record(record, (dataclasses.replace(e, sender=stranger) if e is edge else e
                                for e in record.incident_edges))
    with pytest.raises(ValueError, match="neither a node nor pruned"):
        dataclasses.replace(r, pruned=(bad, *_other_pruned(r)))


def test_graph_result_rejects_non_edge_in_pruned_record():
    r = _valid("g_hub")
    record = r.pruned[0]
    edge = record.incident_edges[0]
    lookalike = SimpleNamespace(**{f.name: getattr(edge, f.name) for f in dataclasses.fields(edge)}, key=edge.key)
    bad = _fake_record(record, (lookalike, *record.incident_edges[1:]))
    with pytest.raises(TypeError, match="Edge"):
        dataclasses.replace(r, pruned=(bad, *_other_pruned(r)))


# --- Contract §7: вхід ------------------------------------------------------------------------


def test_rejection_input_raises_type_error():
    service = GraphService(_shipped())
    for rejection in (Rejection(kind=RejectKind.INVALID_ADDRESS, mint="x", detail="not base58"),
                      Rejection(kind=RejectKind.TOKEN_NOT_FOUND, mint="", detail="")):
        with pytest.raises(TypeError, match="IngestResult.*Rejection"):
            service.analyze(rejection)
    for wrong in (None, {}, _ingest_doc("g_basic"), "g_basic"):
        with pytest.raises(TypeError):
            service.analyze(wrong)
    with pytest.raises(TypeError):
        GraphService(None)
    with pytest.raises(TypeError):
        GraphService(_shipped().thresholds)


@pytest.mark.parametrize("name", SCENARIOS)
def test_no_exception_for_any_fixture_scenario(name):
    """Кожен сценарій дає GraphResult, і він поле за полем дорівнює незалежному еталону `expected.json`."""
    expected = _expected(name)
    result = _analyze(name)
    assert isinstance(result, GraphResult)
    assert _metadata_dict(result.metadata) == _expected_metadata(name, expected)
    assert _completeness_dict(result.completeness) == expected["completeness"]
    assert _report_dict(result.report) == expected["report"]
    prune = expected["prune"]
    assert [_record_view(r) for r in result.pruned] == [_expected_record_view(r) for r in prune["records"]]
    assert [_flag_view(f) for f in result.buyer_flags] == prune["buyer_flags"]
    assert [n.address for n in result.graph.nodes] == prune["after"]["node_addresses"]
    assert [_edge_key(e) for e in result.graph.edges] == prune["after"]["edge_keys"]
    # Усі покупці входу — у графі результату (SC-003).
    assert len(result.graph.buyers()) == result.metadata.wallets_analyzed == len(_ingest_doc(name)["buyers"])


@pytest.mark.parametrize("name", SCENARIOS)
def test_no_exception_without_lists_and_with_not_analyzed_delegated(name):
    base = load_ingest_fixture(name)
    blind = dataclasses.replace(base, delegated=DelegatedAnalysis.NOT_ANALYZED)
    result = GraphService(load_hub_config(SHIPPED_HUBS, None)).analyze(blind)
    assert result.completeness.status is GraphCompletenessStatus.INCOMPLETE
    assert result.completeness.delegated_reason == "not_analyzed"
    assert {NOT_APPLIED, DELEGATED_INCOMPLETE} <= set(result.report.warnings)


def test_analyze_is_deterministic_and_does_not_mutate_input():
    base = load_ingest_fixture("g_hub")
    snapshot = repr(base)
    config = _scenario_config(_expected("g_hub"))
    first = GraphService(config).analyze(base)
    service = GraphService(config)
    assert service.analyze(base) == first == service.analyze(base)
    assert repr(base) == snapshot
    # Перестановка кортежів входу не змінює результату (FR-002-04).
    shuffled = dataclasses.replace(base, buyers=tuple(reversed(base.buyers)),
                                   transfers=tuple(reversed(base.transfers)),
                                   unexpanded=tuple(reversed(base.unexpanded)))
    assert GraphService(config).analyze(shuffled) == first


def test_analyze_uses_no_clock_files_or_network(monkeypatch):
    # Вхід і конфіг читаються ДО заборони (сам сервіс нічого не читає).
    base = load_ingest_fixture("g_unexpanded")
    service = GraphService(_scenario_config(_expected("g_unexpanded")))

    def forbidden(*args, **kwargs):
        raise AssertionError("analyze must not touch the clock, files or the network")

    for name in ("time", "time_ns", "monotonic", "monotonic_ns", "perf_counter", "localtime", "gmtime"):
        monkeypatch.setattr(time, name, forbidden)
    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    result = service.analyze(base)
    assert isinstance(result, GraphResult)


def test_service_module_imports_no_clock_files_or_network():
    tree = ast.parse((SRC / "graph" / "service.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    banned = {"time", "datetime", "os", "pathlib", "io", "socket", "httpx", "random", "uuid", "yaml", "json"}
    assert not {m.split(".")[0] for m in imported} & banned
    allowed = {"__future__", "typing", "unmask.graph.build", "unmask.graph.model", "unmask.hubs.config",
               "unmask.hubs.prune", "unmask.hubs.report", "unmask.ingest.model"}
    assert imported <= allowed, sorted(imported - allowed)


# --- FR-002-20, принцип IV: межі модулів ------------------------------------------------------


FORBIDDEN_INGEST = {f"unmask.ingest.{m}" for m in ("rpc", "collector", "buyers", "funding", "cache", "service")}
FORBIDDEN_NETWORK = {"socket", "httpx", "requests", "urllib", "aiohttp", "http"}
ADDRESSES = "unmask.ingest.addresses"


def _imports(path: Path) -> list[tuple[str, frozenset[str] | None]]:
    """[(модуль, імпортовані імена або None для `import x`)] — з урахуванням `from unmask.ingest import rpc`."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend((alias.name, None) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, f"{path}: relative import"
            module = node.module or ""
            names = frozenset(alias.name for alias in node.names)
            out.append((module, names))
            # `from unmask.ingest import rpc` імпортує підмодуль — рахуємо як `unmask.ingest.rpc`.
            out.extend((f"{module}.{n}", None) for n in names)
        elif isinstance(node, ast.Call) and getattr(node.func, "id", None) == "__import__":
            raise AssertionError(f"{path}: dynamic __import__")
    return out


def _package_files() -> list[Path]:
    files = sorted((SRC / "graph").glob("*.py")) + sorted((SRC / "hubs").glob("*.py"))
    assert {p.name for p in files} >= {"service.py", "build.py", "model.py", "config.py", "criteria.py",
                                       "prune.py", "report.py"}
    return files


def _under(module: str, prefix: str) -> bool:
    return module == prefix or module.startswith(prefix + ".")


def test_graph_packages_import_no_ingest_internals_and_no_network():
    config_py = SRC / "hubs" / "config.py"
    for path in _package_files():
        rel = path.relative_to(SRC).as_posix()
        for module, names in _imports(path):
            assert not any(_under(module, f) for f in FORBIDDEN_INGEST), (rel, module)
            assert module.split(".")[0] not in FORBIDDEN_NETWORK, (rel, module)
            assert not _under(module, "importlib"), (rel, module)
            if _under(module, ADDRESSES):
                # Лише hubs/config.py і лише `from unmask.ingest.addresses import is_valid_address` (ревʼю T-037).
                assert path == config_py, (rel, module)
                if module == ADDRESSES and names is not None:
                    assert names == {"is_valid_address"}, (rel, sorted(names))
                else:
                    assert module == f"{ADDRESSES}.is_valid_address" and names is None, (rel, module)
            if _under(module, "unmask.ingest"):
                # Решта 001 — лише модель і `from_dict` (контракт graph-service.md: залежність через IngestResult).
                if _under(module, "unmask.ingest.serialize"):
                    assert module == "unmask.ingest.serialize.from_dict" or names == {"from_dict"}, (rel, module)
                elif not _under(module, ADDRESSES):
                    assert module in {"unmask.ingest", "unmask.ingest.model"} or module.startswith(
                        "unmask.ingest.model."), (rel, module)
                    if module == "unmask.ingest":
                        assert names is not None and names <= {"model"}, (rel, sorted(names or ()))


def test_import_rule_catches_forbidden_imports(tmp_path):
    """Правило вище справді ловить заборонене (а не проходить на порожньому переліку)."""
    def rule_violations(source: str, as_config: bool) -> bool:
        path = tmp_path / "config.py" if as_config else tmp_path / "prune.py"
        path.write_text(source, encoding="utf-8")
        try:
            for module, names in _imports(path):
                assert not any(_under(module, f) for f in FORBIDDEN_INGEST)
                assert module.split(".")[0] not in FORBIDDEN_NETWORK
                if _under(module, ADDRESSES):
                    assert as_config
                    if module == ADDRESSES and names is not None:
                        assert names == {"is_valid_address"}
                    else:
                        assert module == f"{ADDRESSES}.is_valid_address" and names is None
        except AssertionError:
            return True
        return False

    assert rule_violations("from unmask.ingest import rpc\n", as_config=False)
    assert rule_violations("import unmask.ingest.collector\n", as_config=False)
    assert rule_violations("from unmask.ingest.cache import X\n", as_config=False)
    assert rule_violations("import socket\n", as_config=False)
    assert rule_violations("from httpx import Client\n", as_config=False)
    assert rule_violations("from unmask.ingest.addresses import is_valid_address\n", as_config=False)
    assert rule_violations("from unmask.ingest.addresses import is_valid_address, normalize\n", as_config=True)
    assert rule_violations("import unmask.ingest.addresses\n", as_config=True)
    assert not rule_violations("from unmask.ingest.addresses import is_valid_address\n", as_config=True)
    assert not rule_violations("from unmask.ingest.model import IngestResult\n", as_config=False)


# --- Поріг збору: з метаданих результату, не з файлу --------------------------------------------


def test_ingest_counterparty_threshold_taken_from_result_metadata_not_config_file(monkeypatch):
    base = load_ingest_fixture("g_unexpanded")  # high_degree з counterparties_seen=4 при порозі збору 3
    assert base.metadata.counterparty_threshold == 3
    config = _scenario_config(_expected("g_unexpanded"))

    def no_files(*args, **kwargs):
        raise AssertionError("analyze must not read config files")

    monkeypatch.setattr(builtins, "open", no_files)
    monkeypatch.setattr(Path, "read_text", no_files)

    def ingest_hits(result: GraphResult) -> list:
        return [h for r in result.pruned for h in r.criteria if h.criterion is HubCriterion.INGEST_HIGH_DEGREE]

    hits = ingest_hits(GraphService(config).analyze(base))
    assert [(h.measured, h.threshold) for h in hits] == [(4, 3)]  # не 200 з config/ingest.yaml
    # Інший поріг у метаданих того самого результату — інший поріг хіта.
    lowered = dataclasses.replace(base, metadata=dataclasses.replace(base.metadata, counterparty_threshold=2))
    assert [(h.measured, h.threshold) for h in ingest_hits(GraphService(config).analyze(lowered))] == [(4, 2)]


# --- Вхід, що суперечить контракту 001 → GraphInputError --------------------------------------


def _with_unexpanded(base: IngestResult, reason: UnexpandedReason, **changes) -> IngestResult:
    marks = tuple(dataclasses.replace(u, **changes) if u.reason is reason else u for u in base.unexpanded)
    return dataclasses.replace(base, unexpanded=marks)


def test_high_degree_mark_not_above_result_threshold_raises_graph_input_error():
    base = load_ingest_fixture("g_unexpanded")
    service = GraphService(_scenario_config(_expected("g_unexpanded")))
    for seen in (3, 2, 0):  # рівно поріг і нижче: 001 позначає high_degree лише понад поріг
        with pytest.raises(GraphInputError, match="high_degree"):
            service.analyze(_with_unexpanded(base, UnexpandedReason.HIGH_DEGREE, counterparties_seen=seen))
    # Поріг у метаданих, піднятий до counterparties_seen, — та сама суперечність.
    raised = dataclasses.replace(base, metadata=dataclasses.replace(base.metadata, counterparty_threshold=4))
    with pytest.raises(GraphInputError, match="high_degree"):
        service.analyze(raised)
    # Вимкнений критерій не ховає дефект входу.
    off = GraphService(_config(lists=_lists(), prune_ingest_high_degree=False))
    with pytest.raises(GraphInputError):
        off.analyze(raised)


def test_signature_cap_mark_inconsistent_with_result_raises_graph_input_error():
    base = load_ingest_fixture("g_unexpanded")  # signature_cap: counterparties_seen=2, truncated, поріг 3
    service = GraphService(_scenario_config(_expected("g_unexpanded")))
    assert service.analyze(base)  # узгоджена позначка — валідний вхід
    # Понад поріг збору 001 позначив би high_degree, а не signature_cap.
    for seen in (4, 10):
        with pytest.raises(GraphInputError, match="signature_cap"):
            service.analyze(_with_unexpanded(base, UnexpandedReason.SIGNATURE_CAP, counterparties_seen=seen))
    # Рівно поріг — ще signature_cap (001 зупиняється на новому відправнику понад поріг).
    assert service.analyze(_with_unexpanded(base, UnexpandedReason.SIGNATURE_CAP, counterparties_seen=3))
    # signature_cap без обрізаної історії — суперечність.
    with pytest.raises(GraphInputError, match="signature_cap"):
        service.analyze(_with_unexpanded(base, UnexpandedReason.SIGNATURE_CAP, signatures_truncated=False))


def test_depths_beyond_result_funding_depth_raise_graph_input_error():
    base = load_ingest_fixture("g_incomplete")  # funding_depth=3, missing на глибині 1
    service = GraphService(_scenario_config(_expected("g_incomplete")))
    assert base.metadata.funding_depth == 3
    # Переказ глибше за funding_depth результату (g_basic має перекази глибини 3).
    basic = load_ingest_fixture("g_basic")
    assert max(t.depth for t in basic.transfers) == 3
    basic_service = GraphService(_scenario_config(_expected("g_basic")))
    shallow = dataclasses.replace(basic, metadata=dataclasses.replace(basic.metadata, funding_depth=2))
    with pytest.raises(GraphInputError, match="transfer.*depth"):
        basic_service.analyze(shallow)
    # Запис неповноти глибше межі контракту 001 (missingRef.depth ≤ 3) — GraphInputError, не ValueError.
    entry = base.completeness.missing[0]
    deep = MissingHistory(wallet=entry.wallet, depth=4, reason=entry.reason, detail=entry.detail)
    bad = dataclasses.replace(base, completeness=Completeness.derive((deep,), base.completeness.buyers))
    with pytest.raises(GraphInputError, match="depth"):
        service.analyze(bad)
    # funding_depth понад межу контракту 001 (1..3) — GraphInputError.
    too_deep = dataclasses.replace(base, metadata=dataclasses.replace(base.metadata, funding_depth=4))
    with pytest.raises(GraphInputError, match="funding_depth"):
        service.analyze(too_deep)
    # Позначка нерозгорнутості глибше funding_depth.
    ub = load_ingest_fixture("g_unexpanded")
    ub_service = GraphService(_scenario_config(_expected("g_unexpanded")))
    lifted = dataclasses.replace(ub, metadata=dataclasses.replace(ub.metadata, funding_depth=3),
                                 unexpanded=tuple(dataclasses.replace(u, depth=4) for u in ub.unexpanded))
    with pytest.raises(GraphInputError, match="depth"):
        ub_service.analyze(lifted)


def _at_funding_depth(base: IngestResult, funding_depth: int, **changes) -> IngestResult:
    return dataclasses.replace(base, metadata=dataclasses.replace(base.metadata, funding_depth=funding_depth),
                               **changes)


@pytest.mark.parametrize("funding_depth", [1, 2])
def test_missing_depth_is_checked_against_result_funding_depth_not_only_contract_bound(funding_depth):
    """Відношення до `funding_depth` результату, а не лише межа схеми ≤ 3: depth = funding_depth + 1 ≤ 3."""
    base = load_ingest_fixture("g_incomplete")  # перекази глибини 1, запис неповноти глибини 1
    assert max(t.depth for t in base.transfers) == 1
    service = GraphService(_scenario_config(_expected("g_incomplete")))
    entry = base.completeness.missing[0]

    def with_missing_at(depth: int) -> IngestResult:
        m = MissingHistory(wallet=entry.wallet, depth=depth, reason=entry.reason, detail=entry.detail)
        return _at_funding_depth(base, funding_depth,
                                 completeness=Completeness.derive((m,), base.completeness.buyers))

    assert service.analyze(with_missing_at(funding_depth))  # рівно funding_depth — валідно
    with pytest.raises(GraphInputError, match=r"missing .*depth"):
        service.analyze(with_missing_at(funding_depth + 1))


@pytest.mark.parametrize("name, funding_depth", [("g_buyer_hub", 1), ("g_unexpanded", 2)])
def test_unexpanded_depth_is_checked_against_result_funding_depth_not_only_contract_bound(name, funding_depth):
    base = load_ingest_fixture(name)
    assert base.unexpanded and max(t.depth for t in base.transfers) <= funding_depth
    service = GraphService(_scenario_config(_expected(name)))

    def with_marks_at(depth: int) -> IngestResult:
        return _at_funding_depth(base, funding_depth,
                                 unexpanded=tuple(dataclasses.replace(u, depth=depth) for u in base.unexpanded))

    assert service.analyze(with_marks_at(funding_depth))  # рівно funding_depth — валідно
    with pytest.raises(GraphInputError, match=r"unexpanded .*depth"):
        service.analyze(with_marks_at(funding_depth + 1))


def test_missing_ref_depth_is_bounded_by_ingest_contract():
    """Межа missingRef.depth у моделі = межа схеми = контракт 001 (funding_depth 1..3)."""
    schema = json.loads((ROOT / "specs" / "002-funding-graph-hub-pruning" / "contracts" /
                         "graph-result.schema.json").read_text(encoding="utf-8"))
    schema_max = schema["$defs"]["missingRef"]["properties"]["depth"]["maximum"]
    ingest_schema = json.loads((ROOT / "specs" / "001-onchain-data-ingest" / "contracts" /
                                "ingest-result.schema.json").read_text(encoding="utf-8"))
    defs = ingest_schema["$defs"]
    funding_max = defs["metadata"]["properties"]["funding_depth"]["maximum"]
    assert schema_max == funding_max == defs["missing"]["properties"]["depth"]["maximum"] == 3
    assert MissingRef("W" * 32, 3, MissingReason.TIMEOUT, "").depth == 3
    with pytest.raises(ValueError, match="depth"):
        MissingRef("W" * 32, schema_max + 1, MissingReason.TIMEOUT, "")
    # Та сама межа у вершини (`node.depth` у схемі ≤ 3): найглибша вершина g_basic — рівно 3.
    assert schema["$defs"]["node"]["properties"]["depth"]["maximum"] == schema_max
    deepest = max(_analyze("g_basic").graph.nodes, key=lambda n: n.depth)
    assert deepest.depth == schema_max
    with pytest.raises(ValueError, match="depth"):
        dataclasses.replace(deepest, depth=schema_max + 1)
