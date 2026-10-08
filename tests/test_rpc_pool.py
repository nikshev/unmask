# verifies: FR-007-01, FR-007-02, FR-007-03, FR-007-04
"""Паралельний `get_transactions` пулу (T-132, contracts/pooled-rpc-source.md).

Розподіл за вагами (метод найбільших залишків), одночасне виконання часток, збирання відповіді за
ПОЗИЦІЄЮ підпису (а не за порядком завершення), локи провайдерів, перевірка дедлайну перед раундом,
відмова частки піднімається як є, коли відмовили ВСІ провайдери (перебирання — T-140, `test_rpc_pool_failover.py`). Паралелізм доводиться
`threading.Barrier` і `SequentialRunner`, а не вимірюванням часу (стінний час — лише `-m perf`, T-144).
Мережі немає: `ScriptedProvider` поверх власних `FixtureRpcSource`.

T-133 (нижче, розділ «одиночні методи»): `get_account_info`, `get_signatures_for_address`,
`get_token_accounts_by_owner` — згладжений зважений round-robin (SWRR) за вагою, `name`, `repr`,
потокобезпечність (лок на провайдера). Без маршрутизації за архівністю (T-135/T-136) і без перебирання
(T-140; його тести — `test_rpc_pool_failover.py`): тут відмова піднімається як є, коли відмовили усі провайдери.
"""

import contextlib
import dataclasses
import json
import math
import random
import threading
import time
from pathlib import Path

import pytest

from rpc_pool_doubles import ScriptedFailure, ScriptedProvider, SequentialRunner
from unmask.ingest.budget import Deadline, FakeClock
from unmask.ingest.config import load_config
from unmask.ingest.rpc.fixture import FixtureRpcSource
from unmask.ingest.rpc.pool import PooledRpcSource, Provider, ThreadRunner, apportion
from unmask.ingest.rpc.protocol import (
    RpcBudgetTimeout,
    RpcRateLimited,
    RpcTimeout,
    RpcUnavailable,
)
from unmask.ingest.service import IngestService

ROOT = Path(__file__).resolve().parent.parent
BASIC = ROOT / "tests" / "fixtures" / "scenarios" / "basic"
SHIPPED = ROOT / "config" / "ingest.yaml"
EXPECTED = json.loads((BASIC / "expected.json").read_text())

SHAPE_TEXT = "unexpected response shape: pool share length"


# --------------------------------------------------------------------------- допоміжне


def _deadline(seconds: float = 600.0) -> Deadline:
    return Deadline(FakeClock(), seconds)


def _signatures(n: int) -> list[str]:
    return [f"sig{i:03d}" for i in range(n)]


@pytest.fixture
def scenario(tmp_path) -> Path:
    """Сценарій на 60 підписів: відповідь несе власний підпис, тож порядок видно в результаті."""
    sigs = _signatures(60)
    rpc = {
        "getTransaction": {
            sig: {"slot": 1000 + i, "blockTime": 1_700_000_000 + i, "signature": sig}
            for i, sig in enumerate(sigs)
        }
    }
    (tmp_path / "rpc.json").write_text(json.dumps(rpc), encoding="utf-8")
    return tmp_path


def _provider(name: str, source, weight: float = 1.0, **overrides) -> Provider:
    values = dict(name=name, source=source, weight=weight, archival=True, retention_seconds=None)
    values.update(overrides)
    return Provider(**values)


def _kit(scenario_dir, weights, *, runner=None, scripted=None):
    """Пул із ScriptedProvider на провайдера (власне FixtureRpcSource кожному) і самі дублі."""
    scripted = scripted or {}
    doubles = [
        ScriptedProvider(FixtureRpcSource(scenario_dir), **scripted.get(i, {}))
        for i in range(len(weights))
    ]
    providers = [_provider(f"p{i}", d, w) for i, (d, w) in enumerate(zip(doubles, weights))]
    pool = _pool(providers, runner=runner)
    return pool, doubles


def _pool(providers, *, runner=None, **overrides) -> PooledRpcSource:
    values = dict(clock=FakeClock(), rate_limit_cooldown_seconds=30.0, age_cache_entries=1000)
    values.update(overrides)
    return PooledRpcSource(providers, runner=runner, **values)


@contextlib.contextmanager
def _closing_default_runner(pool):
    try:
        yield pool
    finally:
        runner = getattr(pool, "_runner", None)
        if hasattr(runner, "close"):
            runner.close()


def _shares(doubles) -> list[tuple[str, ...]]:
    """Підписи, які отримав кожен провайдер, одним `get_transactions` (раунд один)."""
    out = []
    for double in doubles:
        calls = [keys for method, keys in double.log if method == "getTransaction"]
        assert len(calls) <= 1
        out.append(calls[0] if calls else ())
    return out


# --------------------------------------------------------------------------- apportion


def test_apportion_largest_remainder_and_ties():
    assert apportion(25, [1, 1, 1]) == [9, 8, 8]  # нічия залишків — менший індекс
    assert apportion(25, [12, 4.5, 4.5, 4.5]) == [12, 5, 4, 4]
    assert apportion(0, [1, 1, 1]) == [0, 0, 0]
    assert apportion(0, [12, 4.5]) == [0, 0]


def test_apportion_tie_goes_to_lower_index_even_for_equal_float_weights():
    # 0.1 не є точним десятковим числом, але три однакові ваги дають рівно рівні залишки (точна арифметика)
    assert apportion(10, [0.1, 0.1, 0.1]) == [4, 3, 3]
    assert apportion(1, [4.5, 4.5, 4.5]) == [1, 0, 0]
    assert apportion(2, [4.5, 4.5, 4.5]) == [1, 1, 0]


def test_apportion_single_item_goes_to_heaviest_weight():
    assert apportion(1, [4.5, 12, 4.5]) == [0, 1, 0]
    assert apportion(1, [12]) == [1]
    assert apportion(7, [3.0]) == [7]


@pytest.mark.parametrize("weights", [[1, 1, 1], [12, 4.5], [12, 4.5, 4.5, 4.5], [1e6, 1], [0.1, 0.7, 0.2]])
def test_apportion_sums_to_n_and_stays_within_one_of_the_quota(weights):
    total = sum(weights)
    for n in range(0, 80):
        counts = apportion(n, weights)
        assert len(counts) == len(weights)
        assert sum(counts) == n
        assert all(c >= 0 for c in counts)
        for count, weight in zip(counts, weights):
            assert abs(count - n * weight / total) < 1  # метод найбільших залишків: квота ± 1


