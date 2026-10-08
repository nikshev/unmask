# verifies: FR-007-15
"""Тестові дублі пулу (tests/rpc_pool_doubles.py): горизонт історії, збої за номером виклику,
бар'єр, затримка, паралельність, послідовний runner, журнал викликів. Без мережі: записаний сценарій
`basic` і крихітні заглушки внутрішнього джерела."""

import threading
import time
from pathlib import Path

import pytest

from rpc_pool_doubles import ScriptedFailure, ScriptedProvider, SequentialRunner
from unmask.ingest.rpc.fixture import FixtureRpcSource
from unmask.ingest.rpc.protocol import RpcRateLimited, RpcTimeout

SCENARIO = Path(__file__).parent / "fixtures" / "scenarios" / "basic"
WALLET = "FeC5ErmXHtwwsjQWYXasUPNRyWFQuY7XBHMR2yspcLPs"
# Записані blockTime (сценарій basic): 4Au5... == 1759400100 (межа), 36vr... == 1759400010 (старіша).
HORIZON = 1759400100
OLD_SIG = "36vrgLqgHt4WA9NyFw9fsywwhUKYvVtzdzuZ2kSXv6RRxFAjEdcSXJ9oruKkfwssgQBVhJLmjVezrhkWqLVEdPgA"
BOUNDARY_SIG = "4Au5MBM8hicf1LYx9YbSUHZ6tA98hcBdtfj8tdvVKcfhdR6zqGaCkuBMD2yrZ5zutNPk2dxB7qN6pzDwSL6AbJ6n"
DEADLINE = None  # фікстурне джерело дедлайн не перевіряє


def _inner() -> FixtureRpcSource:
    return FixtureRpcSource(SCENARIO)


class _NullBlockTimeInner:
    """Заглушка: сторінка підписів і транзакції з записами без blockTime (None) і зі старим blockTime."""

    name = "null-blocktime"

    def get_signatures_for_address(self, address, *, before, until, limit, deadline):
        return [
            {"signature": "NULLTIME", "slot": 1, "err": None, "memo": None, "blockTime": None,
             "confirmationStatus": "finalized"},
            {"signature": "OLDTIME", "slot": 1, "err": None, "memo": None, "blockTime": 1,
             "confirmationStatus": "finalized"},
        ][:limit]

    def get_transactions(self, signatures, *, deadline):
        return [
            {"slot": 1, "blockTime": None, "version": 0, "transaction": {}, "meta": None},
            {"slot": 1, "blockTime": 1, "version": 0, "transaction": {}, "meta": None},
        ]


class _BarrierInner:
    """Заглушка: `get_account_info` блокується, доки n викликів не зійдуться разом (доказ паралельності)."""

    name = "barrier-inner"

    def __init__(self, n: int) -> None:
        self._barrier = threading.Barrier(n, timeout=5)

    def get_account_info(self, address, *, deadline):
        self._barrier.wait()
        return None


