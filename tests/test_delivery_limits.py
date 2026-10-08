# verifies: FR-008-01, FR-008-02, FR-008-06, FR-008-07, FR-008-08, FR-008-14
"""Примітиви обмеження доставки (фіча 008): `SlidingWindowLimiter`/`Decision` (T-162) і
`HeavySlots`/`Lease` (T-163), `UserGate`/`GateDecision` і `build_protection` (T-168).

Годинник — локальний `ManualClock` (R-3): реального часу й `sleep` у тестах немає. Конкурентні
тести синхронізуються лише `threading.Barrier` / `join` з таймаутом.
"""

from __future__ import annotations

import dataclasses
import ipaddress
import math
import random
import sys
import threading
import time
from pathlib import Path

import pytest

from unmask.delivery.config import ProtectionConfig, load_protection_config
from unmask.delivery.limits import (
    Decision,
    GateDecision,
    HeavySlots,
    Lease,
    Protection,
    SlidingWindowLimiter,
    UserGate,
    build_protection,
)


class ManualClock:
    """Керований монотонний годинник; `advance` приймає і від'ємний крок (регрес)."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, dt: float) -> None:
        self.now += dt


def make(limit: int = 30, window: float = 60, start: float = 0.0):
    clock = ManualClock(start)
    return SlidingWindowLimiter(limit, window, clock), clock


# --------------------------------------------------------------------------- рядок T-162


def test_allows_exactly_limit_then_denies_with_retry_after() -> None:
    lim, clock = make(30, 60)
    for _ in range(30):
        assert lim.hit("1.2.3.4") == Decision(True, 0)
    # усі 30 міток на t=0 -> найстаріша звільниться рівно через 60 с
    assert lim.hit("1.2.3.4") == Decision(False, 60)
    clock.advance(10)
    assert lim.hit("1.2.3.4") == Decision(False, 50)


def test_retry_after_is_honest() -> None:
    lim, clock = make(3, 60)
    for t in (0, 5, 20):
        clock.now = t
        assert lim.hit("k").allowed
    clock.now = 21
    denied = lim.hit("k")
    assert denied == Decision(False, 39)  # 0 + 60 - 21
    # на retry_after − 1 ще заборонено (і відмова не змістила вікно)
    clock.now = 21 + denied.retry_after_seconds - 1
    assert not lim.hit("k").allowed
    # через рівно retry_after — пройшло
    clock.now = 21 + denied.retry_after_seconds
    assert lim.hit("k").allowed


def test_retry_after_is_honest_for_fractional_times() -> None:
    lim, clock = make(2, 10)
    clock.now = 0.25
    assert lim.hit("k").allowed
    clock.now = 3.5
    assert lim.hit("k").allowed
    clock.now = 4.0
    denied = lim.hit("k")
    assert denied == Decision(False, 7)  # ceil(0.25 + 10 - 4.0) = ceil(6.25)
    clock.now = 4.0 + 6  # ще рано
    assert not lim.hit("k").allowed
    clock.now = 4.0 + 7
    assert lim.hit("k").allowed


def test_window_slides_not_resets() -> None:
    lim, clock = make(2, 60)
    assert lim.hit("k").allowed  # t=0
    clock.now = 30
    assert lim.hit("k").allowed  # t=30
    clock.now = 59
    assert not lim.hit("k").allowed
    clock.now = 60  # мітка t=0 рівно на межі: now - window == 0 -> відкинута (≤)
    assert lim.hit("k").allowed
    assert not lim.hit("k").allowed  # мітки 30 і 60 — друга не звільнилась
    clock.now = 61
    assert not lim.hit("k").allowed  # звільнилась рівно одна (t=0), вона вже взята t=60
    clock.now = 90  # мітка t=30 покидає вікно
    assert lim.hit("k").allowed


def test_window_slides_exactly_one_freed_at_61() -> None:
    lim, clock = make(3, 60)
    assert lim.hit("k").allowed  # t=0
    clock.now = 30
    assert lim.hit("k").allowed  # t=30
    assert lim.hit("k").allowed  # t=30
    clock.now = 61  # t=0 пішла, обидві t=30 лишаються
    assert lim.hit("k").allowed
    assert not lim.hit("k").allowed


def test_boundary_stamp_exactly_window_old_is_dropped() -> None:
    # ≤, а не <: мітка рівно `window` секунд тому вже не рахується
    lim, clock = make(1, 60)
    assert lim.hit("k").allowed
    clock.now = 59.999
    assert not lim.hit("k").allowed
    clock.now = 60.0
    assert lim.hit("k").allowed


def test_denied_requests_do_not_extend_block() -> None:
    lim, clock = make(2, 60)
    assert lim.hit("k").allowed
    assert lim.hit("k").allowed
    first = lim.hit("k")
    assert first == Decision(False, 60)
    for _ in range(500):  # клієнт «довбає» — вікно не росте
        clock.advance(0.1)
        assert not lim.hit("k").allowed
    assert len(lim._hits["k"]) == 2
    clock.now = 60
    assert lim.hit("k").allowed  # обіцянка першого Retry-After виконана


def test_keys_are_independent() -> None:
    lim, clock = make(2, 60)
    assert lim.hit("a").allowed
    assert lim.hit("a").allowed
    assert not lim.hit("a").allowed
    assert lim.hit("b") == Decision(True, 0)  # b не постраждав від a
    clock.now = 10
    assert lim.hit("b").allowed
    assert lim.hit("b") == Decision(False, 50)  # свої мітки b (0 -> +60) мінус 10
    assert lim.hit("a") == Decision(False, 50)


def test_clock_going_back_never_blocks_longer_than_window() -> None:
    lim, clock = make(2, 60, start=1000.0)
    assert lim.hit("k").allowed
    assert lim.hit("k").allowed
    clock.now = 10.0  # крок назад на 990 с
    denied = lim.hit("k")
    assert not denied.allowed
    assert 1 <= denied.retry_after_seconds <= 60
    assert denied.retry_after_seconds == 60  # мітки затиснуті до now=10
    clock.now = 10.0 + denied.retry_after_seconds
    assert lim.hit("k").allowed


@pytest.mark.parametrize("back", [0.5, 1, 59, 60, 61, 3600, 1e9])
def test_clock_regress_retry_after_always_within_window(back: float) -> None:
    lim, clock = make(3, 60, start=1e9 + 7)
    for _ in range(3):
        assert lim.hit("k").allowed
        clock.advance(1)
    clock.advance(-back)
    d = lim.hit("k")
    if not d.allowed:
        assert 1 <= d.retry_after_seconds <= 60
        clock.advance(d.retry_after_seconds)
        assert lim.hit("k").allowed


def test_clock_back_then_forward_does_not_resurrect_extra_capacity() -> None:
    lim, clock = make(2, 60, start=100.0)
    assert lim.hit("k").allowed
    assert lim.hit("k").allowed
    clock.now = 50.0
    assert not lim.hit("k").allowed
    clock.now = 100.0  # час вернувся до старого значення: мітки були затиснуті до 50
    assert not lim.hit("k").allowed
    assert len(lim._hits["k"]) == 2


def test_stale_keys_are_swept() -> None:
    lim, clock = make(5, 60)
    for i in range(1000):
        assert lim.hit(f"ip-{i}").allowed
    assert len(lim._hits) == 1000
    clock.advance(60)
    assert lim.hit("fresh").allowed  # прибирання відбувається на hit
    assert set(lim._hits) == {"fresh"}  # 1000 одноразових ключів зникли


def test_sweep_runs_at_most_once_per_window() -> None:
    lim, clock = make(5, 60)
    assert lim.hit("seed").allowed  # t=0 -> перше прибирання відраховується від 0
    clock.now = 40
    assert lim.hit("a").allowed  # a: мітка 40
    clock.now = 60
    assert lim.hit("trigger").allowed  # прибирання (60 - 0 ≥ 60): seed зник, a живе
    assert set(lim._hits) == {"a", "trigger"}
    clock.now = 101  # мітка a (40) вже прострочена, але наступне прибирання — на t=120
    assert lim.hit("c").allowed
    assert "a" in lim._hits
    clock.now = 120
    assert lim.hit("d").allowed
    assert "a" not in lim._hits


def test_sweep_keeps_keys_with_live_stamps() -> None:
    lim, clock = make(5, 60)
    assert lim.hit("old").allowed  # t=0
    clock.now = 30
    assert lim.hit("live").allowed  # t=30
    clock.now = 60
    assert lim.hit("x").allowed  # прибирання: old пішов, live живий
    assert set(lim._hits) == {"live", "x"}
    assert lim.hit("live").allowed
    assert lim.hit("live").allowed
    assert lim.hit("live").allowed
    assert lim.hit("live").allowed
    assert not lim.hit("live").allowed  # 5 міток: лічильник живого ключа не обнулено


def test_sweep_after_clock_regress_does_not_stall_forever() -> None:
    lim, clock = make(2, 60, start=10_000.0)
    assert lim.hit("seed").allowed
    clock.now = 100.0  # годинник упав на 9900 с
    assert lim.hit("k").allowed
    assert "seed" in lim._hits  # у цей момент прибирання ще не настало
    clock.now = 160.0
    assert lim.hit("z").allowed  # прибирання: мітка з майбутнього затиснута до now=160
    assert list(lim._hits["seed"]) == [160.0]
    clock.now = 220.0
    assert lim.hit("y").allowed  # ще за вікно — прибирання не «застигло» на 10 000
    assert set(lim._hits) == {"y"}


def test_sweep_clamps_future_stamps_of_other_keys() -> None:
    lim, clock = make(2, 60, start=5000.0)
    assert lim.hit("future").allowed
    clock.now = 100.0
    assert lim.hit("other").allowed  # прибирання вперше після падіння ще не настало
    clock.now = 160.0
    assert lim.hit("other").allowed  # прибирання: мітка "future" затиснута до now=160, не 5000
    assert list(lim._hits["future"]) == [160.0]
    clock.now = 220.0
    assert lim.hit("other").allowed  # ще за вікно вона прострочена й ключ прибрано
    assert "future" not in lim._hits


@pytest.mark.parametrize("threads,limit", [(50, 10), (12, 1), (8, 8), (9, 100)])
def test_concurrent_hits_never_exceed_limit(threads: int, limit: int) -> None:
    lim, _ = make(limit, 60)
    barrier = threading.Barrier(threads, timeout=10)
    results: list[Decision] = []
    errors: list[BaseException] = []
    out = threading.Lock()

    def worker() -> None:
        try:
            barrier.wait()
            d = lim.hit("shared")
            with out:
                results.append(d)
        except BaseException as exc:  # noqa: BLE001
            with out:
                errors.append(exc)

    ts = [threading.Thread(target=worker, daemon=True) for _ in range(threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=10)
    assert not any(t.is_alive() for t in ts)
    assert not errors
    assert len(results) == threads
    assert sum(d.allowed for d in results) == min(limit, threads)
    assert all(d.retry_after_seconds >= 1 for d in results if not d.allowed)
    assert len(lim._hits["shared"]) == min(limit, threads)


def test_concurrent_hits_different_keys_do_not_interfere() -> None:
    lim, _ = make(3, 60)
    keys = [f"k{i}" for i in range(20)]
    barrier = threading.Barrier(len(keys) * 6, timeout=10)
    got: dict[str, list[bool]] = {k: [] for k in keys}
    out = threading.Lock()

    def worker(k: str) -> None:
        barrier.wait()
        d = lim.hit(k)
        with out:
            got[k].append(d.allowed)

    ts = [threading.Thread(target=worker, args=(k,), daemon=True) for k in keys for _ in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=10)
    assert not any(t.is_alive() for t in ts)
    assert all(sum(v) == 3 and len(v) == 6 for v in got.values())


def test_clock_is_read_under_the_lock() -> None:
    # now читається всередині замка, інакше потоки можуть дописати мітки не за порядком
    seen: list[bool] = []

    def clock() -> float:
        seen.append(lim._lock.locked())
        return 0.0

    lim = SlidingWindowLimiter(1, 60, clock)
    lim.hit("a")
    lim.hit("a")
    assert seen and all(seen)


class _RendezvousKey:
    """Ключ, чий ДРУГИЙ `__hash__` чекає на другий потік (детермінований тест гонки).

    У `hit` перший хеш — `self._hits.get(key)` (перевірка), другий — `self._hits[key] = ...`
    (запис нового ключа). Якщо атомарність порушена (лока немає або перевірка/запис виконуються
    після його звільнення), обидва потоки спершу бачать порожній ключ, а потім зустрічаються
    на `Barrier(2)` перед записом -> обидва allowed. З чинним локом другий потік чекає на
    замок, бар'єр розривається за таймаутом (він лише синхронізує потоки; логіка ліміту
    реального часу не читає), а другий потік бачить уже записану мітку.
    """

    def __init__(self, barrier: threading.Barrier) -> None:
        self._barrier = barrier
        self._calls = 0

    def __hash__(self) -> int:
        self._calls += 1
        if self._calls == 2:
            try:
                self._barrier.wait()
            except threading.BrokenBarrierError:
                pass
        return 12345

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _RendezvousKey)


def test_hit_check_and_record_are_atomic_deterministic_race() -> None:
    lim, _ = make(1, 60)
    barrier = threading.Barrier(2, timeout=0.3)
    results: list[Decision] = []
    errors: list[BaseException] = []
    out = threading.Lock()

    def worker() -> None:
        try:
            d = lim.hit(_RendezvousKey(barrier))
            with out:
                results.append(d)
        except BaseException as exc:  # noqa: BLE001
            with out:
                errors.append(exc)

    ts = [threading.Thread(target=worker, daemon=True) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=10)
    assert not any(t.is_alive() for t in ts)
    assert not errors
    assert len(results) == 2
    assert sum(d.allowed for d in results) == 1


def test_hit_does_not_hold_lock_after_clock_error() -> None:
    def boom() -> float:
        raise RuntimeError("clock failed")

    lim = SlidingWindowLimiter(1, 60, boom)
    with pytest.raises(RuntimeError):
        lim.hit("a")
    assert not lim._lock.locked()  # інакше один збій назавжди заблокує сервіс


# --------------------------------------------------------------------------- межові випадки


def test_limit_one_window_one() -> None:
    lim, clock = make(1, 1)
    assert lim.hit("k") == Decision(True, 0)
    assert lim.hit("k") == Decision(False, 1)
    clock.now = 0.5
    assert lim.hit("k") == Decision(False, 1)  # ceil(0.5) = 1
    clock.now = 0.999
    assert lim.hit("k") == Decision(False, 1)  # ceil(0.001) = 1
    clock.now = 1.0
    assert lim.hit("k") == Decision(True, 0)


def test_retry_after_is_clamped_to_at_least_one() -> None:
    # залишилось 0.001 с: ceil дає 1; не 0 (Retry-After: 0 підбурює до негайного повтору)
    lim, clock = make(1, 60)
    assert lim.hit("k").allowed
    clock.now = 59.999
    d = lim.hit("k")
    assert d.retry_after_seconds == 1 and not d.allowed


def test_fractional_window_retry_after_never_zero_never_above_ceil() -> None:
    lim, clock = make(1, 0.25)
    assert lim.hit("k").allowed
    d = lim.hit("k")
    assert d == Decision(False, 1)  # ceil(0.25) = 1
    clock.now = 0.25
    assert lim.hit("k").allowed


def test_retry_after_upper_bound_is_ceil_window() -> None:
    lim, clock = make(1, 2.5, start=100.0)
    assert lim.hit("k").allowed
    d = lim.hit("k")
    assert d.retry_after_seconds == 3  # ceil(2.5)
    clock.now = 100.0 + 3
    assert lim.hit("k").allowed


def test_very_large_values() -> None:
    big = 10**30
    lim, clock = make(big, 10**12)
    for _ in range(100):
        assert lim.hit("k").allowed
    lim2, _ = make(1, 1e308)
    assert lim2.hit("k").allowed
    d = lim2.hit("k")
    assert not d.allowed
    assert d.retry_after_seconds == math.ceil(1e308)
    # сума oldest + window переповнюється до inf: відповідь усе одно скінченна
    lim3, clock3 = make(1, 1.7e308, start=1e308)
    assert lim3.hit("k").allowed
    d3 = lim3.hit("k")
    assert not d3.allowed
    assert d3.retry_after_seconds == math.ceil(1.7e308)


def test_retry_after_upper_clamp_holds_when_float_rounding_overshoots() -> None:
    # при now ~ 2**56 (ulp = 16) oldest + window округлюється вгору: без верхньої межі
    # вийшло б 64 > ceil(60)
    now = float(2**56)
    lim, clock = make(1, 60, start=now)
    assert lim.hit("k").allowed
    assert lim.hit("k") == Decision(False, 60)


def test_retry_after_lower_clamp_holds_when_float_rounding_undershoots() -> None:
    # now ~ 5.18e16: oldest > now − window, але oldest + window − now == 0.0 через округлення;
    # відповідь не може бути 0 (Retry-After: 0 запрошує негайний повтор)
    now, oldest = 5.180748559146957e16, 5.180748559146951e16
    assert oldest > now - 60 and oldest + 60 - now == 0.0
    lim, clock = make(1, 60, start=oldest)
    assert lim.hit("k").allowed
    clock.now = now
    assert lim.hit("k") == Decision(False, 1)


def test_huge_clock_values_stay_exact_for_integer_timestamps() -> None:
    lim, clock = make(2, 60, start=2.0**40)
    assert lim.hit("k").allowed
    assert lim.hit("k").allowed
    assert lim.hit("k") == Decision(False, 60)
    clock.advance(60)
    assert lim.hit("k").allowed


@pytest.mark.parametrize("limit", [0, -1, -100])
def test_nonpositive_limit_rejected(limit: int) -> None:
    with pytest.raises(ValueError, match="limit"):
        SlidingWindowLimiter(limit, 60)


@pytest.mark.parametrize("window", [0, -1, -0.5, 0.0, -1e-9])
def test_nonpositive_window_rejected(window: float) -> None:
    with pytest.raises(ValueError, match="window"):
        SlidingWindowLimiter(5, window)


@pytest.mark.parametrize("window", [float("inf"), float("-inf"), float("nan")])
def test_nonfinite_window_rejected(window: float) -> None:
    with pytest.raises(ValueError, match="window"):
        SlidingWindowLimiter(5, window)


@pytest.mark.parametrize("limit", [True, False, 1.0, 5.5, "5", None, [5]])
def test_non_int_limit_rejected(limit: object) -> None:
    with pytest.raises(ValueError, match="limit"):
        SlidingWindowLimiter(limit, 60)  # type: ignore[arg-type]


@pytest.mark.parametrize("window", [True, False, "60", None, [60]])
def test_non_number_window_rejected(window: object) -> None:
    with pytest.raises(ValueError, match="window"):
        SlidingWindowLimiter(5, window)  # type: ignore[arg-type]


def test_non_callable_clock_rejected() -> None:
    with pytest.raises(ValueError, match="clock"):
        SlidingWindowLimiter(5, 60, "now")  # type: ignore[arg-type]


def test_default_clock_is_monotonic_and_works() -> None:
    import time

    lim = SlidingWindowLimiter(2, 60)
    assert lim._clock is time.monotonic
    assert lim.hit("k").allowed and lim.hit("k").allowed
    d = lim.hit("k")
    assert not d.allowed and 1 <= d.retry_after_seconds <= 60


def test_window_given_as_int_and_float_equivalent() -> None:
    a, ca = make(2, 60)
    b, cb = make(2, 60.0)
    for lim in (a, b):
        assert lim.hit("k").allowed
        assert lim.hit("k").allowed
    assert a.hit("k") == b.hit("k") == Decision(False, 60)


@pytest.mark.parametrize("key", ["", "x" * 100_000, "ключ-🙂", "1.2.3.4", "::1", "a\x00b"])
def test_odd_string_keys(key: str) -> None:
    lim, clock = make(1, 60)
    assert lim.hit(key).allowed
    assert not lim.hit(key).allowed
    assert lim.hit(key + "!").allowed  # довгий/екзотичний ключ не зливається з сусіднім


def test_similar_keys_do_not_merge() -> None:
    lim, _ = make(1, 60)
    for k in ("1.2.3.4", "1.2.3.40", "1.2.3.", "::1", "::01", "A", "a", " a", "a "):
        assert lim.hit(k).allowed, k


def test_unhashable_key_raises_type_error_and_leaves_state_clean() -> None:
    lim, _ = make(1, 60)
    with pytest.raises(TypeError):
        lim.hit(["x"])  # type: ignore[arg-type]
    assert not lim._lock.locked()
    assert lim._hits == {}


def test_memory_is_bounded_by_live_keys_and_limit() -> None:
    lim, clock = make(3, 10)
    for wave in range(20):
        for i in range(500):
            lim.hit(f"w{wave}-{i}")
            lim.hit(f"w{wave}-{i}")
        clock.advance(10)
    # після кожної хвилі лишаються максимум ключі останньої та поточної
    assert len(lim._hits) <= 1000
    assert all(len(v) <= 3 for v in lim._hits.values())
    clock.advance(10)
    lim.hit("final")
    assert set(lim._hits) == {"final"}


def test_denied_hits_do_not_create_or_leak_keys() -> None:
    lim, clock = make(1, 60)
    lim.hit("k")
    for _ in range(1000):
        lim.hit("k")
    assert set(lim._hits) == {"k"} and len(lim._hits["k"]) == 1


def test_invariants_hold_under_random_walk_including_regress() -> None:
    rng = random.Random(20261008)
    lim, clock = make(4, 30, start=1000.0)
    for _ in range(5000):
        clock.advance(rng.choice([-50, -1, 0, 0.5, 1, 7, 31, 100]))
        k = rng.choice(["a", "b", "c"])
        d = lim.hit(k)
        assert all(s <= clock.now for s in lim._hits[k])  # ключ, до якого щойно звернулись
        assert all(len(v) <= 4 for v in lim._hits.values())
        assert all(list(v) == sorted(v) for v in lim._hits.values())
        if d.allowed:
            assert d.retry_after_seconds == 0
        else:
            assert 1 <= d.retry_after_seconds <= 30


def test_honest_retry_after_property_on_dyadic_times() -> None:
    # точна арифметика (кратні 1/8): за retry_after − 1 ще заборонено, за retry_after — дозволено
    rng = random.Random(7)
    checked = 0
    for _ in range(400):
        limit = rng.randint(1, 6)
        window = rng.choice([1, 2, 3, 10, 60, 0.5, 2.5])
        lim, clock = make(limit, window)
        for _ in range(rng.randint(1, 40)):
            clock.advance(rng.randint(0, 80) / 8)
            d = lim.hit("k")
            if d.allowed:
                continue
            start = clock.now
            clock.now = start + d.retry_after_seconds - 1
            assert not lim.hit("k").allowed, (limit, window, d)
            clock.now = start + d.retry_after_seconds
            assert lim.hit("k").allowed, (limit, window, d)
            checked += 1
            break
    assert checked > 100


def test_decision_is_frozen_value_object() -> None:
    d = Decision(True, 0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        d.allowed = False  # type: ignore[misc]
    assert Decision(False, 3) == Decision(False, 3)
    assert hash(Decision(False, 3)) == hash(Decision(False, 3))


@pytest.mark.parametrize("allowed,retry", [(True, 1), (True, -1), (False, 0), (False, -3)])
def test_decision_rejects_inconsistent_values(allowed: bool, retry: int) -> None:
    with pytest.raises(ValueError):
        Decision(allowed, retry)


def test_decision_rejects_bool_or_float_retry() -> None:
    with pytest.raises(ValueError):
        Decision(False, True)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Decision(False, 1.5)  # type: ignore[arg-type]


# =========================================================================== T-163: HeavySlots
# FR-008-02 (обмеження одночасних аналізів, без очікування) і FR-008-06 (слот звільняється за
# будь-якого завершення). Годинник не потрібен: стан — лише лічильник під одним замком.


def slots(capacity: int = 3, retry: int = 7) -> HeavySlots:
    return HeavySlots(capacity, retry)


def test_capacity_then_none() -> None:
    hs = slots(3)
    leases = [hs.try_acquire() for _ in range(3)]
    assert all(isinstance(x, Lease) for x in leases)
    assert hs.in_use == 3
    assert hs.try_acquire() is None  # (capacity+1)-й: без очікування й без побічних ефектів
    assert hs.try_acquire() is None
    assert hs.in_use == 3


def test_capacity_one_boundary() -> None:
    hs = slots(1)
    first = hs.try_acquire()
    assert first is not None and hs.in_use == 1
    assert hs.try_acquire() is None  # саме тут ловить `<=` замість `<`
    first.release()
    assert hs.in_use == 0
    again = hs.try_acquire()
    assert again is not None and hs.in_use == 1
    assert hs.try_acquire() is None


def test_release_frees_slot_for_next() -> None:
    hs = slots(2)
    a, b = hs.try_acquire(), hs.try_acquire()
    assert a is not None and b is not None
    assert hs.try_acquire() is None
    a.release()
    assert hs.in_use == 1
    c = hs.try_acquire()
    assert c is not None and c is not a  # нова ліза, не стара
    assert hs.in_use == 2
    assert hs.try_acquire() is None
    b.release()
    c.release()
    assert hs.in_use == 0


def test_double_release_does_not_underflow() -> None:
    hs = slots(2)
    lease = hs.try_acquire()
    assert lease is not None
    lease.release()
    lease.release()
    lease.release()
    assert hs.in_use == 0  # не -2
    # і ємність не «роздулась»: тепер можна взяти рівно capacity, не більше
    got = [hs.try_acquire() for _ in range(3)]
    assert [g is not None for g in got] == [True, True, False]
    assert hs.in_use == 2


def test_double_release_does_not_free_someone_elses_slot() -> None:
    # справжня шкода подвійного звільнення: лічильник спільний, тож зайве -1 забирає чужий слот
    hs = slots(2)
    a, b = hs.try_acquire(), hs.try_acquire()
    assert a is not None and b is not None
    a.release()
    a.release()  # b досі тримає слот
    assert hs.in_use == 1
    c = hs.try_acquire()
    assert c is not None
    assert hs.try_acquire() is None  # ємність 2: b + c; без прапорця тут був би 3-й
    assert hs.in_use == 2


def test_release_of_one_lease_does_not_affect_another() -> None:
    hs = slots(3)
    a, b, c = hs.try_acquire(), hs.try_acquire(), hs.try_acquire()
    assert None not in (a, b, c)
    b.release()
    assert hs.in_use == 2
    assert not a.released and b.released and not c.released
    a.release()
    assert hs.in_use == 1 and c is not None and not c.released
    c.release()
    assert hs.in_use == 0


def test_slots_instances_are_independent() -> None:
    one, two = slots(1), slots(1)
    a = one.try_acquire()
    assert a is not None
    assert one.try_acquire() is None
    b = two.try_acquire()  # інший пул не бачить зайнятості першого
    assert b is not None
    a.release()
    a.release()
    assert one.in_use == 0 and two.in_use == 1  # звільнення лізи чужого пулу не чіпає наш
    assert two.try_acquire() is None


@pytest.mark.parametrize("exc", [ValueError, KeyboardInterrupt, TimeoutError,
                                 SystemExit, GeneratorExit, MemoryError])
def test_lease_released_on_exception_and_base_exception(exc: type[BaseException]) -> None:
    hs = slots(1)
    lease = hs.try_acquire()
    assert lease is not None
    with pytest.raises(exc):
        with lease:
            assert hs.in_use == 1  # у тілі слот утримується
            raise exc("boom")
    assert hs.in_use == 0
    assert lease.released
    assert not hs._lock.locked()
    nxt = hs.try_acquire()  # слот справді вільний, а не лише лічильник
    assert nxt is not None


def test_slot_is_free_before_exception_reaches_handler() -> None:
    # порядок: __exit__ відпрацював ДО того, як виняток дійшов до зовнішнього except/finally
    hs = slots(1)
    seen: list[int] = []
    lease = hs.try_acquire()
    assert lease is not None
    try:
        try:
            with lease:
                raise ValueError("x")
        finally:
            seen.append(hs.in_use)  # внутрішній finally іде після __exit__
    except ValueError:
        seen.append(hs.in_use)
        assert hs.try_acquire() is not None  # у самому обробнику слот уже можна взяти
    assert seen == [0, 0]


def test_exit_does_not_swallow_the_exception() -> None:
    hs = slots(1)
    lease = hs.try_acquire()
    assert lease is not None
    boom = RuntimeError("original")
    with pytest.raises(RuntimeError) as info:
        with lease:
            raise boom
    assert info.value is boom  # той самий об'єкт, не загорнутий і не проковтнутий
    assert lease.__exit__(None, None, None) in (None, False)
    ok = hs.try_acquire()
    assert ok is not None
    assert ok.__exit__(ValueError, ValueError("v"), None) in (None, False)


def test_context_manager_releases_on_normal_exit() -> None:
    hs = slots(2)
    lease = hs.try_acquire()
    assert lease is not None
    with lease as entered:
        assert entered is lease
        assert hs.in_use == 1
    assert hs.in_use == 0


def test_release_inside_body_then_exit_does_not_underflow() -> None:
    hs = slots(2)
    other = hs.try_acquire()
    lease = hs.try_acquire()
    assert lease is not None and other is not None
    with lease:
        lease.release()  # раннє явне звільнення
        assert hs.in_use == 1
    assert hs.in_use == 1  # __exit__ нічого не зняв удруге: other досі тримає
    other.release()
    assert hs.in_use == 0


def test_reentering_released_lease_never_takes_a_slot_back() -> None:
    hs = slots(1)
    lease = hs.try_acquire()
    assert lease is not None
    with lease:
        pass
    assert hs.in_use == 0
    with lease:  # повторний вхід: слота не відновлює, але й нічого не ламає
        assert hs.in_use == 0
        assert hs.try_acquire() is not None  # слот лишився вільним для інших
    assert hs.in_use == 1  # новий власник; повторний __exit__ старої лізи його не звільнив


def test_nested_reentry_of_live_lease_releases_once() -> None:
    hs = slots(1)
    lease = hs.try_acquire()
    assert lease is not None
    with lease:
        with lease:
            assert hs.in_use == 1
        assert hs.in_use == 0  # внутрішній вихід звільнив (ліза — один слот, не лічильник входів)
    assert hs.in_use == 0  # зовнішній __exit__ ідемпотентний


def test_retry_after_seconds_is_the_configured_value() -> None:
    assert slots(3, 7).retry_after_seconds == 7
    hs = slots(1, 1)
    assert hs.retry_after_seconds == 1
    lease = hs.try_acquire()
    assert lease is not None and hs.try_acquire() is None
    assert hs.retry_after_seconds == 1  # не залежить від зайнятості, ніколи не 0
    big = HeavySlots(2, 10**9)
    assert big.retry_after_seconds == 10**9 and type(big.retry_after_seconds) is int


def test_capacity_property() -> None:
    assert slots(5).capacity == 5


def test_try_acquire_never_blocks_when_full() -> None:
    hs = slots(1)
    held = hs.try_acquire()
    assert held is not None
    box: list[object] = []
    t = threading.Thread(target=lambda: box.append(hs.try_acquire()), daemon=True)
    t.start()
    t.join(timeout=5)
    assert not t.is_alive()  # без очікування: повернувся одразу
    assert box == [None]


def test_in_use_never_negative_or_above_capacity_under_random_walk() -> None:
    rng = random.Random(20261008)
    cap = 3
    hs = slots(cap)
    live: list[Lease] = []
    dead: list[Lease] = []
    for _ in range(5000):
        op = rng.choice(["acq", "acq", "rel", "rel_dead", "with", "with_exc"])
        if op == "acq":
            lease = hs.try_acquire()
            assert (lease is None) == (len(live) == cap)
            if lease is not None:
                live.append(lease)
        elif op == "rel" and live:
            lease = live.pop(rng.randrange(len(live)))
            lease.release()
            dead.append(lease)
        elif op == "rel_dead" and dead:
            rng.choice(dead).release()  # повторні звільнення давно мертвих лізів
        elif op in ("with", "with_exc"):
            lease = hs.try_acquire()
            if lease is not None:
                try:
                    with lease:
                        if op == "with_exc":
                            raise KeyboardInterrupt
                except KeyboardInterrupt:
                    pass
                dead.append(lease)
        assert hs.in_use == len(live)
        assert 0 <= hs.in_use <= cap
    for lease in live:
        lease.release()
    assert hs.in_use == 0


# --- валідація параметрів


@pytest.mark.parametrize("capacity", [0, -1, -100])
def test_nonpositive_capacity_rejected(capacity: int) -> None:
    with pytest.raises(ValueError, match="capacity"):
        HeavySlots(capacity, 5)


@pytest.mark.parametrize("capacity", [True, False, 1.0, 2.5, "3", None, [3], float("nan")])
def test_non_int_capacity_rejected(capacity: object) -> None:
    with pytest.raises(ValueError, match="capacity"):
        HeavySlots(capacity, 5)  # type: ignore[arg-type]


@pytest.mark.parametrize("retry", [0, -1, -60])
def test_nonpositive_retry_after_rejected(retry: int) -> None:
    with pytest.raises(ValueError, match="busy_retry_after_seconds"):
        HeavySlots(2, retry)


@pytest.mark.parametrize("retry", [True, False, 1.0, 2.5, "5", None, [5], float("inf")])
def test_non_int_retry_after_rejected(retry: object) -> None:
    with pytest.raises(ValueError, match="busy_retry_after_seconds"):
        HeavySlots(2, retry)  # type: ignore[arg-type]


# --- одночасність


@pytest.mark.parametrize("threads,capacity", [(40, 3), (12, 1), (8, 8), (9, 100)])
def test_concurrent_acquire_respects_capacity(threads: int, capacity: int) -> None:
    hs = slots(capacity)
    start = threading.Barrier(threads, timeout=10)
    tried = threading.Barrier(threads + 1, timeout=10)  # +1: головний потік
    done = threading.Event()  # утримуємо лізи, поки головний не перевірить in_use
    got: list[Lease | None] = []
    errors: list[BaseException] = []
    out = threading.Lock()

    def worker() -> None:
        try:
            start.wait()
            lease = hs.try_acquire()
            with out:
                got.append(lease)
            tried.wait()
            done.wait(timeout=10)
        except BaseException as exc:  # noqa: BLE001
            with out:
                errors.append(exc)

    ts = [threading.Thread(target=worker, daemon=True) for _ in range(threads)]
    for t in ts:
        t.start()
    tried.wait()  # усі спроби завершені, лізи ще утримуються
    in_use = hs.in_use
    done.set()
    for t in ts:
        t.join(timeout=10)
    assert not any(t.is_alive() for t in ts)
    assert not errors
    leases = [x for x in got if x is not None]
    assert len(got) == threads
    assert len(leases) == min(capacity, threads)  # рівно capacity лізів
    assert in_use == min(capacity, threads)
    assert len({id(x) for x in leases}) == len(leases)
    for lease in leases:
        lease.release()
    assert hs.in_use == 0


def test_concurrent_churn_never_exceeds_capacity_and_ends_empty() -> None:
    cap, threads, rounds = 3, 16, 300
    hs = slots(cap)
    barrier = threading.Barrier(threads, timeout=10)
    out = threading.Lock()
    held = 0
    peak = 0
    errors: list[BaseException] = []

    def worker() -> None:
        nonlocal held, peak
        try:
            barrier.wait()
            for i in range(rounds):
                lease = hs.try_acquire()
                if lease is None:
                    continue
                try:
                    with lease:
                        with out:
                            held += 1
                            peak = max(peak, held)
                        with out:
                            held -= 1
                        if i % 7 == 0:
                            raise ValueError("work failed")
                except ValueError:
                    pass
                lease.release()  # зайве, ідемпотентне
        except BaseException as exc:  # noqa: BLE001
            with out:
                errors.append(exc)

    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)  # агресивні перемикання потоків
    try:
        ts = [threading.Thread(target=worker, daemon=True) for _ in range(threads)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=30)
    finally:
        sys.setswitchinterval(old)
    assert not any(t.is_alive() for t in ts)
    assert not errors
    assert 1 <= peak <= cap
    assert hs.in_use == 0


def test_concurrent_double_release_of_same_lease_frees_once() -> None:
    threads = 16
    hs = slots(2)
    keep = hs.try_acquire()  # другий власник, чий слот не можна зачепити
    lease = hs.try_acquire()
    assert keep is not None and lease is not None
    barrier = threading.Barrier(threads, timeout=10)

    def worker() -> None:
        barrier.wait()
        lease.release()

    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        ts = [threading.Thread(target=worker, daemon=True) for _ in range(threads)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=10)
    finally:
        sys.setswitchinterval(old)
    assert not any(t.is_alive() for t in ts)
    assert hs.in_use == 1  # keep досі тримає; зайві release не вкрали його слот


class _RendezvousInt(int):
    """int, чиє будь-яке порівняння чекає на другий потік (детермінований тест гонки).

    Підставляється замість `_capacity`. У `try_acquire` порівняння `in_use` з ємністю — це
    «перевірка»; якщо між перевіркою й записом немає замка, обидва потоки спершу проходять
    перевірку, зустрічаються на `Barrier(2)`, а потім обидва беруть слот. З чинним замком
    другий потік чекає на замок, бар'єр першого розривається за таймаутом (він лише
    синхронізує потоки; логіка реального часу не читає), а другий бачить уже зайнятий слот.
    """

    barrier: threading.Barrier

    def _meet(self) -> None:
        try:
            self.barrier.wait()
        except threading.BrokenBarrierError:
            pass

    def __lt__(self, other: object) -> bool:
        self._meet()
        return int.__lt__(self, other)  # type: ignore[arg-type]

    def __le__(self, other: object) -> bool:
        self._meet()
        return int.__le__(self, other)  # type: ignore[arg-type]

    def __gt__(self, other: object) -> bool:
        self._meet()
        return int.__gt__(self, other)  # type: ignore[arg-type]

    def __ge__(self, other: object) -> bool:
        self._meet()
        return int.__ge__(self, other)  # type: ignore[arg-type]


def test_check_and_take_are_atomic_deterministic_race() -> None:
    hs = slots(1)
    assert hs._capacity == 1  # тест зав'язаний на приватне поле: зламається гучно, не мовчки
    barrier = threading.Barrier(2, timeout=0.3)
    cls = type("RInt", (_RendezvousInt,), {"barrier": barrier})
    hs._capacity = cls(1)
    got: list[Lease | None] = []
    errors: list[BaseException] = []
    out = threading.Lock()

    def worker() -> None:
        try:
            lease = hs.try_acquire()
            with out:
                got.append(lease)
        except BaseException as exc:  # noqa: BLE001
            with out:
                errors.append(exc)

    ts = [threading.Thread(target=worker, daemon=True) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=10)
    assert not any(t.is_alive() for t in ts)
    assert not errors
    assert len(got) == 2
    assert sum(x is not None for x in got) == 1  # а не 2: перевірка й запис — атомарні
    assert hs.in_use == 1


class _CountingLock:
    """Справжній `Lock` із лічильником входів: один замок, одне утримання на операцію."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.acquisitions = 0

    def acquire(self, *args: object, **kwargs: object) -> bool:
        ok = self._lock.acquire(*args, **kwargs)  # type: ignore[arg-type]
        if ok:
            self.acquisitions += 1
        return ok

    def release(self) -> None:
        self._lock.release()

    def locked(self) -> bool:
        return self._lock.locked()

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *exc: object) -> None:
        self.release()


