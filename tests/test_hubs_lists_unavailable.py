# verifies: FR-002-12
"""Список відомих адрес не застосовано — це видно, а не виглядає як «хабів немає» (T-037; FR-002-12;
конституція V «відсутність ≠ чисто»; research R-11, R-12, R-13; contracts/graph-service.md §4–§6;
data-model «PruneOutcome», «GraphWarning», `HubConfig.lists`).

Що доводиться:

- `lists is None` (файл відсутній / шлях не заданий — справжній `load_hub_config`) → `prune_hubs` дає
  `lists_applied=False`, `lists_version=None`, жодного хіта `list:*`, а `effect_report(lists_applied=False)` —
  попередження `address_lists_not_applied` (разом з іншими — відсортовані за рядком, без дублів);
- без списків працюють решта критеріїв: `degree`, `one_off_senders`, `ingest_high_degree`, `dust_fanout` і
  `known_list` за типом адреси (`off_curve`); еталон — незалежний оракул `expected.json` (`prune.records` /
  `prune.buyer_flags` без хітів `list:*`), а не вихід `prune_hubs`;
- порожні категорії при завантаженому файлі — це `lists_applied=True` з версією (≥ 1) і БЕЗ попередження: файл
  застосовано, хоч нічого не знайдено (відмінність від «не застосовано» — суть вимоги);
- відсутність хітів за списком при `lists_applied=True` не дає попередження.

Інваріант «`metadata.lists_applied == False` ⇒ `address_lists_not_applied ∈ report.warnings`» (data-model,
`GraphResult`) живе в `GraphResult` (T-039): `EffectReport` не знає про `lists_applied` (це параметр функції, не
поле), тож тут перевіряється джерело попередження — `effect_report`.
"""

import json
from pathlib import Path
from types import MappingProxyType

import pytest

from conftest import GRAPH_FIXTURES, load_ingest_fixture
from unmask.graph.build import build_graph
from unmask.graph.model import GraphCompleteness, GraphWarning
from unmask.hubs.config import ADDRESS_CATEGORIES, AddressLists, HubConfig, HubThresholds, load_hub_config
from unmask.hubs.prune import prune_hubs
from unmask.hubs.report import effect_report

ROOT = Path(__file__).resolve().parents[1]
SHIPPED_HUBS = ROOT / "config" / "hubs.yaml"
SHIPPED_LISTS = ROOT / "config" / "hub_addresses.yaml"
SCENARIOS = sorted(p.name for p in Path(GRAPH_FIXTURES).iterdir() if (p / "expected.json").is_file())
NOT_APPLIED = GraphWarning.ADDRESS_LISTS_NOT_APPLIED


# --- Дані -------------------------------------------------------------------------------


def _expected(name: str) -> dict:
    return json.loads((Path(GRAPH_FIXTURES) / name / "expected.json").read_text(encoding="utf-8"))


def _lists(version: int = 1, **categories) -> AddressLists:
    parsed = {name: tuple(categories.get(name, ())) for name in ADDRESS_CATEGORIES}
    index = {address: name for name in ADDRESS_CATEGORIES for address in parsed[name]}
    return AddressLists(version=version, categories=MappingProxyType(parsed), index=MappingProxyType(index))


def _config(expected: dict, *, lists: AddressLists | None, **threshold_changes) -> HubConfig:
    """Конфіг сценарію: пороги рівно з `expected.config`, списки — задані явно."""
    thresholds = dict(expected["config"]["thresholds"], **threshold_changes)
    return HubConfig(
        thresholds=HubThresholds(version=expected["config"]["version"], **thresholds),
        lists=lists,
        thresholds_digest="0" * 64,
        lists_digest=None if lists is None else "1" * 64,
    )


def _oracle_lists(expected: dict) -> AddressLists:
    lists = expected["config"]["lists"]
    return _lists(lists["version"], **lists["categories"])


def _run(name: str, *, lists: AddressLists | None, **threshold_changes):
    """(expected, before, outcome, report, config) сценарію з заданими списками (звіт — з `outcome.lists_applied`)."""
    expected = _expected(name)
    result = load_ingest_fixture(name)
    config = _config(expected, lists=lists, **threshold_changes)
    before = build_graph(result)
    outcome = prune_hubs(before, config, ingest_counterparty_threshold=result.metadata.counterparty_threshold)
    report = effect_report(before, outcome.graph, config, lists_applied=outcome.lists_applied,
                           delegated_complete=GraphCompleteness.derive(result).delegated_complete)
    return expected, before, outcome, report, config


def _hit_tuples(hits) -> list[tuple]:
    return [(h.criterion.value, h.measured, h.threshold, h.detail, h.lists_version) for h in hits]


def _oracle_hit_tuples(hits: list[dict]) -> list[tuple]:
    return [(h["criterion"], h["measured"], h["threshold"], h["detail"], h["lists_version"]) for h in hits]


