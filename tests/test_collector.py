# verifies: FR-001-09, FR-001-10, FR-001-14
"""Оркестрація збору й виведення повноти (T-014).

`collect(state, source, config, clock) -> IngestResult`: перелічення покупців → рівні BFS
1…`funding_depth` → `Completeness.derive` → `RunMetadata`. Статус ніколи не задається вручну:
`complete` лише коли `missing` порожній **і** `buyers.complete` (FR-001-10, принцип V).

SC-001: `serialize.to_dict` ще немає (T-020), тож результат порівнюється з `expected.json` через
`_golden_view` — тестове відображення model-об'єктів у той самий JSON-вигляд, без volatile-полів
metadata (`analyzed_at`, `elapsed_seconds`, `rpc_calls`, `resumed`, `served_from_cache`). T-020
замінить його на `to_dict`. Еталони — незалежний генератор `build_fixtures.py`. Мережі немає.
"""

import copy
import dataclasses
import itertools
import json
import random
from pathlib import Path

import pytest

from unmask.ingest.budget import FakeClock
from unmask.ingest.collector import CollectionState, collect
from unmask.ingest.config import load_config
from unmask.ingest.model import (
    CompletenessStatus,
    IngestResult,
    MissingReason,
    buyer_sort_key,
    transfer_sort_key,
)
from unmask.ingest.rpc.fixture import FailAfter, FailFor, FixtureRpcSource
from unmask.ingest.rpc.protocol import RpcRateLimited, RpcTimeout, RpcUnavailable

ROOT = Path(__file__).resolve().parent.parent
SCENARIOS = ROOT / "tests" / "fixtures" / "scenarios"
BASIC, CORRUPT, HUB = SCENARIOS / "basic", SCENARIOS / "corrupt", SCENARIOS / "hub"
SHIPPED = ROOT / "config" / "ingest.yaml"

EXPECTED = json.loads((BASIC / "expected.json").read_text())
RPC = json.loads((BASIC / "rpc.json").read_text())
M = EXPECTED["mint"]
W = EXPECTED["wallets"]
A, B, D, G = (W[k] for k in ("A", "B", "D", "G"))
BUY_SIG = {b["wallet"]: b["first_buy_signature"] for b in EXPECTED["buyers"]}
BUYERS_IN_ORDER = [b["wallet"] for b in EXPECTED["buyers"]]

CORRUPT_RPC = json.loads((CORRUPT / "rpc.json").read_text())
CW = CORRUPT_RPC["_meta"]["cast"]
(CORRUPT_NULL_SIG,) = CORRUPT_RPC["_meta"]["defects"]["null_transaction"]
(CORRUPT_NO_META_SIG,) = CORRUPT_RPC["_meta"]["defects"]["missing_meta"]

HUB_EXPECTED = json.loads((HUB / "expected.json").read_text())
HUB_CASES = {c["name"]: c for c in HUB_EXPECTED["cases"]}

CONFIG_KEYS = ("first_buyers_n", "funding_depth", "counterparty_threshold",
               "max_signatures_per_wallet", "collect_spl_inbound")
VOLATILE = ("analyzed_at", "elapsed_seconds", "rpc_calls", "resumed", "served_from_cache")


def _sig(prefix: str) -> str:
    (sig,) = [s for s in RPC["getTransaction"] if s.startswith(prefix)]
    return sig


SIG_D_A = _sig("2YTSM2")   # D->A, глибина 2
SIG_B_A = _sig("3sWVjS")   # B->A, глибина 2
SIG_G_A = _sig("2avhiv")   # G->A, глибина 2


def _cfg(expected_config: dict | None = None, **overrides):
    """Поставлений конфіг (версія 2) зі значеннями еталона й точковими замінами."""
    cfg = load_config(SHIPPED)
    values = dict(expected_config or EXPECTED["config"])
    values.update(overrides)
    rpc = values.pop("rpc", None)
    cfg = dataclasses.replace(cfg, **values)
    return dataclasses.replace(cfg, rpc=rpc) if rpc is not None else cfg


def _run(source, cfg, clock=None, mint: str = M) -> tuple[CollectionState, IngestResult]:
    state = CollectionState(mint=mint, config_version=cfg.version)
    return state, collect(state, source, cfg, clock or FakeClock())


