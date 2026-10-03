# verifies: FR-001-16
"""Бюджет часу збору (T-015, research R-11, contracts/rpc-source.md «Deadline»).

`budget.Deadline(clock, seconds)`: відлік від створення (колектор створює його на початку `collect`),
`remaining()` ніколи не від'ємний, `expired()` — коли час дійшов до межі (рівно на межі вже спливло),
`request_timeout(cap) = min(remaining, cap)`.

Ядро перевіряє `expired()` перед КОЖНИМ зверненням до джерела (сторінка історії, пакет транзакцій,
токен-рахунки) у `buyers` і `funding`; після спливу жодного звернення немає (перевіряється журналом
`FixtureRpcSource.calls`). Вичерпання не мовчить (принцип V): нерозгорнуті вершини → `MissingHistory(
reason=budget_exhausted)`, незавершене перелічення → `buyers.complete=false, reason=budget_exhausted`;
зібране зберігається; повторний `collect` на частковому стані дає результат свіжого прогону.

Час — лише `FakeClock(advance_per_call=…)`: фікстурне джерело просуває його на кожному виклику, тож
бюджет `k` при кроці 1 с пропускає рівно `k` звернень. Мережі немає.
"""

import dataclasses
import json
import math
from pathlib import Path

import pytest

from unmask.ingest.budget import Deadline, FakeClock
from unmask.ingest.buyers import enumerate_buyers
from unmask.ingest.collector import CollectionState, collect
from unmask.ingest.config import load_config
from unmask.ingest.model import CompletenessStatus, IngestResult, MissingReason
from unmask.ingest.rpc.fixture import FailFor, FixtureRpcSource
from unmask.ingest.rpc.protocol import RpcTimeout

ROOT = Path(__file__).resolve().parent.parent
SCENARIOS = ROOT / "tests" / "fixtures" / "scenarios"
BASIC, CORRUPT, HUB = SCENARIOS / "basic", SCENARIOS / "corrupt", SCENARIOS / "hub"
SHIPPED = ROOT / "config" / "ingest.yaml"

EXPECTED = json.loads((BASIC / "expected.json").read_text())
M = EXPECTED["mint"]
W = EXPECTED["wallets"]
A, B, D, G, S = (W[k] for k in ("A", "B", "D", "G", "S"))
BUYERS = {b["wallet"] for b in EXPECTED["buyers"]}
CORRUPT_MINT = json.loads((CORRUPT / "rpc.json").read_text())["_meta"]["cast"]["M"]
HUB_EXPECTED = json.loads((HUB / "expected.json").read_text())

VOLATILE = ("analyzed_at", "elapsed_seconds", "rpc_calls", "resumed", "served_from_cache")
BUDGET = MissingReason.BUDGET_EXHAUSTED


def _cfg(config: dict | None = None, **overrides):
    cfg = load_config(SHIPPED)
    values = dict(EXPECTED["config"] if config is None else config)
    values.update(overrides)
    return dataclasses.replace(cfg, **values)


def _timed_run(directory: Path, cfg, budget: float, *, step: float = 1.0, mint: str = M, failures=None):
    """Збір із бюджетом `budget` с; кожне звернення до джерела триває `step` с."""
    cfg = dataclasses.replace(cfg, time_budget_seconds=budget)
    clock = FakeClock(advance_per_call=step)
    source = FixtureRpcSource(directory, clock=clock, failures=failures)
    state = CollectionState(mint=mint, config_version=cfg.version)
    return state, source, collect(state, source, cfg, clock)


def _stable(result: IngestResult, *also_ignore: str) -> tuple:
    meta = dataclasses.asdict(result.metadata)
    for key in VOLATILE + also_ignore:
        meta.pop(key)
    return (meta, result.completeness, result.buyers, result.transfers, result.unexpanded)