def _without_list_hits(hits: list[dict]) -> list[dict]:
    return [h for h in hits if not h["detail"].startswith("list:")]


def _edge_keys(edges) -> list[list]:
    return [[e.kind.value, e.sender, e.receiver, "" if e.asset is None else str(e.asset)] for e in edges]


def _all_hits(outcome) -> list:
    return [h for item in (*outcome.records, *outcome.buyer_flags) for h in item.criteria]


def _write_lists_yaml(path: Path, *, version: int, categories: dict[str, list[str]]) -> Path:
    lines = [f"version: {version}", "categories:"]
    for name in ADDRESS_CATEGORIES:
        addresses = categories.get(name, [])
        if not addresses:
            lines.append(f"  {name}: []")
        else:
            lines.append(f"  {name}:")
            lines.extend(f'    - "{a}"' for a in addresses)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# --- 1. Файл списків відсутній → lists_applied=False + попередження ----------------------


@pytest.mark.parametrize("lists_path_kind", ["not_passed", "missing_file"])
def test_missing_lists_file_gives_lists_applied_false_and_warning(tmp_path, lists_path_kind):
    """Справжній `load_hub_config` без списків → `lists_applied=False`, `lists_version=None` у результаті відсікання
    і `address_lists_not_applied` у звіті; при завантажених комітованих списках попередження немає."""
    lists_path = None if lists_path_kind == "not_passed" else tmp_path / "no_such_hub_addresses.yaml"
    config = load_hub_config(SHIPPED_HUBS, lists_path)
    assert config.lists is None and config.lists_digest is None

    result = load_ingest_fixture("g_known")
    before = build_graph(result)
    threshold = result.metadata.counterparty_threshold
    outcome = prune_hubs(before, config, ingest_counterparty_threshold=threshold)
    assert (outcome.lists_applied, outcome.lists_version) == (False, None)
    assert all(r.lists_version is None for r in outcome.records)

    delegated = GraphCompleteness.derive(result).delegated_complete
    report = effect_report(before, outcome.graph, config, lists_applied=outcome.lists_applied,
                           delegated_complete=delegated)
    assert NOT_APPLIED in report.warnings

    # Контраст: ті самі дані з завантаженими списками — без попередження і з відсіканням за списком.
    with_lists = load_hub_config(SHIPPED_HUBS, SHIPPED_LISTS)
    outcome_ok = prune_hubs(before, with_lists, ingest_counterparty_threshold=threshold)
    report_ok = effect_report(before, outcome_ok.graph, with_lists, lists_applied=outcome_ok.lists_applied,
                              delegated_complete=delegated)
    assert (outcome_ok.lists_applied, outcome_ok.lists_version) == (True, 1)
    assert NOT_APPLIED not in report_ok.warnings


def test_no_hits_without_lists_still_warns_so_it_never_reads_as_no_hubs():
    """Найгірший випадок вимоги: без списків і без інших спрацьовувань записів немає — але звіт НЕ порожній."""
    expected, before, outcome, report, _ = _run("g_known", lists=None, prune_off_curve=False)
    assert (outcome.records, outcome.buyer_flags) == ((), ())
    assert outcome.graph == before  # нічого не відсічено
    assert (outcome.lists_applied, outcome.lists_version) == (False, None)
    assert report.warnings == (NOT_APPLIED,)
    assert report.pruned_nodes == 0 and report.pruned_edges == 0
    # Той самий граф із застосованими списками: Pump.fun відсічено за списком (отже «немає хабів» було б хибним).
    _, _, outcome_ok, report_ok, _ = _run("g_known", lists=_oracle_lists(expected), prune_off_curve=False)
    assert [r.address for r in outcome_ok.records] == [expected["wallets"]["PUMP"]]
    assert NOT_APPLIED not in report_ok.warnings


def test_warning_is_sorted_with_other_warnings_without_duplicates():
    """`address_lists_not_applied` + `delegated_incomplete` + `giant_component` — у порядку за рядком."""
    expected = _expected("g_hub")
    result = load_ingest_fixture("g_hub")
    before = build_graph(result)
    config = _config(expected, lists=None)
    # `after is before`: без відсікання частка 1.0 > 0.5 (giant_component).
    report = effect_report(before, before, config, lists_applied=False, delegated_complete=False)
    assert report.warnings == (NOT_APPLIED, GraphWarning.DELEGATED_INCOMPLETE, GraphWarning.GIANT_COMPONENT)
    assert [w.value for w in report.warnings] == sorted({w.value for w in report.warnings})
    assert len(set(report.warnings)) == len(report.warnings)

    only = effect_report(before, before, config, lists_applied=False, delegated_complete=True)
    assert only.warnings == (NOT_APPLIED, GraphWarning.GIANT_COMPONENT)
    with_lists = effect_report(before, before, config, lists_applied=True, delegated_complete=False)
    assert with_lists.warnings == (GraphWarning.DELEGATED_INCOMPLETE, GraphWarning.GIANT_COMPONENT)