def test_apportion_uses_exact_arithmetic_where_float_gives_another_result():
    # З float-квотами тут виходить [14, 5, 1] і [11, 3, 1, 3]: залишки, що мають бути рівними/різними,
    # розходяться на шумі плаваючої коми. Точні дроби дають детермінований результат.
    assert apportion(20, [12, 4.5, 0.3]) == [14, 6, 0]
    assert apportion(18, [0.7, 0.2, 0.1, 0.2]) == [10, 3, 2, 3]


def test_apportion_is_deterministic_and_does_not_depend_on_call_order():
    first = [apportion(n, [12, 4.5, 4.5]) for n in range(30)]
    second = [apportion(n, [12, 4.5, 4.5]) for n in range(30)]
    assert first == second


@pytest.mark.parametrize(
    "n, weights",
    [
        (-1, [1, 1]),
        (3, []),
        (3, [1, 0]),
        (3, [1, -2]),
        (3, [1, float("nan")]),
        (3, [1, float("inf")]),
        (3, [1, True]),
        (3, [1, "2"]),
        (True, [1, 1]),
        (2.5, [1, 1]),
    ],
)
def test_apportion_rejects_invalid_input(n, weights):
    with pytest.raises(ValueError):
        apportion(n, weights)


# --------------------------------------------------------------------------- порядок і розподіл


def test_order_preserved_across_two_providers(scenario):
    pool, doubles = _kit(scenario, [1, 1])
    sigs = _signatures(50)
    random.Random(7).shuffle(sigs)  # не відсортований вхід: порядок береться з позицій, а не з підписів
    with _closing_default_runner(pool):
        got = pool.get_transactions(sigs, deadline=_deadline())
    assert [raw["signature"] for raw in got] == sigs
    shares = _shares(doubles)
    assert all(share for share in shares)  # кожен провайдер отримав непорожню частку
    assert sorted(sum(shares, ())) == sorted(sigs)  # кожен підпис рівно раз
    assert [len(s) for s in shares] == apportion(50, [1, 1])
    # частки — суцільні відрізки позицій у порядку провайдерів конфігу
    assert shares[0] == tuple(sigs[:25]) and shares[1] == tuple(sigs[25:])


def test_faster_provider_gets_proportionally_more(scenario):
    # Метод найбільших залишків: 50 · 12/16,5 = 36,36 і 50 · 4,5/16,5 = 13,64 -> 36/14
    # (у рядку задачі стояло «37/13» — арифметична похибка, див. звіт).
    pool, doubles = _kit(scenario, [12, 4.5], runner=SequentialRunner())
    sigs = _signatures(50)
    got = pool.get_transactions(sigs, deadline=_deadline())
    assert [raw["signature"] for raw in got] == sigs
    fast, slow = (len(share) for share in _shares(doubles))
    assert (fast, slow) == (36, 14)
    assert fast > 2 * slow  # пропорційно вазі (12 : 4,5 ≈ 2,67), а не порівну
    assert (fast, slow) != (25, 25)


def test_four_providers_split_follows_weights_and_config_order(scenario):
    pool, doubles = _kit(scenario, [12, 4.5, 4.5, 4.5], runner=SequentialRunner())
    sigs = _signatures(25)
    pool.get_transactions(sigs, deadline=_deadline())
    shares = _shares(doubles)
    assert [len(s) for s in shares] == [12, 5, 4, 4]
    assert sum(shares, ()) == tuple(sigs)  # відрізки йдуть підряд у порядку конфігу


@pytest.mark.parametrize("count, calls", [(1, 1), (2, 2), (3, 3), (4, 3)])
def test_provider_without_a_share_is_not_called_at_all(scenario, count, calls):
    pool, doubles = _kit(scenario, [4.5, 4.5, 4.5], runner=SequentialRunner())
    got = pool.get_transactions(_signatures(count), deadline=_deadline())
    assert [raw["signature"] for raw in got] == _signatures(count)
    # порожніх звернень немає: провайдер із нульовою часткою взагалі не викликається
    assert sum(len(d.log) for d in doubles) == calls
    assert all(keys for d in doubles for _, keys in d.log)


def test_input_order_accepts_tuples_and_duplicate_signatures(scenario):
    pool, doubles = _kit(scenario, [1, 1, 1], runner=SequentialRunner("reverse"))
    sigs = ("sig005", "sig001", "sig005", "sig000", "sig001", "sig005")
    got = pool.get_transactions(sigs, deadline=_deadline())
    assert [raw["signature"] for raw in got] == list(sigs)  # дублі займають свої позиції


def test_unknown_signature_stays_none_at_its_position(scenario):
    pool, _ = _kit(scenario, [1, 1], runner=SequentialRunner("reverse"))
    sigs = ["sig000", "missing", "sig002", "sig003"]
    got = pool.get_transactions(sigs, deadline=_deadline())
    assert [None if raw is None else raw["signature"] for raw in got] == ["sig000", None, "sig002", "sig003"]


def test_provider_skipping_get_transaction_gets_no_share(scenario):
    doubles = [ScriptedProvider(FixtureRpcSource(scenario)) for _ in range(2)]
    providers = [
        _provider("a", doubles[0], 12.0, skip_methods=frozenset({"getTransaction"})),
        _provider("b", doubles[1], 4.5),
    ]
    pool = _pool(providers, runner=SequentialRunner())
    sigs = _signatures(10)
    got = pool.get_transactions(sigs, deadline=_deadline())
    assert [raw["signature"] for raw in got] == sigs
    assert doubles[0].log == []
    assert _shares(doubles)[1] == tuple(sigs)


@pytest.mark.parametrize(
    "weights, skipper, expected",
    [
        ([1, 12, 4.5], 0, (0, 36, 14)),  # провайдер зі skip першим
        ([12, 1, 4.5], 1, (36, 0, 14)),  # ... посередині: ваги придатних беруться за їхніми індексами
        ([12, 4.5, 1], 2, (36, 14, 0)),  # ... останнім
    ],
)
def test_split_uses_weights_of_eligible_providers_only(scenario, weights, skipper, expected):
    doubles = [ScriptedProvider(FixtureRpcSource(scenario)) for _ in weights]
    providers = [
        _provider(
            f"p{i}", d, w, skip_methods=frozenset({"getTransaction"}) if i == skipper else frozenset()
        )
        for i, (d, w) in enumerate(zip(doubles, weights))
    ]
    pool = _pool(providers, runner=SequentialRunner())
    sigs = _signatures(50)
    got = pool.get_transactions(sigs, deadline=_deadline())
    assert [raw["signature"] for raw in got] == sigs
    assert tuple(len(share) for share in _shares(doubles)) == expected  # 50 · 12/16,5 = 36,4 -> 36/14
    assert doubles[skipper].log == []


# --------------------------------------------------------------------------- незалежність від порядку завершення