def test_every_operation_holds_the_single_lock_exactly_once() -> None:
    # «розщеплений» замок (перевірка під одним утриманням, запис під іншим) — це гонка;
    # «без замка» (читання in_use, release) — теж. Кожна операція = рівно одне утримання.
    hs = slots(1)
    lock = _CountingLock()
    hs._lock = lock  # type: ignore[assignment]

    def count(fn):
        before = lock.acquisitions
        result = fn()
        return result, lock.acquisitions - before

    lease, n = count(hs.try_acquire)
    assert lease is not None and n == 1
    none, n = count(hs.try_acquire)
    assert none is None and n == 1
    value, n = count(lambda: hs.in_use)
    assert value == 1 and n == 1
    _, n = count(lease.release)
    assert n == 1
    _, n = count(lease.release)  # повторне теж перевіряє прапорець під замком
    assert n == 1
    assert not lock.locked()


def test_lock_is_released_when_exit_runs_with_exception() -> None:
    hs = slots(1)
    lease = hs.try_acquire()
    assert lease is not None
    with pytest.raises(KeyboardInterrupt):
        with lease:
            raise KeyboardInterrupt
    assert not hs._lock.locked()
    assert hs.in_use == 0


# --------------------------------------------------------------------------- T-168: UserGate

OK = GateDecision("ok", 0)
IN_PROGRESS = GateDecision("in_progress", 0)