def _plain(obj) -> object:
    """Model-об'єкт → JSON-вигляд (tuple → list, Asset/StrEnum → str)."""
    return json.loads(json.dumps(dataclasses.asdict(obj)))


def _golden_view(result: IngestResult) -> dict:
    """Відображення результату у форму `expected.json` (до появи `serialize.to_dict`, T-020)."""
    return {
        "mint": result.metadata.mint,
        "config": {k: getattr(result.metadata, k) for k in CONFIG_KEYS},
        "completeness": {
            "status": result.completeness.status.value,
            "missing": [_plain(m) for m in result.completeness.missing],
            "buyers_complete": result.completeness.buyers.complete,
        },
        "buyers": [_plain(b) for b in result.buyers],
        "transfers": [_plain(t) for t in result.transfers],
        "unexpanded": [_plain(u) for u in result.unexpanded],
    }


def _stable(result: IngestResult) -> tuple:
    """Результат без volatile-полів metadata — для порівняння двох прогонів."""
    meta = dataclasses.asdict(result.metadata)
    for key in VOLATILE:
        meta.pop(key)
    return (meta, _golden_view(result))


def _variant(tmp_path: Path, data: dict, name: str = "basic") -> FixtureRpcSource:
    directory = tmp_path / name
    directory.mkdir(exist_ok=True)
    (directory / "rpc.json").write_text(json.dumps(data))
    return FixtureRpcSource(directory)


def _mutated(tmp_path: Path, mutate) -> FixtureRpcSource:
    data = copy.deepcopy(RPC)
    mutate(data)
    return _variant(tmp_path, data)


# --- SC-001: basic проти незалежного еталона ------------------------------------------


def test_basic_scenario_matches_expected_golden():
    _state, result = _run(FixtureRpcSource(BASIC), _cfg())
    expected = {k: EXPECTED[k] for k in ("mint", "config", "completeness", "buyers", "transfers", "unexpanded")}
    assert _golden_view(result) == expected


def test_result_ordered_by_model_keys():
    _state, result = _run(FixtureRpcSource(BASIC), _cfg())
    assert list(result.buyers) == sorted(result.buyers, key=buyer_sort_key)
    assert [b.rank for b in result.buyers] == list(range(1, len(result.buyers) + 1))
    assert list(result.transfers) == sorted(result.transfers, key=transfer_sort_key)
    assert len(result.transfers) == len(EXPECTED["transfers"])


# --- Повнота: complete тоді й лише тоді, коли missing порожній І buyers.complete ------


def test_complete_status_with_empty_missing_when_all_fetched():
    _state, result = _run(FixtureRpcSource(BASIC), _cfg())
    assert result.completeness.status is CompletenessStatus.COMPLETE
    assert result.completeness.missing == ()
    assert result.completeness.buyers.complete is True
    assert result.completeness.buyers.reason is None


def test_mint_history_failure_marks_incomplete_with_reason_even_with_empty_missing():
    source = FixtureRpcSource(BASIC, failures=[FailFor(M, RpcUnavailable("node down"), times=1)])
    _state, result = _run(source, _cfg())
    c = result.completeness
    assert c.status is CompletenessStatus.INCOMPLETE
    assert c.missing == ()  # жодного гаманця не розбирали — і все одно не «чисто»
    assert (c.buyers.complete, c.buyers.reason) == (False, MissingReason.UNAVAILABLE)
    assert "getSignaturesForAddress" in c.buyers.detail and "node down" in c.buyers.detail
    assert result.buyers == () and result.transfers == ()
    assert result.metadata.wallets_analyzed == 0