def _fresh(directory: Path, cfg, mint: str = M) -> tuple[FixtureRpcSource, IngestResult]:
    source = FixtureRpcSource(directory)
    return source, collect(CollectionState(mint=mint, config_version=cfg.version), source, cfg, FakeClock())


def _missing(result: IngestResult) -> dict[tuple[str, MissingReason], object]:
    return {(m.wallet, m.reason): m for m in result.completeness.missing}


# --- Deadline ----------------------------------------------------------------------------


def test_deadline_remaining_and_expired_with_fake_clock():
    clock = FakeClock(start=100.0)
    deadline = Deadline(clock, 10.0)  # відлік — від створення, не від нуля годинника
    assert deadline.remaining() == 10.0 and not deadline.expired()
    clock.advance(9.75)
    assert deadline.remaining() == 0.25 and not deadline.expired()
    clock.advance(0.25)  # рівно на межі — уже спливло, залишок нуль
    assert deadline.remaining() == 0.0 and deadline.expired()
    clock.advance(5.0)  # після межі залишок не від'ємний
    assert deadline.remaining() == 0.0 and deadline.expired()


def test_request_timeout_is_min_of_remaining_and_cap():
    clock = FakeClock()
    deadline = Deadline(clock, 25.0)
    assert deadline.request_timeout(10.0) == 10.0  # залишок більший — ліміт запиту
    clock.advance(18.0)
    assert deadline.request_timeout(10.0) == 7.0   # залишок менший — обрізається залишком
    assert deadline.request_timeout(7.0) == 7.0
    clock.advance(7.0)
    assert deadline.request_timeout(10.0) == 0.0   # спливло — нуль, не від'ємне
    clock.advance(100.0)
    assert deadline.request_timeout(10.0) == 0.0
    assert Deadline(FakeClock(), math.inf).request_timeout(10.0) == 10.0


def test_deadline_with_zero_seconds_is_expired_immediately_and_infinite_never_expires():
    clock = FakeClock()
    assert Deadline(clock, 0.0).expired() and Deadline(clock, 0.0).remaining() == 0.0
    endless = Deadline(clock, math.inf)
    clock.advance(1e12)
    assert not endless.expired() and endless.remaining() == math.inf


@pytest.mark.parametrize("seconds", [-1.0, -1e-9, math.nan, True, "40", None])
def test_deadline_rejects_invalid_seconds(seconds):
    with pytest.raises((ValueError, TypeError)):
        Deadline(FakeClock(), seconds)


@pytest.mark.parametrize("cap", [0.0, -1.0, math.nan, True, "10", None])
def test_request_timeout_rejects_invalid_cap(cap):
    with pytest.raises((ValueError, TypeError)):
        Deadline(FakeClock(), 10.0).request_timeout(cap)


# --- Збір у межах бюджету ---------------------------------------------------------------------


def test_within_budget_result_complete():
    _source, fresh = _fresh(BASIC, _cfg())
    # 0.25 с на звернення, бюджет 40 с: усі звернення вміщаються
    _state, source, result = _timed_run(BASIC, _cfg(), 40.0, step=0.25)
    assert result.completeness.status is CompletenessStatus.COMPLETE
    assert result.completeness.missing == () and result.completeness.buyers.complete
    assert _stable(result) == _stable(fresh)
    assert source.calls == _source.calls  # бюджет, що не сплив, не змінює жодного звернення
    assert result.metadata.time_budget_seconds == 40.0


def test_frozen_fake_clock_never_expires_and_collection_terminates():
    # годинник, що не рухається (advance_per_call=0), — бюджет ніколи не спливає, збір завершується сам
    cfg = dataclasses.replace(_cfg(), time_budget_seconds=1e-9)
    fresh_source, fresh = _fresh(BASIC, cfg)
    _state, source, result = _timed_run(BASIC, cfg, 1e-9, step=0.0)
    assert result.completeness.status is CompletenessStatus.COMPLETE
    assert source.calls == fresh_source.calls
    assert _stable(result) == _stable(fresh)


