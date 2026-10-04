# verifies: FR-001-11
"""Явні відмови без збою (T-019, contracts/ingest-service.md, кроки 1 і 4).

- Крок 1: некоректна адреса → `Rejection(invalid_address)` ДО будь-якого звернення до кешу чи джерела.
- Крок 4: `get_account_info(mint)` → `None` → `Rejection(token_not_found, account_missing)`; рахунок, що
  не належить Token/Token-2022 або не є mint (`data.parsed.type != "mint"`, відсутня/крива форма) →
  `Rejection(token_not_found, not_a_mint)`. Token-2022 mint приймається. Відмови не кешуються.
- Збій джерела на кроці 4 (будь-який `RpcError`) — не відмова й не виняток: `IngestResult` зі
  `status=incomplete`, `buyers.complete=false`, порожніми покупцями/переказами; стан кладеться в
  `partial`, наступний виклик продовжує. Не-`RpcError` виняток джерела — дефект адаптера, виходить.
- Крок 4 виконується на кожному шляху, що не віддається з кешу (новий збір і resume), під тим самим
  бюджетом часу, що й збір; `metadata.rpc_calls` включає це звернення.
Мережі немає: `FixtureRpcSource`, `FakeClock`.
"""

import copy
import dataclasses
import json
import math
from pathlib import Path

import pytest

from unmask.ingest.budget import FakeClock
from unmask.ingest.cache import ResultCache
from unmask.ingest.collector import CollectionState, collect
from unmask.ingest.config import load_config
from unmask.ingest.model import (
    CompletenessStatus,
    IngestResult,
    MissingReason,
    RejectKind,
    Rejection,
)
from unmask.ingest.rpc.fixture import FailAfter, FailFor, FixtureRpcSource
from unmask.ingest.rpc.protocol import RpcError, RpcRateLimited, RpcTimeout, RpcUnavailable
from unmask.ingest.service import IngestService

ROOT = Path(__file__).resolve().parent.parent
SCENARIOS = ROOT / "tests" / "fixtures" / "scenarios"
BASIC, NOTFOUND = SCENARIOS / "basic", SCENARIOS / "notfound"
SHIPPED = ROOT / "config" / "ingest.yaml"

EXPECTED = json.loads((BASIC / "expected.json").read_text())
M = EXPECTED["mint"]
A = EXPECTED["wallets"]["A"]
CAST = json.loads((NOTFOUND / "rpc.json").read_text())["_meta"]["cast"]
MISSING_MINT, SYSTEM_OWNED, TOKEN_ACCOUNT = CAST["MISSING_MINT"], CAST["SYSTEM_OWNED"], CAST["TOKEN_ACCOUNT"]
SYSTEM_PROGRAM = "11111111111111111111111111111111"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
ACCOUNT_CALL = ("getAccountInfo", {"address": M})

VOLATILE = ("analyzed_at", "elapsed_seconds", "rpc_calls", "resumed", "served_from_cache")

_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _ALPHABET[r] + out
    return "1" * (len(data) - len(data.lstrip(b"\0"))) + out


def _cfg(**overrides):
    cfg = dataclasses.replace(load_config(SHIPPED), **EXPECTED["config"])
    return dataclasses.replace(cfg, **overrides)


def _stable(result: IngestResult):
    meta = dataclasses.asdict(result.metadata)
    for key in VOLATILE:
        meta.pop(key)
    return (meta, result.completeness, result.buyers, result.transfers, result.unexpanded)


def _fresh(cfg=None, directory: Path = BASIC, mint: str = M):
    cfg = cfg or _cfg()
    source = FixtureRpcSource(directory)
    result = collect(CollectionState(mint=mint, config_version=cfg.version), source, cfg, FakeClock())
    return result, source.calls


def _service(cfg=None, *, directory: Path = BASIC, failures=None, clock=None, cache=None, source=None):
    clock = clock if clock is not None else FakeClock()
    source = source if source is not None else FixtureRpcSource(directory, failures=failures, clock=clock)
    return IngestService(cfg or _cfg(), source, clock=clock, cache=cache), source


class _ForbiddenCache(ResultCache):
    """Кеш, будь-яке звернення до якого — провал тесту (крок 1 стоїть до кешу)."""

    def __getattribute__(self, name):
        if name.startswith("get_") or name.startswith("put_") or name == "drop_partial":
            raise AssertionError(f"cache.{name} touched before address validation")
        return super().__getattribute__(name)