def test_corrupt_scenario_marks_incomplete_with_corrupt_data_reason_and_keeps_other_transfers():
    source = FixtureRpcSource(CORRUPT)
    _state, result = _run(source, _cfg(first_buyers_n=3, funding_depth=1), mint=CW["M"])
    c = result.completeness
    assert c.buyers.complete is True  # покупці перелічені повністю; неповнота — лише у фінансуванні
    assert c.status is CompletenessStatus.INCOMPLETE
    by_wallet = {(m.wallet, m.reason): m for m in c.missing}
    assert set(by_wallet) == {(CW["K2"], MissingReason.UNAVAILABLE), (CW["K3"], MissingReason.CORRUPT_DATA)}
    k2, k3 = by_wallet[(CW["K2"], MissingReason.UNAVAILABLE)], by_wallet[(CW["K3"], MissingReason.CORRUPT_DATA)]
    assert (k2.depth, k2.detail) == (0, CORRUPT_NULL_SIG)  # detail — підпис транзакції
    assert k3.depth == 0 and CORRUPT_NO_META_SIG in k3.detail
    # здоровий K1 не постраждав: його переказ F1->K1 у результаті
    assert [(t.sender, t.receiver, t.depth) for t in result.transfers] == [(CW["F1"], CW["K1"], 1)]
    assert {b.wallet for b in result.buyers} == {CW["K1"], CW["K2"], CW["K3"]}


def test_corrupt_record_keeps_rest_of_same_wallet_transfers(tmp_path):
    source = _mutated(tmp_path, lambda d: d["getTransaction"][SIG_G_A].pop("meta"))
    _state, result = _run(source, _cfg())
    c = result.completeness
    assert c.status is CompletenessStatus.INCOMPLETE
    (m,) = c.missing
    assert (m.wallet, m.depth, m.reason) == (A, 1, MissingReason.CORRUPT_DATA)
    assert SIG_G_A in m.detail
    keys = {(t.signature, t.instruction_path) for t in result.transfers}
    assert {(SIG_B_A, "0"), (SIG_D_A, "0")} <= keys  # решта записів A зберігається
    assert SIG_G_A not in {t.signature for t in result.transfers}


def test_null_transaction_marks_unavailable_not_silently_dropped(tmp_path):
    source = _mutated(tmp_path, lambda d: d["getTransaction"].__setitem__(SIG_G_A, None))
    _state, result = _run(source, _cfg())
    c = result.completeness
    assert c.status is CompletenessStatus.INCOMPLETE
    assert c.buyers.complete is True
    (m,) = c.missing
    assert (m.wallet, m.depth, m.reason, m.detail) == (A, 1, MissingReason.UNAVAILABLE, SIG_G_A)
    keys = {(t.signature, t.instruction_path) for t in result.transfers}
    assert {(SIG_B_A, "0"), (SIG_D_A, "0")} <= keys


@pytest.mark.parametrize("name", ["hub_signature_cap", "hub_high_degree"])
def test_unexpanded_does_not_affect_status(name):
    case = HUB_CASES[name]
    source = FixtureRpcSource(HUB)
    cfg = _cfg(case["config"])
    state = CollectionState(mint=HUB_EXPECTED["mint"], config_version=cfg.version)
    result = collect(state, source, cfg, FakeClock())
    assert result.unexpanded, "сценарій мусить мати нерозгорнуту вершину"
    assert result.completeness.status is CompletenessStatus.COMPLETE
    assert result.completeness.missing == ()
    assert [_plain(t) for t in result.transfers] == case["transfers"]
    got = [_plain(u) for u in result.unexpanded]
    want = [{k: v for k, v in u.items() if k != "not_asserted"} for u in case["unexpanded"]]
    assert [{k: g[k] for k in w} for g, w in zip(got, want)] == want and len(got) == len(want)


# --- Метадані (FR-001-14) ------------------------------------------------------------