def _run_threads(targets):
    """Запускає цілі в потоках; повертає [(результат, виняток)] за порядком цілей. Зависання — помилка."""
    outcomes = [None] * len(targets)

    def wrap(i, fn):
        try:
            outcomes[i] = (fn(), None)
        except BaseException as exc:  # noqa: BLE001
            outcomes[i] = (None, exc)

    threads = [threading.Thread(target=wrap, args=(i, fn)) for i, fn in enumerate(targets)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
        assert not t.is_alive(), "потік завис: бар'єр або дубль не відпускає"
    return outcomes


def test_horizon_hides_old_transactions_as_none():
    provider = ScriptedProvider(_inner(), horizon_block_time=HORIZON)
    got = provider.get_transactions([OLD_SIG, BOUNDARY_SIG], deadline=DEADLINE)
    assert got[0] is None  # blockTime < horizon -> None
    assert got[1] is not None  # blockTime == horizon -> видно (сувора нерівність)
    assert got[1]["blockTime"] == HORIZON


def test_horizon_truncates_signature_pages():
    full = _inner().get_signatures_for_address(
        WALLET, before=None, until=None, limit=1000, deadline=DEADLINE
    )
    provider = ScriptedProvider(_inner(), horizon_block_time=HORIZON)
    got = provider.get_signatures_for_address(
        WALLET, before=None, until=None, limit=1000, deadline=DEADLINE
    )
    assert [e["signature"] for e in got] == [
        e["signature"] for e in full if e["blockTime"] >= HORIZON
    ]
    assert len(got) < len(full)
    # limit=6 перетинає горизонт (записи 12 і 10 — старі): сторінка коротшає до 5, без добору.
    short = provider.get_signatures_for_address(
        WALLET, before=None, until=None, limit=6, deadline=DEADLINE
    )
    assert len(full) >= 6 and full[5]["blockTime"] < HORIZON
    assert [e["signature"] for e in short] == [
        e["signature"] for e in full[:6] if e["blockTime"] >= HORIZON
    ]
    assert len(short) == 5


def test_horizon_keeps_entries_without_block_time():
    provider = ScriptedProvider(_NullBlockTimeInner(), horizon_block_time=HORIZON)
    page = provider.get_signatures_for_address(
        WALLET, before=None, until=None, limit=10, deadline=DEADLINE
    )
    assert [e["signature"] for e in page] == ["NULLTIME"]  # без blockTime — не доведено «старий»
    txs = provider.get_transactions(["NULLTIME", "OLDTIME"], deadline=DEADLINE)
    assert txs[0] is not None  # blockTime None не ховається
    assert txs[1] is None  # blockTime 1 < horizon — ховається


def test_failure_fires_on_listed_calls_only():
    failure = ScriptedFailure("getAccountInfo", frozenset({2}), RpcRateLimited(1.0))
    provider = ScriptedProvider(_inner(), failures=[failure])
    first = provider.get_account_info(WALLET, deadline=DEADLINE)
    with pytest.raises(RpcRateLimited):
        provider.get_account_info(WALLET, deadline=DEADLINE)  # другий виклик цього методу
    third = provider.get_account_info(WALLET, deadline=DEADLINE)
    assert first == third
    # Невдалий виклик теж у журналі; інші методи не рахуються у цей лічильник.
    assert [m for m, _ in provider.log] == ["getAccountInfo"] * 3


def test_failure_call_numbers_are_per_method():
    # Інший метод між викликами не має зсувати номери: збій «другого getAccountInfo» — саме він.
    failure = ScriptedFailure("getAccountInfo", frozenset({2}), RpcRateLimited(1.0))
    provider = ScriptedProvider(_inner(), failures=[failure])
    provider.get_account_info(WALLET, deadline=DEADLINE)  # getAccountInfo №1
    provider.get_token_accounts_by_owner(WALLET, deadline=DEADLINE)  # інший метод
    with pytest.raises(RpcRateLimited):
        provider.get_account_info(WALLET, deadline=DEADLINE)  # getAccountInfo №2 -> збій
    assert [m for m, _ in provider.log] == [
        "getAccountInfo",
        "getTokenAccountsByOwner",
        "getAccountInfo",
    ]


def test_barrier_is_waited_before_get_transactions():
    # Один виклик без партнерів: бар'єр на 2 мусить зірватися. Доводить, що бар'єр викликається.
    provider = ScriptedProvider(
        _inner(), barrier=threading.Barrier(2, timeout=0.5)
    )
    with pytest.raises(threading.BrokenBarrierError):
        provider.get_transactions([OLD_SIG], deadline=DEADLINE)


def test_barrier_precedes_scripted_failure():
    # Три провайдери на одному бар'єрі; у другого кожен getTransaction падає RpcTimeout.
    # Збій мусить відбуватися ПІСЛЯ бар'єра: усі три доходять до бар'єра, двоє повертають списки.
    barrier = threading.Barrier(3, timeout=5)
    failing = RpcTimeout("scripted")
    providers = [
        ScriptedProvider(_inner(), barrier=barrier),
        ScriptedProvider(
            _inner(),
            barrier=barrier,
            failures=[ScriptedFailure("getTransaction", None, failing)],
        ),
        ScriptedProvider(_inner(), barrier=barrier),
    ]
    results = _run_threads(
        [lambda p=p: p.get_transactions([OLD_SIG], deadline=DEADLINE) for p in providers]
    )
    assert isinstance(results[0][0], list)
    assert results[1][0] is None and results[1][1] is failing
    assert isinstance(results[2][0], list)


def test_sequential_runner_returns_outcomes_in_task_order_for_reverse():
    ran: list[int] = []

    def task(i, fail=False):
        def run():
            ran.append(i)
            if fail:
                raise ValueError(f"task {i}")
            return i * 10

        return run

    tasks = [task(0), task(1, fail=True), task(2)]
    outcomes = SequentialRunner("reverse")(tasks)
    assert ran == [2, 1, 0]  # виконано у зворотному порядку
    assert outcomes[0][0] == 0 and outcomes[0][1] is None
    assert outcomes[1][0] is None and isinstance(outcomes[1][1], ValueError)
    assert outcomes[2][0] == 20 and outcomes[2][1] is None  # результати — у порядку задач


def test_sequential_runner_permutation_runs_in_given_order_returns_task_order():
    ran: list[int] = []

    def task(i):
        def run():
            ran.append(i)
            return f"r{i}"

        return run

    outcomes = SequentialRunner([1, 2, 0])([task(0), task(1), task(2)])
    assert ran == [1, 2, 0]  # порядок виконання — перестановка
    assert [o[0] for o in outcomes] == ["r0", "r1", "r2"]  # outcomes — у порядку задач
    assert all(o[1] is None for o in outcomes)


def test_max_concurrent_counts_overlapping_calls():
    provider = ScriptedProvider(_BarrierInner(3))
    results = _run_threads(
        [lambda: provider.get_account_info(WALLET, deadline=DEADLINE) for _ in range(3)]
    )
    assert all(exc is None for _, exc in results)  # три виклики зійшлись разом — паралельно
    assert provider.max_concurrent == 3


def test_delay_seconds_delays_each_call():
    provider = ScriptedProvider(_inner(), delay_seconds=0.2)
    t0 = time.perf_counter()
    provider.get_account_info(WALLET, deadline=DEADLINE)
    assert time.perf_counter() - t0 >= 0.2


def test_log_records_share_signatures():
    provider = ScriptedProvider(_inner())
    sigs = provider.get_signatures_for_address(
        WALLET, before=None, until=None, limit=1000, deadline=DEADLINE
    )
    picked = [e["signature"] for e in sigs[:3]]
    provider.get_transactions(picked, deadline=DEADLINE)
    assert provider.log[0] == ("getSignaturesForAddress", (WALLET,))
    assert provider.log[1] == ("getTransaction", tuple(picked))
    assert provider.name == _inner().name