@pytest.mark.parametrize("order", ["forward", "reverse", [1, 2, 0], [2, 0, 1], [0, 2, 1]])
def test_completion_order_does_not_change_result(scenario, order):
    sigs = _signatures(40)
    random.Random(3).shuffle(sigs)
    pool, doubles = _kit(scenario, [12, 4.5, 4.5], runner=SequentialRunner(order))
    got = pool.get_transactions(sigs, deadline=_deadline())
    reference, _ = _kit(scenario, [12, 4.5, 4.5], runner=SequentialRunner("forward"))
    assert got == reference.get_transactions(sigs, deadline=_deadline())
    assert [raw["signature"] for raw in got] == sigs
    # склад часток теж не залежить від порядку завершення
    assert [len(s) for s in _shares(doubles)] == apportion(40, [12, 4.5, 4.5])


def test_completion_order_does_not_change_result_with_threads(scenario):
    sigs = _signatures(45)
    reference, _ = _kit(scenario, [12, 4.5, 4.5], runner=SequentialRunner())
    expected = reference.get_transactions(sigs, deadline=_deadline())
    for _ in range(5):  # реальні потоки: порядок завершення довільний
        pool, _ = _kit(scenario, [12, 4.5, 4.5])
        with _closing_default_runner(pool):
            assert pool.get_transactions(sigs, deadline=_deadline()) == expected


# --------------------------------------------------------------------------- справжня одночасність


@pytest.mark.parametrize("runner_kind", ["explicit", "default"])
def test_providers_run_concurrently(scenario, runner_kind):
    # Усі три дублі чекають на спільному бар'єрі перед обслуговуванням: послідовне виконання зламало б
    # його (BrokenBarrierError через timeout). Час не вимірюється.
    barrier = threading.Barrier(3, timeout=5)
    runner = ThreadRunner(3) if runner_kind == "explicit" else None
    pool, doubles = _kit(scenario, [1, 1, 1], runner=runner, scripted={i: {"barrier": barrier} for i in range(3)})
    sigs = _signatures(30)
    with contextlib.closing(runner) if runner is not None else _closing_default_runner(pool):
        got = pool.get_transactions(sigs, deadline=_deadline())
    assert [raw["signature"] for raw in got] == sigs
    assert not barrier.broken
    assert all(share for share in _shares(doubles))


def test_thread_runner_outcomes_follow_task_order_not_completion_order():
    first_done_last = threading.Event()

    def slow():
        assert first_done_last.wait(timeout=5)  # завершиться ПІСЛЯ третьої задачі
        return "a"

    def instant():
        return "b"

    def last():
        first_done_last.set()
        return "c"

    with contextlib.closing(ThreadRunner(3)) as runner:
        outcomes = runner([slow, instant, last])
    assert outcomes == [("a", None), ("b", None), ("c", None)]


def test_thread_runner_runs_tasks_in_parallel_and_catches_base_exceptions():
    class Stop(BaseException):
        pass

    barrier = threading.Barrier(3, timeout=5)
    boom = ValueError("boom")
    stop = Stop()

    def meet(result, exc=None):
        def run():
            barrier.wait()
            if exc is not None:
                raise exc
            return result

        return run

    with contextlib.closing(ThreadRunner(3)) as runner:
        outcomes = runner([meet("ok"), meet(None, boom), meet(None, stop)])
    assert outcomes[0] == ("ok", None)
    assert outcomes[1][0] is None and outcomes[1][1] is boom
    assert outcomes[2][0] is None and outcomes[2][1] is stop


def test_thread_runner_more_tasks_than_workers_and_empty_input():
    with contextlib.closing(ThreadRunner(2)) as runner:
        assert runner([]) == []
        outcomes = runner([(lambda i=i: i * i) for i in range(7)])
    assert outcomes == [(i * i, None) for i in range(7)]


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, "3", None])
def test_thread_runner_rejects_invalid_max_workers(bad):
    with pytest.raises(ValueError, match="max_workers"):
        ThreadRunner(bad)


def test_thread_runner_close_is_idempotent():
    runner = ThreadRunner(2)
    assert runner([lambda: 1]) == [(1, None)]
    runner.close()
    runner.close()


# --------------------------------------------------------------------------- SC-001, віртуальний makespan


def test_virtual_makespan_three_equal_providers_at_most_half():
    cfg = dataclasses.replace(load_config(SHIPPED), **EXPECTED["config"])
    clock = FakeClock()
    plain = FixtureRpcSource(BASIC, clock=clock)
    IngestService(cfg, plain, clock).collect(EXPECTED["mint"])
    batches = [params["signatures"] for method, params in plain.calls if method == "getTransaction"]
    assert any(len(batch) >= 3 for batch in batches)  # перевірка не порожня

    reference = FixtureRpcSource(BASIC)
    pool, doubles = _kit(BASIC, [4.5, 4.5, 4.5], runner=SequentialRunner("reverse"))
    for batch in batches:
        before = [len(d.log) for d in doubles]
        got = pool.get_transactions(batch, deadline=_deadline())
        assert got == reference.get_transactions(batch, deadline=_deadline())  # відповідь, як в одиночного
        shares = [
            len(d.log[-1][1]) if len(d.log) > b else 0 for d, b in zip(doubles, before)
        ]
        n = len(batch)
        assert sum(shares) == n
        assert max(shares) == math.ceil(n / 3)  # ідеальний поділ на три рівні провайдери
        if n >= 2:
            # віртуальний makespan: max частки <= 0,5 · сума (SC-001); n == 1 неподільний
            assert max(shares) <= 0.5 * n


# --------------------------------------------------------------------------- дедлайн


def test_expired_deadline_raises_budget_timeout_with_zero_calls(scenario):
    pool, doubles = _kit(scenario, [1, 1, 1], runner=SequentialRunner())
    deadline = Deadline(FakeClock(), 0)
    assert deadline.expired()
    with pytest.raises(RpcBudgetTimeout) as info:
        pool.get_transactions(_signatures(9), deadline=deadline)
    assert str(info.value) == "budget"
    assert info.value.__cause__ is None and info.value.__context__ is None
    assert all(d.log == [] for d in doubles)


def test_deadline_expiring_exactly_at_boundary_is_expired(scenario):
    clock = FakeClock()
    deadline = Deadline(clock, 10)
    pool, doubles = _kit(scenario, [1, 1], runner=SequentialRunner())
    clock.advance(9.999)
    assert len(pool.get_transactions(_signatures(4), deadline=deadline)) == 4
    clock.advance(0.001)  # рівно межа: бюджет вичерпано
    calls = sum(len(d.log) for d in doubles)
    with pytest.raises(RpcBudgetTimeout):
        pool.get_transactions(_signatures(4), deadline=deadline)
    assert sum(len(d.log) for d in doubles) == calls