@pytest.mark.parametrize("name", SCENARIOS)
def test_warning_depends_only_on_lists_applied_on_every_scenario(name):
    expected, before, outcome, report, config = _run(name, lists=None)
    assert NOT_APPLIED in report.warnings
    delegated = GraphCompleteness.derive(load_ingest_fixture(name)).delegated_complete
    same_graphs_applied = effect_report(before, outcome.graph, config, lists_applied=True,
                                        delegated_complete=delegated)
    assert report.warnings == tuple(sorted({*same_graphs_applied.warnings, NOT_APPLIED}, key=lambda w: w.value))
    assert NOT_APPLIED not in same_graphs_applied.warnings
    # Оракул з застосованими списками ніколи не містить цього попередження (воно — лише про відсутність списків).
    assert NOT_APPLIED.value not in expected["report"]["warnings"]


# --- 2. known_list: за списком не спрацьовує, за типом (off_curve) — так -------------------


def test_known_list_by_address_not_fired_without_lists_but_off_curve_still_is():
    expected, before, outcome, _, _ = _run("g_known", lists=None)
    pda, pump = expected["wallets"]["PDA_SRC"], expected["wallets"]["PUMP"]

    assert [r.address for r in outcome.records] == [pda]
    record = outcome.records[0]
    oracle = next(r for r in expected["prune"]["records"] if r["address"] == pda)
    assert _hit_tuples(record.criteria) == _oracle_hit_tuples(oracle["criteria"])
    assert [(h.criterion.value, h.detail, h.lists_version) for h in record.criteria] == \
        [("known_list", "address_type:off_curve", None)]
    assert _edge_keys(record.incident_edges) == [
        [e["kind"], e["sender"], e["receiver"], e["asset"] or ""] for e in oracle["incident_edges"]]

    # Pump.fun — у комітованому списку (launchpads), але списків немає → не відсічений, вершина й ребра на місці.
    assert pump not in {r.address for r in outcome.records}
    assert outcome.graph.node(pump) is before.node(pump)
    assert {e.key for e in before.edges if pump in (e.sender, e.receiver)} <= {e.key for e in outcome.graph.edges}
    assert not [h for h in _all_hits(outcome) if h.detail.startswith("list:")]

    # Контраст: із застосованими списками Pump.fun відсікається саме за `list:launchpads`.
    _, _, applied, _, _ = _run("g_known", lists=_oracle_lists(expected))
    pump_record = next(r for r in applied.records if r.address == pump)
    assert [(h.detail, h.lists_version) for h in pump_record.criteria] == [("list:launchpads", 1)]


def test_off_curve_is_independent_of_lists_and_switched_only_by_its_own_flag():
    expected, _, outcome, _, _ = _run("g_known", lists=None, prune_off_curve=False)
    assert outcome.records == ()  # без списків і без off_curve на g_known хітів немає
    _, _, on, _, _ = _run("g_known", lists=None, prune_off_curve=True)
    assert [r.address for r in on.records] == [expected["wallets"]["PDA_SRC"]]


# --- 3. Решта критеріїв без списків не змінюється ----------------------------------------


def test_degree_and_one_off_and_ingest_criteria_unaffected_by_missing_lists():
    # g_hub: `one_off_senders` на хабі — з lists=None той самий запис, що в оракулі.
    expected, _, outcome, _, _ = _run("g_hub", lists=None)
    _, _, applied, _, _ = _run("g_hub", lists=_oracle_lists(expected))
    assert [r.address for r in outcome.records] == [r["address"] for r in expected["prune"]["records"]]
    assert [(r.address, _hit_tuples(r.criteria), r.incident_edges, r.measures) for r in outcome.records] == \
        [(r.address, _hit_tuples(r.criteria), r.incident_edges, r.measures) for r in applied.records]
    assert [h.criterion.value for r in outcome.records for h in r.criteria] == ["one_off_senders"]
    assert [r.lists_version for r in outcome.records] == [None]  # версія списків не вигадується

    # Критерії, що перекривають усі сценарії (degree, ingest_high_degree, dust_fanout, off_curve, one_off) —
    # проти оракула: записи й позначки = еталон без хітів `list:*`, запис без хітів зникає.
    seen: set[str] = set()
    for name in SCENARIOS:
        expected, _, outcome, _, _ = _run(name, lists=None)
        want_records = []
        for r in expected["prune"]["records"]:
            hits = _without_list_hits(r["criteria"])
            if hits:
                want_records.append((r["address"], _oracle_hit_tuples(hits), [
                    [e["kind"], e["sender"], e["receiver"], e["asset"] or ""] for e in r["incident_edges"]]))
        want_flags = []
        for f in expected["prune"]["buyer_flags"]:
            hits = _without_list_hits(f["criteria"])
            if hits:
                want_flags.append((f["address"], f["buyer_rank"], _oracle_hit_tuples(hits)))
        got_records = [(r.address, _hit_tuples(r.criteria), _edge_keys(r.incident_edges)) for r in outcome.records]
        got_flags = [(f.address, f.buyer_rank, _hit_tuples(f.criteria)) for f in outcome.buyer_flags]
        assert got_records == want_records, name
        assert got_flags == want_flags, name
        assert (outcome.lists_applied, outcome.lists_version) == (False, None), name
        seen.update(h.detail if h.criterion.value == "known_list" else h.criterion.value
                    for h in _all_hits(outcome))

    # Тест не порожній: усі п'ять без-списочних критеріїв реально спрацьовували на фікстурах.
    assert seen == {"degree", "one_off_senders", "ingest_high_degree", "dust_fanout", "address_type:off_curve"}


