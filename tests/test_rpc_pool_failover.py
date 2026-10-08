# verifies: FR-007-05, FR-007-08, FR-007-12
"""Перебирання провайдерів і класи помилок пулу (T-140, critical; contracts/pooled-rpc-source.md, research R-4).

Помилка тут тиха: пул, що при відмові підмінив клас винятку (429 -> `unavailable`), загубив частку,
перебрав дефект або дав давній транзакції неархівному вузлу, не ламає збірку, а змінює висновок збору
(причину неповноти, а то й сам результат). Тому тести перевіряють не лише «відмовив один — вийшло», а й межі:

- відмова `RpcRateLimited`/`RpcTimeout`/`RpcUnavailable`/`RpcBudgetTimeout` (і хибна форма частки) переводить
  частку на інших придатних; провайдер, що відмовив, виключений до кінця ВИКЛИКУ (але не назавжди), тож
  завершення гарантоване, а відмовлений вдруге не викликається;
- після `RpcRateLimited` провайдер на паузі `max(cooldown, retry_after)` за годинником пулу: поки пауза триває,
  нових запитів не отримує, коли є інші; на межі (`now == until`) пауза вже скінчилась; усі на паузі -> пауза
  ігнорується; непридатний `retry_after` (NaN, inf, мінус, bool) паузу не подовжує;
- коли придатних не лишилось — піднімається ОСТАННЯ помилка найменшої нерозвʼязаної позиції, ТОЙ САМИЙ об'єкт
  адаптера, без `__cause__`/`__context__` (raise поза except), без дописаного пулом тексту; клас = клас
  одинокого провайдера (збір -> `incomplete` з тією ж причиною);
- не-`RpcError` (дефект) не перебирається й пріоритетніший за відмови; `RpcBudgetTimeout` ключа перебирається
  без паузи, а вичерпаний дедлайн -> свіжий `RpcBudgetTimeout` без нових звернень; архівність поважається: давня транзакція неархівному не віддається й після відмови
  архівного; SC-005: відмова будь-яких N-1 із N провайдерів дає той самий результат збору.

Без мережі: `ScriptedProvider` поверх власних `FixtureRpcSource`, `FakeClock`, `SequentialRunner`.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import random
import threading
from itertools import combinations
from pathlib import Path

import pytest

from rpc_pool_doubles import ScriptedFailure, ScriptedProvider, SequentialRunner
from unmask.ingest.budget import Deadline, FakeClock
from unmask.ingest.config import load_config
from unmask.ingest.model import CompletenessStatus
from unmask.ingest.rpc.fixture import FixtureRpcSource
from unmask.ingest.rpc.pool import PooledRpcSource, Provider, ThreadRunner
from unmask.ingest.rpc.protocol import (
    RpcBudgetTimeout,
    RpcError,
    RpcRateLimited,
    RpcTimeout,
    RpcUnavailable,
)
from unmask.ingest.serialize import to_dict, to_json
from unmask.ingest.service import IngestService

ROOT = Path(__file__).resolve().parent.parent
SCENARIOS = ROOT / "tests" / "fixtures" / "scenarios"
SHIPPED = ROOT / "config" / "ingest.yaml"
BASIC, HUB = SCENARIOS / "basic", SCENARIOS / "hub"
BASIC_EXPECTED = json.loads((BASIC / "expected.json").read_text(encoding="utf-8"))
HUB_EXPECTED = json.loads((HUB / "expected.json").read_text(encoding="utf-8"))
CASES = {
    "basic": (BASIC, BASIC_EXPECTED["mint"], BASIC_EXPECTED["config"]),
    "hub": (HUB, HUB_EXPECTED["mint"], {**BASIC_EXPECTED["config"], **HUB_EXPECTED["cases"][0]["config"]}),
}

TX = "getTransaction"
SHAPE_TEXT = "unexpected response shape: pool share length"
RETENTION_TEXT = "history beyond provider retention"
NOW = 1_700_000_000
RETENTION = 1000
FRESH = NOW - 10
OLD = NOW - RETENTION - 5000
ADDRESS = "ADDR"
SECRET = "SENTINEL-API-KEY-0123456789"

ERRORS = {
    "rate_limited": lambda: RpcRateLimited(2.5),
    "timeout": lambda: RpcTimeout("request timed out"),
    "unavailable": lambda: RpcUnavailable("http 5xx server error"),
    "budget": RpcBudgetTimeout,
}


# --------------------------------------------------------------------------- допоміжне


def _deadline(seconds: float = 600.0) -> Deadline:
    return Deadline(FakeClock(), seconds)


def _signatures(n: int) -> list[str]:
    return [f"sig{i:03d}" for i in range(n)]


@pytest.fixture
def scenario(tmp_path) -> Path:
    """60 підписів: відповідь несе власний підпис, тож порядок і повноту видно в результаті."""
    rpc = {
        "getTransaction": {
            sig: {"slot": 1000 + i, "blockTime": 1_700_000_000 + i, "signature": sig}
            for i, sig in enumerate(_signatures(60))
        }
    }
    (tmp_path / "rpc.json").write_text(json.dumps(rpc), encoding="utf-8")
    return tmp_path


def _fail(exc, method: str = TX, calls=None) -> ScriptedFailure:
    return ScriptedFailure(method, None if calls is None else frozenset(calls), exc)


def _provider(name: str, source, weight: float = 1.0, **overrides) -> Provider:
    values = dict(name=name, source=source, weight=weight, archival=True, retention_seconds=None)
    values.update(overrides)
    return Provider(**values)


def _pool(providers, *, runner=None, clock=None, cooldown: float = 30.0) -> PooledRpcSource:
    return PooledRpcSource(
        providers,
        clock=clock or FakeClock(wall_start=NOW),
        rate_limit_cooldown_seconds=cooldown,
        age_cache_entries=1000,
        runner=runner or SequentialRunner(),
    )


def _kit(scenario_dir, weights, fails=None, *, runner=None, clock=None, cooldown: float = 30.0):
    """Пул із `ScriptedProvider` на провайдера (власне `FixtureRpcSource` кожному); `fails` — `{індекс: [збої]}`."""
    fails = fails or {}
    doubles = [
        ScriptedProvider(FixtureRpcSource(scenario_dir), failures=fails.get(i, ())) for i in range(len(weights))
    ]
    providers = [_provider(f"p{i}", d, w) for i, (d, w) in enumerate(zip(doubles, weights))]
    return _pool(providers, runner=runner, clock=clock, cooldown=cooldown), doubles


class _ShuffleRunner:
    """Виконує задачі раунду у випадковому (зерно) порядку: довжина раунду змінюється після перебирання."""

    def __init__(self, seed: int) -> None:
        self._rng = random.Random(seed)

    def __call__(self, tasks):
        order = list(range(len(tasks)))
        self._rng.shuffle(order)
        return SequentialRunner(order)(tasks)


def _runner(kind):
    return _ShuffleRunner(kind) if isinstance(kind, int) else SequentialRunner(kind)


def _tx_log(double: ScriptedProvider) -> list[tuple[str, ...]]:
    return [keys for method, keys in double.log if method == TX]


def _got(raws) -> list[str | None]:
    return [None if raw is None else raw["signature"] for raw in raws]


class _Src:
    """Мінімальне джерело для одиночних методів: повертає свою мітку; дедлайн ігнорує."""

    def __init__(self, label: str, name: str = "src") -> None:
        self.label, self.name = label, name

    def get_account_info(self, address, *, deadline):
        return {"owner": self.label}

    def get_signatures_for_address(self, address, *, before, until, limit, deadline):
        return [{"signature": f"{self.label}{i}", "blockTime": FRESH} for i in range(limit)]

    def get_token_accounts_by_owner(self, owner, *, deadline):
        return [{"pubkey": self.label, "account": {}}]

    def get_transactions(self, signatures, *, deadline):
        return [{"signature": s} for s in signatures]


SINGLE = {
    "getAccountInfo": lambda pool, dl: pool.get_account_info("addr", deadline=dl),
    "getSignaturesForAddress": lambda pool, dl: pool.get_signatures_for_address(
        "addr", before=None, until=None, limit=5, deadline=dl
    ),
    "getTokenAccountsByOwner": lambda pool, dl: pool.get_token_accounts_by_owner("owner", deadline=dl),
}


def _singles(labels, fails=None, *, clock=None, cooldown: float = 30.0, weights=None, **overrides):
    """Пул з `ScriptedProvider(_Src)` на мітку; `fails` — `{мітка: [збої]}`."""
    fails = fails or {}
    doubles = [ScriptedProvider(_Src(label), failures=fails.get(label, ())) for label in labels]
    weights = weights or [1.0] * len(labels)
    providers = [
        _provider(label, d, w, **overrides.get(label, {})) for label, d, w in zip(labels, doubles, weights)
    ]
    return _pool(providers, clock=clock, cooldown=cooldown), doubles


def _calls(double: ScriptedProvider, method: str) -> int:
    return sum(1 for m, _ in double.log if m == method)


def _assert_clean(exc: BaseException, expected: BaseException | None = None) -> None:
    if expected is not None:
        assert exc is expected  # сам об'єкт адаптера: клас і фіксований текст збережені
    assert exc.__cause__ is None and exc.__context__ is None and not exc.__suppress_context__


# --------------------------------------------------------------------------- US3-1: частку беруть інші


@pytest.mark.parametrize("order", ["forward", "reverse"])
def test_rate_limited_share_taken_by_others(scenario, order):
    sigs = _signatures(30)
    pool, (d0, d1, d2) = _kit(
        scenario, [1, 1, 1], {1: [_fail(RpcRateLimited(2.5))]}, runner=SequentialRunner(order)
    )

    got = pool.get_transactions(sigs, deadline=_deadline())

    assert _got(got) == sigs  # результат повний і в початковому порядку
    assert _tx_log(d1) == [tuple(sigs[10:20])]  # провайдер, що відмовив, викликаний рівно раз
    retaken = [keys for d in (d0, d2) for keys in _tx_log(d)[1:]]
    assert sorted(sig for keys in retaken for sig in keys) == sigs[10:20]  # його частку розділили d0 і d2
    assert len(_tx_log(d0)) == 2 and len(_tx_log(d2)) == 2


# Відмова провайдера. `RpcBudgetTimeout` ключа (пейсер не вмістив запит у дедлайн) — теж: інший ключ має свій кошик (SC-005).
RETRYABLE = ["rate_limited", "timeout", "unavailable", "budget"]


@pytest.mark.parametrize("name", RETRYABLE)
def test_any_provider_failure_class_fails_over(scenario, name):
    sigs = _signatures(20)
    pool, (d0, d1) = _kit(scenario, [1, 1], {0: [_fail(ERRORS[name]())]})
    assert _got(pool.get_transactions(sigs, deadline=_deadline())) == sigs
    assert [len(_tx_log(d)) for d in (d0, d1)] == [1, 2]


def test_wrong_share_shape_is_a_failure_that_fails_over(scenario):
    class Broken:
        name = "broken"

        def get_transactions(self, signatures, *, deadline):
            return []

    good = ScriptedProvider(FixtureRpcSource(scenario))
    pool = _pool([_provider("broken", Broken()), _provider("good", good)])
    sigs = _signatures(10)
    assert _got(pool.get_transactions(sigs, deadline=_deadline())) == sigs
    assert _tx_log(good) == [tuple(sigs[5:]), tuple(sigs[:5])]


def test_failed_provider_is_excluded_until_the_end_of_the_call_not_retried(scenario):
    """p0 впав у 1-му зверненні (далі б відповів), p1 падає завжди: p0 у цьому виклику вдруге НЕ викликається."""
    sigs = _signatures(20)
    pool, (d0, d1) = _kit(
        scenario, [1, 1], {0: [_fail(RpcTimeout("request timed out"), calls=[1])], 1: [_fail(RpcTimeout("x"))]}
    )
    with pytest.raises(RpcTimeout):
        pool.get_transactions(sigs, deadline=_deadline())
    assert (len(_tx_log(d0)), len(_tx_log(d1))) == (1, 1)  # ніхто не викликаний вдруге: завершення гарантоване


def test_failed_provider_is_usable_again_in_the_next_call_when_not_rate_limited(scenario):
    sigs = _signatures(20)
    pool, (d0, d1) = _kit(scenario, [1, 1], {0: [_fail(RpcTimeout("request timed out"), calls=[1])]})
    pool.get_transactions(sigs, deadline=_deadline())
    assert len(_tx_log(d0)) == 1
    pool.get_transactions(sigs, deadline=_deadline())  # виключення діє до кінця ВИКЛИКУ, не назавжди
    assert len(_tx_log(d0)) == 2


@pytest.mark.parametrize("runner", ["forward", "reverse", 1, 2, 3])
def test_answers_stay_in_position_order_after_failover(scenario, runner):
    sigs = _signatures(45)
    rng = random.Random(7)
    shuffled = sigs[:]
    rng.shuffle(shuffled)  # підписи не за зростанням: порядок відповіді тримається на позиції, не на значенні
    pool, doubles = _kit(
        scenario, [12, 4.5, 4.5], {0: [_fail(RpcRateLimited(None), calls=[1])], 2: [_fail(RpcTimeout("t"), calls=[1])]},
        runner=_runner(runner),
    )
    assert _got(pool.get_transactions(shuffled, deadline=_deadline())) == shuffled


def test_failover_with_real_threads_gives_full_result(scenario):
    runner = ThreadRunner(3)
    try:
        for _ in range(5):
            pool, _ = _kit(
                scenario, [1, 1, 1], {1: [_fail(RpcRateLimited(1.0))], 2: [_fail(RpcTimeout("t"), calls=[1])]},
                runner=runner,
            )
            sigs = _signatures(30)
            assert _got(pool.get_transactions(sigs, deadline=_deadline())) == sigs
    finally:
        runner.close()


# --------------------------------------------------------------------------- паузи після 429


def test_rate_limited_provider_cooled_down_for_next_calls(scenario):
    clock = FakeClock(wall_start=NOW)
    sigs = _signatures(30)
    pool, (d0, d1, d2) = _kit(scenario, [1, 1, 1], {1: [_fail(RpcRateLimited(None), calls=[1])]}, clock=clock)
    pool.get_transactions(sigs, deadline=_deadline())
    assert len(_tx_log(d1)) == 1

    clock.advance(29.999)  # пауза 30 с ще триває
    got = pool.get_transactions(sigs, deadline=_deadline())
    assert _got(got) == sigs and len(_tx_log(d1)) == 1  # p1 нічого не отримав
    assert _tx_log(d0)[-1] == tuple(sigs[:15]) and _tx_log(d2)[-1] == tuple(sigs[15:])  # ділять між двома

    clock.advance(0.001)  # now == until: пауза скінчилась (межа: `now < until` — ще пауза)
    pool.get_transactions(sigs, deadline=_deadline())
    assert _tx_log(d1)[-1] == tuple(sigs[10:20])  # знову в розподілі


@pytest.mark.parametrize(
    ("retry_after", "pause"),
    [(None, 30.0), (5.0, 30.0), (30.0, 30.0), (120.0, 120.0), (float("nan"), 30.0), (float("inf"), 30.0),
     (-50.0, 30.0), (True, 30.0)],
    ids=["none", "below_cooldown", "equal", "above_cooldown", "nan", "inf", "negative", "bool"],
)
def test_pause_is_max_of_cooldown_and_valid_retry_after(scenario, retry_after, pause):
    exc = RpcRateLimited(retry_after)
    clock = FakeClock(wall_start=NOW)
    sigs = _signatures(20)
    pool, (d0, d1) = _kit(scenario, [1, 1], {1: [_fail(exc, calls=[1])]}, clock=clock)
    pool.get_transactions(sigs, deadline=_deadline())
    before = len(_tx_log(d1))
    clock.advance(pause - 1)
    pool.get_transactions(sigs, deadline=_deadline())
    assert len(_tx_log(d1)) == before  # за секунду до кінця паузи p1 ще не отримує нічого
    clock.advance(1)
    pool.get_transactions(sigs, deadline=_deadline())
    assert len(_tx_log(d1)) == before + 1  # рівно в кінці паузи — отримує


def test_pause_applies_to_single_methods_too_and_ends(scenario):
    clock = FakeClock()
    pool, (a, b) = _singles(["a", "b"], {"a": [_fail(RpcRateLimited(None), "getAccountInfo", calls=[1])]}, clock=clock)
    assert pool.get_account_info("addr", deadline=_deadline())["owner"] == "b"  # a: 429 -> b
    for _ in range(4):  # a на паузі: SWRR лишається за b
        assert pool.get_account_info("addr", deadline=_deadline())["owner"] == "b"
    assert _calls(a, "getAccountInfo") == 1
    clock.advance(30)
    owners = {pool.get_account_info("addr", deadline=_deadline())["owner"] for _ in range(4)}
    assert owners == {"a", "b"}


def test_pause_from_transactions_covers_single_methods(scenario):
    """Один кошик ліміту на всі методи ключа: 429 на getTransaction ставить на паузу ключ цілком."""
    clock = FakeClock(wall_start=NOW)
    d0 = ScriptedProvider(FixtureRpcSource(scenario), failures=[_fail(RpcRateLimited(None))])
    d1 = ScriptedProvider(FixtureRpcSource(scenario))
    pool = _pool([_provider("p0", d0), _provider("p1", d1)], clock=clock)
    pool.get_transactions(_signatures(10), deadline=_deadline())
    for _ in range(3):
        pool.get_account_info("addr", deadline=_deadline())
    assert _calls(d0, "getAccountInfo") == 0 and _calls(d1, "getAccountInfo") == 3


def test_all_providers_paused_pause_is_ignored(scenario):
    clock = FakeClock(wall_start=NOW)
    fails = {0: [_fail(RpcRateLimited(None), calls=[1])], 1: [_fail(RpcRateLimited(None), calls=[1])]}
    pool, (d0, d1) = _kit(scenario, [1, 1], fails, clock=clock)
    sigs = _signatures(20)
    with pytest.raises(RpcRateLimited):
        pool.get_transactions(sigs, deadline=_deadline())  # обидва на паузі
    got = pool.get_transactions(sigs, deadline=_deadline())  # годинник не рухався, але політика сама не валить збір
    assert _got(got) == sigs
    assert (len(_tx_log(d0)), len(_tx_log(d1))) == (2, 2)


def test_all_paused_single_methods_still_served():
    clock = FakeClock()
    fails = {l: [_fail(RpcRateLimited(None), "getAccountInfo", calls=[1])] for l in "ab"}
    pool, _ = _singles(["a", "b"], fails, clock=clock)
    with pytest.raises(RpcRateLimited):
        pool.get_account_info("addr", deadline=_deadline())
    assert pool.get_account_info("addr", deadline=_deadline()) is not None


def test_non_rate_limit_failure_does_not_pause_the_provider(scenario):
    sigs = _signatures(20)
    pool, (d0, d1) = _kit(scenario, [1, 1], {1: [_fail(RpcUnavailable("http 5xx server error"), calls=[1])]})
    pool.get_transactions(sigs, deadline=_deadline())
    pool.get_transactions(sigs, deadline=_deadline())
    assert len(_tx_log(d1)) == 2 and _tx_log(d1)[-1] == tuple(sigs[10:])  # без паузи p1 одразу в розподілі


# --------------------------------------------------------------------------- US3-2/3: остання помилка, клас


def test_all_timeout_raises_timeout(scenario):
    sigs = _signatures(20)
    # (а) 429, потім timeout: позиція 0 побувала в p0 (429) і в p1 (timeout) -> піднімається ОСТАННЯ помилка
    last = RpcTimeout("request timed out")
    first = RpcRateLimited(1.0)
    pool, (d0, d1) = _kit(scenario, [1, 1], {0: [_fail(first)], 1: [_fail(last, calls=[2])]})
    with pytest.raises(RpcTimeout) as info:
        pool.get_transactions(sigs, deadline=_deadline())
    _assert_clean(info.value, last)
    assert (len(_tx_log(d0)), len(_tx_log(d1))) == (1, 2)

    # (б) усі відмовляють timeout -> клас timeout
    pool, _ = _kit(scenario, [1, 1, 1], {i: [_fail(RpcTimeout("request timed out"))] for i in range(3)})
    with pytest.raises(RpcTimeout) as info:
        pool.get_transactions(sigs, deadline=_deadline())
    assert type(info.value) is RpcTimeout


def _cfg(values: dict):
    return dataclasses.replace(load_config(SHIPPED), **values)


def _collect(source, case: str):
    directory, mint, values = CASES[case]
    return IngestService(_cfg(values), source, FakeClock()).collect(mint)


def _reasons(result) -> set[str]:
    """Причини неповноти: `missing[]` і причина вікна покупців (`completeness.buyers.reason`)."""
    completeness = to_dict(result)["completeness"]
    reasons = {m["reason"] for m in completeness["missing"]}
    if completeness["buyers"]["reason"] is not None:
        reasons.add(completeness["buyers"]["reason"])
    return reasons


def _tx_source(fails):
    """Пул із двох архівних провайдерів; `fails` — `{індекс: [збої getTransaction]}`."""
    doubles = [ScriptedProvider(FixtureRpcSource(BASIC), failures=fails.get(i, ())) for i in range(2)]
    return _pool([_provider(f"p{i}", d) for i, d in enumerate(doubles)])


def test_all_timeout_through_ingest_service_is_incomplete_with_timeout_reason():
    timeout = lambda: RpcTimeout("request timed out")  # noqa: E731
    single = _collect(ScriptedProvider(FixtureRpcSource(BASIC), failures=[_fail(timeout())]), "basic")
    pooled = _collect(_tx_source({0: [_fail(timeout())], 1: [_fail(timeout())]}), "basic")
    assert pooled.completeness.status is CompletenessStatus.INCOMPLETE
    assert _reasons(pooled) == {"timeout"} == _reasons(single)
    assert to_json(pooled) == to_json(single)  # той самий клас помилки -> той самий результат побайтово

    # 429, потім timeout для тієї самої позиції: причина — timeout (остання), а не rate_limited (перша)
    mixed = _collect(
        _tx_source({0: [_fail(RpcRateLimited(1.0), calls=[1])], 1: [_fail(timeout(), calls=range(2, 1000))]}),
        "basic",
    )
    assert mixed.completeness.status is CompletenessStatus.INCOMPLETE
    assert "timeout" in _reasons(mixed) and "rate_limited" not in _reasons(mixed)


@pytest.mark.parametrize("name", ["rate_limited", "timeout", "unavailable", "budget"])
@pytest.mark.parametrize("method", ["getAccountInfo", "getSignaturesForAddress", "getTokenAccountsByOwner", TX])
def test_unavailable_class_same_as_single(scenario, name, method):
    """Усі відмовляють -> клас, текст і об'єкт той самі, що в одинокого провайдера (FR-007-08, US3-3)."""

    def call(pool):
        if method == TX:
            return pool.get_transactions(_signatures(12), deadline=_deadline())
        return SINGLE[method](pool, _deadline())

    def build(count):
        excs = [ERRORS[name]() for _ in range(count)]
        doubles = [ScriptedProvider(_Src(f"s{i}"), failures=[_fail(excs[i], method)]) for i in range(count)]
        if method == TX:  # транзакції беремо з власного `FixtureRpcSource`
            doubles = [
                ScriptedProvider(FixtureRpcSource(scenario), failures=[_fail(excs[i], method)]) for i in range(count)
            ]
        return _pool([_provider(f"p{i}", d) for i, d in enumerate(doubles)]), excs

    single_pool, (single_exc,) = build(1)
    with pytest.raises(RpcError) as single_info:
        call(single_pool)
    pool, excs = build(3)
    with pytest.raises(RpcError) as info:
        call(pool)

    assert type(info.value) is type(single_info.value) is type(excs[0])
    assert str(info.value) == str(single_info.value)
    _assert_clean(info.value)
    assert any(info.value is exc for exc in excs)  # сам об'єкт адаптера, не копія й не обгортка