def gate(checks: int = 5, window: float = 600, start: float = 0.0):
    clock = ManualClock(start)
    return UserGate(checks, window, clock), clock


# --- рядок T-168


def test_second_enter_while_active_is_in_progress() -> None:
    g, _ = gate()
    assert g.try_enter("u", counts=True) == OK
    assert g.try_enter("u", counts=True) == IN_PROGRESS
    assert g.try_enter("u", counts=False) == IN_PROGRESS  # правило «один одночасний» діє й для counts=False


def test_leave_allows_next() -> None:
    g, _ = gate()
    assert g.try_enter("u", counts=True) == OK
    g.leave("u")
    assert g.try_enter("u", counts=True) == OK
    assert g.try_enter("u", counts=True) == IN_PROGRESS


def test_window_counts_only_when_counts_true() -> None:
    g, _ = gate(checks=2, window=600)
    for _ in range(10):  # кешовані токени/невалідні адреси: вікно не споживається
        assert g.try_enter("u", counts=False) == OK
        g.leave("u")
    for _ in range(2):  # тож обидва «важкі» ще проходять
        assert g.try_enter("u", counts=True) == OK
        g.leave("u")
    assert g.try_enter("u", counts=True) == GateDecision("rate_limited", 600)
    # вікно вичерпане, але counts=False все одно проходить (кеш віддається будь-кому)
    assert g.try_enter("u", counts=False) == OK