def test_huge_step_stops_after_first_call_without_hanging():
    _state, source, result = _timed_run(BASIC, _cfg(), 40.0, step=1e9)
    assert len(source.calls) == 1
    assert result.completeness.status is CompletenessStatus.INCOMPLETE
    assert (result.completeness.buyers.complete, result.completeness.buyers.reason) == (False, BUDGET)


# --- Вичерпання посеред збору -----------------------------------------------------------------
# Порядок звернень basic (крок 1 с): 1–3 перелічення покупців; 4–32 рівень 0 (покупці); 33–37 S і
# 38–41 A (рівень 1, перекази глибини 2); 42–50 G, D, B (рівень 2, глибина 3).


def test_budget_exhausted_mid_funding_returns_partial_incomplete_with_reason():
    # бюджет 39 с: S розгорнуто повністю, A — лише дві сторінки історії гаманця (рівень BFS 2)
    _fresh_source, fresh = _fresh(BASIC, _cfg())
    _state, source, result = _timed_run(BASIC, _cfg(), 39.0)
    assert len(source.calls) == 39
    c = result.completeness
    assert c.status is CompletenessStatus.INCOMPLETE
    assert c.buyers.complete is True  # перелічення завершилось у межах бюджету
    missing = _missing(result)
    assert set(missing) == {(A, BUDGET)}
    assert missing[(A, BUDGET)].depth == 1
    # зібране зберігається: рівні 0 і S повністю; жодного вигаданого переказу
    fresh_transfers = set(fresh.transfers)
    assert set(result.transfers) <= fresh_transfers
    assert {t for t in fresh.transfers if t.depth == 1} <= set(result.transfers)
    assert {t for t in fresh.transfers if t.receiver == S} <= set(result.transfers)
    assert not [t for t in result.transfers if t.depth == 3]
    assert result.buyers == fresh.buyers


def test_budget_exhausted_marks_unexpanded_nodes_of_next_levels():
    # бюджет 41 с: рівень 1 (S, A) розгорнуто повністю; вершини рівня 2 (G, D, B) уже відомі, але не
    # розгорнуті -> кожна в missing з budget_exhausted і глибиною 2
    _state, source, result = _timed_run(BASIC, _cfg(), 41.0)
    assert len(source.calls) == 41
    missing = _missing(result)
    assert set(missing) == {(G, BUDGET), (D, BUDGET), (B, BUDGET)}
    assert {m.depth for m in missing.values()} == {2}
    assert result.completeness.status is CompletenessStatus.INCOMPLETE


def test_budget_exhausted_on_level_zero_marks_current_level_nodes():
    # бюджет 10 с: на рівні 0 розгорнуто лише частину покупців; решта рівня 0 -> budget_exhausted
    _state, source, result = _timed_run(BASIC, _cfg(), 10.0)
    assert len(source.calls) == 10
    missing = _missing(result)
    assert missing and all(reason is BUDGET for _w, reason in missing)
    assert {w for w, _r in missing} <= BUYERS and {m.depth for m in missing.values()} == {0}
    assert result.completeness.buyers.complete is True


def test_budget_exhausted_during_buyer_enumeration_marks_buyers_incomplete():
    # бюджет 2 с: обидві сторінки історії mint отримано, пакет транзакцій — уже ні
    state, source, result = _timed_run(BASIC, _cfg(), 2.0)
    assert len(source.calls) == 2
    b = result.completeness.buyers
    assert (b.complete, b.reason) == (False, BUDGET)
    assert "getTransaction" in b.detail
    assert result.completeness.status is CompletenessStatus.INCOMPLETE
    assert result.buyers == () and result.transfers == ()
    assert state.mint_history_exhausted  # перегорнуту історію збережено для повтору


