# verifies: FR-002-20, FR-002-22
"""Генератор фікстур графа 002 (T-026): детермінізм, схема 001, round-trip `from_dict`, незалежність еталона.

Фікстури `tests/fixtures/graph/<scenario>/{ingest.json, expected.json}` будує
`tests/fixtures/build_graph_fixtures.py` з декларативного опису (вершини з ролями й глибиною, перекази,
`unexpanded`, `missing`, хаби з причинами). `expected.json` — еталон, складений генератором простими
словниками/перебором/BFS, а НЕ кодом, що тестується (`unmask.graph`, `unmask.hubs`).

Тут еталон перевіряється ще раз, іншим способом: `_recompute` нижче заново виводить ребра, виміри,
хіти критеріїв і компоненти з `ingest.json` (union-find замість BFS, лічильники замість перебору пар)
і звіряє з `expected.json`. Розбіжність двох незалежних оракулів — дефект генератора, а не реалізації.
T-056 (FR-002-22): виміри `buyer_fanout`/`median_to_buyers` і критерій `dust_fanout`, `config.version == 2`,
сценарії `g_dust`, `g_financier`, `g_dust_mixed`. Мережі немає.
"""

import ast
import importlib.util
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from solders.pubkey import Pubkey

from conftest import load_ingest_fixture
from unmask.ingest.model import AddressType, IngestResult, MissingReason, UnexpandedReason
from unmask.ingest.serialize import from_dict, to_dict

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
GRAPH = FIXTURES / "graph"
BUILDER = FIXTURES / "build_graph_fixtures.py"
SCHEMA = json.loads((ROOT / "specs/001-onchain-data-ingest/contracts/ingest-result.schema.json").read_text())
VALIDATOR = Draft202012Validator(SCHEMA)

SCENARIOS = ["g_basic", "g_hub", "g_known", "g_buyer_hub", "g_incomplete", "g_empty", "g_all_hubs", "g_unexpanded",
             "g_dust", "g_financier", "g_dust_mixed", "g_delegated"]
DUST_T = 1_000_000  # dust_amount_lamports у hubs.yaml v2 (у v3 без змін)
THRESHOLD_KEYS = {"degree_threshold", "one_off_senders_share", "one_off_min_senders", "giant_component_warn_share",
                  "prune_off_curve", "prune_ingest_high_degree", "dust_amount_lamports", "dust_min_fanout"}
FILES = ["ingest.json", "expected.json"]
PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"