def test_rate_limited_has_honest_retry_after() -> None:
    g, clock = gate(checks=2, window=60)
    assert g.try_enter("u", counts=True) == OK  # t=0
    g.leave("u")
    clock.now = 5
    assert g.try_enter("u", counts=True) == OK
    g.leave("u")
    clock.now = 10
    denied = g.try_enter("u", counts=True)
    assert denied == GateDecision("rate_limited", 50)  # 0 + 60 - 10
    clock.now = 10 + 50 - 1  # ще рано (і відмова вікно не зсунула)
    assert g.try_enter("u", counts=True) == GateDecision("rate_limited", 1)
    clock.now = 10 + 50  # рівно через retry_after — проходить
    assert g.try_enter("u", counts=True) == OK


def test_in_progress_does_not_consume_window() -> None:
    g, _ = gate(checks=2, window=600)
    assert g.try_enter("u", counts=True) == OK  # 1 з 2
    for _ in range(10):
        assert g.try_enter("u", counts=True) == IN_PROGRESS  # не їсть вікно
    g.leave("u")
    assert g.try_enter("u", counts=True) == OK  # 2 з 2: якщо б in_progress їв вікно -> rate_limited
    g.leave("u")
    assert g.try_enter("u", counts=True).kind == "rate_limited"


def test_in_progress_takes_precedence_over_rate_limited() -> None:
    g, _ = gate(checks=1, window=600)
    assert g.try_enter("u", counts=True) == OK
    # вікно вичерпане І користувач активний: порядок in_progress -> rate_limited -> ok
    assert g.try_enter("u", counts=True) == IN_PROGRESS
    g.leave("u")
    assert g.try_enter("u", counts=True) == GateDecision("rate_limited", 600)