def test_budget_exhausted_during_mint_paging_keeps_cursor():
    state, source, result = _timed_run(BASIC, _cfg(), 1.0)
    assert len(source.calls) == 1
    b = result.completeness.buyers
    assert (b.complete, b.reason) == (False, BUDGET)
    assert "getSignaturesForAddress" in b.detail
    assert state.signature_cursor is not None and not state.mint_history_exhausted


def test_budget_reason_takes_precedence_when_enumeration_cut_after_other_problem(tmp_path):
    # page_size=1: найстаріша транзакція mint недоступна (unavailable), далі бюджет обриває перелічення;
    # перелічення незавершене — причина budget_exhausted, попередня проблема лишається в detail
    data = json.loads((BASIC / "rpc.json").read_text())
    oldest = data["getSignaturesForAddress"][M][-1]["signature"]
    data["getTransaction"][oldest] = None
    (tmp_path / "basic").mkdir()
    (tmp_path / "basic" / "rpc.json").write_text(json.dumps(data))
    cfg = _cfg(rpc=dataclasses.replace(load_config(SHIPPED).rpc, page_size=1))
    pages = len(data["getSignaturesForAddress"][M]) + 1  # по одному запису на сторінку + порожня
    _state, source, result = _timed_run(tmp_path / "basic", cfg, float(pages + 2))  # + два пакети
    assert [m for m, _p in source.calls[pages:]] == ["getTransaction", "getTransaction"]
    b = result.completeness.buyers
    assert (b.complete, b.reason) == (False, BUDGET)
    assert "unavailable" in b.detail and oldest in b.detail


def test_budget_exhausted_before_first_call_makes_no_calls():
    class _Jumping(FakeClock):
        """Кожне читання monotonic() після першого — на `step` с пізніше (бюджет спливає до звернення)."""

        def monotonic(self) -> float:
            now = super().monotonic()
            self.advance(5.0)
            return now

    cfg = dataclasses.replace(_cfg(), time_budget_seconds=1.0)
    clock = _Jumping()
    source = FixtureRpcSource(BASIC)
    result = collect(CollectionState(mint=M, config_version=cfg.version), source, cfg, clock)
    assert source.calls == []
    assert (result.completeness.buyers.complete, result.completeness.buyers.reason) == (False, BUDGET)
    assert result.completeness.status is CompletenessStatus.INCOMPLETE
    assert result.metadata.rpc_calls == 0


def test_expired_deadline_passed_to_enumerate_buyers_makes_no_calls():
    clock = FakeClock()
    source = FixtureRpcSource(BASIC)
    state = CollectionState(mint=M, config_version=1)
    completeness = enumerate_buyers(source, M, state, _cfg(), Deadline(clock, 0.0))
    assert source.calls == [] and state.rpc_calls == 0
    assert (completeness.complete, completeness.reason) == (False, BUDGET)


def test_budget_cut_during_paging_never_keeps_stale_buyers():
    # стан із покупцями, але з недогорнутою історією: обірване бюджетом перегортання не лишає старої вибірки
    state = CollectionState(mint=M, config_version=1)
    assert enumerate_buyers(FixtureRpcSource(BASIC), M, state, _cfg(), Deadline(FakeClock(), math.inf)).complete
    assert state.buyers
    state.mint_history_exhausted = False
    completeness = enumerate_buyers(FixtureRpcSource(BASIC), M, state, _cfg(), Deadline(FakeClock(), 0.0))
    assert (completeness.complete, completeness.reason) == (False, BUDGET)
    assert state.buyers == ()


