# verifies: FR-008-01, FR-008-14
"""Ковзне вікно запитів (T-162, фіча 008): `SlidingWindowLimiter` і `Decision`.

Годинник — локальний `ManualClock` (R-3): реального часу й `sleep` у тестах немає. Конкурентні
тести синхронізуються лише `threading.Barrier` / `join` з таймаутом.
"""

from __future__ import annotations

import dataclasses
import math
import random
import threading

import pytest

from unmask.delivery.limits import Decision, SlidingWindowLimiter


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