def test_empty_signatures_zero_calls(scenario):
    pool, doubles = _kit(scenario, [1, 1, 1], runner=SequentialRunner())
    assert pool.get_transactions([], deadline=_deadline()) == []
    assert pool.get_transactions((), deadline=Deadline(FakeClock(), 0)) == []  # порожній вхід — до перевірки дедлайну
    assert all(d.log == [] for d in doubles)


def test_same_deadline_object_reaches_every_provider():
    class Probe:
        name = "probe"

        def __init__(self):
            self.deadlines = []

        def get_transactions(self, signatures, *, deadline):
            self.deadlines.append(deadline)
            return [{"signature": s} for s in signatures]

    probes = [Probe(), Probe(), Probe()]
    pool = _pool([_provider(f"p{i}", probe) for i, probe in enumerate(probes)], runner=SequentialRunner())
    deadline = _deadline()
    pool.get_transactions(_signatures(9), deadline=deadline)
    assert all(probe.deadlines == [deadline] for probe in probes)


# --------------------------------------------------------------------------- відмови частки (усі провайдери; перебирання — T-140)


@pytest.mark.parametrize(
    "exc",
    [RpcRateLimited(2.5), RpcTimeout("request timed out"), RpcUnavailable("http 5xx server error"), RpcBudgetTimeout()],
    ids=["rate_limited", "timeout", "unavailable", "budget"],
)
def test_share_error_is_raised_as_is_without_chain(scenario, exc):
    failing = {i: {"failures": [ScriptedFailure("getTransaction", None, exc)]} for i in range(3)}  # T-140: відмовили всі
    pool, _ = _kit(scenario, [1, 1, 1], runner=SequentialRunner(), scripted=failing)
    with pytest.raises(type(exc)) as info:
        pool.get_transactions(_signatures(30), deadline=_deadline())
    assert info.value is exc  # сам об'єкт адаптера: клас і фіксований текст збережені
    assert info.value.__cause__ is None and info.value.__context__ is None


def test_failure_gives_no_partial_result_and_no_retry(scenario):
    exc = RpcUnavailable("http 5xx server error")
    failing = {i: {"failures": [ScriptedFailure("getTransaction", None, exc)]} for i in range(3)}  # T-140: відмовили всі
    pool, doubles = _kit(scenario, [1, 1, 1], runner=SequentialRunner(), scripted=failing)
    with pytest.raises(RpcUnavailable):
        pool.get_transactions(_signatures(30), deadline=_deadline())
    # відмовлений провайдер не викликається вдруге: кожен рівно раз
    assert [len(d.log) for d in doubles] == [1, 1, 1]


@pytest.mark.parametrize("order", ["forward", "reverse", [2, 0, 1], [1, 2, 0]])
def test_error_of_the_smallest_position_wins_regardless_of_runner_order(scenario, order):
    first, last = RpcRateLimited(1.0), RpcTimeout("request timed out")
    failing = {
        0: {"failures": [ScriptedFailure("getTransaction", None, first)]},
        1: {"failures": [ScriptedFailure("getTransaction", None, RpcUnavailable("http 5xx server error"))]},  # T-140
        2: {"failures": [ScriptedFailure("getTransaction", None, last)]},
    }
    pool, _ = _kit(scenario, [1, 1, 1], runner=SequentialRunner(order), scripted=failing)
    with pytest.raises(RpcRateLimited) as info:
        pool.get_transactions(_signatures(30), deadline=_deadline())
    assert info.value is first


def test_error_of_the_smallest_position_wins_with_real_threads(scenario):
    first, last = RpcRateLimited(1.0), RpcTimeout("request timed out")
    for _ in range(5):
        failing = {
            0: {"failures": [ScriptedFailure("getTransaction", None, first)]},  # T-140: відмовляють усі, найменша позиція — p0
            1: {"failures": [ScriptedFailure("getTransaction", None, RpcUnavailable("http 5xx server error"))]},
            2: {"failures": [ScriptedFailure("getTransaction", None, last)]},
        }
        pool, _ = _kit(scenario, [1, 1, 1], scripted=failing)
        with _closing_default_runner(pool), pytest.raises(RpcRateLimited) as info:
            pool.get_transactions(_signatures(30), deadline=_deadline())
        assert info.value is first



def test_defect_propagates_not_masked_by_an_rpc_error(scenario):
    defect = ValueError("adapter bug")
    failing = {
        0: {"failures": [ScriptedFailure("getTransaction", None, RpcTimeout("request timed out"))]},
        2: {"failures": [ScriptedFailure("getTransaction", None, defect)]},
    }
    pool, _ = _kit(scenario, [1, 1, 1], runner=SequentialRunner(), scripted=failing)
    with pytest.raises(ValueError) as info:
        pool.get_transactions(_signatures(30), deadline=_deadline())
    assert info.value is defect


def test_base_exception_from_a_share_is_not_swallowed(scenario):
    class Stop(BaseException):
        pass

    stop = Stop()
    failing = {1: {"failures": [ScriptedFailure("getTransaction", None, stop)]}}
    pool, _ = _kit(scenario, [1, 1], runner=SequentialRunner(), scripted=failing)
    with pytest.raises(Stop) as info:
        pool.get_transactions(_signatures(10), deadline=_deadline())
    assert info.value is stop


@pytest.mark.parametrize("bad", [lambda sigs: [], lambda sigs: [{}] * (len(sigs) + 1), lambda sigs: None],
                         ids=["empty", "too_long", "not_a_list"])
def test_wrong_share_shape_is_unavailable_not_a_result(scenario, bad):
    class Broken:
        name = "broken"

        def get_transactions(self, signatures, *, deadline):
            return bad(signatures)

    pool = _pool([_provider("broken1", Broken()), _provider("broken", Broken())], runner=SequentialRunner())  # T-140: усі хибні
    with pytest.raises(RpcUnavailable) as info:
        pool.get_transactions(_signatures(10), deadline=_deadline())
    assert str(info.value) == SHAPE_TEXT
    assert info.value.__cause__ is None and info.value.__context__ is None


def test_runner_returning_wrong_number_of_outcomes_is_a_defect(scenario):
    pool, _ = _kit(scenario, [1, 1], runner=lambda tasks: [])
    with pytest.raises(RuntimeError):
        pool.get_transactions(_signatures(10), deadline=_deadline())


# --------------------------------------------------------------------------- локи провайдерів