def test_corrupt_responses_from_all_providers_give_unavailable(scenario):
    class Broken:
        name = "broken"

        def get_transactions(self, signatures, *, deadline):
            return None

    pool = _pool([_provider("a", Broken()), _provider("b", Broken())])
    with pytest.raises(RpcUnavailable) as info:
        pool.get_transactions(_signatures(10), deadline=_deadline())
    assert type(info.value) is RpcUnavailable and str(info.value) == SHAPE_TEXT
    _assert_clean(info.value)


# --------------------------------------------------------------------------- пріоритет між частками


@pytest.mark.parametrize("order", ["forward", "reverse", 1, 2, 3])
def test_error_of_the_smallest_position_wins_after_failover(scenario, order):
    """Позиція 0 востаннє впала в p1 (раунд 2), позиції 30–39 — у p2 (раунд 2); піднімається помилка позиції 0."""
    sigs = _signatures(40)
    a, b, c = RpcUnavailable("A"), RpcTimeout("B"), RpcRateLimited(9.0)
    pool, doubles = _kit(
        scenario,
        [1, 1, 1, 1],
        {
            0: [_fail(RpcRateLimited(1.0), calls=[1])],
            3: [_fail(c, calls=[1])],
            1: [_fail(a, calls=[2])],
            2: [_fail(b, calls=[2])],
        },
        runner=_runner(order),
    )
    with pytest.raises(RpcUnavailable) as info:
        pool.get_transactions(sigs, deadline=_deadline())
    _assert_clean(info.value, a)  # не помилка пізнішої позиції (b) і не перша глобальна (429 від p0)


