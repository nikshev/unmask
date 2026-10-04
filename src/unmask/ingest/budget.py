# impl: FR-001-16
"""Годинник і бюджет часу збору (FR-001-16, research R-11).

`Clock` — єдиний спосіб, у який код збору дізнається про час: так бюджет тестується без реального
очікування. `SystemClock` — для роботи, `FakeClock` — для тестів.

`Deadline(clock, seconds)` — бюджет `time_budget_seconds` (версіонований параметр конфігу, принцип III);
колектор створює його на початку `collect` і передає в кожен виклик джерела. Ядро перед кожним
зверненням викликає `ensure_time` (сплив → `BudgetExhausted`, звернення не робиться), а саме звернення
обгортає в `deadline_timeouts` (`RpcTimeout` через дедлайн або `RpcBudgetTimeout` → `BudgetExhausted`). `BudgetExhausted`
перетворюють на чесну неповноту `buyers` і `funding` (`budget_exhausted`).

Залежить від: `rpc.protocol` (`Deadline`-протокол, `RpcTimeout`, `RpcBudgetTimeout`).
"""

from __future__ import annotations

import math
import time
from contextlib import contextmanager
from typing import Iterator, Protocol

from unmask.ingest.rpc.protocol import Deadline as DeadlineLike
from unmask.ingest.rpc.protocol import RpcBudgetTimeout, RpcTimeout


class Clock(Protocol):
    def monotonic(self) -> float:
        """Монотонний час у секундах (для вимірювання тривалості)."""
        ...

    def wall(self) -> int:
        """Поточний Unix-час у секундах (для `analyzed_at`)."""
        ...


class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def wall(self) -> int:
        return int(time.time())


class FakeClock:
    """Керований годинник: стоїть на місці, доки його не просунуть.

    `advance_per_call` — на скільки секунд `tick()` просуває час; фікстурне джерело
    викликає `tick()` на кожен звернення, імітуючи тривалість мережевого запиту.
    """

    def __init__(self, start: float = 0.0, *, advance_per_call: float = 0.0, wall_start: int = 0) -> None:
        if advance_per_call < 0:
            raise ValueError("advance_per_call must be >= 0")
        self._now = float(start)
        self._wall_offset = int(wall_start) - int(start)
        self.advance_per_call = float(advance_per_call)

    def monotonic(self) -> float:
        return self._now

    def wall(self) -> int:
        return int(self._now) + self._wall_offset

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("time cannot go backwards")
        self._now += seconds

    def tick(self) -> None:
        self.advance(self.advance_per_call)


def _seconds(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name}: expected a number of seconds, got {value!r}")
    value = float(value)
    if math.isnan(value):
        raise ValueError(f"{name}: NaN is not a duration")
    return value


class Deadline:
    """Бюджет часу збору (FR-001-16, contracts/rpc-source.md «Deadline»).

    Відлік — від створення (колектор створює дедлайн на початку `collect`), за `clock.monotonic()`.
    - `remaining()` — скільки лишилось, ніколи не від'ємне (`inf` для нескінченного бюджету);
    - `expired()` — час дійшов до межі: **рівно на межі бюджет уже вичерпано** (залишок 0 — на запит
      часу немає), тож перевірка «перед зверненням» не пропускає звернення, що почалося б о межі;
    - `request_timeout(cap)` — таймаут одного запиту: `min(remaining(), cap)`, `cap` —
      `rpc.request_timeout_seconds`. Адаптер, що обрізає запит цим значенням, піднімає `RpcTimeout`
      саме тоді, коли винен дедлайн, — і на момент обробки винятку `expired()` уже істинне (так ядро
      розрізняє `budget_exhausted` і звичайний `timeout`, див. `deadline_timeouts`).
    `seconds` — `>= 0` (0 — вичерпано одразу) або `inf`; конфіг сам вимагає `> 0`.
    """

    def __init__(self, clock: Clock, seconds: float) -> None:
        seconds = _seconds("Deadline.seconds", seconds)
        if seconds < 0:
            raise ValueError(f"Deadline.seconds: {seconds!r} must be >= 0")
        self._clock = clock
        self._end = clock.monotonic() + seconds

    def remaining(self) -> float:
        return max(0.0, self._end - self._clock.monotonic())

    def expired(self) -> bool:
        return self._clock.monotonic() >= self._end

    def request_timeout(self, cap: float) -> float:
        cap = _seconds("request_timeout.cap", cap)
        if not cap > 0:
            raise ValueError(f"request_timeout.cap: {cap!r} must be > 0")
        return min(self.remaining(), cap)


class BudgetExhausted(Exception):
    """Внутрішній сигнал ядра: бюджет вичерпано — звернення не зроблено або його таймаут спричинив дедлайн.

    Ловиться в `buyers` (перелічення → `complete=false, reason=budget_exhausted`) і в `funding`
    (вершина → `MissingHistory(reason=budget_exhausted)`); з `collect` не виходить. `detail` — що саме
    не встигли (метод, адреса/підпис, курсор).
    """

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def ensure_time(deadline: DeadlineLike, what: str) -> None:
    """Перевірка ПЕРЕД кожним зверненням до джерела: після спливу звернень немає."""
    if deadline.expired():
        raise BudgetExhausted(f"{what}: time budget exhausted before the call")


@contextmanager
def deadline_timeouts(deadline: DeadlineLike, what: str) -> Iterator[None]:
    """`RpcTimeout` усередині звернення, спричинений дедлайном, → `BudgetExhausted`; звичайний — далі.

    `RpcBudgetTimeout` (T-051) → `BudgetExhausted` БЕЗУМОВНО, навіть коли `deadline.expired()` ще хибне: це
    вже рішення адаптера «звернення не вміститься в бюджет» (пейсер: очікування токенів ≥ `remaining()`;
    `expired()` чи `request_timeout() <= 0` перед запитом). Без цього та сама ситуація давала `timeout` або
    `budget_exhausted` залежно від того, чи встиг годинник дійти до межі (знахідка живого прогону).

    Звичайний `RpcTimeout` — як і раніше: адаптер обрізає таймаут запиту залишком (`request_timeout(cap) = min(remaining, cap)`),
    тож запит, обірваний через дедлайн, закінчується не раніше за межу бюджету — і на момент обробки
    винятку `deadline.expired()` істинне. Таймаут, що настав раніше межі (`cap < remaining`), — звичайний
    `timeout`: бюджет ще є, збір триває. Таймаут, що збігся з межею, — `budget_exhausted` (далі звертатись
    однаково не можна). Інші винятки джерела (`RpcRateLimited`, `RpcUnavailable`) — справжня причина
    й лишаються собою навіть після спливу; наступне звернення зупинить `ensure_time`.
    """
    try:
        yield
    except RpcTimeout as exc:
        if isinstance(exc, RpcBudgetTimeout) or deadline.expired():
            raise BudgetExhausted(f"{what}: {exc}") from exc
        raise