def test_concurrent_get_transactions_never_overlap_on_one_provider():
    class Probe:
        name = "probe"

        def __init__(self):
            self._guard = threading.Lock()
            self.inside = 0
            self.max_inside = 0

        def get_transactions(self, signatures, *, deadline):
            with self._guard:
                self.inside += 1
                self.max_inside = max(self.max_inside, self.inside)
            time.sleep(0.002)  # лише розширює вікно гонки; перевіряється лічильник, а не час
            with self._guard:
                self.inside -= 1
            return [{"signature": s} for s in signatures]

    probes = [Probe(), Probe()]
    runner = ThreadRunner(8)
    pool = _pool([_provider(f"p{i}", probe) for i, probe in enumerate(probes)], runner=runner)
    errors = []

    def worker():
        try:
            for _ in range(10):
                got = pool.get_transactions(_signatures(6), deadline=_deadline())
                assert [raw["signature"] for raw in got] == _signatures(6)
        except BaseException as exc:  # noqa: BLE001 — збираємо помилки потоків для основного
            errors.append(exc)

    with contextlib.closing(runner):
        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
    assert not errors
    assert [probe.max_inside for probe in probes] == [1, 1]  # провайдер однопотоковий


# --------------------------------------------------------------------------- Provider і конструктор


def test_provider_repr_shows_only_the_name():
    class Leaky:
        def __repr__(self):
            return "Leaky(url='https://rpc.example/SECRET-KEY-123')"

    provider = _provider("helius_1", Leaky(), 4.5)
    assert repr(provider) == "Provider(name='helius_1')"
    assert "SECRET" not in f"{provider!r} {[provider]!r} {provider}"


class _Src:
    name = "s"


def _case(case_id, field, forbidden=None, *, providers=None, **kwargs):
    return pytest.param(providers, kwargs, field, forbidden, id=case_id)


_BAD_CONSTRUCTOR_INPUT = [
    _case("no_providers", "providers", providers=[]),
    _case("name_duplicate", "providers", "SENTINEL-NAME",
          providers=[_provider("SENTINEL-NAME", _Src()), _provider("SENTINEL-NAME", _Src())]),
    _case("name_empty", "name", providers=[_provider("", _Src())]),
    _case("name_not_str", "name", "777", providers=[_provider(777, _Src())]),
    _case("weight_zero", "weight", providers=[_provider("a", _Src(), 0)]),
    _case("weight_negative", "weight", "7.25", providers=[_provider("a", _Src(), -7.25)]),
    _case("weight_nan", "weight", "nan", providers=[_provider("a", _Src(), float("nan"))]),
    _case("weight_inf", "weight", "inf", providers=[_provider("a", _Src(), math.inf)]),
    _case("weight_bool", "weight", "True", providers=[_provider("a", _Src(), True)]),
    _case("weight_str", "weight", "SENTINEL-W", providers=[_provider("a", _Src(), "SENTINEL-W")]),
    _case("archival_with_retention", "retention_seconds", "100.5",
          providers=[_provider("a", _Src(), archival=True, retention_seconds=100.5)]),
    _case("nonarchival_without_retention", "retention_seconds",
          providers=[_provider("a", _Src(), archival=False, retention_seconds=None)]),
    _case("retention_zero", "retention_seconds",
          providers=[_provider("a", _Src(), archival=False, retention_seconds=0)]),
    _case("retention_negative", "retention_seconds", "77.5",
          providers=[_provider("a", _Src(), archival=False, retention_seconds=-77.5)]),
    _case("retention_nan", "retention_seconds", "nan",
          providers=[_provider("a", _Src(), archival=False, retention_seconds=float("nan"))]),
    _case("retention_inf", "retention_seconds", "inf",
          providers=[_provider("a", _Src(), archival=False, retention_seconds=math.inf)]),
    _case("skip_unknown_method", "skip_methods", "SENTINEL-METHOD",
          providers=[_provider("a", _Src(), skip_methods=frozenset({"SENTINEL-METHOD"}))]),
    _case("skip_leaves_signatures_unserved", "skip_methods",
          providers=[_provider("a", _Src(), skip_methods=frozenset({"getSignaturesForAddress"}))]),
    _case("skip_leaves_transactions_unserved_across_pool", "skip_methods",
          providers=[_provider("a", _Src(), skip_methods=frozenset({"getTransaction"})),
                     _provider("b", _Src(), skip_methods=frozenset({"getTransaction"}))]),
    _case("cooldown_zero", "rate_limit_cooldown_seconds",
          providers=[_provider("a", _Src())], rate_limit_cooldown_seconds=0),
    _case("cooldown_nan", "rate_limit_cooldown_seconds", "nan",
          providers=[_provider("a", _Src())], rate_limit_cooldown_seconds=float("nan")),
    _case("cooldown_bool", "rate_limit_cooldown_seconds", "True",
          providers=[_provider("a", _Src())], rate_limit_cooldown_seconds=True),
    _case("age_cache_zero", "age_cache_entries",
          providers=[_provider("a", _Src())], age_cache_entries=0),
    _case("age_cache_bool", "age_cache_entries", "True",
          providers=[_provider("a", _Src())], age_cache_entries=True),
    _case("age_cache_float", "age_cache_entries", "1.5",
          providers=[_provider("a", _Src())], age_cache_entries=1.5),
]


@pytest.mark.parametrize("providers, kwargs, field, forbidden", _BAD_CONSTRUCTOR_INPUT)
def test_constructor_rejects_invalid_input_naming_the_field_without_values(providers, kwargs, field, forbidden):
    with pytest.raises(ValueError) as info:
        _pool(providers, **kwargs)
    message = str(info.value)
    assert field in message
    if forbidden is not None:
        assert forbidden not in message  # значення не відлунюється
    assert info.value.__cause__ is None and info.value.__context__ is None


def test_constructor_accepts_valid_pool_and_does_not_call_providers():
    class Untouched:
        name = "u"

        def __getattr__(self, attr):
            raise AssertionError("constructor must not touch provider sources")

    pool = _pool([
        _provider("fast", Untouched(), 12.0, skip_methods=frozenset({"getTokenAccountsByOwner"})),
        _provider("slow", Untouched(), 4.5, archival=False, retention_seconds=77400.0),
    ])
    assert isinstance(pool, PooledRpcSource)


# =========================================================================== T-133: одиночні методи, ім'я, repr, потоки

MINT = EXPECTED["mint"]
SINGLE_METHODS = ("get_account_info", "get_signatures_for_address", "get_token_accounts_by_owner")
JSONRPC_OF = {
    "get_account_info": "getAccountInfo",
    "get_signatures_for_address": "getSignaturesForAddress",
    "get_token_accounts_by_owner": "getTokenAccountsByOwner",
}