def test_smallest_position_error_with_real_threads(scenario):
    for _ in range(5):
        first, last = RpcRateLimited(1.0), RpcTimeout("request timed out")
        pool, _ = _kit(
            scenario, [1, 1, 1], {0: [_fail(first)], 2: [_fail(last)], 1: [_fail(RpcUnavailable("x"))]},
            runner=ThreadRunner(3),
        )
        try:
            with pytest.raises(RpcRateLimited) as info:
                pool.get_transactions(_signatures(30), deadline=_deadline())
            assert info.value is first
        finally:
            pool._runner.close()


# --------------------------------------------------------------------------- дедлайн / бюджет


def test_budget_timeout_class_preserved(scenario):
    # усі провайдери відмовляють RpcBudgetTimeout (пейсер) -> той самий клас і текст "budget", а не RpcTimeout/Unavailable
    excs = [RpcBudgetTimeout() for _ in range(3)]
    pool, doubles = _kit(scenario, [1, 1, 1], {i: [_fail(excs[i])] for i in range(3)})
    with pytest.raises(RpcBudgetTimeout) as info:
        pool.get_transactions(_signatures(30), deadline=_deadline())
    assert type(info.value) is RpcBudgetTimeout and str(info.value) == "budget"
    _assert_clean(info.value)
    assert [len(_tx_log(d)) for d in doubles] == [1, 1, 1]

    # після 429 на іншому провайдері остання помилка позиції — бюджет: клас зберігається
    last = RpcBudgetTimeout()
    pool, _ = _kit(scenario, [1, 1], {0: [_fail(RpcRateLimited(1.0), calls=[1])], 1: [_fail(last, calls=[2])]})
    with pytest.raises(RpcBudgetTimeout) as info:
        pool.get_transactions(_signatures(10), deadline=_deadline())
    _assert_clean(info.value, last)