class _ScriptedAccount(FixtureRpcSource):
    """Фікстурне джерело, у якого `get_account_info` повертає задану форму (журнал і годинник — як у базового)."""

    def __init__(self, directory, account, **kw):
        super().__init__(directory, **kw)
        self._scripted = account

    def get_account_info(self, address, *, deadline):
        super().get_account_info(address, deadline=deadline)  # журнал, годинник, політики збоїв
        value = self._scripted(address) if callable(self._scripted) else self._scripted
        return copy.deepcopy(value)


def _basic_account() -> dict:
    return json.loads((BASIC / "rpc.json").read_text())["getAccountInfo"][M]


# --- Крок 1: некоректна адреса ------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("", id="empty"),
        pytest.param("   ", id="whitespace"),
        pytest.param(M[:-1] + "0", id="non-base58-0"),
        pytest.param(M[:-1] + "O", id="non-base58-O"),
        pytest.param(M[:-1] + "l", id="non-base58-l"),
        pytest.param(" " + M, id="leading-space"),
        pytest.param(M + "\n", id="trailing-newline"),
        pytest.param(_b58(b"\x07" * 31), id="31-bytes"),
        pytest.param(_b58(b"\x07" * 33), id="33-bytes"),
        pytest.param("1" * 31, id="31-zero-bytes"),
        pytest.param("1" * 33, id="33-zero-bytes"),
        pytest.param("x" * 10_000, id="very-long"),
    ],
)
def test_invalid_address_rejected_without_source_call(value):
    service, source = _service(cache=_ForbiddenCache())
    outcome = service.collect(value)
    assert outcome == Rejection(kind=RejectKind.INVALID_ADDRESS, mint=value, detail=outcome.detail)
    assert outcome.detail  # пояснення, що саме не так
    assert source.calls == []


@pytest.mark.parametrize("value", [None, 123, b"FeC5", ["x"], 1.5], ids=repr)
def test_non_string_address_rejected_without_source_call(value):
    service, source = _service(cache=_ForbiddenCache())
    outcome = service.collect(value)
    assert isinstance(outcome, Rejection)
    assert outcome.kind is RejectKind.INVALID_ADDRESS
    assert outcome.mint == ""  # Rejection.mint — рядок; не-рядок не відтворюється
    assert type(value).__name__ in outcome.detail
    assert source.calls == []


def test_invalid_address_rejected_even_when_cache_has_entries():
    cache = ResultCache()
    service, source = _service(cache=cache)
    service.collect(M)
    n = len(source.calls)
    assert isinstance(service.collect(M[:-1] + "0"), Rejection)
    assert len(source.calls) == n


def test_valid_system_program_address_passes_step_1():
    # 32 нульові байти — коректна адреса: крок 1 пропускає, відмову дає крок 4 (рахунку немає у фікстурі)
    service, source = _service()
    outcome = service.collect(SYSTEM_PROGRAM)
    assert outcome == Rejection(RejectKind.TOKEN_NOT_FOUND, SYSTEM_PROGRAM, "account_missing")
    assert source.calls == [("getAccountInfo", {"address": SYSTEM_PROGRAM})]


# --- Крок 4: існування токена -------------------------------------------------------------


def test_missing_account_is_token_not_found_account_missing():
    service, source = _service(directory=NOTFOUND)
    outcome = service.collect(MISSING_MINT)
    assert outcome == Rejection(kind=RejectKind.TOKEN_NOT_FOUND, mint=MISSING_MINT, detail="account_missing")
    assert source.calls == [("getAccountInfo", {"address": MISSING_MINT})]  # збору немає


def test_system_owned_account_is_token_not_found_not_a_mint():
    service, source = _service(directory=NOTFOUND)
    outcome = service.collect(SYSTEM_OWNED)
    assert outcome == Rejection(kind=RejectKind.TOKEN_NOT_FOUND, mint=SYSTEM_OWNED, detail="not_a_mint")
    assert source.calls == [("getAccountInfo", {"address": SYSTEM_OWNED})]


def test_token_account_owned_by_token_program_is_not_a_mint():
    service, source = _service(directory=NOTFOUND)
    outcome = service.collect(TOKEN_ACCOUNT)
    assert outcome == Rejection(kind=RejectKind.TOKEN_NOT_FOUND, mint=TOKEN_ACCOUNT, detail="not_a_mint")
    assert source.calls == [("getAccountInfo", {"address": TOKEN_ACCOUNT})]