class _Recorder:
    """Мінімальне джерело: пише мітку в спільний журнал порядку й повертає мітку у відповіді."""

    def __init__(self, label, order, name="rec"):
        self.label = label
        self.order = order
        self.name = name

    def get_account_info(self, address, *, deadline):
        self.order.append(self.label)
        return {"owner": self.label, "address": address}

    def get_signatures_for_address(self, address, *, before, until, limit, deadline):
        self.order.append(self.label)
        return [{"signature": self.label, "address": address, "before": before, "until": until, "limit": limit}]

    def get_token_accounts_by_owner(self, owner, *, deadline):
        self.order.append(self.label)
        return [{"pubkey": self.label, "account": {"owner": owner}}]

    def get_transactions(self, signatures, *, deadline):
        self.order.append(self.label)
        return [{"signature": s} for s in signatures]


def _call_single(pool, method, deadline=None):
    deadline = deadline or _deadline()
    if method == "get_account_info":
        return pool.get_account_info("addr", deadline=deadline)
    if method == "get_signatures_for_address":
        return pool.get_signatures_for_address("addr", before="b", until="u", limit=7, deadline=deadline)
    return pool.get_token_accounts_by_owner("owner", deadline=deadline)


def _recorders(weights, **provider_overrides):
    order: list[str] = []
    labels = "abcdefgh"[: len(weights)]
    sources = [_Recorder(label, order) for label in labels]
    providers = [
        _provider(label, source, weight, **provider_overrides.get(label, {}))
        for label, source, weight in zip(labels, sources, weights)
    ]
    return _pool(providers, runner=SequentialRunner()), sources, order


def _reference_swrr(weights, calls):
    """Незалежна модель SWRR (nginx) на цілих числах: нічия -> менший індекс."""
    current = [0] * len(weights)
    total = sum(weights)
    picked = []
    for _ in range(calls):
        for i, w in enumerate(weights):
            current[i] += w
        best = max(range(len(weights)), key=lambda i: (current[i], -i))
        current[best] -= total
        picked.append(best)
    return picked


# --------------------------------------------------------------------------- ротація за вагою


@pytest.mark.parametrize("method", SINGLE_METHODS)
def test_single_calls_rotate_by_weight(method):
    pool, _, order = _recorders([2, 1])
    for _ in range(6):
        _call_single(pool, method)
    assert order == ["a", "b", "a", "a", "b", "a"]


def test_single_calls_rotation_is_shared_across_the_three_methods():
    # Лічильник звернень провайдера — один на всі методи (кошик адаптера глобальний), тож і SWRR спільний:
    # змішані методи йдуть однією послідовністю, а не трьома незалежними.
    pool, _, order = _recorders([2, 1])
    for method in SINGLE_METHODS * 2:
        _call_single(pool, method)
    assert order == ["a", "b", "a", "a", "b", "a"]


@pytest.mark.parametrize("weights", [[2, 1], [1, 1, 1], [24, 9, 9], [5, 1, 1, 3], [1000, 1], [3], [7, 7, 1]])
def test_swrr_matches_independent_reference_model(weights):
    pool, _, order = _recorders(weights)
    calls = 3 * sum(weights) if sum(weights) <= 60 else 1001
    for _ in range(calls):
        _call_single(pool, "get_account_info")
    assert order == ["abcdefgh"[i] for i in _reference_swrr(weights, calls)]


def test_swrr_shipped_weights_12_4_5_4_5_follow_the_ratio_each_period():
    # 12 : 4,5 : 4,5 = 8 : 3 : 3 — за кожен період із 14 викликів рівно стільки, без голодування й черг.
    pool, _, order = _recorders([12, 4.5, 4.5])
    for _ in range(14 * 5):
        _call_single(pool, "get_account_info")
    for start in range(0, 14 * 5, 14):
        period = order[start : start + 14]
        assert [period.count(label) for label in "abc"] == [8, 3, 3]
    assert order[:14] == order[14:28]  # період повторюється: стан вертається в нуль
    assert max(len(run) for run in "".join(order).replace("b", " ").replace("c", " ").split()) <= 3  # гладко


def test_swrr_equal_weights_is_plain_round_robin_in_config_order():
    pool, _, order = _recorders([0.1, 0.1, 0.1])  # 0.1 не точне у двійковій — стан точний, нічиїх немає
    for _ in range(9):
        _call_single(pool, "get_account_info")
    assert order == ["a", "b", "c"] * 3


def test_swrr_extreme_weight_ratio_never_starves_the_light_provider():
    # math.inf профілю відображається у вагу 1e6; полегшений 1000:1 -> за період 1001 легкий отримує рівно 1
    pool, _, order = _recorders([1000, 1])
    for _ in range(1001):
        _call_single(pool, "get_account_info")
    assert order.count("a") == 1000 and order.count("b") == 1
    assert order[500] == "b"  # SWRR розносить: легкий — посередині періоду, а не в кінці


def test_single_provider_pool_always_uses_it():
    pool, _, order = _recorders([4.5])
    for method in SINGLE_METHODS * 3:
        _call_single(pool, method)
    assert order == ["a"] * 9


def test_provider_with_skip_method_never_gets_it_but_serves_the_rest():
    pool, _, order = _recorders(
        [1, 1, 1], b={"skip_methods": frozenset({"getAccountInfo"})}
    )
    for _ in range(6):
        pool.get_account_info("addr", deadline=_deadline())
    assert order == ["a", "c", "a", "c", "a", "c"]  # b не кандидат: ділять a і c
    del order[:]
    for _ in range(9):
        pool.get_token_accounts_by_owner("o", deadline=_deadline())
    assert order.count("b") == 3  # інші методи b обслуговує за своєю вагою


def test_skip_methods_are_per_method_for_each_single_call():
    skips = {
        "a": {"skip_methods": frozenset({"getAccountInfo"})},
        "b": {"skip_methods": frozenset({"getSignaturesForAddress"})},
        "c": {"skip_methods": frozenset({"getTokenAccountsByOwner", "getTransaction"})},
    }
    pool, _, order = _recorders([1, 1, 1], **skips)
    forbidden = {"get_account_info": "a", "get_signatures_for_address": "b", "get_token_accounts_by_owner": "c"}
    for method, banned in forbidden.items():
        del order[:]
        for _ in range(12):
            _call_single(pool, method)
        assert banned not in order and len(set(order)) == 2
        assert order.count(order[0]) == 6  # решта ділиться порівну


def test_skipped_pick_does_not_distort_weights_for_other_methods():
    # Провайдер a (вага 2) не обслуговує getAccountInfo: ці виклики йдуть лише між b і c і не псують
    # співвідношення для методів, які a обслуговує.
    pool, _, order = _recorders([2, 1, 1], a={"skip_methods": frozenset({"getAccountInfo"})})
    for _ in range(10):
        pool.get_account_info("x", deadline=_deadline())
    assert set(order) == {"b", "c"} and order.count("b") == order.count("c") == 5
    del order[:]
    for _ in range(40):
        pool.get_token_accounts_by_owner("o", deadline=_deadline())
    assert [order.count(label) for label in "abc"] == [20, 10, 10]