def test_budget_timeout_of_one_provider_fails_over_to_another(scenario):
    """Пейсер одного ключа не вмістив запит у дедлайн, дедлайн ще не сплив: частку бере інший ключ (SC-005)."""
    sigs = _signatures(20)
    pool, (d0, d1) = _kit(scenario, [1, 1], {0: [_fail(RpcBudgetTimeout())]})
    assert _got(pool.get_transactions(sigs, deadline=_deadline())) == sigs
    assert (len(_tx_log(d0)), len(_tx_log(d1))) == (1, 2)


def test_budget_timeout_does_not_pause_the_provider(scenario):
    """Пауза — лише після `RpcRateLimited`: стан кошика ключа не означає, що ключ треба «охолоджувати»."""
    sigs = _signatures(20)
    pool, (d0, d1) = _kit(scenario, [1, 1], {1: [_fail(RpcBudgetTimeout(), calls=[1])]})
    pool.get_transactions(sigs, deadline=_deadline())
    assert len(_tx_log(d1)) == 1  # перший виклик: частка p1 впала з бюджетом
    pool.get_transactions(sigs, deadline=_deadline())
    assert len(_tx_log(d1)) == 2 and _tx_log(d1)[-1] == tuple(sigs[10:])  # p1 одразу в розподілі, без паузи