def test_metadata_reports_used_config_values_and_version():
    cfg = _cfg(version=7, first_buyers_n=4, funding_depth=2, counterparty_threshold=150,
               max_signatures_per_wallet=250, collect_spl_inbound=False, time_budget_seconds=33.5)
    clock = FakeClock(start=10.0, advance_per_call=0.25, wall_start=1_759_500_000)
    clock.advance(100.0)  # час до збору не входить у elapsed
    source = FixtureRpcSource(BASIC, clock=clock)
    state = CollectionState(mint=M, config_version=cfg.version)
    result = collect(state, source, cfg, clock)
    meta = result.metadata

    assert meta.mint == M
    assert meta.config_version == 7
    assert (meta.first_buyers_n, meta.funding_depth, meta.counterparty_threshold,
            meta.max_signatures_per_wallet, meta.collect_spl_inbound,
            meta.time_budget_seconds) == (4, 2, 150, 250, False, 33.5)
    assert meta.wallets_analyzed == len(result.buyers) == 4
    assert meta.source == "fixture:basic"
    assert meta.rpc_calls == len(source.calls) > 0
    assert meta.transactions_scanned == state.transactions_scanned > 0
    assert meta.elapsed_seconds == pytest.approx(0.25 * len(source.calls))
    assert meta.analyzed_at == 1_759_500_000 + 100  # Clock.wall() на початку збору
    assert (meta.resumed, meta.served_from_cache) == (False, False)
    # використані значення справді діяли: SPL не збирались, глибина <= 2
    assert all(t.asset == "sol" and t.depth <= 2 for t in result.transfers)


def test_wallets_analyzed_equals_buyers_when_fewer_than_n():
    _state, result = _run(FixtureRpcSource(BASIC), _cfg(first_buyers_n=300))
    assert result.metadata.wallets_analyzed == len(result.buyers) == len(EXPECTED["buyers"])
    assert result.metadata.first_buyers_n == 300


# --- Детермінізм -----------------------------------------------------------------------


def _shuffled_items(mapping: dict, rng: random.Random) -> list:
    items = list(mapping.items())
    rng.shuffle(items)
    return items


def _shuffle_same_slot(history: list, rng: random.Random) -> list:
    out = []
    for _slot, group in itertools.groupby(history, key=lambda e: e["slot"]):
        group = list(group)
        shuffled = rng.sample(group, len(group))
        if len(group) > 1 and shuffled == group:
            shuffled.reverse()  # перестановка гарантована: інакше сід перевіряв би вихідний порядок
        out.extend(shuffled)
    return out


def _shuffle_rpc(data: dict, rng: random.Random) -> dict:
    """Перемішати все, що в RPC не несе значення: порядок ключів, порядок токен-рахунків, порядок
    записів одного слота в історії mint (позиції в блоці RPC не гарантує — R-6). Історії гаманців
    у межах слота не чіпаються: там позиція відносно межі — дані (R-1), а не порядок відповіді."""
    out = {}
    for method, value in _shuffled_items(data, rng):
        if method == "getSignaturesForAddress":
            value = {addr: (_shuffle_same_slot(h, rng) if addr == M else h) for addr, h in _shuffled_items(value, rng)}
        elif method == "getTokenAccountsByOwner":
            value = {owner: rng.sample(accs, len(accs)) for owner, accs in _shuffled_items(value, rng)}
        elif isinstance(value, dict):
            value = dict(_shuffled_items(value, rng))
        out[method] = value
    return out


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_output_order_independent_of_rpc_response_order(tmp_path, seed):
    rng = random.Random(seed)
    shuffled = _shuffle_rpc(copy.deepcopy(RPC), rng)
    same_slot = lambda doc: [e["signature"] for e in doc["getSignaturesForAddress"][M] if e["slot"] == 210]
    assert len(same_slot(RPC)) > 1 and same_slot(shuffled) != same_slot(RPC)  # P2 і P3 в одному слоті
    _s1, reference = _run(FixtureRpcSource(BASIC), _cfg())
    _s2, result = _run(_variant(tmp_path, shuffled), _cfg())
    assert _stable(result) == _stable(reference)
    assert _golden_view(result)["transfers"] == EXPECTED["transfers"]


def test_collect_twice_on_same_state_is_idempotent():
    source = FixtureRpcSource(BASIC)
    cfg = _cfg()
    state, first = _run(source, cfg)
    calls = len(source.calls)
    second = collect(state, source, cfg, FakeClock())
    assert len(source.calls) == calls  # усе вже розгорнуто — жодного нового звернення
    assert second.metadata.rpc_calls == 0  # «за цей прогін»
    assert _stable(second) == _stable(first)