# --------------------------------------------------------------------------- передача аргументів і результатів


def test_arguments_and_results_pass_through_unchanged():
    pool, _, _ = _recorders([1])
    assert pool.get_account_info("A1", deadline=_deadline()) == {"owner": "a", "address": "A1"}
    assert pool.get_signatures_for_address("A2", before="B", until="U", limit=11, deadline=_deadline()) == [
        {"signature": "a", "address": "A2", "before": "B", "until": "U", "limit": 11}
    ]
    assert pool.get_signatures_for_address("A3", before=None, until=None, limit=1, deadline=_deadline())[0][
        "before"
    ] is None
    assert pool.get_token_accounts_by_owner("O", deadline=_deadline()) == [
        {"pubkey": "a", "account": {"owner": "O"}}
    ]


def test_same_deadline_object_reaches_the_provider_in_single_calls():
    seen = []

    class Probe(_Recorder):
        def get_account_info(self, address, *, deadline):
            seen.append(deadline)
            return None

    pool = _pool([_provider("a", Probe("a", []))])
    deadline = _deadline()
    assert pool.get_account_info("x", deadline=deadline) is None  # None (рахунку немає) повертається як є
    assert seen == [deadline]


def test_pool_answers_like_the_plain_fixture_source_on_real_scenario():
    plain = FixtureRpcSource(BASIC)
    owner = next(iter(json.loads((BASIC / "rpc.json").read_text())["getTokenAccountsByOwner"]))
    doubles = [ScriptedProvider(FixtureRpcSource(BASIC)) for _ in range(2)]
    pool = _pool([_provider("a", doubles[0], 2.0), _provider("b", doubles[1], 1.0)])
    for _ in range(3):  # кілька викликів — відповідає то один, то інший провайдер, відповідь та сама
        assert pool.get_account_info(MINT, deadline=_deadline()) == plain.get_account_info(MINT, deadline=_deadline())
        assert pool.get_signatures_for_address(
            MINT, before=None, until=None, limit=1000, deadline=_deadline()
        ) == plain.get_signatures_for_address(MINT, before=None, until=None, limit=1000, deadline=_deadline())
        assert pool.get_token_accounts_by_owner(owner, deadline=_deadline()) == plain.get_token_accounts_by_owner(
            owner, deadline=_deadline()
        )
    assert pool.get_account_info("unknown-address", deadline=_deadline()) is None
    assert all(len(d.log) > 0 for d in doubles)


# --------------------------------------------------------------------------- дедлайн і відмови (усі провайдери; перебирання — T-140)


@pytest.mark.parametrize("method", SINGLE_METHODS)
def test_single_call_with_expired_deadline_raises_budget_timeout_with_zero_calls(method):
    pool, _, order = _recorders([2, 1])
    dead = Deadline(FakeClock(), 0)
    with pytest.raises(RpcBudgetTimeout) as info:
        _call_single(pool, method, dead)
    assert str(info.value) == "budget"
    assert info.value.__cause__ is None and info.value.__context__ is None
    assert order == []
    # прострочений виклик не зсунув ротацію: наступний виклик іде першому провайдеру
    _call_single(pool, method)
    assert order == ["a"]


@pytest.mark.parametrize("method", SINGLE_METHODS)
@pytest.mark.parametrize(
    "exc",
    [RpcRateLimited(2.5), RpcTimeout("request timed out"), RpcUnavailable("http 5xx server error"), RpcBudgetTimeout()],
    ids=["rate_limited", "timeout", "unavailable", "budget"],
)
def test_single_call_error_is_raised_as_is_when_every_provider_fails(method, exc):
    doubles = [
        ScriptedProvider(FixtureRpcSource(BASIC), failures=[ScriptedFailure(JSONRPC_OF[method], None, exc)]),
        ScriptedProvider(FixtureRpcSource(BASIC), failures=[ScriptedFailure(JSONRPC_OF[method], None, exc)]),
    ]
    pool = _pool([_provider("a", doubles[0], 1.0), _provider("b", doubles[1], 1.0)])
    with pytest.raises(type(exc)) as info:
        _call_single(pool, method)
    assert info.value is exc  # сам об'єкт адаптера: клас і фіксований текст
    assert info.value.__cause__ is None and info.value.__context__ is None
    # T-140: відмовлений перебрано на другого (кожен рівно раз), і бюджет ключа теж
    assert len(doubles[0].log) == 1 and len(doubles[1].log) == 1


def test_single_call_defect_propagates_unchanged():
    defect = ValueError("adapter bug")
    double = ScriptedProvider(FixtureRpcSource(BASIC), failures=[ScriptedFailure("getAccountInfo", None, defect)])
    pool = _pool([_provider("a", double)])
    with pytest.raises(ValueError) as info:
        pool.get_account_info(MINT, deadline=_deadline())
    assert info.value is defect


def test_failed_single_call_leaves_the_pool_usable_and_lock_released():
    exc = RpcTimeout("request timed out")
    double = ScriptedProvider(
        FixtureRpcSource(BASIC), failures=[ScriptedFailure("getAccountInfo", frozenset({1}), exc)]
    )
    pool = _pool([_provider("a", double)])
    with pytest.raises(RpcTimeout):
        pool.get_account_info(MINT, deadline=_deadline())
    # лок провайдера відпущено (інакше цей виклик завис би): наступний виклик проходить
    done = []
    thread = threading.Thread(target=lambda: done.append(pool.get_account_info(MINT, deadline=_deadline())))
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive() and done and done[0] is not None


def test_state_lock_is_not_held_while_a_provider_is_called():
    holder = {}

    class Probe(_Recorder):
        def get_account_info(self, address, *, deadline):
            holder["held"] = holder["pool"]._state_lock.locked()
            return None

        def get_transactions(self, signatures, *, deadline):
            holder["held_tx"] = holder["pool"]._state_lock.locked()
            return [None] * len(signatures)

    pool = _pool([_provider("a", Probe("a", []))], runner=SequentialRunner())
    holder["pool"] = pool
    pool.get_account_info("x", deadline=_deadline())
    pool.get_transactions(["s1"], deadline=_deadline())
    assert holder == {"pool": pool, "held": False, "held_tx": False}


# --------------------------------------------------------------------------- name і repr