def test_budget_and_rate_limit_failures_in_one_round_both_fail_over(scenario):
    sigs = _signatures(30)
    pool, doubles = _kit(scenario, [1, 1, 1], {0: [_fail(RpcRateLimited(1.0))], 1: [_fail(RpcBudgetTimeout())]})
    assert _got(pool.get_transactions(sigs, deadline=_deadline())) == sigs
    assert (len(_tx_log(doubles[0])), len(_tx_log(doubles[1]))) == (1, 1)  # відмовлені вдруге не викликані
    assert sorted(sig for keys in _tx_log(doubles[2])[1:] for sig in keys) == sigs[:20]  # p2 забрав обидві частки


def test_defect_wins_over_budget_timeout(scenario):
    defect = ValueError("adapter bug")
    pool, _ = _kit(scenario, [1, 1], {0: [_fail(RpcBudgetTimeout())], 1: [_fail(defect)]})
    with pytest.raises(ValueError) as info:
        pool.get_transactions(_signatures(20), deadline=_deadline())
    _assert_clean(info.value, defect)


@pytest.mark.parametrize("method", list(SINGLE))
def test_single_budget_timeout_fails_over_and_does_not_pause(method):
    pool, (a, b) = _singles(["a", "b"], {"a": [_fail(RpcBudgetTimeout(), method, calls=[1])]})
    assert SINGLE[method](pool, _deadline()) is not None
    assert _calls(a, method) == 1 and _calls(b, method) == 1
    SINGLE[method](pool, _deadline())  # без паузи a лишається в ротації: SWRR віддає йому наступний виклик
    SINGLE[method](pool, _deadline())
    assert _calls(a, method) == 2


def test_expired_deadline_stops_failover_without_further_calls(scenario):
    clock = FakeClock()
    deadline = Deadline(clock, 100)

    class SlowFailing:
        name = "slow"

        def __init__(self):
            self.calls = 0

        def get_transactions(self, signatures, *, deadline):
            self.calls += 1
            clock.advance(200)  # дедлайн сплив під час спроби
            raise RpcRateLimited(None)

    slow, good = SlowFailing(), ScriptedProvider(FixtureRpcSource(scenario))
    pool = _pool([_provider("slow", slow), _provider("good", good)])
    with pytest.raises(RpcBudgetTimeout) as info:
        pool.get_transactions(_signatures(10), deadline=deadline)
    assert type(info.value) is RpcBudgetTimeout and str(info.value) == "budget"
    _assert_clean(info.value)
    assert slow.calls == 1 and len(_tx_log(good)) == 1  # перебирання після спливу дедлайну не було


def test_expired_deadline_single_call_stops_failover():
    clock = FakeClock()
    deadline = Deadline(clock, 100)

    class SlowFailing(_Src):
        calls = 0

        def get_account_info(self, address, *, deadline):
            type(self).calls += 1
            clock.advance(200)
            raise RpcTimeout("request timed out")

    good = ScriptedProvider(_Src("good"))
    pool = _pool([_provider("slow", SlowFailing("slow")), _provider("good", good)])
    with pytest.raises(RpcBudgetTimeout):
        pool.get_account_info("addr", deadline=deadline)
    assert SlowFailing.calls == 1 and _calls(good, "getAccountInfo") == 0


# --------------------------------------------------------------------------- дефект не перебирається


@pytest.mark.parametrize("order", ["forward", "reverse"])
def test_defect_exception_propagates_not_masked(scenario, order):
    defect = ValueError("adapter bug")
    pool, doubles = _kit(
        scenario, [1, 1, 1], {0: [_fail(RpcRateLimited(1.0))], 1: [_fail(defect)]}, runner=SequentialRunner(order)
    )
    with pytest.raises(ValueError) as info:
        pool.get_transactions(_signatures(30), deadline=_deadline())
    _assert_clean(info.value, defect)  # дефект пріоритетніший за 429 з меншою позицією
    assert [len(_tx_log(d)) for d in doubles] == [1, 1, 1]  # і не перебрано: жодного другого раунду


def test_defect_in_a_later_round_is_not_masked_by_earlier_rpc_errors(scenario):
    defect = KeyError("adapter bug")
    pool, doubles = _kit(scenario, [1, 1], {0: [_fail(RpcTimeout("t"), calls=[1])], 1: [_fail(defect, calls=[2])]})
    with pytest.raises(KeyError) as info:
        pool.get_transactions(_signatures(20), deadline=_deadline())
    _assert_clean(info.value, defect)


@pytest.mark.parametrize("method", list(SINGLE))
def test_defect_in_single_method_is_not_failed_over(method):
    defect = ValueError("adapter bug")
    pool, (a, b) = _singles(["a", "b"], {"a": [_fail(defect, method)]})
    with pytest.raises(ValueError) as info:
        SINGLE[method](pool, _deadline())
    _assert_clean(info.value, defect)
    assert _calls(b, method) == 0


def test_base_exception_is_not_failed_over(scenario):
    class Stop(BaseException):
        pass

    stop = Stop()
    pool, doubles = _kit(scenario, [1, 1], {0: [_fail(stop)]})
    with pytest.raises(Stop) as info:
        pool.get_transactions(_signatures(10), deadline=_deadline())
    assert info.value is stop and len(_tx_log(doubles[1])) == 1