def test_state_of_other_config_version_is_refused_loudly():
    # стан іншої версії конфігу не змішується з новим прогоном (принцип III); відкидає його resume (T-017)
    cfg = _cfg()
    state = CollectionState(mint=M, config_version=cfg.version + 1)
    source = FixtureRpcSource(BASIC)
    with pytest.raises(ValueError, match="config_version"):
        collect(state, source, cfg, FakeClock())
    assert source.calls == []


# --- Повторний collect на частковому стані == свіжий прогін (рівень 0 — з поточних покупців) ----


RPC_ERRORS = [RpcUnavailable("node down"), RpcRateLimited(retry_after=1.0), RpcTimeout("slow")]


def _fresh(cfg, directory: Path = BASIC, mint: str = M) -> IngestResult:
    return _run(FixtureRpcSource(directory), cfg, mint=mint)[1]


def _assert_equals_fresh(resumed: IngestResult, cfg, directory: Path = BASIC, mint: str = M) -> None:
    """Повтор після збою == свіжий прогін за ВСІМ, крім volatile-полів metadata (`rpc_calls` — лічильник
    реальної роботи за прогін). `transactions_scanned` детермінований (останній прохід перелічення),
    тож теж збігається."""
    fresh = _fresh(cfg, directory, mint)
    assert resumed.metadata.transactions_scanned == fresh.metadata.transactions_scanned
    assert _stable(resumed) == _stable(fresh)


@pytest.mark.parametrize("exc", RPC_ERRORS, ids=lambda e: type(e).__name__)
def test_recollect_after_failed_mint_paging_equals_fresh_run(exc):
    cfg = _cfg()
    state, first = _run(FixtureRpcSource(BASIC, failures=[FailAfter(1, exc)]), cfg)
    assert first.completeness.status is CompletenessStatus.INCOMPLETE and first.buyers == ()
    second = collect(state, FixtureRpcSource(BASIC), cfg, FakeClock())
    assert second.completeness.status is CompletenessStatus.COMPLETE
    assert len(second.transfers) == len(EXPECTED["transfers"]) == 10
    _assert_equals_fresh(second, cfg)
    assert _golden_view(second)["transfers"] == EXPECTED["transfers"]


class _FailTransactionsOnce(FixtureRpcSource):
    """Перший пакет `getTransaction`, що містить `signature`, падає з `exc` (курсори перегортання не зачіпає)."""

    def __init__(self, scenario_dir, signature: str, exc: Exception) -> None:
        super().__init__(scenario_dir)
        self._signature, self._exc, self._armed = signature, exc, True

    def get_transactions(self, signatures, *, deadline):
        if self._armed and self._signature in signatures:
            self._armed = False
            self.calls.append(("getTransaction", {"signatures": list(signatures)}))
            raise self._exc
        return super().get_transactions(signatures, deadline=deadline)


@pytest.mark.parametrize("exc", RPC_ERRORS, ids=lambda e: type(e).__name__)
def test_recollect_after_partial_enumeration_equals_fresh_run(exc):
    # tx_batch_size=2 (T-052; до нього — page_size=2): пакети транзакцій mint від найстаріших; пакет [P2, P4]
    # падає один раз, тож у першій вибірці лише P1, P3 — без P2, у якого є власне фінансування (A->P2, S->P2)
    cfg = _cfg(rpc=dataclasses.replace(load_config(SHIPPED).rpc, page_size=2, tx_batch_size=2))
    state, first = _run(_FailTransactionsOnce(BASIC, BUY_SIG[W["P2"]], exc), cfg)
    assert first.completeness.buyers.complete is False
    assert [b.wallet for b in first.buyers] == [W["P1"], W["P3"]]  # вибірку обірвано — частина покупців
    assert first.transfers  # і для них уже щось зібрано
    second = collect(state, FixtureRpcSource(BASIC), cfg, FakeClock())
    assert second.completeness.status is CompletenessStatus.COMPLETE
    _assert_equals_fresh(second, cfg)