def test_concurrent_enter_same_user_admits_one() -> None:
    for counts in (True, False):
        g, _ = gate(checks=100, window=600)
        threads = 20
        barrier = threading.Barrier(threads, timeout=10)
        results: list[GateDecision] = []
        out = threading.Lock()

        def worker() -> None:
            barrier.wait()
            d = g.try_enter("u", counts=counts)
            with out:
                results.append(d)

        ts = [threading.Thread(target=worker, daemon=True) for _ in range(threads)]
        old = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)
        try:
            for t in ts:
                t.start()
            for t in ts:
                t.join(timeout=10)
        finally:
            sys.setswitchinterval(old)
        assert not any(t.is_alive() for t in ts)
        assert len(results) == threads
        assert sum(d.kind == "ok" for d in results) == 1
        assert sum(d.kind == "in_progress" for d in results) == threads - 1


# --- межові випадки


def test_users_are_independent() -> None:
    g, _ = gate(checks=1, window=600)
    assert g.try_enter("a", counts=True) == OK
    assert g.try_enter("b", counts=True) == OK  # a активний, але це не заважає b
    assert g.try_enter("a", counts=True) == IN_PROGRESS
    g.leave("a")
    assert g.try_enter("a", counts=True).kind == "rate_limited"  # вікно a вичерпане
    assert g.try_enter("b", counts=True) == IN_PROGRESS  # b своє ще тримає
    g.leave("b")
    g.leave("a")  # ні-оп
    assert g.try_enter("c", counts=True) == OK  # c чистий