@pytest.mark.parametrize("budget", [1.0, 2.0, 3.0, 10.0, 33.0, 38.0, 39.0, 41.0, 45.0, 49.0])
def test_no_rpc_call_made_after_deadline_expired(budget):
    clock = FakeClock(advance_per_call=1.0)
    cfg = dataclasses.replace(_cfg(), time_budget_seconds=budget)
    stamps: list[float] = []

    class _Stamped(FixtureRpcSource):
        def _enter(self, method, params):
            stamps.append(clock.monotonic())  # час на момент звернення, до просування
            super()._enter(method, params)

    source = _Stamped(BASIC, clock=clock)
    result = collect(CollectionState(mint=M, config_version=cfg.version), source, cfg, clock)
    assert len(source.calls) == int(budget) == len(stamps)
    assert all(t < budget for t in stamps)  # кожне звернення почалось до спливу
    assert result.metadata.rpc_calls == len(source.calls)
    assert result.completeness.status is CompletenessStatus.INCOMPLETE


def test_budget_boundary_between_steps():
    # бюджет 38.5 с: 39-те звернення почалось о 38 с (< 38.5) — дозволене; 40-ве (39 с) — ні
    _state, source, _result = _timed_run(BASIC, _cfg(), 38.5)
    assert len(source.calls) == 39
    _state, source, _result = _timed_run(BASIC, _cfg(), 9.75, step=0.25)  # рівно 39 кроків по 0.25
    assert len(source.calls) == 39


# --- RpcTimeout: спричинений дедлайном -> budget_exhausted, звичайний -> timeout ---------------


def test_rpc_timeout_caused_by_deadline_maps_to_budget_exhausted():
    # перше звернення про A (38-ме) триває до межі бюджету й закінчується таймаутом: винен дедлайн
    _state, source, result = _timed_run(BASIC, _cfg(), 38.0, failures=[FailFor(A, RpcTimeout("timed out"), times=1)])
    assert len(source.calls) == 38
    missing = _missing(result)
    assert (A, BUDGET) in missing and (A, MissingReason.TIMEOUT) not in missing
    assert "timed out" in missing[(A, BUDGET)].detail


def test_plain_rpc_timeout_maps_to_timeout_and_collection_continues():
    _state, source, result = _timed_run(BASIC, _cfg(), 1000.0, failures=[FailFor(A, RpcTimeout("timed out"), times=1)])
    missing = _missing(result)
    assert set(missing) == {(A, MissingReason.TIMEOUT)}
    assert len(source.calls) > 38  # після звичайного таймауту збір триває


class _TimeoutOnCall(FixtureRpcSource):
    """k-те звернення (рахунок з 1) триває до кінця кроку й закінчується `RpcTimeout`."""

    def __init__(self, scenario_dir, k: int, **kwargs) -> None:
        super().__init__(scenario_dir, **kwargs)
        self._k = k

    def _enter(self, method, params):
        super()._enter(method, params)  # журнал і просування годинника — до винятку
        if self._call_count == self._k:
            raise RpcTimeout("request timed out")


def _reasons(result: IngestResult) -> set[MissingReason]:
    c = result.completeness
    return {m.reason for m in c.missing} | ({c.buyers.reason} if not c.buyers.complete else set())


# Звернення basic (крок 1 с): 3 — пакет транзакцій перелічення покупців; 6 — токен-рахунки P2;
# 11 — пакет транзакцій вершини P2 (funding).
@pytest.mark.parametrize(("k", "method"), [
    (3, "getTransaction"), (6, "getTokenAccountsByOwner"), (11, "getTransaction"),
], ids=["buyers_getTransaction", "funding_getTokenAccountsByOwner", "funding_getTransaction"])
def test_rpc_timeout_ending_at_deadline_maps_to_budget_exhausted_for_every_call_kind(k, method):
    clock = FakeClock(advance_per_call=1.0)
    cfg = dataclasses.replace(_cfg(), time_budget_seconds=float(k))  # k-те звернення закінчується рівно на межі
    source = _TimeoutOnCall(BASIC, k, clock=clock)
    result = collect(CollectionState(mint=M, config_version=cfg.version), source, cfg, clock)
    assert source.calls[k - 1][0] == method and len(source.calls) == k
    reasons = _reasons(result)
    assert BUDGET in reasons and MissingReason.TIMEOUT not in reasons