@pytest.mark.parametrize("n", [3, 4])
def test_recollect_when_buyer_set_and_order_changed_equals_fresh_run(tmp_path, n):
    # перший прохід: купівля P1 (найраніший) недоступна -> вибірка зсувається, у неї потрапляє
    # пізніший покупець; другий прохід: P1 повертається на rank 1, зайвий покупець зникає
    cfg = _cfg(first_buyers_n=n)
    broken = _mutated(tmp_path, lambda d: d["getTransaction"].__setitem__(BUY_SIG[W["P1"]], None))
    state, first = _run(broken, cfg)
    assert [b.wallet for b in first.buyers] == BUYERS_IN_ORDER[1:n + 1]
    assert first.transfers
    second = collect(state, FixtureRpcSource(BASIC), cfg, FakeClock())
    assert [b.wallet for b in second.buyers] == BUYERS_IN_ORDER[:n]
    assert second.completeness.status is CompletenessStatus.COMPLETE
    _assert_equals_fresh(second, cfg)
    # піддерево зниклого покупця не лишилось у стані
    vanished = BUYERS_IN_ORDER[n]
    assert vanished not in state.frontier_by_depth[0]
    assert not [t for t in state.transfers.values() if t.receiver == vanished and t.depth == 1]


# --- unexpanded: порядок (depth, wallet), без дублів на повторі -------------------------


@pytest.mark.parametrize(("override", "count"), [
    ({"counterparty_threshold": 1}, 2),
    ({"max_signatures_per_wallet": 1}, 3),
])
def test_unexpanded_ordered_by_depth_then_wallet_and_not_duplicated_on_recollect(override, count):
    source = FixtureRpcSource(BASIC)
    cfg = _cfg(**override)
    state, first = _run(source, cfg)
    keys = [(u.depth, u.wallet) for u in first.unexpanded]
    assert len(keys) == count
    assert keys == sorted(keys) and len(set(u.wallet for u in first.unexpanded)) == count
    random.Random(7).shuffle(state.unexpanded)  # стан (напр. з партиційного кешу) у довільному порядку
    state.unexpanded.reverse()
    second = collect(state, source, cfg, FakeClock())
    assert second.unexpanded == first.unexpanded


# --- source = source.name, не літерал -------------------------------------------------


class _NamedSource(FixtureRpcSource):
    def __init__(self, scenario_dir, name: str) -> None:
        super().__init__(scenario_dir)
        self._name = name

    @property
    def name(self) -> str:
        return self._name


@pytest.mark.parametrize(("directory", "mint", "config"), [
    (BASIC, M, EXPECTED["config"]),
    (CORRUPT, CW["M"], {"first_buyers_n": 3, "funding_depth": 1}),
    (HUB, HUB_EXPECTED["mint"], HUB_CASES["hub_control"]["config"]),
], ids=["basic", "corrupt", "hub"])
@pytest.mark.parametrize("custom", [False, True])
def test_metadata_source_is_source_name(directory, mint, config, custom):
    source = _NamedSource(directory, f"fixture:custom_{directory.name}") if custom else FixtureRpcSource(directory)
    _state, result = _run(source, _cfg(config), mint=mint)
    assert result.metadata.source == source.name
    assert result.metadata.source == (f"fixture:custom_{directory.name}" if custom else f"fixture:{directory.name}")


class _FailTokenAccountsOnce(FixtureRpcSource):
    """Перший `getTokenAccountsByOwner(owner)` падає: вершину позначено (`unexpanded`) і записано в `missing`."""

    def __init__(self, scenario_dir, owner: str) -> None:
        super().__init__(scenario_dir)
        self._owner, self._armed = owner, True

    def get_token_accounts_by_owner(self, owner, *, deadline):
        if self._armed and owner == self._owner:
            self._armed = False
            self.calls.append(("getTokenAccountsByOwner", {"owner": owner}))
            raise RpcUnavailable("token accounts down")
        return super().get_token_accounts_by_owner(owner, deadline=deadline)


@pytest.mark.parametrize("override", [{"counterparty_threshold": 1}, {"max_signatures_per_wallet": 1}])
def test_reexpanded_unexpanded_node_replaced_not_duplicated_on_recollect(override):
    # A (рівень 1) позначено й у missing; повторний collect розгортає A знову — запис замінюється
    cfg = _cfg(**override)
    state, first = _run(_FailTokenAccountsOnce(BASIC, A), cfg)
    assert (A, MissingReason.UNAVAILABLE) in {(m.wallet, m.reason) for m in first.completeness.missing}
    assert A in {u.wallet for u in first.unexpanded}
    second = collect(state, FixtureRpcSource(BASIC), cfg, FakeClock())
    wallets = [u.wallet for u in second.unexpanded]
    assert len(wallets) == len(set(wallets))
    assert [(u.depth, u.wallet) for u in second.unexpanded] == sorted((u.depth, u.wallet) for u in second.unexpanded)
    _assert_equals_fresh(second, cfg)