def test_leave_of_one_user_does_not_release_another() -> None:
    g, _ = gate()
    assert g.try_enter("a", counts=False) == OK
    assert g.try_enter("b", counts=False) == OK
    g.leave("a")
    assert g.try_enter("b", counts=False) == IN_PROGRESS
    assert g.try_enter("a", counts=False) == OK


def test_leave_of_inactive_user_is_noop() -> None:
    g, _ = gate()
    g.leave("never-seen")  # не кидає
    assert g.try_enter("never-seen", counts=True) == OK
    g.leave("other")  # чужий leave не чіпає активного
    assert g.try_enter("never-seen", counts=True) == IN_PROGRESS


def test_leave_twice_is_idempotent() -> None:
    g, _ = gate(checks=5)
    assert g.try_enter("u", counts=True) == OK
    g.leave("u")
    g.leave("u")  # нічого не ламає
    assert g.try_enter("u", counts=True) == OK
    assert g.try_enter("u", counts=True) == IN_PROGRESS
    assert g._active == {"u"}  # type: ignore[attr-defined]


def test_leave_does_not_refund_the_window() -> None:
    g, _ = gate(checks=1, window=600)
    assert g.try_enter("u", counts=True) == OK
    g.leave("u")
    assert g.try_enter("u", counts=True) == GateDecision("rate_limited", 600)


def test_rate_limited_does_not_mark_user_active() -> None:
    g, clock = gate(checks=1, window=60)
    assert g.try_enter("u", counts=True) == OK
    g.leave("u")
    assert g.try_enter("u", counts=True).kind == "rate_limited"
    clock.now = 60
    assert g.try_enter("u", counts=True) == OK  # якби відмова лишила «активним» -> in_progress
    assert g.try_enter("u", counts=True) == IN_PROGRESS


def test_rate_limited_denials_do_not_extend_block() -> None:
    g, clock = gate(checks=1, window=60)
    assert g.try_enter("u", counts=True) == OK
    g.leave("u")
    for t in (10, 20, 30, 59):
        clock.now = t
        assert g.try_enter("u", counts=True).kind == "rate_limited"
    clock.now = 60
    assert g.try_enter("u", counts=True) == OK


def test_window_frees_after_it_slides_and_user_is_ok_again() -> None:
    g, clock = gate(checks=2, window=100)
    for _ in range(2):
        assert g.try_enter("u", counts=True) == OK
        g.leave("u")
    assert g.try_enter("u", counts=True).kind == "rate_limited"
    clock.now = 100
    assert g.try_enter("u", counts=True) == OK


def test_int_and_str_keys_are_distinct() -> None:
    g, _ = gate()
    assert g.try_enter(1, counts=True) == OK
    assert g.try_enter("1", counts=True) == OK  # не зливаються
    assert g.try_enter(1, counts=True) == IN_PROGRESS
    assert g.try_enter("1", counts=True) == IN_PROGRESS
    g.leave(1)
    assert g.try_enter("1", counts=True) == IN_PROGRESS
    assert g.try_enter(1, counts=True) == OK


def test_negative_and_large_int_keys() -> None:
    g, _ = gate()
    for key in (-1001234567890, 0, 2**63):  # chat.id груп бувають від'ємними
        assert g.try_enter(key, counts=True) == OK
        assert g.try_enter(key, counts=True) == IN_PROGRESS
    g.leave(-1001234567890)
    assert g.try_enter(0, counts=True) == IN_PROGRESS


def test_counts_must_be_a_keyword_bool() -> None:
    g, _ = gate()
    with pytest.raises(TypeError):
        g.try_enter("u", True)  # type: ignore[misc]
    for bad in (1, 0, None, "yes"):
        with pytest.raises(TypeError):
            g.try_enter("u", counts=bad)  # type: ignore[arg-type]
    assert g.try_enter("u", counts=True) == OK  # відхилений виклик нічого не зачепив
    assert not g._lock.locked()  # type: ignore[attr-defined]


def test_unhashable_user_raises_type_error_and_leaves_state_clean() -> None:
    g, _ = gate()
    with pytest.raises(TypeError):
        g.try_enter(["u"], counts=True)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        g.leave(["u"])  # type: ignore[arg-type]
    assert not g._lock.locked()  # type: ignore[attr-defined]
    assert g._active == set()  # type: ignore[attr-defined]
    assert g.try_enter("u", counts=True) == OK


def test_clock_failure_does_not_mark_user_active_nor_hold_lock() -> None:
    state = {"fail": True}

    def clock() -> float:
        if state["fail"]:
            raise RuntimeError("clock failed")
        return 0.0

    g = UserGate(1, 60, clock)
    with pytest.raises(RuntimeError):
        g.try_enter("u", counts=True)
    assert not g._lock.locked()  # type: ignore[attr-defined]
    state["fail"] = False
    assert g.try_enter("u", counts=True) == OK  # не лишився «активним» після збою


def test_counts_false_never_reads_clock_or_window() -> None:
    def clock() -> float:
        raise AssertionError("counts=False must not touch the window")

    g = UserGate(1, 60, clock)
    assert g.try_enter("u", counts=False) == OK
    assert g.try_enter("u", counts=False) == IN_PROGRESS
    g.leave("u")


def test_clock_regress_never_blocks_longer_than_window() -> None:
    g, clock = gate(checks=1, window=60, start=1000.0)
    assert g.try_enter("u", counts=True) == OK
    g.leave("u")
    clock.now = 100.0  # годинник стрибнув назад
    denied = g.try_enter("u", counts=True)
    assert denied.kind == "rate_limited" and 1 <= denied.retry_after_seconds <= 60
    clock.now += denied.retry_after_seconds
    assert g.try_enter("u", counts=True) == OK


def test_active_set_is_bounded_by_live_users() -> None:
    g, _ = gate(checks=1000, window=600)
    for i in range(500):
        assert g.try_enter(i, counts=True) == OK
        g.leave(i)
    assert g._active == set()  # type: ignore[attr-defined]