# --------------------------------------------------------------------------- без ланцюга винятків і секретів


@pytest.mark.parametrize("name", list(ERRORS))
def test_raised_error_has_no_context_chain(scenario, name):
    excs = [ERRORS[name]() for _ in range(3)]
    pool, _ = _kit(scenario, [1, 1, 1], {i: [_fail(excs[i])] for i in range(3)})
    # жоден виняток не «обробляється» в момент підняття
    with pytest.raises(RpcError) as info:
        pool.get_transactions(_signatures(30), deadline=_deadline())
    _assert_clean(info.value)
    assert info.value is excs[0]  # позиція 0 побувала лише в p0 (раунд 1)

    labels = ["a", "b", "c"]
    for method in SINGLE:
        excs = [ERRORS[name]() for _ in labels]
        pool, _ = _singles(labels, {l: [_fail(excs[i], method)] for i, l in enumerate(labels)})
        with pytest.raises(RpcError) as info:
            SINGLE[method](pool, _deadline())
        _assert_clean(info.value)
        assert info.value is excs[-1]  # SWRR при рівних вагах: a, b, c -> остання спроба c


def test_error_is_raised_outside_any_handler_even_after_multiple_attempts():
    """Підняття не успадковує `__context__` попередньої відмови: ланцюг пройшов би httpx-виняток з URL."""
    labels = ["a", "b"]
    first, second = RpcTimeout("first"), RpcUnavailable("second")
    pool, _ = _singles(labels, {"a": [_fail(first, "getAccountInfo")], "b": [_fail(second, "getAccountInfo")]})
    with pytest.raises(RpcUnavailable) as info:
        pool.get_account_info("addr", deadline=_deadline())
    _assert_clean(info.value, second)
    assert first.__context__ is None and first.__cause__ is None


def test_failover_never_leaks_secrets_into_logs_texts_or_repr(scenario, caplog):
    class Secretive(ScriptedProvider):
        def __repr__(self):
            return f"https://rpc.example/?api-key={SECRET}"

        __str__ = __repr__

    excs = [RpcRateLimited(1.0), RpcTimeout("request timed out"), RpcUnavailable("http 5xx server error")]
    methods = ("getAccountInfo", "getSignaturesForAddress", TX, "getTokenAccountsByOwner")
    doubles = [
        Secretive(FixtureRpcSource(scenario), failures=[_fail(exc, m) for m in methods]) for exc in excs
    ]
    pool = _pool([_provider(f"p{i}", d) for i, d in enumerate(doubles)])
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(RpcError) as info:
            pool.get_transactions(_signatures(30), deadline=_deadline())
        for call in SINGLE.values():
            with pytest.raises(RpcError):
                call(pool, _deadline())
    haystack = "\n".join([caplog.text, repr(pool), str(info.value), repr(info.value), *map(repr, pool._providers)])
    assert SECRET not in haystack
    assert str(info.value) == str(excs[0])  # пул не дописує тексту ззовні


# --------------------------------------------------------------------------- одиночні методи


@pytest.mark.parametrize("method", list(SINGLE))
@pytest.mark.parametrize("name", RETRYABLE)
def test_single_method_fails_over_to_next_provider(method, name):
    pool, (a, b, c) = _singles(["a", "b", "c"], {"a": [_fail(ERRORS[name](), method)]})
    result = SINGLE[method](pool, _deadline())
    assert result is not None
    assert _calls(a, method) == 1 and _calls(b, method) + _calls(c, method) == 1


@pytest.mark.parametrize("method", list(SINGLE))
def test_single_method_tries_each_provider_once_then_raises_the_last(method):
    excs = {l: RpcTimeout(f"t-{l}") for l in "abc"}
    pool, doubles = _singles(list("abc"), {l: [_fail(e, method)] for l, e in excs.items()})
    with pytest.raises(RpcTimeout) as info:
        SINGLE[method](pool, _deadline())
    assert [_calls(d, method) for d in doubles] == [1, 1, 1]  # кожен рівно раз: перебирання скінченне
    assert info.value is excs["c"]  # SWRR: a, b, c -> остання спроба c


def test_single_method_respects_skip_methods_when_failing_over():
    pool, (a, b, c) = _singles(
        ["a", "b", "c"],
        {"a": [_fail(RpcTimeout("t"), "getAccountInfo")]},
        weights=[10.0, 1.0, 1.0],
        b={"skip_methods": frozenset({"getAccountInfo"})},
    )
    assert pool.get_account_info("addr", deadline=_deadline())["owner"] == "c"
    assert _calls(b, "getAccountInfo") == 0  # провайдер, що пропускає метод, не стає запасним


def test_signatures_failover_archival_to_archival_and_to_nonarchival_fallback():
    # архівний a -> архівний b (повна сторінка)
    pool, (a, b, n) = _singles(
        ["a", "b", "n"],
        {"a": [_fail(RpcTimeout("t"), "getSignaturesForAddress")]},
        weights=[5, 1, 20],
        n={"archival": False, "retention_seconds": 1000.0},
    )
    page = SINGLE["getSignaturesForAddress"](pool, _deadline())
    assert page[0]["signature"].startswith("b") and _calls(n, "getSignaturesForAddress") == 0

    # обидва архівні відмовили -> неархівний запасний: повна сторінка приймається
    pool, (a, b, n) = _singles(
        ["a", "b", "n"],
        {l: [_fail(RpcUnavailable("x"), "getSignaturesForAddress")] for l in "ab"},
        n={"archival": False, "retention_seconds": 1000.0},
    )
    page = SINGLE["getSignaturesForAddress"](pool, _deadline())
    assert len(page) == 5 and page[0]["signature"].startswith("n")


def test_signatures_nonarchival_short_page_after_archival_failure_is_unavailable_not_clean():
    class ShortPage(_Src):
        def get_signatures_for_address(self, address, *, before, until, limit, deadline):
            return []

    a = ScriptedProvider(_Src("a"), failures=[_fail(RpcTimeout("t"), "getSignaturesForAddress")])
    n = ScriptedProvider(ShortPage("n"))
    pool = _pool([_provider("a", a), _provider("n", n, archival=False, retention_seconds=1000.0)])
    with pytest.raises(RpcUnavailable) as info:
        SINGLE["getSignaturesForAddress"](pool, _deadline())
    assert str(info.value) == RETENTION_TEXT  # порожня сторінка запасного — не «історія скінчилась»
    _assert_clean(info.value)


# --------------------------------------------------------------------------- архівність при перебиранні