# --- missing перебудовується з кожного (повторного) розгортання вершини ----------------------


@pytest.mark.parametrize(("n", "depth"), [(3, 1), (300, 1), (3, 2)])
def test_recollect_rebuilds_missing_reasons_from_latest_pass(n, depth):
    # перший прохід: таймаут із 4-го звернення — K2/K3 у missing з причиною timeout; чистий повтор
    # бачить справжні дефекти (null -> unavailable, без meta -> corrupt_data); старі причини не лишаються
    cfg = _cfg(first_buyers_n=n, funding_depth=depth)
    state, first = _run(FixtureRpcSource(CORRUPT, failures=[FailAfter(4, RpcTimeout("slow"))]), cfg, mint=CW["M"])
    assert MissingReason.TIMEOUT in {m.reason for m in first.completeness.missing} or not first.completeness.buyers.complete
    second = collect(state, FixtureRpcSource(CORRUPT), cfg, FakeClock())
    assert {(m.wallet, m.reason) for m in second.completeness.missing} == {
        (CW["K2"], MissingReason.UNAVAILABLE), (CW["K3"], MissingReason.CORRUPT_DATA)}
    _assert_equals_fresh(second, cfg, CORRUPT, CW["M"])


def test_failed_retry_keeps_evidence_but_reports_only_current_reasons(tmp_path):
    # повтор вершини знову невдалий, але з іншою причиною: докази першої спроби лишаються, причина — актуальна
    cfg = _cfg(funding_depth=1)
    state, first = _run(_FailTokenAccountsOnce(BASIC, W["P2"]), cfg)
    assert {(m.wallet, m.reason) for m in first.completeness.missing} == {(W["P2"], MissingReason.UNAVAILABLE)}
    kept = {(t.signature, t.instruction_path) for t in first.transfers if t.receiver == W["P2"]}
    assert kept
    second = collect(state, FixtureRpcSource(BASIC, failures=[FailFor(W["P2"], RpcTimeout("slow"), times=1)]),
                     cfg, FakeClock())
    assert {(m.wallet, m.reason) for m in second.completeness.missing} == {(W["P2"], MissingReason.TIMEOUT)}
    assert kept <= {(t.signature, t.instruction_path) for t in second.transfers}


# --- transactions_scanned детермінований; повтор на повному стані — без звернень --------------


class _NullTransactionOnce(FixtureRpcSource):
    """Перший раз, коли кожен із `signatures` запитано, джерело повертає для нього `None`."""

    def __init__(self, scenario_dir, *signatures: str) -> None:
        super().__init__(scenario_dir)
        self._armed = set(signatures)

    def get_transactions(self, signatures, *, deadline):
        raws = super().get_transactions(signatures, deadline=deadline)
        for i, sig in enumerate(signatures):
            if sig in self._armed:
                self._armed.discard(sig)
                raws[i] = None
        return raws


def test_transactions_scanned_after_recollect_equals_fresh_run():
    # tx_batch_size=1 (T-052; до нього — page_size=1): найстаріша транзакція mint (36vrgL…, слот 10) спершу
    # недоступна; на повторі її вже закешувало фінансування — правило купівлі її однаково проходить, і
    # лічильник = свіжому прогону
    cfg = _cfg(funding_depth=1, rpc=dataclasses.replace(load_config(SHIPPED).rpc, page_size=1, tx_batch_size=1))
    state, first = _run(_NullTransactionOnce(BASIC, _sig("36vrgL")), cfg)
    assert first.completeness.buyers.complete is False
    second = collect(state, FixtureRpcSource(BASIC), cfg, FakeClock())
    _assert_equals_fresh(second, cfg)