def _load_builder():
    spec = importlib.util.spec_from_file_location("build_graph_fixtures", BUILDER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # FileNotFoundError, якщо генератора немає
    return module


@pytest.fixture(scope="module")
def builder():
    return _load_builder()


def _ingest(name):
    return json.loads((GRAPH / name / "ingest.json").read_text(encoding="utf-8"))


def _expected(name):
    return json.loads((GRAPH / name / "expected.json").read_text(encoding="utf-8"))


# --- T-026: детермінізм і файли на диску ------------------------------------------------------------


def test_build_is_deterministic_and_matches_committed_files(builder):
    first = builder.build_all()
    second = builder.build_all()
    assert first == second
    assert set(first) == {f"{s}/{f}" for s in SCENARIOS for f in FILES}
    stale = [rel for rel, text in first.items() if (GRAPH / rel).read_text(encoding="utf-8") != text]
    assert stale == []
    on_disk = {p.relative_to(GRAPH).as_posix() for p in GRAPH.rglob("*") if p.is_file()}
    assert on_disk == set(first), "на диску є файли, яких генератор не створює (або навпаки)"


def test_scenario_list_is_exactly_the_twelve_after_t047(builder):
    assert list(builder.SCENARIO_NAMES) == SCENARIOS


def test_committed_files_are_valid_json_with_final_newline_and_no_nan():
    for s in SCENARIOS:
        for f in FILES:
            text = (GRAPH / s / f).read_text(encoding="utf-8")
            assert text.endswith("\n") and not text.endswith("\n\n")
            json.loads(text, parse_constant=lambda c: pytest.fail(f"{s}/{f}: {c}"))


def test_check_mode_reports_clean_stale_missing_and_extra_files(builder, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(builder, "OUT_DIR", tmp_path)
    assert builder.main([]) == 0  # запис
    assert builder.main(["--check"]) == 0
    capsys.readouterr()
    target = tmp_path / "g_hub" / "expected.json"
    original = target.read_text(encoding="utf-8")
    target.write_text(original.replace("0.9", "0.8", 1) if "0.9" in original else original + " ", encoding="utf-8")
    assert builder.main(["--check"]) == 1
    assert "g_hub/expected.json" in capsys.readouterr().out
    target.write_text(original, encoding="utf-8")
    assert builder.main(["--check"]) == 0
    (tmp_path / "g_hub" / "ingest.json").unlink()
    assert builder.main(["--check"]) == 1
    builder.main([])
    (tmp_path / "g_stale").mkdir()
    (tmp_path / "g_stale" / "ingest.json").write_text("{}\n", encoding="utf-8")
    assert builder.main(["--check"]) == 1


def test_check_mode_does_not_write(builder, monkeypatch, tmp_path):
    monkeypatch.setattr(builder, "OUT_DIR", tmp_path)
    assert builder.main(["--check"]) == 1
    assert list(tmp_path.iterdir()) == []


# --- T-026: схема 001, round-trip ---------------------------------------------------------------------


@pytest.mark.parametrize("name", SCENARIOS)
def test_every_ingest_json_validates_against_001_schema(name):
    doc = _ingest(name)
    errors = [f"{'/'.join(map(str, e.absolute_path))}: {e.message}" for e in VALIDATOR.iter_errors(doc)]
    assert errors == []


def test_schema_check_is_not_vacuous():
    doc = _ingest("g_basic")
    doc["buyers"][0]["rank"] = 0
    assert list(VALIDATOR.iter_errors(doc))
    doc = _ingest("g_basic")
    doc["surprise"] = 1
    assert list(VALIDATOR.iter_errors(doc))


@pytest.mark.parametrize("name", SCENARIOS)
def test_from_dict_round_trips_to_dict_on_every_scenario(name):
    doc = _ingest(name)
    result = from_dict(doc)
    assert isinstance(result, IngestResult)
    assert to_dict(result) == doc
    assert from_dict(to_dict(result)) == result


@pytest.mark.parametrize("name", SCENARIOS)
def test_load_ingest_fixture_returns_the_same_result_as_from_dict(name):
    assert load_ingest_fixture(name) == from_dict(_ingest(name))


def test_load_ingest_fixture_unknown_scenario_fails_loudly():
    with pytest.raises(FileNotFoundError):
        load_ingest_fixture("g_nonexistent")


@pytest.mark.parametrize("name", SCENARIOS)
def test_ingest_json_is_internally_consistent_with_model_rules(name):
    result = load_ingest_fixture(name)
    assert result.metadata.wallets_analyzed == len(result.buyers)
    assert [b.rank for b in result.buyers] == list(range(1, len(result.buyers) + 1))
    assert result.metadata.source == f"fixture:{name}"
    assert all(t.sender != t.receiver for t in result.transfers)
    keys = [(t.slot, t.signature, tuple(map(int, t.instruction_path.split(".")))) for t in result.transfers]
    assert keys == sorted(keys)
    order = [(b.first_buy_slot, b.first_buy_signature, b.wallet) for b in result.buyers]
    assert order == sorted(order)


@pytest.mark.parametrize("name", SCENARIOS)
def test_addresses_are_real_pubkeys_and_pda_types_match_the_curve(name):
    result = load_ingest_fixture(name)
    for b in result.buyers:
        on_curve = Pubkey.from_string(b.wallet).is_on_curve()
        assert on_curve == (b.address_type is AddressType.WALLET), b.wallet
    expected = _expected(name)
    for node in expected["graph"]["nodes"]:
        on_curve = Pubkey.from_string(node["address"]).is_on_curve()
        assert on_curve == (node["address_type"] == "wallet"), node["address"]


# --- T-026: еталон не походить із коду, що тестується ---------------------------------------------------

STDLIB_OK = {"__future__", "dataclasses", "hashlib", "json", "sys", "pathlib", "collections", "itertools",
             "typing", "decimal", "re", "math", "functools"}


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative import in generator"
            out.add((node.module or "").split(".")[0])
    return out


def test_expected_prune_records_are_derived_by_oracle_not_by_code():
    source = BUILDER.read_text(encoding="utf-8")
    assert source.lstrip().startswith("# trace: ignore-file")
    modules = _imported_modules(BUILDER)
    assert "unmask" not in modules
    assert modules <= STDLIB_OK | {"solders"}, modules - STDLIB_OK - {"solders"}
    # не обходиться динамічним імпортом чи рядковими посиланнями на код, що тестується
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", getattr(node.func, "attr", ""))
            assert name not in {"__import__", "import_module", "exec", "eval"}, name
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert not node.value.startswith("unmask."), node.value
    # оракул справді є в генераторі: перебір вершин, а не виклик відсікання
    funcs = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert {"aggregate_edges", "oracle_measures", "oracle_hits", "bfs_components"} <= funcs


def test_generator_runs_without_unmask_on_the_import_path(tmp_path):
    """Генератор виконується в чистому інтерпретаторі й не підтягує `unmask` у `sys.modules`."""
    import subprocess
    import sys

    env = {"PYTHONPATH": "", "PATH": "/usr/bin:/bin"}
    code = (
        "import sys, importlib.util;"
        f"spec = importlib.util.spec_from_file_location('g', {str(BUILDER)!r});"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m);"
        "assert 'unmask' not in sys.modules; assert len(m.build_all()) == 24"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, cwd=tmp_path)
    assert done.returncode == 0, done.stderr


def test_oracle_threshold_rule_is_strictly_greater_at_both_sides(builder):
    """Оракул генератора сам перевіряється на малих графах: рівно поріг — не хаб (`>`, не `>=`)."""
    th = dict(degree_threshold=3, one_off_senders_share=0.8, one_off_min_senders=10, giant_component_warn_share=0.5,
              prune_off_curve=True, prune_ingest_high_degree=True, dust_amount_lamports=DUST_T, dust_min_fanout=5)
    node = dict(address="X", address_type="wallet", unexpanded=None)

    def hits(measures, **over):
        return [h["criterion"] for h in builder.oracle_hits(node, measures, {**th, **over}, None, 200)]

    def m(degree=0, senders=0, one_off=0):
        return dict(degree=degree, unique_senders=senders, one_off_senders=one_off,
                    one_off_share=None if senders == 0 else one_off / senders, buyer_fanout=0, median_to_buyers=None)

    assert hits(m(degree=2)) == [] and hits(m(degree=3)) == [] and hits(m(degree=4)) == ["degree"]
    assert hits(m(senders=10, one_off=8)) == []  # рівно 0.8
    assert hits(m(senders=10, one_off=9)) == ["one_off_senders"]
    assert hits(m(senders=9, one_off=9)) == []  # передумова: ≥ 10 включно
    assert hits(m(senders=10, one_off=10)) == ["one_off_senders"]
    assert hits(m(senders=10, one_off=7)) == []
    # поріг частки береться з конфігу, а не з константи
    assert hits(m(senders=10, one_off=6), one_off_senders_share=0.5) == ["one_off_senders"]
    assert hits(m(degree=5), degree_threshold=5) == [] and hits(m(degree=6), degree_threshold=5) == ["degree"]


def test_oracle_known_list_off_curve_and_ingest_rules(builder):
    th = dict(degree_threshold=100, one_off_senders_share=0.8, one_off_min_senders=10, giant_component_warn_share=0.5,
              prune_off_curve=True, prune_ingest_high_degree=True, dust_amount_lamports=DUST_T, dust_min_fanout=5)
    zero = dict(degree=0, unique_senders=0, one_off_senders=0, one_off_share=None, buyer_fanout=0, median_to_buyers=None)
    lists = {"version": 7, "categories": {"launchpads": ["L"], "exchanges": []}}

    def node(address="X", address_type="wallet", unexpanded=None):
        return dict(address=address, address_type=address_type, unexpanded=unexpanded)

    listed = builder.oracle_hits(node("L"), zero, th, lists, 200)
    assert listed == [dict(criterion="known_list", measured=None, threshold=None, detail="list:launchpads", lists_version=7)]
    assert builder.oracle_hits(node("L"), zero, th, None, 200) == []  # списку немає — не застосовано
    pda = builder.oracle_hits(node(address_type="off_curve"), zero, th, lists, 200)
    assert pda == [dict(criterion="known_list", measured=None, threshold=None, detail="address_type:off_curve", lists_version=None)]
    assert builder.oracle_hits(node(address_type="off_curve"), zero, {**th, "prune_off_curve": False}, lists, 200) == []
    high = dict(reason="high_degree", counterparties_seen=201, signatures_seen=5, signatures_truncated=False)
    assert builder.oracle_hits(node(unexpanded=high), zero, th, lists, 200) == [
        dict(criterion="ingest_high_degree", measured=201, threshold=200, detail="unexpanded:high_degree", lists_version=None)]
    assert builder.oracle_hits(node(unexpanded=high), zero, {**th, "prune_ingest_high_degree": False}, lists, 200) == []
    cap = dict(reason="signature_cap", counterparties_seen=2, signatures_seen=300, signatures_truncated=True)
    assert builder.oracle_hits(node(unexpanded=cap), zero, th, lists, 200) == []
    # усі чотири одночасно, упорядковано за значенням criterion
    many = builder.oracle_hits(node("L", "off_curve", high), dict(
        degree=101, unique_senders=10, one_off_senders=10, one_off_share=1.0, buyer_fanout=5, median_to_buyers=DUST_T - 1),
        th, lists, 200)
    assert [h["criterion"] for h in many] == [
        "degree", "dust_fanout", "ingest_high_degree", "known_list", "known_list", "one_off_senders"]
    assert [h["detail"] for h in many if h["criterion"] == "known_list"] == ["address_type:off_curve", "list:launchpads"]


def test_oracle_warnings_boundaries(builder):
    th = {"giant_component_warn_share": 0.5}

    def snap(share, total=10):
        return {"largest_component_buyer_share": share, "buyers_total": total}

    def w(share, *, total=10, applied=True, src_before=1, src_after=1):
        return builder.oracle_warnings(snap(1.0, total), snap(share, total), th, lists_applied=applied,
                                       sources_before=src_before, sources_after=src_after)

    assert w(0.4) == [] and w(0.5) == [] and w(0.6) == ["giant_component"]  # рівно поріг — не попередження
    assert w(0.0, total=0) == ["empty_graph"]
    assert w(0.1, applied=False) == ["address_lists_not_applied"]
    assert w(0.1, src_before=3, src_after=0) == ["all_sources_pruned"]
    assert w(0.1, src_before=0, src_after=0) == []  # джерел не було — нічого й не відсічено
    assert w(0.1, src_before=3, src_after=1) == []
    assert w(0.9, total=0, applied=False, src_before=2, src_after=0) == [
        "address_lists_not_applied", "all_sources_pruned", "empty_graph", "giant_component"]


def test_oracle_measures_sum_transfer_counts_across_assets_per_sender(builder):
    def edge(sender, asset, count, receiver="R"):
        return dict(kind="transfer", sender=sender, receiver=receiver, asset=asset, count=count, amount=1)

    edges = [edge("S1", "sol", 1), edge("S2", "sol", 1), edge("S2", "spl:X", 1), edge("S3", "sol", 2),
             edge("R", "sol", 1, receiver="T")]
    dust0 = dict(buyer_fanout=0, median_to_buyers=None)  # покупців немає — виміри пилу порожні
    m = builder.oracle_measures("R", edges, set())
    assert m == dict(degree=4, unique_senders=3, one_off_senders=1, one_off_share=1 / 3, **dust0)
    assert builder.oracle_measures("T", edges, set()) == dict(
        degree=1, unique_senders=1, one_off_senders=1, one_off_share=1.0, **dust0)
    assert builder.oracle_measures("Z", edges, set()) == dict(
        degree=0, unique_senders=0, one_off_senders=0, one_off_share=None, **dust0)


def test_oracle_dust_rule_is_strictly_less_and_fanout_precondition_inclusive(builder):
    """`dust_fanout`: медіана СТРОГО менша за поріг, fan-out >= передумови включно (research R-22, FR-002-14)."""
    th = dict(degree_threshold=100, one_off_senders_share=0.8, one_off_min_senders=10, giant_component_warn_share=0.5,
              prune_off_curve=True, prune_ingest_high_degree=True, dust_amount_lamports=DUST_T, dust_min_fanout=5)
    node = dict(address="X", address_type="wallet", unexpanded=None)

    def hits(fanout, median, **over):
        measures = dict(degree=fanout, unique_senders=0, one_off_senders=0, one_off_share=None,
                        buyer_fanout=fanout, median_to_buyers=median)
        return builder.oracle_hits(node, measures, {**th, **over}, None, 200)

    for fanout in (4, 5, 6):
        for median, below in ((DUST_T - 1, True), (DUST_T, False), (DUST_T + 1, False)):
            got = [h["criterion"] for h in hits(fanout, median)]
            assert got == (["dust_fanout"] if below and fanout >= 5 else []), (fanout, median)
    (hit,) = hits(5, DUST_T - 1)
    assert hit == dict(criterion="dust_fanout", measured=DUST_T - 1, threshold=DUST_T, detail="measured",
                       lists_version=None)
    # поріг і передумова беруться з конфігу, а не з констант генератора
    assert hits(2, 9, dust_amount_lamports=10, dust_min_fanout=2) != []
    assert hits(2, 10, dust_amount_lamports=10, dust_min_fanout=2) == []
    assert hits(1, 9, dust_amount_lamports=10, dust_min_fanout=2) == []
    assert hits(50, 1, dust_amount_lamports=1) == []  # `dust_amount_lamports: 1` вимикає критерій (ребро >= 1)
    assert hits(4, 1, dust_min_fanout=4) != [] and hits(4, 1, dust_min_fanout=5) == []


def test_oracle_median_is_upper_median_of_per_buyer_sol_edge_sums_ignoring_spl_and_non_buyers(builder):
    def tr(i, sender, receiver, amount, asset="sol"):
        return dict(sender=sender, receiver=receiver, asset=asset, amount=amount, decimals=None if asset == "sol" else 6,
                    slot=i, signature=f"sig{i}", instruction_path="0", block_time=1000 + i)

    def measures(transfers, buyers, address="V"):
        return builder.oracle_measures(address, builder.aggregate_edges(transfers), set(buyers))

    # парне n: верхня медіана `sorted[n // 2]` (нижня дала б 20)
    four = [tr(i, "V", f"b{i}", 10 * i) for i in (1, 2, 3, 4)]
    assert measures(four, {"b1", "b2", "b3", "b4"}) == dict(
        degree=4, unique_senders=0, one_off_senders=0, one_off_share=None, buyer_fanout=4, median_to_buyers=30)
    # непарне n: середній елемент
    assert measures(four[:3], {"b1", "b2", "b3"})["median_to_buyers"] == 20
    # два (й більше) перекази одному покупцю — одна сума ребра, а не два значення: [3, 10, 20] -> 10, не [1,1,1,10,20] -> 1
    repeated = [tr(1, "V", "b1", 1), tr(2, "V", "b1", 1), tr(3, "V", "b1", 1), tr(4, "V", "b2", 10), tr(5, "V", "b3", 20)]
    m = measures(repeated, {"b1", "b2", "b3"})
    assert (m["buyer_fanout"], m["median_to_buyers"]) == (3, 10)
    # один великий переказ серед пилу медіану не зрушує (максимум/середнє були б 5 SOL і ~263 000)
    amounts = [500_000] * 18 + [5 * 10**9]
    big = [tr(i, "V", f"b{i}", a) for i, a in enumerate(amounts)]
    m = measures(big, {f"b{i}" for i in range(19)})
    assert (m["buyer_fanout"], m["median_to_buyers"]) == (19, 500_000)
    # SPL не рахується ні у fan-out, ні в медіані: лише-SPL вершина — fan-out 0, медіана None
    spl_only = [tr(i, "V", f"b{i}", 10**12, asset="spl:USDC") for i in range(5)]
    m = measures(spl_only, {f"b{i}" for i in range(5)})
    assert (m["buyer_fanout"], m["median_to_buyers"], m["degree"]) == (0, None, 5)
    # SOL і SPL одному покупцю: рахується лише SOL-ребро
    mixed_assets = [tr(1, "V", "b1", 100), tr(2, "V", "b1", 10**12, asset="spl:USDC"), tr(3, "V", "b2", 300)]
    m = measures(mixed_assets, {"b1", "b2"})
    assert (m["buyer_fanout"], m["median_to_buyers"]) == (2, 300)
    # не-покупці не рахуються
    to_others = [tr(1, "V", "b1", 100), tr(2, "V", "n1", 1), tr(3, "V", "n2", 1), tr(4, "V", "n3", 1)]
    m = measures(to_others, {"b1"})
    assert (m["buyer_fanout"], m["median_to_buyers"]) == (1, 100)
    # вхідні ребра покупця (його фінансують) не рахуються як fan-out отримувача
    incoming = [tr(1, "S", "V", 5), tr(2, "S2", "V", 7)]
    m = measures(incoming, {"V"})
    assert (m["buyer_fanout"], m["median_to_buyers"]) == (0, None)
    # вершина без ребер
    assert measures([], set(), "Z") == dict(degree=0, unique_senders=0, one_off_senders=0, one_off_share=None,
                                            buyer_fanout=0, median_to_buyers=None)


def test_oracle_depth_is_minimum_over_both_rules(builder):
    def tr(sender, receiver, depth):
        return dict(sender=sender, receiver=receiver, depth=depth)

    # лише правило отримувача: Y нічого не надсилає, переказ глибини 2 дає Y глибину 1 (а не 2)
    assert builder.oracle_depths(["B", "X", "Y"], {"B"}, [tr("X", "Y", 2)]) == {"B": 0, "X": 2, "Y": 1}
    # лише правило відправника: покупець B отримує переказ глибини 1, відправник X — глибина 1
    assert builder.oracle_depths(["B", "X"], {"B"}, [tr("X", "B", 1)]) == {"B": 0, "X": 1}
    # мінімум з кількох шляхів: Z досяжний на глибинах 3 і 2 -> 2
    got = builder.oracle_depths(["B", "Z", "Q"], {"B"}, [tr("Z", "Q", 3), tr("Z", "B", 2)])
    assert got["Z"] == 2 and got["Q"] == 2
    # покупець лишається на 0, навіть якщо його правила дали б більше
    assert builder.oracle_depths(["B", "X"], {"B", "X"}, [tr("B", "X", 3)])["X"] == 0


def test_oracle_components_bfs_handles_isolated_nodes_and_direction(builder):
    edges = [dict(sender="b", receiver="a"), dict(sender="b", receiver="c"), dict(sender="x", receiver="y")]
    comps = builder.bfs_components(["a", "b", "c", "x", "y", "z"], edges)
    assert sorted(map(tuple, comps)) == [("a", "b", "c"), ("x", "y"), ("z",)]


# --- незалежний переобрахунок еталона з ingest.json (інший алгоритм, ніж у генераторі) --------------------


def _recompute(name):
    doc = _ingest(name)
    exp = _expected(name)
    th = exp["config"]["thresholds"]
    lists = exp["config"]["lists"]
    in_threshold = doc["metadata"]["counterparty_threshold"]
    buyers = {b["wallet"]: b for b in doc["buyers"]}
    unexpanded = {u["wallet"]: u for u in doc["unexpanded"]}

    # ребра: ключ (sender, receiver, asset), перекази згруповано Counter-ами
    groups = defaultdict(list)
    for t in doc["transfers"]:
        groups[(t["sender"], t["receiver"], t["asset"])].append(t)
    edges = {}
    for (s, r, a), ts in groups.items():
        ts = sorted(ts, key=lambda t: (t["slot"], t["signature"], [int(p) for p in t["instruction_path"].split(".")]))
        edges[("transfer", s, r, a)] = dict(
            amount=sum(t["amount"] for t in ts), count=len(ts), first_slot=ts[0]["slot"], last_slot=ts[-1]["slot"],
            first_time=ts[0]["block_time"], last_time=ts[-1]["block_time"],
            decimals=ts[0]["decimals"], refs=[(t["signature"], t["slot"], t["instruction_path"]) for t in ts])

    # делеговані купівлі (T-047, R-5): окреме ребро `delegated_buy` за парою (payer, receiver), без активу й суми
    links = doc["delegated"]["links"]
    dgroups = defaultdict(list)
    for link in links:
        dgroups[(link["payer"], link["receiver"])].append(link)
    for (s, r), ls in dgroups.items():
        ls = sorted(ls, key=lambda l: (l["slot"], l["signature"]))
        edges[("delegated_buy", s, r, None)] = dict(
            amount=None, count=len(ls), first_slot=ls[0]["slot"], last_slot=ls[-1]["slot"],
            first_time=ls[0]["block_time"], last_time=ls[-1]["block_time"],
            decimals=None, refs=[(l["signature"], l["slot"], None) for l in ls])

    addresses = (set(buyers) | {t["sender"] for t in doc["transfers"]} | {t["receiver"] for t in doc["transfers"]}
                 | {l["payer"] for l in links} | {l["receiver"] for l in links})

    # виміри
    measures, dust_sums = {}, {}
    for v in addresses:
        counterparties = {k[2] for k in edges if k[1] == v} | {k[1] for k in edges if k[2] == v}
        per_sender = Counter()
        for k, e in edges.items():
            if k[2] == v and k[0] == "transfer":  # delegated_buy відправника не робить (R-8)
                per_sender[k[1]] += e["count"]
        one_off = sum(1 for c in per_sender.values() if c == 1)
        # пил: сума ребра на покупця; верхня медіана стандартною `statistics.median_high` (не `sorted[n // 2]`)
        by_buyer = defaultdict(list)
        for k, e in edges.items():
            if k[1] == v and k[3] == "sol" and k[2] in buyers:
                by_buyer[k[2]].append(e["amount"])
        sums = [sum(parts) for parts in by_buyer.values()]
        measures[v] = dict(degree=len(counterparties), unique_senders=len(per_sender), one_off_senders=one_off,
                           one_off_share=one_off / len(per_sender) if per_sender else None,
                           buyer_fanout=len(sums), median_to_buyers=statistics.median_high(sums) if sums else None)
        dust_sums[v] = sums

    # хіти
    list_index = {a: cat for cat, addrs in lists["categories"].items() for a in addrs} if lists else {}
    hits = {}
    for v in addresses:
        node_type = buyers[v]["address_type"] if v in buyers else (
            "wallet" if Pubkey.from_string(v).is_on_curve() else "off_curve")
        m = measures[v]
        found = set()
        if v in list_index:
            found.add(("known_list", f"list:{list_index[v]}"))
        if th["prune_off_curve"] and node_type == "off_curve":
            found.add(("known_list", "address_type:off_curve"))
        if m["degree"] > th["degree_threshold"]:
            found.add(("degree", "measured"))
        if m["unique_senders"] >= th["one_off_min_senders"] and m["one_off_share"] > th["one_off_senders_share"]:
            found.add(("one_off_senders", "measured"))
        if th["prune_ingest_high_degree"] and unexpanded.get(v, {}).get("reason") == "high_degree":
            found.add(("ingest_high_degree", "unexpanded:high_degree"))
        # пил: «строго більше половини сум менші за поріг» (еквівалент `median_high < поріг`, research R-22)
        sums = dust_sums[v]
        if len(sums) >= th["dust_min_fanout"] and sum(1 for a in sums if a < th["dust_amount_lamports"]) * 2 > len(sums):
            found.add(("dust_fanout", "measured"))
        hits[v] = found

    # компоненти: union-find (у генераторі — BFS)
    parent = {v: v for v in addresses}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def components(alive, alive_edges):
        for v in alive:
            parent[v] = v
        for _, s, r, _ in alive_edges:
            parent[find(s)] = find(r)
        groups_ = defaultdict(set)
        for v in alive:
            groups_[find(v)].add(v)
        return list(groups_.values())

    def snapshot(alive, alive_edges):
        comps = components(alive, alive_edges)
        total = len(buyers)
        best = max((len(c & set(buyers)) for c in comps), default=0)
        touched = {s for _, s, _, _ in alive_edges} | {r for _, _, r, _ in alive_edges}
        return dict(nodes=len(alive), edges=len(alive_edges), components=len(comps), buyers_total=total,
                    buyers_in_largest_component=best, largest_component_buyer_share=best / total if total else 0.0,
                    isolated_buyers=len(set(buyers) - touched))

    return dict(edges=edges, measures=measures, hits=hits, buyers=buyers, addresses=addresses,
                snapshot=snapshot, thresholds=th, in_threshold=in_threshold)


@pytest.mark.parametrize("name", SCENARIOS)
def test_expected_edges_equal_independent_aggregation_of_ingest_transfers(name):
    r = _recompute(name)
    exp = _expected(name)
    got = {(e["kind"], e["sender"], e["receiver"], e["asset"]): e for e in exp["graph"]["edges"]}
    assert set(got) == set(r["edges"])
    assert len(got) == len(exp["graph"]["edges"])
    for key, e in r["edges"].items():
        g = got[key]
        for field in ("amount", "count", "first_slot", "last_slot", "first_time", "last_time", "decimals"):
            assert g[field] == e[field], (key, field)
        assert [(x["signature"], x["slot"], x["instruction_path"]) for x in g["refs"]] == e["refs"], key
        assert g["count"] == len(g["refs"])
    order = [(e["kind"], e["sender"], e["receiver"], e["asset"] or "") for e in exp["graph"]["edges"]]
    assert order == sorted(order)


@pytest.mark.parametrize("name", SCENARIOS)
def test_expected_nodes_and_measures_equal_independent_recount(name):
    r = _recompute(name)
    exp = _expected(name)
    nodes = {n["address"]: n for n in exp["graph"]["nodes"]}
    assert set(nodes) == r["addresses"]
    assert [n["address"] for n in exp["graph"]["nodes"]] == sorted(nodes)
    for address, n in nodes.items():
        assert n["measures"] == r["measures"][address], address
        assert ("buyer" in n["roles"]) == (address in r["buyers"])
        assert (n["buyer_rank"] is not None) == (address in r["buyers"])
        if address in r["buyers"]:
            assert n["buyer_rank"] == r["buyers"][address]["rank"] and n["depth"] == 0
        assert n["roles"] == sorted(n["roles"]) and n["roles"]
    funders = {k[1] for k in r["edges"] if k[0] == "transfer"}
    assert {a for a, n in nodes.items() if "funder" in n["roles"]} == funders
    payers = {k[1] for k in r["edges"] if k[0] == "delegated_buy"}
    receivers = {k[2] for k in r["edges"] if k[0] == "delegated_buy"}
    assert {a for a, n in nodes.items() if "delegated_payer" in n["roles"]} == payers
    assert {a for a, n in nodes.items() if "delegated_receiver" in n["roles"]} == receivers
    assert all(set(n["roles"]) <= {"buyer", "funder", "delegated_payer", "delegated_receiver"} for n in nodes.values())
    # depth: мінімум за правилами R-6, перераховано з переказів і делегованих зв'язків входу
    doc = _ingest(name)
    best = {a: (0 if a in r["buyers"] else 99) for a in r["addresses"]}
    for t in doc["transfers"]:
        best[t["receiver"]] = min(best[t["receiver"]], t["depth"] - 1)
        best[t["sender"]] = min(best[t["sender"]], t["depth"])
    for link in doc["delegated"]["links"]:
        best[link["receiver"]] = min(best[link["receiver"]], 0)
        best[link["payer"]] = min(best[link["payer"]], 1)
    assert {a: n["depth"] for a, n in nodes.items()} == best
    # кандидати без пари вершинами не стають, якщо іншої підстави немає
    assert not ({u["wallet"] for u in doc["delegated"]["unpaired"]} - r["addresses"]) & set(nodes)


@pytest.mark.parametrize("name", SCENARIOS)
def test_expected_hits_equal_independent_recount(name):
    r = _recompute(name)
    exp = _expected(name)
    got = {}
    for rec in exp["prune"]["records"]:
        got[rec["address"]] = {(h["criterion"], h["detail"]) for h in rec["criteria"]}
        assert rec["address"] not in r["buyers"], "покупця відсічено"
    for flag in exp["prune"]["buyer_flags"]:
        assert flag["address"] in r["buyers"]
        got[flag["address"]] = {(h["criterion"], h["detail"]) for h in flag["criteria"]}
    assert got == {a: h for a, h in r["hits"].items() if h}
    th = r["thresholds"]
    for rec in exp["prune"]["records"] + exp["prune"]["buyer_flags"]:
        for h in rec["criteria"]:
            if h["criterion"] == "degree":
                assert (h["measured"], h["threshold"]) == (r["measures"][rec["address"]]["degree"], th["degree_threshold"])
                assert h["measured"] > h["threshold"]
            if h["criterion"] == "one_off_senders":
                assert h["threshold"] == th["one_off_senders_share"] and h["measured"] > h["threshold"]
            if h["criterion"] == "ingest_high_degree":
                assert h["threshold"] == r["in_threshold"] and h["measured"] > h["threshold"]
            if h["criterion"] == "dust_fanout":
                m = r["measures"][rec["address"]]
                assert h["threshold"] == th["dust_amount_lamports"] and h["detail"] == "measured"
                assert h["measured"] == m["median_to_buyers"] and h["measured"] < h["threshold"]  # строго менше
                assert m["buyer_fanout"] >= th["dust_min_fanout"]  # передумова включно, видима в measures запису
            if h["criterion"] == "known_list":
                assert h["measured"] is None and h["threshold"] is None
        keys = [(h["criterion"], h["detail"]) for h in rec["criteria"]]
        assert keys == sorted(keys) and keys
        assert rec["measures"] == r["measures"][rec["address"]]
    assert [x["address"] for x in exp["prune"]["records"]] == sorted(x["address"] for x in exp["prune"]["records"])
    assert [x["buyer_rank"] for x in exp["prune"]["buyer_flags"]] == sorted(x["buyer_rank"] for x in exp["prune"]["buyer_flags"])


@pytest.mark.parametrize("name", SCENARIOS)
def test_expected_record_incident_edges_are_exactly_edges_touching_the_hub(name):
    r = _recompute(name)
    exp = _expected(name)
    edges = {(e["kind"], e["sender"], e["receiver"], e["asset"]): e for e in exp["graph"]["edges"]}
    for rec in exp["prune"]["records"]:
        touching = {k for k in edges if rec["address"] in (k[1], k[2])}
        assert {(e["kind"], e["sender"], e["receiver"], e["asset"]) for e in rec["incident_edges"]} == touching
        for e in rec["incident_edges"]:
            assert e == edges[(e["kind"], e["sender"], e["receiver"], e["asset"])]
        assert rec["measures"] == r["measures"][rec["address"]]
        assert (rec["config_version"], rec["lists_version"]) == (exp["prune"]["config_version"], exp["prune"]["lists_version"])


@pytest.mark.parametrize("name", SCENARIOS)
def test_expected_report_equals_independent_union_find(name):
    r = _recompute(name)
    exp = _expected(name)
    hubs = {rec["address"] for rec in exp["prune"]["records"]}
    all_edges = list(r["edges"])
    after_edges = [k for k in all_edges if k[1] not in hubs and k[2] not in hubs]
    assert exp["report"]["before"] == r["snapshot"](r["addresses"], all_edges)
    assert exp["report"]["after"] == r["snapshot"](r["addresses"] - hubs, after_edges)
    assert exp["report"]["pruned_nodes"] == len(hubs)
    assert exp["report"]["pruned_edges"] == len(all_edges) - len(after_edges)
    assert exp["report"]["warn_share"] == r["thresholds"]["giant_component_warn_share"]
    # граф після відсікання: усі покупці лишаються (SC-003)
    assert set(r["buyers"]) <= set(exp["prune"]["after"]["node_addresses"])
    assert set(exp["prune"]["after"]["node_addresses"]) == r["addresses"] - hubs
    # edge_keys у еталоні: [kind, sender, receiver, asset або ""] (у delegated_buy активу немає)
    assert {tuple(k) for k in exp["prune"]["after"]["edge_keys"]} == {(k, s, r, a or "") for k, s, r, a in after_edges}
    # компоненти: розбиття збігається з union-find
    for side, alive, alive_edges in (("before", r["addresses"], all_edges), ("after", r["addresses"] - hubs, after_edges)):
        listed = exp["components"][side]
        got = sorted(tuple(c["members"]) for c in listed)
        assert got == sorted(tuple(sorted(c)) for c in _union_components(alive, alive_edges))
        for c in listed:
            assert c["id"] == min(c["members"]) and c["buyers"] == len(set(c["members"]) & set(r["buyers"]))
        assert [c["id"] for c in listed] == sorted(c["id"] for c in listed)


def _union_components(alive, alive_edges):
    parent = {v: v for v in alive}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for _, s, rcv, _ in alive_edges:
        parent[find(s)] = find(rcv)
    out = defaultdict(set)
    for v in alive:
        out[find(v)].add(v)
    return list(out.values())


@pytest.mark.parametrize("name", SCENARIOS)
def test_expected_warnings_follow_the_documented_rules(name):
    exp = _expected(name)
    rep = exp["report"]
    warnings = set(rep["warnings"])
    assert len(rep["warnings"]) == len(warnings)
    assert rep["warnings"] == sorted(rep["warnings"])
    assert ("giant_component" in warnings) == (rep["after"]["largest_component_buyer_share"] > rep["warn_share"])
    assert ("empty_graph" in warnings) == (rep["before"]["buyers_total"] == 0)
    assert ("address_lists_not_applied" in warnings) == (exp["config"]["lists"] is None)
    non_buyers_before = [n for n in exp["graph"]["nodes"] if "buyer" not in n["roles"]]
    pruned_all = bool(non_buyers_before) and len(exp["prune"]["records"]) == len(non_buyers_before)
    assert ("all_sources_pruned" in warnings) == pruned_all
    assert "delegated_incomplete" not in warnings  # поле `delegated` у 001 з'явиться з T-044
    assert warnings <= {"address_lists_not_applied", "giant_component", "empty_graph", "all_sources_pruned"}


@pytest.mark.parametrize("name", SCENARIOS)
def test_expected_completeness_mirrors_ingest_document(name):
    doc = _ingest(name)
    comp = _expected(name)["completeness"]
    assert comp["ingest_status"] == doc["completeness"]["status"]
    assert comp["missing"] == doc["completeness"]["missing"]
    assert comp["buyers_complete"] == doc["completeness"]["buyers"]["complete"]
    assert comp["buyers_reason"] == doc["completeness"]["buyers"]["reason"]
    assert comp["status"] == ("complete" if comp["ingest_status"] == "complete" else "incomplete")


@pytest.mark.parametrize("name", SCENARIOS)
def test_expected_config_carries_explicit_thresholds_and_lists(name):
    exp = _expected(name)
    th = exp["config"]["thresholds"]
    assert set(th) == THRESHOLD_KEYS and len(th) == 8
    assert th["dust_amount_lamports"] == DUST_T and th["dust_min_fanout"] == 5
    assert isinstance(th["dust_amount_lamports"], int) and isinstance(th["dust_min_fanout"], int)
    assert exp["config"]["version"] == 2
    assert exp["prune"]["config_version"] == 2
    assert {rec["config_version"] for rec in exp["prune"]["records"]} <= {2}
    lists = exp["config"]["lists"]
    assert lists["version"] == 1
    assert set(lists["categories"]) == {"system_programs", "token_programs", "dex_routers", "amm_programs",
                                        "launchpads", "exchanges", "market_makers"}
    assert lists["categories"]["exchanges"] == [] and lists["categories"]["market_makers"] == []
    assert exp["ingest_counterparty_threshold"] == _ingest(name)["metadata"]["counterparty_threshold"]
    assert exp["mint"] == _ingest(name)["metadata"]["mint"]


# --- вимоги рядка T-026: зміст сценаріїв ---------------------------------------------------------------------


def _edge(exp, sender, receiver, asset="sol"):
    labels = exp["wallets"]
    found = [e for e in exp["graph"]["edges"]
             if (e["sender"], e["receiver"], e["asset"]) == (labels[sender], labels[receiver], asset)]
    assert len(found) <= 1
    return found[0] if found else None


def test_g_basic_declares_the_shape_of_001_basic():
    exp, doc = _expected("g_basic"), _ingest("g_basic")
    w = exp["wallets"]
    assert doc["metadata"]["wallets_analyzed"] == 5 and len(doc["buyers"]) == 5
    assert exp["prune"]["records"] == [] and exp["prune"]["buyer_flags"] == []
    for s, r in (("A", "P1"), ("A", "P2"), ("B", "A"), ("C", "B"), ("A", "D"), ("D", "A")):
        assert _edge(exp, s, r) is not None, (s, r)
    # покупець P3 фінансує покупця P4: одна вершина з двома ролями
    p3 = next(n for n in exp["graph"]["nodes"] if n["address"] == w["P3"])
    assert p3["roles"] == ["buyer", "funder"] and p3["depth"] == 0 and p3["buyer_rank"] == 3
    assert _edge(exp, "P3", "P4") is not None
    # цикл A<->D: обидва напрямки — окремі ребра
    assert _edge(exp, "A", "D")["sender"] != _edge(exp, "D", "A")["sender"]
    # кілька переказів однієї пари й активу -> одне ребро із сумою, лічильником і всіма посиланнями
    multi = _edge(exp, "A", "P2")
    assert multi["count"] >= 2 and multi["count"] == len(multi["refs"])
    assert multi["amount"] == sum(
        t["amount"] for t in doc["transfers"]
        if (t["sender"], t["receiver"], t["asset"]) == (w["A"], w["P2"], "sol"))
    assert multi["first_slot"] < multi["last_slot"] and multi["first_time"] < multi["last_time"]
    assert len({r["signature"] for r in multi["refs"]}) < len(multi["refs"]) or len(multi["refs"]) >= 2
    # SOL і spl:* між тією самою парою -> два ребра
    pair = [e for e in exp["graph"]["edges"] if (e["sender"], e["receiver"]) == (w["A"], w["P2"])]
    assert sorted(e["asset"] for e in pair) == sorted(["sol", f"spl:{w['USDC']}"])
    spl = next(e for e in pair if e["asset"] != "sol")
    assert spl["decimals"] == 6 and next(e for e in pair if e["asset"] == "sol")["decimals"] is None
    # P5 — покупець без фінансування: ізольований
    assert exp["report"]["before"]["isolated_buyers"] == 1


def test_g_hub_declares_pre_share_above_0_9():
    exp, doc = _expected("g_hub"), _ingest("g_hub")
    before, after = exp["report"]["before"], exp["report"]["after"]
    assert before["buyers_total"] == 20 and len(doc["buyers"]) == 20
    assert before["largest_component_buyer_share"] >= 0.9
    assert after["largest_component_buyer_share"] < 0.5
    assert after["largest_component_buyer_share"] < exp["report"]["warn_share"]
    assert "giant_component" not in exp["report"]["warnings"]
    w = exp["wallets"]
    assert [r["address"] for r in exp["prune"]["records"]] == [w["H"]]
    rec = exp["prune"]["records"][0]
    assert [h["criterion"] for h in rec["criteria"]] == ["one_off_senders"]  # лише одноразові відправники
    funded_by_h = {e["receiver"] for e in exp["graph"]["edges"] if e["sender"] == w["H"]}
    assert len(funded_by_h) == 18
    one_off = [e for e in exp["graph"]["edges"] if e["receiver"] == w["H"] and e["count"] == 1]
    assert len(one_off) >= 30
    assert rec["measures"]["one_off_senders"] == 30 and rec["measures"]["unique_senders"] == 32
    assert rec["measures"]["one_off_share"] == 30 / 32  # S31 (2 перекази) і S32 (SOL + spl = 2) не одноразові
    f_edges = {e["receiver"] for e in exp["graph"]["edges"] if e["sender"] == w["F"]}
    assert len(f_edges) == 3
    assert w["F"] in exp["prune"]["after"]["node_addresses"]  # F лишається
    assert rec["measures"]["degree"] <= exp["config"]["thresholds"]["degree_threshold"]


def test_g_known_has_launchpad_source_off_curve_source_and_empty_exchanges():
    exp = _expected("g_known")
    hits = {r["address"]: r["criteria"] for r in exp["prune"]["records"]}
    assert PUMP in exp["config"]["lists"]["categories"]["launchpads"]
    assert exp["config"]["lists"]["categories"]["exchanges"] == []
    assert hits[PUMP] == [dict(criterion="known_list", measured=None, threshold=None,
                               detail="list:launchpads", lists_version=1)]
    pda = exp["wallets"]["PDA_SRC"]
    assert hits[pda] == [dict(criterion="known_list", measured=None, threshold=None,
                              detail="address_type:off_curve", lists_version=None)]
    assert not Pubkey.from_string(pda).is_on_curve()
    assert len(hits) == 2  # звичайний фінансувальник не відсічений
    assert exp["wallets"]["F1"] in exp["prune"]["after"]["node_addresses"]


def test_g_buyer_hub_buyer_meets_all_five_criteria_and_is_flagged_not_pruned():
    exp, doc = _expected("g_buyer_hub"), _ingest("g_buyer_hub")
    x = exp["wallets"]["X"]
    assert exp["prune"]["records"] == []
    assert [f["address"] for f in exp["prune"]["buyer_flags"]] == [x]
    flag = exp["prune"]["buyer_flags"][0]
    assert [(h["criterion"], h["detail"]) for h in flag["criteria"]] == [
        ("degree", "measured"), ("dust_fanout", "measured"), ("ingest_high_degree", "unexpanded:high_degree"),
        ("known_list", "address_type:off_curve"), ("one_off_senders", "measured")]
    # покупець X пилить 89 інших покупців: суми X -> Y* по 500 000 лампортів (< 1 000 000)
    assert flag["measures"] == dict(degree=101, unique_senders=12, one_off_senders=12, one_off_share=1.0,
                                    buyer_fanout=89, median_to_buyers=500_000)
    dust = next(h for h in flag["criteria"] if h["criterion"] == "dust_fanout")
    assert (dust["measured"], dust["threshold"]) == (500_000, DUST_T)
    assert {e["amount"] for e in exp["graph"]["edges"] if e["sender"] == x} == {500_000}
    assert flag["buyer_rank"] == next(b["rank"] for b in doc["buyers"] if b["wallet"] == x)
    assert x in exp["prune"]["after"]["node_addresses"]
    assert exp["report"]["pruned_nodes"] == 0 and exp["report"]["before"] == exp["report"]["after"]
    assert "giant_component" in exp["report"]["warnings"]


def test_g_incomplete_has_one_missing_entry_and_complete_buyers():
    exp, doc = _expected("g_incomplete"), _ingest("g_incomplete")
    assert len(doc["completeness"]["missing"]) == 1
    assert doc["completeness"]["buyers"] == {"complete": True, "reason": None, "detail": ""}
    assert doc["completeness"]["status"] == "incomplete"
    assert exp["completeness"]["status"] == "incomplete" and exp["completeness"]["buyers_complete"] is True
    assert MissingReason(doc["completeness"]["missing"][0]["reason"])


def test_g_empty_has_zero_buyers_and_empty_graph_warning():
    exp, doc = _expected("g_empty"), _ingest("g_empty")
    assert doc["buyers"] == [] and doc["transfers"] == [] and doc["unexpanded"] == []
    assert doc["metadata"]["wallets_analyzed"] == 0
    assert exp["graph"] == {"nodes": [], "edges": []}
    assert exp["report"]["warnings"] == ["empty_graph"]
    assert exp["report"]["before"]["largest_component_buyer_share"] == 0.0
    assert exp["report"]["before"] == exp["report"]["after"]
    assert exp["report"]["before"]["components"] == 0 and exp["prune"]["records"] == []


def test_g_all_hubs_every_non_buyer_matches_a_criterion():
    exp = _expected("g_all_hubs")
    non_buyers = {n["address"] for n in exp["graph"]["nodes"] if "buyer" not in n["roles"]}
    assert non_buyers and non_buyers == {r["address"] for r in exp["prune"]["records"]}
    assert "all_sources_pruned" in exp["report"]["warnings"]
    assert exp["report"]["after"]["nodes"] == exp["report"]["before"]["buyers_total"]
    kinds = {h["criterion"] + ":" + h["detail"] for r in exp["prune"]["records"] for h in r["criteria"]}
    assert kinds == {"known_list:list:launchpads", "known_list:address_type:off_curve", "degree:measured",
                     "ingest_high_degree:unexpanded:high_degree"}
    assert all(len(r["criteria"]) == 1 for r in exp["prune"]["records"])  # кожен — рівно за одним критерієм


def test_g_unexpanded_has_one_high_degree_and_one_signature_cap_vertex():
    exp, doc = _expected("g_unexpanded"), _ingest("g_unexpanded")
    reasons = sorted(u["reason"] for u in doc["unexpanded"])
    assert reasons == sorted(r.value for r in UnexpandedReason)
    marks = {n["address"]: n["unexpanded"] for n in exp["graph"]["nodes"] if n["unexpanded"]}
    assert {m["reason"] for m in marks.values()} == {"high_degree", "signature_cap"}
    pruned = {r["address"]: r for r in exp["prune"]["records"]}
    high = next(a for a, m in marks.items() if m["reason"] == "high_degree")
    cap = next(a for a, m in marks.items() if m["reason"] == "signature_cap")
    assert set(pruned) == {high}
    assert [h["criterion"] for h in pruned[high]["criteria"]] == ["ingest_high_degree"]
    assert cap in exp["prune"]["after"]["node_addresses"]  # signature_cap — не хаб
    assert marks[high]["counterparties_seen"] > exp["ingest_counterparty_threshold"]
    assert marks[cap]["signatures_truncated"] is True


def test_every_criterion_of_the_five_fires_alone_somewhere_in_the_fixtures():
    seen = defaultdict(set)
    for name in SCENARIOS:
        for rec in _expected(name)["prune"]["records"] + _expected(name)["prune"]["buyer_flags"]:
            if len(rec["criteria"]) == 1:
                h = rec["criteria"][0]
                seen[h["criterion"]].add(h["detail"])
    assert seen["known_list"] == {"list:launchpads", "address_type:off_curve"}
    assert seen["dust_fanout"] == {"measured"}
    assert set(seen) == {"known_list", "degree", "one_off_senders", "ingest_high_degree", "dust_fanout"}


def _node(exp, label):
    return next(n for n in exp["graph"]["nodes"] if n["address"] == exp["wallets"][label])


def _dust_hit(rec):
    return [h for h in rec["criteria"] if h["criterion"] == "dust_fanout"]


def test_g_dust_D_fires_dust_fanout_alone_E_stays_and_share_drops_below_warn():
    exp, doc = _expected("g_dust"), _ingest("g_dust")
    w = exp["wallets"]
    assert len(doc["buyers"]) == 20
    assert [r["address"] for r in exp["prune"]["records"]] == [w["D"]]
    assert exp["prune"]["buyer_flags"] == []
    d = exp["prune"]["records"][0]
    # D: единий критерій — пил; fan-out 19 (B01…B19), медіана — пил, один переказ на 5 SOL її не зрушив
    assert d["criteria"] == [dict(criterion="dust_fanout", measured=500_000, threshold=DUST_T, detail="measured",
                                  lists_version=None)]
    assert d["measures"] == dict(degree=21, unique_senders=2, one_off_senders=2, one_off_share=1.0,
                                 buyer_fanout=19, median_to_buyers=500_000)
    assert d["measures"]["unique_senders"] < exp["config"]["thresholds"]["one_off_min_senders"]  # one_off не застосовний
    d_to_buyers = sorted(e["amount"] for e in exp["graph"]["edges"]
                         if e["sender"] == w["D"] and e["receiver"] in {b["wallet"] for b in doc["buyers"]})
    assert d_to_buyers == [500_000] * 18 + [5 * 10**9]
    assert len(d["incident_edges"]) == 19 + 2
    # E: fan-out 4 = поріг − 1 — передумова не виконана, лишається, хоча медіана така ж пилова
    e_node = _node(exp, "E")
    assert e_node["measures"]["buyer_fanout"] == 4 and e_node["measures"]["median_to_buyers"] == 500_000
    assert w["E"] in exp["prune"]["after"]["node_addresses"]
    # F: справжній фінансист трьох покупців (B01, B19, B20 по 2 SOL), лишається
    f_node = _node(exp, "F")
    assert f_node["measures"]["buyer_fanout"] == 3 and f_node["measures"]["median_to_buyers"] == 2 * 10**9
    assert w["F"] in exp["prune"]["after"]["node_addresses"]
    before, after = exp["report"]["before"], exp["report"]["after"]
    assert before["buyers_total"] == 20 and before["largest_component_buyer_share"] >= 0.9
    assert after["largest_component_buyer_share"] == 0.2
    assert after["largest_component_buyer_share"] < 0.5 <= exp["report"]["warn_share"]
    assert exp["report"]["warnings"] == []


def test_g_financier_has_no_records_no_flags_and_giant_component_warning():
    exp, doc = _expected("g_financier"), _ingest("g_financier")
    w = exp["wallets"]
    assert len(doc["buyers"]) == 30
    assert exp["prune"]["records"] == [] and exp["prune"]["buyer_flags"] == []
    r = _node(exp, "R")
    # 29 справжніх сум і один пиловий серед справжніх: медіана справжня
    assert r["measures"] == dict(degree=32, unique_senders=2, one_off_senders=2, one_off_share=1.0,
                                 buyer_fanout=30, median_to_buyers=700_000_000)
    th = exp["config"]["thresholds"]
    assert 30 <= r["measures"]["degree"] < th["degree_threshold"]  # >= числа покупців, але під порогом ступеня
    sums = sorted(e["amount"] for e in exp["graph"]["edges"] if e["sender"] == w["R"] and e["receiver"] in
                  {b["wallet"] for b in doc["buyers"]})
    assert sums == [400_000] + [700_000_000] * 29
    assert exp["report"]["before"] == exp["report"]["after"]
    assert exp["report"]["pruned_nodes"] == 0 and exp["report"]["pruned_edges"] == 0
    assert exp["report"]["after"]["largest_component_buyer_share"] == 1.0
    assert exp["report"]["warnings"] == ["giant_component"]  # один справжній фінансист усіх покупців — сигнал для 003
    assert w["R"] in exp["prune"]["after"]["node_addresses"]


def test_g_dust_mixed_declares_strict_majority_and_threshold_boundaries():
    exp, doc = _expected("g_dust_mixed"), _ingest("g_dust_mixed")
    w = exp["wallets"]
    assert len(doc["buyers"]) == 10
    assert [r["address"] for r in exp["prune"]["records"]] == sorted([w["X"], w["W"]])
    assert exp["prune"]["buyer_flags"] == []
    by_addr = {r["address"]: r for r in exp["prune"]["records"]}
    buyer_addresses = {b["wallet"] for b in doc["buyers"]}

    def sums(label):
        return sorted(e["amount"] for e in exp["graph"]["edges"]
                      if e["sender"] == w[label] and e["asset"] == "sol" and e["receiver"] in buyer_addresses)

    x, y = _node(exp, "X"), _node(exp, "Y")
    # X: 4 пилових + 3 справжніх (n = 7): верхня медіана — 4-й елемент, пил -> хаб
    assert len(sums("X")) == 7 and sums("X")[:4] == [500_000] * 4 and sums("X")[4:] == [500_000_000] * 3
    assert x["measures"]["buyer_fanout"] == 7 and x["measures"]["median_to_buyers"] == 500_000
    assert [(h["criterion"], h["measured"]) for h in by_addr[w["X"]]["criteria"]] == [("dust_fanout", 500_000)]
    # Y: 3 + 3 (n = 6): верхня медіана справжня -> не хаб (нижня медіана дала б пил)
    ys = sums("Y")
    assert len(ys) == 6 and ys[:3] == [500_000] * 3 and ys[3:] == [500_000_000] * 3
    assert ys[(len(ys) - 1) // 2] < DUST_T <= ys[len(ys) // 2]
    assert y["measures"]["buyer_fanout"] == 6 and y["measures"]["median_to_buyers"] == 500_000_000
    assert w["Y"] in exp["prune"]["after"]["node_addresses"]
    # W / Z / V: поріг − 1 / поріг / поріг + 1 при fan-out 5 (рівно передумова)
    for label, amount, hub in (("W", DUST_T - 1, True), ("Z", DUST_T, False), ("V", DUST_T + 1, False)):
        assert sums(label) == [amount] * 5, label
        node = _node(exp, label)
        assert (node["measures"]["buyer_fanout"], node["measures"]["median_to_buyers"]) == (5, amount)
        assert (w[label] in by_addr) == hub, label
        assert (w[label] in exp["prune"]["after"]["node_addresses"]) == (not hub), label
    assert by_addr[w["W"]]["criteria"] == [dict(criterion="dust_fanout", measured=999_999, threshold=DUST_T,
                                                detail="measured", lists_version=None)]
    # U: п'ять покупців лише в spl:USDC — fan-out 0, медіани немає, критерій не застосовний
    u = _node(exp, "U")
    assert (u["measures"]["buyer_fanout"], u["measures"]["median_to_buyers"], u["measures"]["degree"]) == (0, None, 5)
    u_edges = [e for e in exp["graph"]["edges"] if e["sender"] == w["U"]]
    assert len(u_edges) == 5 and {e["asset"] for e in u_edges} == {f"spl:{w['USDC']}"}
    assert w["U"] in exp["prune"]["after"]["node_addresses"]
    assert exp["report"]["pruned_nodes"] == 2


def test_dust_fanout_hits_appear_only_where_declared():
    """Старі вісім сценаріїв не отримали несподіваних пилових хітів: лише `g_buyer_hub` (X, п'ятий критерій)."""
    where = {}
    for name in SCENARIOS:
        exp = _expected(name)
        for rec in exp["prune"]["records"] + exp["prune"]["buyer_flags"]:
            if _dust_hit(rec):
                where.setdefault(name, set()).add(next(l for l, a in exp["wallets"].items() if a == rec["address"]))
    assert where == {"g_dust": {"D"}, "g_dust_mixed": {"X", "W"}, "g_buyer_hub": {"X"}}


def test_every_scenario_has_dust_measures_on_every_node():
    for name in SCENARIOS:
        exp = _expected(name)
        for node in exp["graph"]["nodes"]:
            m = node["measures"]
            assert set(m) == {"degree", "unique_senders", "one_off_senders", "one_off_share", "buyer_fanout",
                              "median_to_buyers"}, (name, node["address"])
            assert type(m["buyer_fanout"]) is int and m["buyer_fanout"] >= 0
            assert (m["median_to_buyers"] is None) == (m["buyer_fanout"] == 0), (name, node["address"])
            if m["buyer_fanout"]:
                assert type(m["median_to_buyers"]) is int and m["median_to_buyers"] >= 1
        by_address = {n["address"]: n["measures"] for n in exp["graph"]["nodes"]}
        for rec in exp["prune"]["records"] + exp["prune"]["buyer_flags"]:
            assert rec["measures"] == by_address[rec["address"]]


def test_metadata_thresholds_are_coherent_with_collected_senders():
    """Фікстура відповідає механіці 001: вершина `high_degree` має рівно `counterparty_threshold` зібраних відправників."""
    for name in SCENARIOS:
        doc = _ingest(name)
        senders = defaultdict(set)
        for t in doc["transfers"]:
            senders[t["receiver"]].add(t["sender"])
        limit = doc["metadata"]["counterparty_threshold"]
        for u in doc["unexpanded"]:
            if u["reason"] == "high_degree":
                assert len(senders[u["wallet"]]) == limit, (name, u["wallet"])
                assert u["counterparties_seen"] == limit + 1
        for receiver, found in senders.items():
            assert len(found) <= limit, (name, receiver)