def test_checks_one_window_one() -> None:
    g, clock = gate(checks=1, window=1)
    assert g.try_enter("u", counts=True) == OK
    g.leave("u")
    assert g.try_enter("u", counts=True) == GateDecision("rate_limited", 1)
    clock.now = 1
    assert g.try_enter("u", counts=True) == OK


def test_fractional_window() -> None:
    g, clock = gate(checks=1, window=0.5)
    assert g.try_enter("u", counts=True) == OK
    g.leave("u")
    assert g.try_enter("u", counts=True) == GateDecision("rate_limited", 1)  # ceil(0.5), не 0
    clock.now = 0.5
    assert g.try_enter("u", counts=True) == OK


@pytest.mark.parametrize("checks", [0, -1, True, 1.5, "5", None])
def test_invalid_checks_rejected(checks: object) -> None:
    with pytest.raises(ValueError):
        UserGate(checks, 60)  # type: ignore[arg-type]


@pytest.mark.parametrize("window", [0, -1, math.inf, math.nan, "60", None])
def test_invalid_window_rejected(window: object) -> None:
    with pytest.raises(ValueError):
        UserGate(5, window)  # type: ignore[arg-type]


def test_non_callable_clock_rejected() -> None:
    with pytest.raises(ValueError):
        UserGate(5, 60, 0)  # type: ignore[arg-type]


def test_default_clock_is_monotonic_and_works() -> None:
    g = UserGate(2, 60)
    assert g.try_enter("u", counts=True) == OK
    assert g.try_enter("u", counts=True) == IN_PROGRESS


def test_gates_are_independent_instances() -> None:
    g1, _ = gate(checks=1)
    g2, _ = gate(checks=1)
    assert g1.try_enter("u", counts=True) == OK
    assert g2.try_enter("u", counts=True) == OK


def test_random_walk_keeps_invariants_against_a_reference_model() -> None:
    rng = random.Random(168)
    window, checks = 100, 3
    g, clock = gate(checks=checks, window=window)
    active: set[int] = set()
    stamps: dict[int, list[float]] = {}
    for _ in range(3000):
        clock.now += rng.choice([0, 0, 1, 5, 40, 101])
        user = rng.randrange(4)
        if rng.random() < 0.4:
            g.leave(user)
            active.discard(user)
            continue
        counts = rng.random() < 0.7
        got = g.try_enter(user, counts=counts)
        if user in active:
            assert got == IN_PROGRESS
            continue
        live = [s for s in stamps.get(user, []) if s > clock.now - window]
        stamps[user] = live
        if counts and len(live) >= checks:
            assert got.kind == "rate_limited"
            assert got.retry_after_seconds == math.ceil(live[0] + window - clock.now)
            continue
        assert got == OK
        active.add(user)
        if counts:
            live.append(clock.now)


# --- GateDecision


def test_gate_decision_is_frozen_value_object() -> None:
    d = GateDecision("rate_limited", 5)
    assert d == GateDecision("rate_limited", 5)
    assert d != GateDecision("rate_limited", 6)
    assert d != GateDecision("in_progress", 0)
    assert hash(d) == hash(GateDecision("rate_limited", 5))
    with pytest.raises(dataclasses.FrozenInstanceError):
        d.kind = "ok"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        d.retry_after_seconds = 1  # type: ignore[misc]


@pytest.mark.parametrize(
    "kind,retry",
    [
        ("ok", 1),
        ("in_progress", 3),
        ("rate_limited", 0),
        ("rate_limited", -1),
        ("busy", 0),
        ("OK", 0),
        ("", 0),
        (None, 0),
        ("ok", True),
        ("ok", 0.0),
        ("rate_limited", 5.0),
        ("rate_limited", True),  # bool — не int-кількість секунд (True == 1)
    ],
)
def test_gate_decision_rejects_inconsistent_values(kind: object, retry: object) -> None:
    with pytest.raises(ValueError):
        GateDecision(kind, retry)  # type: ignore[arg-type]


# --- детермінований тест гонки (Barrier-рандеву, як T-162/T-163)


class _RendezvousUser:
    """Ключ користувача, чий N-й `__hash__` чекає на другий потік.

    Кожен потік має власний екземпляр (лічильник викликів — на екземпляр), усі екземпляри
    рівні й з однаковим хешем. У `try_enter` перший хеш — `user in self._active` (перевірка
    активності). Якщо активність перевіряється й позначається НЕ під одним утриманням замка
    (замка немає або він розщеплений на «перевірку» й «запис»), обидва потоки проходять
    перевірку й зустрічаються на `Barrier(2)` у наступному хешуванні (вікно/запис) -> обидва
    `ok`. З чинним замком другий потік чекає на замок, бар'єр першого розривається за
    таймаутом (він лише синхронізує потоки; логіка реального часу не читає), а другий бачить
    уже позначену активність -> `in_progress`.
    """

    def __init__(self, barrier: threading.Barrier, wait_on: int) -> None:
        self._barrier = barrier
        self._wait_on = wait_on
        self._calls = 0

    def __hash__(self) -> int:
        self._calls += 1
        if self._calls == self._wait_on:
            try:
                self._barrier.wait()
            except threading.BrokenBarrierError:
                pass
        return 777

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _RendezvousUser)


@pytest.mark.parametrize("counts,wait_on", [(False, 2), (True, 2), (True, 3)])
def test_check_and_mark_active_are_atomic_deterministic_race(counts: bool, wait_on: int) -> None:
    # counts=False: у try_enter два хешування ключа (перевірка, запис); counts=True — чотири
    g, _ = gate(checks=100, window=600)  # велике вікно: гонку не маскує rate_limited
    barrier = threading.Barrier(2, timeout=0.3)
    results: list[GateDecision] = []
    errors: list[BaseException] = []
    out = threading.Lock()

    def worker() -> None:
        try:
            d = g.try_enter(_RendezvousUser(barrier, wait_on), counts=counts)
            with out:
                results.append(d)
        except BaseException as exc:  # noqa: BLE001
            with out:
                errors.append(exc)

    ts = [threading.Thread(target=worker, daemon=True) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=10)
    assert not any(t.is_alive() for t in ts)
    assert not errors
    assert len(results) == 2
    assert sorted(d.kind for d in results) == ["in_progress", "ok"]


class _RendezvousLock:
    """Справжній `Lock`, після ПЕРШОГО звільнення кожним потоком чекає на другий потік.

    Ловить «розщеплений» замок: перевірка активності під одним утриманням, запис — під іншим.
    Хеш-рандеву (`_RendezvousUser`) тут безсилий: воно спрацьовує всередині утримання, тож
    другий потік просто блокується на замку. Рандеву ПОЗА замком — між утриманнями — змушує
    обох пройти «перевірку» до того, як хтось «запише». З чинним єдиним замком потік, що вийшов,
    уже все записав, тож другий бачить `in_progress`, а бар'єр збирається одразу (без простою).
    """

    def __init__(self, barrier: threading.Barrier) -> None:
        self._lock = threading.Lock()
        self._barrier = barrier
        self._seen: set[int] = set()
        self._guard = threading.Lock()

    def acquire(self, *args: object, **kwargs: object) -> bool:
        return self._lock.acquire(*args, **kwargs)  # type: ignore[arg-type]

    def release(self) -> None:
        self._lock.release()
        me = threading.get_ident()
        with self._guard:
            first = me not in self._seen
            self._seen.add(me)
        if first:
            try:
                self._barrier.wait()
            except threading.BrokenBarrierError:
                pass

    def locked(self) -> bool:
        return self._lock.locked()

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *exc: object) -> None:
        self.release()


@pytest.mark.parametrize("counts", [False, True])
def test_check_and_mark_active_not_split_across_lock_holds_deterministic_race(counts: bool) -> None:
    g, _ = gate(checks=100, window=600)
    g._lock = _RendezvousLock(threading.Barrier(2, timeout=0.3))  # type: ignore[attr-defined,assignment]
    results: list[GateDecision] = []
    out = threading.Lock()

    def worker() -> None:
        d = g.try_enter("u", counts=counts)
        with out:
            results.append(d)

    ts = [threading.Thread(target=worker, daemon=True) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=10)
    assert not any(t.is_alive() for t in ts)
    assert sorted(d.kind for d in results) == ["in_progress", "ok"]


def test_window_hit_and_activity_mark_are_one_critical_section() -> None:
    # вікно (hit) відбувається ПІД замком гейта: інакше між hit і позначкою активності
    # можна вклинитись — і «один одночасний» перестав би бути гарантією
    g, _ = gate(checks=5)
    seen: list[bool] = []
    real = g._window.hit  # type: ignore[attr-defined]

    def spy(key):  # type: ignore[no-untyped-def]
        seen.append(g._lock.locked())  # type: ignore[attr-defined]
        return real(key)

    g._window.hit = spy  # type: ignore[attr-defined,method-assign]
    assert g.try_enter("u", counts=True) == OK
    assert seen == [True]