@pytest.mark.parametrize("n", [1, 2, 3, 4])
def test_recollect_on_complete_state_makes_no_source_calls_even_when_cut_inside_batch(n):
    # tx_batch_size=1000 (T-052; до нього — page_size=1000): пакет транзакцій mint захоплює й транзакції за
    # слотом N-го покупця
    cfg = _cfg(first_buyers_n=n, rpc=dataclasses.replace(load_config(SHIPPED).rpc, tx_batch_size=1000))
    source = FixtureRpcSource(BASIC)
    state, first = _run(source, cfg)
    assert first.completeness.status is CompletenessStatus.COMPLETE
    calls = len(source.calls)
    second = collect(state, source, cfg, FakeClock())
    assert source.calls[calls:] == []
    assert _stable(second) == _stable(first)


# --- unexpanded: спершу глибина, потім гаманець -----------------------------------------------


def test_unexpanded_ordered_by_depth_before_wallet(tmp_path):
    # A (рівень 1) перейменовано на адресу, лексикографічно меншу за P2 і P5 (рівень 0)
    renamed = "17DUeBUtEcb7nujVZRJmeBju3X1mo6PpnWNtJ9EBhdY"
    assert renamed < min(W["P2"], W["P5"]) and renamed not in json.dumps(RPC)
    data = json.loads(json.dumps(RPC).replace(A, renamed))
    _state, result = _run(_variant(tmp_path, data), _cfg(max_signatures_per_wallet=1))
    got = [(u.depth, u.wallet) for u in result.unexpanded]
    assert got == [(0, min(W["P2"], W["P5"])), (0, max(W["P2"], W["P5"])), (1, renamed)]



# --- ліниві пакети транзакцій mint ------------------------------------------------------------

MINT_SIGS = {e["signature"] for e in RPC["getSignaturesForAddress"][M]}


def _mint_tx_batches(calls) -> list[list[str]]:
    return [p["signatures"] for m, p in calls if m == "getTransaction" and set(p["signatures"]) <= MINT_SIGS]


def test_recollect_fetches_only_uncached_mint_transactions_in_one_batch():
    # tx_batch_size=2 (T-052; до нього — page_size=2); перший прохід: купівлі P1 і P2 недоступні (між ними —
    # закешована P3); повтор запитує рівно [P1, P2] одним пакетом — закешовані не перезапитуються й не
    # «з'їдають» місце в пакеті. N=2: у першій вибірці P3, P4 (пул P5, чия історія містить усі купівлі, не
    # розгортається)
    cfg = _cfg(first_buyers_n=2, rpc=dataclasses.replace(load_config(SHIPPED).rpc, page_size=2, tx_batch_size=2))
    state, first = _run(_NullTransactionOnce(BASIC, BUY_SIG[W["P1"]], BUY_SIG[W["P2"]]), cfg)
    assert [b.wallet for b in first.buyers] == [W["P3"], W["P4"]]
    assert first.completeness.buyers.complete is False
    source = FixtureRpcSource(BASIC)
    second = collect(state, source, cfg, FakeClock())
    assert _mint_tx_batches(source.calls) == [sorted([BUY_SIG[W["P1"]], BUY_SIG[W["P2"]]],
                                                     key=lambda sig: (210 if sig == BUY_SIG[W["P2"]] else 200, sig))]
    _assert_equals_fresh(second, cfg)


def test_after_nth_buyer_only_rest_of_its_slot_is_fetched():
    # N=2, tx_batch_size=4 (T-052; до нього — page_size=4): перший пакет [10, 12, P1, P3] дає N у слоті 210;
    # добирається лише P2 (той самий слот), а не наступні P4/P5
    source = FixtureRpcSource(BASIC)
    _state, result = _run(source, _cfg(first_buyers_n=2, rpc=dataclasses.replace(
        load_config(SHIPPED).rpc, page_size=4, tx_batch_size=4)))
    batches = _mint_tx_batches(source.calls)
    assert batches[-1] == [BUY_SIG[W["P2"]]]
    assert not {BUY_SIG[W["P4"]], BUY_SIG[W["P5"]]} & {s for b in batches for s in b}
    assert [b.wallet for b in result.buyers] == BUYERS_IN_ORDER[:2]