@pytest.mark.parametrize(("k", "method"), [
    (3, "getTransaction"), (6, "getTokenAccountsByOwner"), (11, "getTransaction"),
], ids=["buyers_getTransaction", "funding_getTokenAccountsByOwner", "funding_getTransaction"])
def test_rpc_timeout_before_deadline_stays_timeout_for_every_call_kind(k, method):
    clock = FakeClock(advance_per_call=1.0)
    cfg = dataclasses.replace(_cfg(), time_budget_seconds=1000.0)
    source = _TimeoutOnCall(BASIC, k, clock=clock)
    result = collect(CollectionState(mint=M, config_version=cfg.version), source, cfg, clock)
    assert source.calls[k - 1][0] == method
    assert len(source.calls) > k or k == 3  # фінансування триває; обірване перелічення лишає рівень 0 порожнім
    reasons = _reasons(result)
    assert MissingReason.TIMEOUT in reasons and BUDGET not in reasons


def test_rpc_timeout_during_enumeration_caused_by_deadline_maps_to_budget_exhausted():
    _state, _source, result = _timed_run(BASIC, _cfg(), 1.0, failures=[FailFor(M, RpcTimeout("timed out"), times=1)])
    b = result.completeness.buyers
    assert (b.complete, b.reason) == (False, BUDGET)
    _state, _source, result = _timed_run(BASIC, _cfg(), 100.0, failures=[FailFor(M, RpcTimeout("timed out"), times=1)])
    b = result.completeness.buyers
    assert (b.complete, b.reason) == (False, MissingReason.TIMEOUT)


# --- Повтор після вичерпання == свіжий прогін; жодного звернення після спливу ------------------

CASES = [
    ("basic", BASIC, M, EXPECTED["config"]),
    ("basic_ps2", BASIC, M, {**EXPECTED["config"], "rpc": "ps2"}),
    ("basic_caps", BASIC, M, {**EXPECTED["config"], "counterparty_threshold": 1, "max_signatures_per_wallet": 2}),
    ("hub", HUB, HUB_EXPECTED["mint"], HUB_EXPECTED["cases"][0]["config"]),
    ("corrupt", CORRUPT, CORRUPT_MINT, {"first_buyers_n": 3, "funding_depth": 2}),
]


def _case_cfg(config: dict):
    config = dict(config)
    if config.pop("rpc", None) == "ps2":
        return _cfg(config, rpc=dataclasses.replace(load_config(SHIPPED).rpc, page_size=2))
    return _cfg(config)


@pytest.mark.parametrize(("name", "directory", "mint", "config"), CASES, ids=[c[0] for c in CASES])
def test_every_budget_cut_is_honest_and_resume_equals_fresh(name, directory, mint, config):
    cfg = _case_cfg(config)
    fresh_source, fresh = _fresh(directory, cfg, mint)
    total = len(fresh_source.calls)
    fresh_reasons = {m.reason for m in fresh.completeness.missing}
    for k in range(1, total + 2):
        state, source, partial = _timed_run(directory, cfg, float(k), mint=mint)
        assert source.calls == fresh_source.calls[:k], k  # той самий порядок, обрізаний рівно на k
        c = partial.completeness
        if k < total:
            assert c.status is CompletenessStatus.INCOMPLETE, k
            reasons = {m.reason for m in c.missing} | ({c.buyers.reason} if not c.buyers.complete else set())
            assert BUDGET in reasons, k
            assert reasons <= fresh_reasons | {BUDGET}, k
            assert set(partial.transfers) <= set(fresh.transfers), k  # нічого не вигадано
        else:
            assert _stable(partial, "time_budget_seconds") == _stable(fresh, "time_budget_seconds"), k
        resumed = collect(state, FixtureRpcSource(directory), cfg, FakeClock())
        assert _stable(resumed) == _stable(fresh), k
        assert resumed.metadata.transactions_scanned == fresh.metadata.transactions_scanned, k