def test_every_gate_operation_holds_the_single_lock_exactly_once() -> None:
    g, _ = gate(checks=5)
    lock = _CountingLock()
    g._lock = lock  # type: ignore[attr-defined,assignment]

    def count(fn):  # type: ignore[no-untyped-def]
        before = lock.acquisitions
        result = fn()
        return result, lock.acquisitions - before

    got, n = count(lambda: g.try_enter("u", counts=True))
    assert got == OK and n == 1
    got, n = count(lambda: g.try_enter("u", counts=True))
    assert got == IN_PROGRESS and n == 1
    got, n = count(lambda: g.try_enter("v", counts=False))
    assert got == OK and n == 1
    _, n = count(lambda: g.leave("u"))
    assert n == 1
    _, n = count(lambda: g.leave("u"))  # повторний/зайвий leave теж під замком
    assert n == 1
    assert not lock.locked()


def test_concurrent_churn_one_active_per_user_and_window_never_exceeded() -> None:
    g, _ = gate(checks=7, window=10_000)
    users = ["a", "b", "c"]
    threads = 12
    barrier = threading.Barrier(threads, timeout=10)
    inside = {u: 0 for u in users}
    peak = {u: 0 for u in users}
    oks = {u: 0 for u in users}
    guard = threading.Lock()
    errors: list[BaseException] = []

    def worker(i: int) -> None:
        user = users[i % len(users)]
        try:
            barrier.wait()
            while True:
                d = g.try_enter(user, counts=True)
                if d.kind == "rate_limited":
                    return
                if d.kind == "ok":
                    with guard:
                        inside[user] += 1
                        peak[user] = max(peak[user], inside[user])
                        oks[user] += 1
                    with guard:
                        inside[user] -= 1
                    g.leave(user)
        except BaseException as exc:  # noqa: BLE001
            with guard:
                errors.append(exc)

    ts = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(threads)]
    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=30)
    finally:
        sys.setswitchinterval(old)
    assert not any(t.is_alive() for t in ts)
    assert not errors
    assert all(p == 1 for p in peak.values())  # одночасно не більше одного на користувача
    assert oks == {"a": 7, "b": 7, "c": 7}  # рівно checks_per_window допусків на користувача
    assert g._active == set()  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- T-168: build_protection

# Явні числа config/protection.yaml v1 (golden, принцип III): зміна конфігу без зміни тесту — червоне.
V1 = dict(version=1, api_requests_per_window=30, api_window_seconds=60, max_concurrent_analyses=3,
          busy_retry_after_seconds=60, trusted_proxies=("127.0.0.1", "::1"),
          bot_checks_per_window=5, bot_window_seconds=600, digest="d")

# Усі числа різні: переплутане поле конфігу видно (у v1 api.window == busy_retry == 60).
DISTINCT = dict(version=2, api_requests_per_window=7, api_window_seconds=11, max_concurrent_analyses=2,
                busy_retry_after_seconds=13, trusted_proxies=("10.0.0.0/8",),
                bot_checks_per_window=4, bot_window_seconds=17, digest="d")

REPO = Path(__file__).resolve().parents[1]


def test_protection_fields_and_frozen() -> None:
    p = build_protection(ProtectionConfig(**V1), ManualClock())
    assert [f.name for f in dataclasses.fields(Protection)] == ["api", "slots", "users", "trusted_proxies"]
    assert isinstance(p.api, SlidingWindowLimiter)
    assert isinstance(p.slots, HeavySlots)
    assert isinstance(p.users, UserGate)
    assert isinstance(p.trusted_proxies, tuple)
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.api = p.api  # type: ignore[misc]


def test_build_protection_maps_every_config_field() -> None:
    clock = ManualClock()
    p = build_protection(ProtectionConfig(**V1), clock)
    # api: 30 запитів / 60 с
    for _ in range(30):
        assert p.api.hit("ip") == Decision(True, 0)
    assert p.api.hit("ip") == Decision(False, 60)
    # slots: 3 одночасні, busy Retry-After 60
    assert p.slots.capacity == 3 and p.slots.retry_after_seconds == 60
    leases = [p.slots.try_acquire() for _ in range(3)]
    assert all(leases) and p.slots.try_acquire() is None
    # users: 5 перевірок / 600 с
    for _ in range(5):
        assert p.users.try_enter("u", counts=True) == OK
        p.users.leave("u")
    assert p.users.try_enter("u", counts=True) == GateDecision("rate_limited", 600)
    # trusted: 127.0.0.1 і ::1 як мережі
    assert p.trusted_proxies == (ipaddress.ip_network("127.0.0.1/32"), ipaddress.ip_network("::1/128"))


def test_build_protection_does_not_swap_similar_fields() -> None:
    clock = ManualClock()
    p = build_protection(ProtectionConfig(**DISTINCT), clock)
    assert (p.slots.capacity, p.slots.retry_after_seconds) == (2, 13)
    for _ in range(7):
        assert p.api.hit("ip").allowed
    assert p.api.hit("ip") == Decision(False, 11)
    for _ in range(4):
        assert p.users.try_enter("u", counts=True) == OK
        p.users.leave("u")
    assert p.users.try_enter("u", counts=True) == GateDecision("rate_limited", 17)
    assert p.trusted_proxies == (ipaddress.ip_network("10.0.0.0/8"),)


def test_build_protection_api_and_users_are_separate_windows() -> None:
    p = build_protection(ProtectionConfig(**V1), ManualClock())
    for _ in range(30):
        p.api.hit("1")
    assert p.api.hit("1").allowed is False
    assert p.users.try_enter("1", counts=True) == OK  # ключ "1" в api не впливає на users
    assert p.slots.in_use == 0


def test_build_protection_shares_one_injected_clock() -> None:
    clock = ManualClock()
    p = build_protection(ProtectionConfig(**DISTINCT), clock)
    for _ in range(7):
        p.api.hit("ip")
    for _ in range(4):
        p.users.try_enter("u", counts=True)
        p.users.leave("u")
    assert not p.api.hit("ip").allowed
    assert p.users.try_enter("u", counts=True).kind == "rate_limited"
    clock.advance(11)
    assert p.api.hit("ip").allowed  # api — своє вікно 11 с
    assert p.users.try_enter("u", counts=True).kind == "rate_limited"  # users ще 6 с
    clock.advance(6)
    assert p.users.try_enter("u", counts=True) == OK


def test_build_protection_default_clock_is_monotonic() -> None:
    p = build_protection(ProtectionConfig(**V1))
    assert p.api._clock is time.monotonic  # type: ignore[attr-defined]
    assert p.users._window._clock is time.monotonic  # type: ignore[attr-defined]


def test_build_protection_trusted_proxy_forms() -> None:
    def trusted(*items: str):  # type: ignore[no-untyped-def]
        return build_protection(ProtectionConfig(**{**V1, "trusted_proxies": items}),
                                ManualClock()).trusted_proxies

    assert trusted() == ()  # порожній список дозволено
    assert trusted("10.0.0.0/8", "fd00::/8") == (
        ipaddress.ip_network("10.0.0.0/8"), ipaddress.ip_network("fd00::/8"))
    assert trusted("10.1.2.3/8") == (ipaddress.ip_network("10.0.0.0/8"),)  # strict=False, як у конфігу
    assert trusted("192.168.1.5") == (ipaddress.ip_network("192.168.1.5/32"),)  # порядок збережено
    assert [str(n) for n in trusted("::1", "127.0.0.1")] == ["::1/128", "127.0.0.1/32"]


def test_build_protection_from_real_protection_yaml_v1_golden() -> None:
    cfg = load_protection_config(REPO / "config" / "protection.yaml")
    expected = {k: v for k, v in V1.items() if k != "digest"}
    assert {k: getattr(cfg, k) for k in expected} == expected  # змінили YAML — оновлюйте версію й журнал
    p = build_protection(cfg, ManualClock())
    assert p.slots.capacity == 3 and p.slots.retry_after_seconds == 60
    assert p.trusted_proxies == (ipaddress.ip_network("127.0.0.1/32"), ipaddress.ip_network("::1/128"))


def test_build_protection_instances_are_independent() -> None:
    a = build_protection(ProtectionConfig(**V1), ManualClock())
    b = build_protection(ProtectionConfig(**V1), ManualClock())
    assert a.users is not b.users and a.slots is not b.slots and a.api is not b.api
    assert a.users.try_enter("u", counts=True) == OK
    assert b.users.try_enter("u", counts=True) == OK