# --- 4. Порожні категорії: файл застосовано --------------------------------------------


def test_empty_categories_count_as_applied_with_version(tmp_path):
    lists_path = _write_lists_yaml(tmp_path / "hub_addresses.yaml", version=3, categories={})
    config = load_hub_config(SHIPPED_HUBS, lists_path)
    assert config.lists is not None and config.lists.version == 3
    assert all(config.lists.categories[c] == () for c in ADDRESS_CATEGORIES)

    result = load_ingest_fixture("g_known")
    before = build_graph(result)
    outcome = prune_hubs(before, config, ingest_counterparty_threshold=result.metadata.counterparty_threshold)
    assert (outcome.lists_applied, outcome.lists_version) == (True, 3)
    assert outcome.lists_version >= 1
    # Порожній список нічого не знаходить; off_curve працює; кожен запис несе версію списків.
    assert [r.address for r in outcome.records] == [_expected("g_known")["wallets"]["PDA_SRC"]]
    assert all(r.lists_version == 3 for r in outcome.records)
    assert not [h for h in _all_hits(outcome) if h.detail.startswith("list:")]

    report = effect_report(before, outcome.graph, config, lists_applied=outcome.lists_applied,
                           delegated_complete=True)
    assert NOT_APPLIED not in report.warnings


def test_empty_categories_built_in_memory_are_applied_with_version_one():
    expected = _expected("g_known")
    config = _config(expected, lists=_lists(1))
    assert config.lists_applied is True
    _, _, outcome, report, _ = _run("g_known", lists=_lists(1))
    assert (outcome.lists_applied, outcome.lists_version) == (True, 1)
    assert NOT_APPLIED not in report.warnings


def test_list_version_is_carried_to_outcome_records_and_hits():
    expected = _expected("g_known")
    lists = _lists(7, launchpads=[expected["wallets"]["PUMP"]])
    _, _, outcome, report, _ = _run("g_known", lists=lists)
    assert (outcome.lists_applied, outcome.lists_version) == (True, 7)
    assert {r.lists_version for r in outcome.records} == {7}
    pump = next(r for r in outcome.records if r.address == expected["wallets"]["PUMP"])
    assert [(h.detail, h.lists_version) for h in pump.criteria] == [("list:launchpads", 7)]
    assert NOT_APPLIED not in report.warnings


# --- 5. Відсутність хітів за списком при lists_applied=True — без попередження ------------


@pytest.mark.parametrize("name", ["g_hub", "g_basic", "g_incomplete", "g_dust"])
def test_absence_of_list_hits_with_lists_applied_true_has_no_warning(name):
    expected = _expected(name)
    _, _, outcome, report, _ = _run(name, lists=_oracle_lists(expected))
    assert outcome.lists_applied is True and outcome.lists_version == expected["config"]["lists"]["version"]
    assert not [h for h in _all_hits(outcome) if h.detail.startswith("list:")]  # жодного спрацювання за списком
    assert NOT_APPLIED not in report.warnings
    assert [w.value for w in report.warnings] == expected["report"]["warnings"]


def test_committed_lists_applied_gives_no_warning_on_hub_fixture():
    config = load_hub_config(SHIPPED_HUBS, SHIPPED_LISTS)
    result = load_ingest_fixture("g_hub")
    before = build_graph(result)
    outcome = prune_hubs(before, config, ingest_counterparty_threshold=result.metadata.counterparty_threshold)
    report = effect_report(before, outcome.graph, config, lists_applied=outcome.lists_applied,
                           delegated_complete=True)
    assert (outcome.lists_applied, outcome.lists_version) == (True, 1)
    assert NOT_APPLIED not in report.warnings