def _write_scenario(tmp_path: Path, ages: dict[str, int]) -> Path:
    history = [
        {"signature": s, "slot": 100 + i, "err": None, "memo": None, "blockTime": bt, "confirmationStatus": "finalized"}
        for i, (s, bt) in enumerate(ages.items())
    ]
    txs = {s: {"slot": 100 + i, "blockTime": bt, "signature": s} for i, (s, bt) in enumerate(ages.items())}
    directory = tmp_path / "routing"
    directory.mkdir()
    (directory / "rpc.json").write_text(
        json.dumps({"getSignaturesForAddress": {ADDRESS: history}, "getTransaction": txs}), encoding="utf-8"
    )
    return directory


def _arch_nonarch(scenario_dir, *, arch_fails=(), n_weight=12.0, arch_weights=(4.5,)):
    non_d = ScriptedProvider(FixtureRpcSource(scenario_dir), horizon_block_time=NOW - RETENTION)
    arch_ds = [
        ScriptedProvider(FixtureRpcSource(scenario_dir), failures=arch_fails if i == 0 else ())
        for i in range(len(arch_weights))
    ]
    providers = [
        Provider(
            "n", non_d, n_weight, False, float(RETENTION), frozenset({"getSignaturesForAddress"})
        ),
        *[Provider(f"a{i}", d, w, True, None) for i, (d, w) in enumerate(zip(arch_ds, arch_weights))],
    ]
    return _pool(providers), non_d, arch_ds


def _seed(pool):
    pool.get_signatures_for_address(ADDRESS, before=None, until=None, limit=1000, deadline=_deadline())


def test_failover_never_gives_known_old_data_to_nonarchival(tmp_path):
    sigs = [f"o{i:02d}" for i in range(10)]
    scenario = _write_scenario(tmp_path, {s: OLD for s in sigs})
    exc = RpcTimeout("request timed out")
    pool, non_d, (a_d,) = _arch_nonarch(scenario, arch_fails=[_fail(exc)])
    _seed(pool)
    with pytest.raises(RpcTimeout) as info:
        pool.get_transactions(sigs, deadline=_deadline())
    _assert_clean(info.value, exc)  # неархівний НЕ запасний для давніх даних: піднімається помилка архівного
    assert _tx_log(non_d) == [] and len(_tx_log(a_d)) == 1


def test_fresh_share_of_failed_archival_is_taken_by_nonarchival(tmp_path):
    sigs = [f"f{i:02d}" for i in range(10)]
    scenario = _write_scenario(tmp_path, {s: FRESH for s in sigs})
    pool, non_d, (a_d,) = _arch_nonarch(scenario, arch_fails=[_fail(RpcRateLimited(None))])
    _seed(pool)
    assert _got(pool.get_transactions(sigs, deadline=_deadline())) == sigs
    assert sorted(s for keys in _tx_log(non_d) for s in keys) == sigs  # неархівний обслужив і свою, і чужу частку


def test_unconfirmed_none_is_confirmed_by_the_surviving_archival(tmp_path):
    """Неархівний каже None (вік невідомий), a0 падає, a1 підтверджує: дані, а не None, і без втрати часток."""
    sigs = [f"u{i:02d}" for i in range(12)]
    scenario = _write_scenario(tmp_path, {s: OLD for s in sigs})
    pool, non_d, (a0, a1) = _arch_nonarch(
        scenario, arch_fails=[_fail(RpcTimeout("t"), calls=[1])], arch_weights=(4.5, 4.5)
    )  # без _seed: вік невідомий
    got = pool.get_transactions(sigs, deadline=_deadline())
    assert _got(got) == sigs
    assert len(_tx_log(a0)) == 1  # a0 відмовив і вдруге не викликаний
    assert sum(len(keys) for keys in _tx_log(a1)) >= 1


def test_confirmation_fails_with_the_archival_error_of_that_position(tmp_path):
    sigs = [f"c{i:02d}" for i in range(6)]
    scenario = _write_scenario(tmp_path, {s: OLD for s in sigs})
    exc = RpcTimeout("request timed out")
    pool, non_d, (a_d,) = _arch_nonarch(scenario, arch_fails=[_fail(exc, calls=[1])], n_weight=1.0, arch_weights=(1e-9,))
    # вага архівного мізерна: майже все віддається неархівному (None), а підтвердження потрапляє до архівного й падає
    with pytest.raises(RpcTimeout) as info:
        pool.get_transactions(sigs, deadline=_deadline())
    _assert_clean(info.value, exc)


def test_position_unconfirmable_after_archival_failure_raises_the_archival_error(tmp_path):
    """Позиція 0 віддана неархівному (None), підтвердити нікому: єдиний архівний упав на ІНШИХ позиціях раунду.
    Чесна причина — відмова архівного (тимчасова), а не «історія за межею» (постійна)."""
    sigs = [f"x{i:02d}" for i in range(10)]
    scenario = _write_scenario(tmp_path, {s: OLD for s in sigs})
    exc = RpcTimeout("request timed out")
    pool, non_d, (a_d,) = _arch_nonarch(scenario, arch_fails=[_fail(exc)], n_weight=1.0, arch_weights=(1.0,))
    with pytest.raises(RpcTimeout) as info:
        pool.get_transactions(sigs, deadline=_deadline())
    _assert_clean(info.value, exc)


def test_no_provider_ever_suitable_is_retention_unavailable_not_a_stale_error(tmp_path):
    """Давня позиція, архівного немає взагалі: `RpcUnavailable(retention)`, а не помилка стороннього провайдера."""
    sigs = [f"y{i:02d}" for i in range(6)]
    scenario = _write_scenario(tmp_path, {s: OLD for s in sigs})
    non_d = ScriptedProvider(FixtureRpcSource(scenario), horizon_block_time=NOW - RETENTION)
    other = ScriptedProvider(FixtureRpcSource(scenario), failures=[_fail(RpcTimeout("t"))])
    pool = _pool([
        Provider("n", non_d, 1.0, False, float(RETENTION), frozenset({"getSignaturesForAddress"})),
        Provider("m", other, 1.0, False, float(RETENTION), frozenset()),
    ])
    _seed_via(pool, scenario)
    with pytest.raises(RpcUnavailable) as info:
        pool.get_transactions(sigs, deadline=_deadline())
    assert str(info.value) == RETENTION_TEXT and _tx_log(non_d) == [] and _tx_log(other) == []
    _assert_clean(info.value)


def _seed_via(pool, scenario_dir):
    """Вік підписів стає відомим: сторінку історії віддає окремий архівний дубль, а не провайдери пулу."""
    archival = Provider("seed", ScriptedProvider(FixtureRpcSource(scenario_dir)), 1.0, True, None)
    seed_pool = _pool([archival])
    page = seed_pool.get_signatures_for_address(ADDRESS, before=None, until=None, limit=1000, deadline=_deadline())
    pool._remember_ages(page)


# --------------------------------------------------------------------------- SC-005: N-1 відмов -> той самий результат