def test_name_is_common_provider_name_else_pool():
    same = _pool([_provider("a", _Recorder("a", [], name="http")), _provider("b", _Recorder("b", [], name="http"))])
    assert same.name == "http"
    mixed = _pool([_provider("a", _Recorder("a", [], name="http")), _provider("b", _Recorder("b", [], name="fixture:x"))])
    assert mixed.name == "pool"
    single = _pool([_provider("a", _Recorder("a", [], name="fixture:basic"))])
    assert single.name == "fixture:basic"
    fixtures = _pool([_provider(f"p{i}", FixtureRpcSource(BASIC), 1.0) for i in range(3)])
    assert fixtures.name == FixtureRpcSource(BASIC).name == "fixture:basic"
    other = _pool([_provider("a", FixtureRpcSource(BASIC)), _provider("b", FixtureRpcSource(scenario_other()))])
    assert other.name == "pool"


def scenario_other() -> Path:
    return ROOT / "tests" / "fixtures" / "scenarios" / "hub"


def test_name_is_stable_across_calls_and_does_not_call_providers(scenario):
    double = ScriptedProvider(FixtureRpcSource(scenario))
    pool = _pool([_provider("a", double)])
    assert pool.name == pool.name
    assert double.log == []
    assert isinstance(pool.name, str)


def test_repr_contains_only_provider_names():
    class Leaky(_Recorder):
        def __repr__(self):
            return "Leaky(url='https://rpc.example/?api-key=SECRET-KEY-123')"

        @property
        def url(self):
            return "https://rpc.example/?api-key=SECRET-KEY-123"

    pool = _pool([
        _provider("rpcfast", Leaky("a", []), 12.0),
        _provider("helius_1", Leaky("b", []), 4.5),
    ])
    assert repr(pool) == "PooledRpcSource(providers=('rpcfast', 'helius_1'))"
    for text in (repr(pool), str(pool), f"{pool}", f"{[pool]!r}", f"{ {'k': pool} }", "%s %r" % (pool, pool)):
        assert "SECRET" not in text and "https" not in text and "api-key" not in text
        assert "rpcfast" in text and "helius_1" in text


def test_repr_of_single_provider_pool_is_a_valid_one_tuple():
    pool = _pool([_provider("only", _Recorder("a", []))])
    assert repr(pool) == "PooledRpcSource(providers=('only',))"


def test_repr_does_not_touch_provider_sources():
    class Untouched:
        def __getattr__(self, attr):
            raise AssertionError("repr must not touch provider sources")

    pool = _pool([_provider("a", Untouched()), _provider("b", Untouched())])
    assert repr(pool) == "PooledRpcSource(providers=('a', 'b'))"


# --------------------------------------------------------------------------- потокобезпечність одиночних викликів


def _run_threads(target, count):
    errors = []

    def guarded(index):
        try:
            target(index)
        except BaseException as exc:  # noqa: BLE001 — збираємо помилки потоків для основного
            errors.append(exc)

    threads = [threading.Thread(target=guarded, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not any(thread.is_alive() for thread in threads)
    return errors


def test_concurrent_calls_never_overlap_on_one_provider():
    # Дубль лічить одночасний вхід під власним локом; delay лише розширює вікно гонки.
    doubles = [ScriptedProvider(FixtureRpcSource(BASIC), delay_seconds=0.001) for _ in range(2)]
    pool = _pool([_provider("a", doubles[0], 1.0), _provider("b", doubles[1], 1.0)])

    def worker(index):
        method = SINGLE_METHODS[index % 3]
        for _ in range(20):
            _call_single(pool, method)

    assert _run_threads(worker, 4) == []
    assert [d.max_concurrent for d in doubles] == [1, 1]  # провайдер однопотоковий
    assert sum(len(d.log) for d in doubles) == 80  # жоден виклик не загублено і не продубльовано


def test_concurrent_calls_split_by_weight_deterministically():
    # SWRR під _state_lock: мультимножина виборів не залежить від переплетення потоків.
    for _ in range(3):
        doubles = [ScriptedProvider(FixtureRpcSource(BASIC)) for _ in range(2)]
        pool = _pool([_provider("a", doubles[0], 2.0), _provider("b", doubles[1], 1.0)])
        errors = _run_threads(lambda i: [_call_single(pool, "get_account_info") for _ in range(30)], 4)
        assert errors == []
        assert [len(d.log) for d in doubles] == [80, 40]  # 120 викликів, період 3 -> рівно 2:1


def test_single_calls_and_batch_shares_share_one_lock_per_provider():
    # Виклик get_transactions (потоки runner-а) і одиночні виклики з інших потоків не перетинаються на провайдері.
    doubles = [ScriptedProvider(FixtureRpcSource(BASIC), delay_seconds=0.001) for _ in range(2)]
    runner = ThreadRunner(4)
    pool = _pool([_provider("a", doubles[0], 1.0), _provider("b", doubles[1], 1.0)], runner=runner)
    sigs = list(json.loads((BASIC / "rpc.json").read_text())["getTransaction"])[:6]

    def worker(index):
        for _ in range(10):
            if index % 2:
                pool.get_transactions(sigs, deadline=_deadline())
            else:
                _call_single(pool, SINGLE_METHODS[index % 3])

    with contextlib.closing(runner):
        assert _run_threads(worker, 4) == []
    assert [d.max_concurrent for d in doubles] == [1, 1]


def test_distinct_providers_are_not_serialized_by_one_global_lock():
    # Лок — на провайдера, а не глобальний: два потоки одночасно в різних провайдерах зустрічаються на бар'єрі.
    barrier = threading.Barrier(2, timeout=5)

    class Meeting(_Recorder):
        def get_account_info(self, address, *, deadline):
            barrier.wait()  # обидва провайдери всередині одночасно
            return {"owner": self.label}

    pool = _pool([_provider("a", Meeting("a", [])), _provider("b", Meeting("b", []))])
    results = []
    errors = _run_threads(lambda i: results.append(pool.get_account_info("x", deadline=_deadline())), 2)
    assert errors == [] and not barrier.broken
    assert sorted(r["owner"] for r in results) == ["a", "b"]


def test_swrr_state_updates_are_serialized_by_the_state_lock():
    # Стан SWRR підміняється списком, що «думає» на кожному читанні: без _state_lock потоки перемішали б
    # читання й запис і розподіл поплив би. Із локом вибір атомарний, тож розподіл рівно 2:1.
    class Slow(list):
        def __getitem__(self, index):
            value = super().__getitem__(index)
            time.sleep(0.0002)
            return value

    doubles = [ScriptedProvider(FixtureRpcSource(BASIC)) for _ in range(2)]
    pool = _pool([_provider("a", doubles[0], 2.0), _provider("b", doubles[1], 1.0)])
    pool._swrr_current = Slow(pool._swrr_current)
    errors = _run_threads(lambda i: [_call_single(pool, "get_account_info") for _ in range(15)], 4)
    assert errors == []
    assert [len(d.log) for d in doubles] == [40, 20]