def test_mint_shaped_account_of_foreign_owner_is_not_a_mint():
    forged = _basic_account() | {"owner": SYSTEM_PROGRAM}
    service, source = _service(source=_ScriptedAccount(BASIC, forged))
    assert service.collect(M) == Rejection(RejectKind.TOKEN_NOT_FOUND, M, "not_a_mint")
    assert source.calls == [ACCOUNT_CALL]


@pytest.mark.parametrize("owner", [TOKEN_PROGRAM, TOKEN_2022_PROGRAM], ids=["token", "token-2022"])
def test_token_2022_mint_accepted(owner):
    account = _basic_account() | {"owner": owner}
    service, source = _service(source=_ScriptedAccount(BASIC, account))
    result = service.collect(M)
    assert isinstance(result, IngestResult)
    fresh, fresh_calls = _fresh()
    assert _stable(result) == _stable(fresh)
    assert source.calls == [ACCOUNT_CALL] + fresh_calls  # рівно одне звернення кроку 4 перед збором


def test_rejection_not_cached_and_retried_next_call():
    cache = ResultCache()
    service, source = _service(directory=NOTFOUND, cache=cache)
    for _ in range(3):  # кожен виклик знову питає джерело
        assert service.collect(MISSING_MINT).detail == "account_missing"
        assert cache.get_complete(MISSING_MINT) is None and cache.get_partial(MISSING_MINT) is None
    assert source.calls == [("getAccountInfo", {"address": MISSING_MINT})] * 3
    assert service.collect(SYSTEM_OWNED).detail == "not_a_mint"
    assert cache.get_complete(SYSTEM_OWNED) is None and cache.get_partial(SYSTEM_OWNED) is None


def test_token_that_appears_later_is_collected_after_rejection():
    seen = {"n": 0}

    def account(address):
        seen["n"] += 1
        return None if seen["n"] == 1 else _basic_account()

    service, source = _service(source=_ScriptedAccount(BASIC, account))
    assert service.collect(M) == Rejection(RejectKind.TOKEN_NOT_FOUND, M, "account_missing")
    result = service.collect(M)  # відмова не закешована: питаємо знову, токен тепер є
    assert isinstance(result, IngestResult)
    assert result.completeness.status is CompletenessStatus.COMPLETE
    assert _stable(result) == _stable(_fresh()[0])


# --- Гарантія: жодного винятку через дані -------------------------------------------------

_GOOD = {"owner": TOKEN_PROGRAM, "data": {"parsed": {"type": "mint", "info": {}}}}

_CURVED = [
    pytest.param(None, "account_missing", id="none"),
    pytest.param({}, "not_a_mint", id="empty-dict"),
    pytest.param({"data": _GOOD["data"]}, "not_a_mint", id="no-owner"),
    pytest.param({"owner": None, "data": _GOOD["data"]}, "not_a_mint", id="owner-none"),
    pytest.param({"owner": 7, "data": _GOOD["data"]}, "not_a_mint", id="owner-int"),
    pytest.param({"owner": [TOKEN_PROGRAM], "data": _GOOD["data"]}, "not_a_mint", id="owner-list-unhashable"),
    pytest.param({"owner": {"x": 1}, "data": _GOOD["data"]}, "not_a_mint", id="owner-dict-unhashable"),
    pytest.param({"owner": TOKEN_PROGRAM.lower(), "data": _GOOD["data"]}, "not_a_mint", id="owner-case"),
    pytest.param({"owner": TOKEN_PROGRAM}, "not_a_mint", id="no-data"),
    pytest.param({"owner": TOKEN_PROGRAM, "data": None}, "not_a_mint", id="data-none"),
    pytest.param({"owner": TOKEN_PROGRAM, "data": ["AAAA", "base64"]}, "not_a_mint", id="data-base64-list"),
    pytest.param({"owner": TOKEN_PROGRAM, "data": "AAAA"}, "not_a_mint", id="data-str"),
    pytest.param({"owner": TOKEN_PROGRAM, "data": {}}, "not_a_mint", id="data-without-parsed"),
    pytest.param({"owner": TOKEN_PROGRAM, "data": {"parsed": None}}, "not_a_mint", id="parsed-none"),
    pytest.param({"owner": TOKEN_PROGRAM, "data": {"parsed": "mint"}}, "not_a_mint", id="parsed-str"),
    pytest.param({"owner": TOKEN_PROGRAM, "data": {"parsed": ["mint"]}}, "not_a_mint", id="parsed-list"),
    pytest.param({"owner": TOKEN_PROGRAM, "data": {"parsed": {}}}, "not_a_mint", id="parsed-without-type"),
    pytest.param({"owner": TOKEN_PROGRAM, "data": {"parsed": {"type": None}}}, "not_a_mint", id="type-none"),
    pytest.param({"owner": TOKEN_PROGRAM, "data": {"parsed": {"type": ["mint"]}}}, "not_a_mint", id="type-list"),
    pytest.param({"owner": TOKEN_PROGRAM, "data": {"parsed": {"type": {"m": 1}}}}, "not_a_mint", id="type-dict"),
    pytest.param({"owner": TOKEN_PROGRAM, "data": {"parsed": {"type": "Mint"}}}, "not_a_mint", id="type-case"),
    pytest.param({"owner": TOKEN_PROGRAM, "data": {"parsed": {"type": "account"}}}, "not_a_mint", id="type-account"),
    pytest.param("account", "not_a_mint", id="not-a-dict-str"),
    pytest.param(["owner", TOKEN_PROGRAM], "not_a_mint", id="not-a-dict-list"),
    pytest.param(42, "not_a_mint", id="not-a-dict-int"),
    pytest.param(True, "not_a_mint", id="not-a-dict-bool"),
    pytest.param(0, "not_a_mint", id="falsy-int"),
    pytest.param("", "not_a_mint", id="falsy-str"),
]