def _service_source(case: str, fail_indices, exc_factory, *, runner=None):
    directory = CASES[case][0]
    methods = ("getAccountInfo", "getSignaturesForAddress", TX, "getTokenAccountsByOwner")
    doubles = [
        ScriptedProvider(
            FixtureRpcSource(directory),
            failures=[_fail(exc_factory(), m) for m in methods] if i in fail_indices else (),
        )
        for i in range(3)
    ]
    providers = [_provider(f"p{i}", d, w) for i, (d, w) in enumerate(zip(doubles, (12.0, 4.5, 4.5)))]
    return _pool(providers, runner=runner or SequentialRunner("reverse")), doubles


@pytest.mark.parametrize("case", ["basic", "hub"])
@pytest.mark.parametrize("name", RETRYABLE)
@pytest.mark.parametrize(
    "failing", [c for k in (1, 2) for c in combinations(range(3), k)], ids=lambda c: "fail" + "".join(map(str, c))
)
def test_n_minus_one_failures_same_result(case, name, failing):
    directory, mint, values = CASES[case]
    expected = to_json(_collect(FixtureRpcSource(directory), case))
    pool, doubles = _service_source(case, set(failing), ERRORS[name])
    result = _collect(pool, case)
    assert to_json(result) == expected  # побайтово: покупці, перекази, повнота, rpc_calls ядра
    assert result.completeness.status is CompletenessStatus.COMPLETE
    assert any(doubles[i].log for i in failing)  # відмови справді відбулися


@pytest.mark.parametrize("case", ["basic", "hub"])
def test_n_minus_one_failures_same_result_with_real_threads(case):
    expected = to_json(_collect(FixtureRpcSource(CASES[case][0]), case))
    runner = ThreadRunner(3)
    try:
        pool, _ = _service_source(case, {0, 2}, ERRORS["rate_limited"], runner=runner)
        assert to_json(_collect(pool, case)) == expected
    finally:
        runner.close()


@pytest.mark.parametrize("name", list(ERRORS))
def test_all_providers_failing_gives_same_result_as_single(name):
    """Усі відмовляють на getTransaction: збір через пул == збір через одного провайдера з тією ж відмовою."""
    single = _collect(ScriptedProvider(FixtureRpcSource(BASIC), failures=[_fail(ERRORS[name]())]), "basic")
    pool = _tx_source({0: [_fail(ERRORS[name]())], 1: [_fail(ERRORS[name]())]})
    pooled = _collect(pool, "basic")
    assert pooled.completeness.status is single.completeness.status
    assert to_json(pooled) == to_json(single)


# --------------------------------------------------------------------------- потоки: без взаємного блокування


def test_failover_never_holds_state_lock_while_calling_a_provider(scenario):
    holders = []

    class Probe:
        name = "probe"

        def __init__(self, pool_ref):
            self.pool_ref = pool_ref

        def get_transactions(self, signatures, *, deadline):
            holders.append(self.pool_ref[0]._state_lock.locked())
            raise RpcRateLimited(None)

    ref: list = []
    good = ScriptedProvider(FixtureRpcSource(scenario))
    pool = _pool([_provider("probe", Probe(ref)), _provider("good", good)])
    ref.append(pool)
    pool.get_transactions(_signatures(10), deadline=_deadline())
    assert holders == [False]
    assert not pool._state_lock.locked() and not any(lock.locked() for lock in pool._locks)


def test_concurrent_calls_with_failures_complete_and_are_correct(scenario):
    runner = ThreadRunner(8)
    errors: list[BaseException] = []
    pool, _ = _kit(
        scenario, [1, 1, 1], {1: [_fail(RpcRateLimited(0.5), calls=range(1, 4))], 2: [_fail(RpcTimeout("t"), calls=[2, 5])]},
        runner=runner,
    )

    def worker():
        try:
            for _ in range(8):
                sigs = _signatures(18)
                assert _got(pool.get_transactions(sigs, deadline=_deadline())) == sigs
        except BaseException as exc:  # noqa: BLE001 — збираємо помилки потоків
            errors.append(exc)

    try:
        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        assert not any(t.is_alive() for t in threads) and not errors
    finally:
        runner.close()


# --------------------------------------------------------------------------- додатково (ревʼю T-140)


def test_unconfirmable_position_gets_the_error_of_a_suitable_excluded_provider_not_the_latest(tmp_path):
    """Підтвердити позицію 0 міг би лише архівний `a` (відмовив timeout). Неархівний `n2` відмовив ПІЗНІШЕ в
    порядку виключення, але для підтвердження непридатний: піднімається помилка `a`, а не `n2`."""
    sigs = [f"z{i:02d}" for i in range(9)]
    scenario = _write_scenario(tmp_path, {s: OLD for s in sigs})
    err_a, err_n2 = RpcTimeout("request timed out"), RpcUnavailable("http 5xx server error")
    hidden = NOW - RETENTION
    n1 = ScriptedProvider(FixtureRpcSource(scenario), horizon_block_time=hidden)  # відповідає None -> підтвердження
    a = ScriptedProvider(FixtureRpcSource(scenario), failures=[_fail(err_a)])
    n2 = ScriptedProvider(FixtureRpcSource(scenario), horizon_block_time=hidden, failures=[_fail(err_n2)])
    skip = frozenset({"getSignaturesForAddress"})
    pool = _pool([
        Provider("n1", n1, 1.0, False, float(RETENTION), skip),
        Provider("a", a, 1.0, True, None),
        Provider("n2", n2, 1.0, False, float(RETENTION), skip),
    ])  # вік невідомий: придатні всі
    with pytest.raises(RpcTimeout) as info:
        pool.get_transactions(sigs, deadline=_deadline())
    _assert_clean(info.value, err_a)


def test_repeated_rate_limit_with_smaller_retry_after_does_not_shorten_the_pause(scenario):
    clock = FakeClock(wall_start=NOW)
    sigs = _signatures(20)
    pool, (d0, d1) = _kit(
        scenario, [1, 1],
        {0: [_fail(RpcRateLimited(120.0), calls=[1]), _fail(RpcRateLimited(1.0), calls=[2])],
         1: [_fail(RpcRateLimited(None), calls=[3])]},
        clock=clock,
    )
    pool.get_transactions(sigs, deadline=_deadline())  # t=0: p0 -> пауза до 120
    clock.advance(10)
    with pytest.raises(RpcRateLimited):  # p1 впав 429 -> усі на паузі -> p0 (пауза ігнорується) впав 429 з retry_after=1
        pool.get_transactions(sigs, deadline=_deadline())
    before = len(_tx_log(d0))
    clock.advance(40)  # t=50: пауза p1 (до 40) скінчилась, p0 (до 120) триває
    pool.get_transactions(sigs, deadline=_deadline())
    assert len(_tx_log(d0)) == before  # коротший повторний retry_after паузу не скоротив (max)
    clock.advance(70)  # t=120
    pool.get_transactions(sigs, deadline=_deadline())
    assert len(_tx_log(d0)) == before + 1

