# impl: FR-001-16
"""Годинник збору. Мінімум для фікстурного джерела; `Deadline` додає T-015.

`Clock` — єдиний спосіб, у який код збору дізнається про час: так бюджет (FR-001-16)
тестується без реального очікування. `SystemClock` — для роботи, `FakeClock` — для тестів.
"""

from __future__ import annotations

import time
from typing import Protocol


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