@pytest.mark.parametrize("account, detail", _CURVED)
def test_no_exception_escapes_collect_for_any_data_problem(account, detail):
    cache = ResultCache()
    service, source = _service(source=_ScriptedAccount(BASIC, account), cache=cache)
    outcome = service.collect(M)  # без винятку
    assert outcome == Rejection(kind=RejectKind.TOKEN_NOT_FOUND, mint=M, detail=detail)
    assert source.calls == [ACCOUNT_CALL]  # збір не почався
    assert cache.get_complete(M) is None and cache.get_partial(M) is None


def test_minimal_valid_mint_shape_is_accepted():
    service, source = _service(source=_ScriptedAccount(BASIC, _GOOD))
    assert isinstance(service.collect(M), IngestResult)


# --- Збій джерела на кроці 4: неповнота, а не відмова ---------------------------------------


class _UnknownRpcError(RpcError):
    """Підклас поза контрактом (дефект адаптера) — захисний шар мапить у unavailable."""


class _SlowRateLimit(RpcRateLimited):
    """Підклас відомого винятку зберігає його причину."""


_STEP4_FAILURES = [
    pytest.param(RpcRateLimited(retry_after=2.0), MissingReason.RATE_LIMITED, id="rate_limited"),
    pytest.param(RpcTimeout("slow"), MissingReason.TIMEOUT, id="timeout"),
    pytest.param(RpcUnavailable("node down"), MissingReason.UNAVAILABLE, id="unavailable"),
    pytest.param(RpcError("base"), MissingReason.UNAVAILABLE, id="base-RpcError"),
    pytest.param(_UnknownRpcError("odd"), MissingReason.UNAVAILABLE, id="unknown-subclass"),
    pytest.param(_SlowRateLimit(1.0), MissingReason.RATE_LIMITED, id="known-subclass"),
]


@pytest.mark.parametrize("exc, reason", _STEP4_FAILURES)
def test_source_failure_on_token_check_is_incomplete_not_rejection(exc, reason):
    cache = ResultCache()
    cfg = _cfg()
    service, source = _service(cfg, failures=[FailFor(M, exc, times=1)], cache=cache)
    result = service.collect(M)
    assert isinstance(result, IngestResult)  # не Rejection і не виняток
    assert source.calls == [ACCOUNT_CALL]  # після збою на кроці 4 збір не йде
    c = result.completeness
    assert c.status is CompletenessStatus.INCOMPLETE
    assert c.buyers.complete is False and c.buyers.reason is reason
    assert c.missing == ()
    for part in ("getAccountInfo", M, type(exc).__name__):
        assert part in c.buyers.detail
    assert result.buyers == () and result.transfers == () and result.unexpanded == ()
    meta = result.metadata
    assert (meta.mint, meta.config_version, meta.rpc_calls, meta.wallets_analyzed) == (M, cfg.version, 1, 0)
    assert (meta.first_buyers_n, meta.funding_depth, meta.counterparty_threshold) == (
        cfg.first_buyers_n, cfg.funding_depth, cfg.counterparty_threshold)
    assert (meta.max_signatures_per_wallet, meta.collect_spl_inbound, meta.time_budget_seconds) == (
        cfg.max_signatures_per_wallet, cfg.collect_spl_inbound, cfg.time_budget_seconds)
    assert meta.transactions_scanned == 0 and meta.source == source.name
    assert meta.resumed is False and meta.served_from_cache is False
    # неповний — не остаточний; стан у partial, щоб наступний виклик продовжив
    assert cache.get_complete(M) is None
    assert cache.get_partial(M) == CollectionState(mint=M, config_version=cfg.version)

    calls_before = len(source.calls)
    second = service.collect(M)  # джерело відпустило
    fresh, fresh_calls = _fresh(cfg)
    assert source.calls[calls_before:] == [ACCOUNT_CALL] + fresh_calls
    assert second.completeness.status is CompletenessStatus.COMPLETE
    assert _stable(second) == _stable(fresh)
    assert cache.get_partial(M) is None and cache.get_complete(M) == second


def test_step4_failure_while_resuming_keeps_partial_and_next_call_continues():
    cache = ResultCache()
    cfg = _cfg()
    # 1) неповний збір: A недоступний → partial
    first, _ = _service(cfg, failures=[FailFor(A, RpcUnavailable("down"), times=1)], cache=cache)
    assert first.collect(M).completeness.status is CompletenessStatus.INCOMPLETE
    saved = cache.get_partial(M)
    assert saved is not None and saved.missing
    # 2) resume: збій на кроці 4 → неповний результат, partial не зіпсовано
    failing, failing_source = _service(cfg, failures=[FailAfter(1, RpcTimeout("slow"))], cache=cache)
    r = failing.collect(M)
    assert isinstance(r, IngestResult) and r.completeness.status is CompletenessStatus.INCOMPLETE
    assert r.completeness.buyers.reason is MissingReason.TIMEOUT
    assert r.buyers == () and r.transfers == () and r.metadata.rpc_calls == 1
    assert r.metadata.resumed is False  # продовження не відбулось
    assert failing_source.calls == [ACCOUNT_CALL]
    assert cache.get_partial(M) == saved and cache.get_complete(M) is None
    # 3) здоровий resume: крок 4 ще раз + лише недоотримане
    healthy, healthy_source = _service(cfg, cache=cache)
    final = healthy.collect(M)
    assert healthy_source.calls[0] == ACCOUNT_CALL
    assert len(healthy_source.calls) < 1 + len(_fresh(cfg)[1])
    assert final.metadata.resumed is True
    assert _stable(final) == _stable(_fresh(cfg)[0])
    assert final.metadata.rpc_calls == len(healthy_source.calls)


def test_token_check_runs_on_resume_path_too():
    cache = ResultCache()
    first, _ = _service(failures=[FailFor(A, RpcUnavailable("down"), times=1)], cache=cache)
    first.collect(M)
    assert cache.get_partial(M) is not None
    # токен «зник» між викликами: resume не йде, відмова, partial лишається (відмова нічого не чіпає)
    saved = cache.get_partial(M)
    gone, source = _service(source=_ScriptedAccount(BASIC, None), cache=cache)
    assert gone.collect(M) == Rejection(RejectKind.TOKEN_NOT_FOUND, M, "account_missing")
    assert source.calls == [ACCOUNT_CALL]
    assert cache.get_partial(M) == saved


def test_non_rpc_error_from_source_is_adapter_defect_and_escapes():
    class Broken(FixtureRpcSource):
        def get_account_info(self, address, *, deadline):
            raise KeyError("adapter bug")

    service, _ = _service(source=Broken(BASIC))
    with pytest.raises(KeyError):
        service.collect(M)


def test_budget_exhausting_timeout_on_token_check_is_budget_exhausted():
    cfg = _cfg(time_budget_seconds=5.0)
    clock = FakeClock(advance_per_call=5.0)  # запит з'їв увесь бюджет, адаптер обірвав його таймаутом
    service, source = _service(cfg, clock=clock, failures=[FailFor(M, RpcTimeout("budget"), times=1)])
    result = service.collect(M)
    assert source.calls == [ACCOUNT_CALL]
    assert result.completeness.buyers.reason is MissingReason.BUDGET_EXHAUSTED
    assert "getAccountInfo" in result.completeness.buyers.detail
    assert result.metadata.elapsed_seconds == 5.0


def test_budget_expired_before_token_check_makes_no_call_and_counts_none():
    class Jumping:  # кожне читання монотонного часу просуває його на весь бюджет
        def __init__(self, step):
            self.t, self.step = -step, step

        def monotonic(self):
            self.t += self.step
            return self.t

        def wall(self):
            return 0

    cfg = _cfg(time_budget_seconds=3.0)
    cache = ResultCache()
    source = FixtureRpcSource(BASIC)
    result = IngestService(cfg, source, clock=Jumping(3.0), cache=cache).collect(M)
    assert source.calls == []
    assert result.completeness.buyers.reason is MissingReason.BUDGET_EXHAUSTED
    assert "getAccountInfo" in result.completeness.buyers.detail
    assert result.metadata.rpc_calls == 0
    assert cache.get_partial(M) is not None and cache.get_complete(M) is None


# --- Бюджет і метадані: крок 4 — частина прогону сервісу -----------------------------------


@pytest.mark.parametrize("budget", [2.0, 3.0, 5.0, 7.5, 12.0, 25.0])
def test_token_check_runs_under_the_service_budget(budget):
    # кожне звернення триває 1 с; бюджет покриває і крок 4, і збір: звернень, що почались до межі,
    # не більше за ceil(budget) — без спільного дедлайну збір мав би повний бюджет після кроку 4
    clock = FakeClock(advance_per_call=1.0)
    service, source = _service(_cfg(time_budget_seconds=budget), clock=clock)
    result = service.collect(M)
    assert len(source.calls) <= math.ceil(budget)
    assert result.metadata.elapsed_seconds <= budget + _cfg().rpc.request_timeout_seconds
    assert result.completeness.status is CompletenessStatus.INCOMPLETE
    assert result.completeness.buyers.complete is False or any(
        m.reason is MissingReason.BUDGET_EXHAUSTED for m in result.completeness.missing)


@pytest.mark.parametrize("budget", [2.0, 3.0, 4.0, 5.0, 7.5, 8.0])
def test_token_check_and_resume_share_the_service_budget(budget):
    # те саме на шляху resume: крок 4 і `collector.resume` ділять один дедлайн сервісу
    cache = ResultCache()
    first, _ = _service(failures=[FailFor(A, RpcUnavailable("down"), times=1)], cache=cache)
    assert first.collect(M).completeness.status is CompletenessStatus.INCOMPLETE
    assert cache.get_partial(M) is not None
    clock = FakeClock(advance_per_call=1.0)
    service, source = _service(_cfg(time_budget_seconds=budget), clock=clock, cache=cache)
    result = service.collect(M)
    assert result.metadata.resumed is True
    assert source.calls[0] == ACCOUNT_CALL
    assert len(source.calls) <= math.ceil(budget)
    assert result.metadata.elapsed_seconds <= math.ceil(budget)
    assert result.completeness.status is CompletenessStatus.INCOMPLETE  # бюджет справді обмежив resume


def test_metadata_counts_token_check_call_and_time():
    clock = FakeClock(advance_per_call=1.0, wall_start=1_700_000_000)  # цілі секунди: wall теж іде
    service, source = _service(_cfg(time_budget_seconds=1000.0), clock=clock)
    result = service.collect(M)
    assert result.completeness.status is CompletenessStatus.COMPLETE
    assert result.metadata.rpc_calls == len(source.calls) == 1 + len(_fresh()[1])
    assert result.metadata.elapsed_seconds == 1.0 * len(source.calls)
    assert result.metadata.analyzed_at == 1_700_000_000  # початок прогону сервісу, не збору після кроку 4
    # повторний — з кешу, ті самі метадані першого повернення
    assert service.collect(M).metadata.rpc_calls == result.metadata.rpc_calls


def test_collector_without_service_makes_no_account_info_call():
    # інваріант попередніх задач: свіжий збір колектором (без сервісу) кроку 4 не робить
    _result, calls = _fresh()
    assert all(method != "getAccountInfo" for method, _ in calls)
