# impl: FR-008-01, FR-008-02, FR-008-06, FR-008-07, FR-008-08, FR-008-14
"""Примітиви обмеження навантаження доставки (фіча 008; data-model.md, R-1, R-3).

Чисті, потокобезпечні, без I/O: стан лише в пам'яті процесу. Годинник інжектується
(`clock`, умовчання `time.monotonic`), тож тести керують часом без `sleep` (FR-008-14).

Тут — ковзне вікно запитів на ключ (адресу клієнта), слоти важких аналізів (`HeavySlots`/`Lease`),
ворота користувача бота (`UserGate`/`GateDecision`) і збирання всього з конфігу
(`Protection`/`build_protection`). Жодна межа не живе в цьому файлі: значення приходять із `config/protection.yaml` через
викликача (принцип III).
"""

from __future__ import annotations

import ipaddress
import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from ipaddress import IPv4Network, IPv6Network
from typing import TYPE_CHECKING, Callable, Hashable

if TYPE_CHECKING:  # лише для анотації: примітиви не залежать від завантажувача конфігу
    from unmask.delivery.config import ProtectionConfig

__all__ = [
    "Decision",
    "GateDecision",
    "HeavySlots",
    "Lease",
    "Protection",
    "SlidingWindowLimiter",
    "UserGate",
    "build_protection",
]


@dataclass(frozen=True)
class Decision:
    """Рішення ліміту: `retry_after_seconds` = 0, якщо `allowed`, інакше ціле ≥ 1."""

    allowed: bool
    retry_after_seconds: int

    def __post_init__(self) -> None:
        r = self.retry_after_seconds
        if type(self.allowed) is not bool or type(r) is not int:
            raise ValueError("Decision needs allowed: bool and retry_after_seconds: int")
        if self.allowed and r != 0:
            raise ValueError("an allowed Decision must have retry_after_seconds == 0")
        if not self.allowed and r < 1:
            raise ValueError("a denied Decision must have retry_after_seconds >= 1")


class SlidingWindowLimiter:
    """Не більше `limit` дозволених `hit` на ключ за будь-які `window_seconds` поспіль.

    Відхилені запити НЕ записуються: клієнт, що довбає, не продовжує власне блокування,
    а `retry_after` — чесна обіцянка (після неї наступний `hit` проходить). Годинник читається
    під замком, мітки з «майбутнього» (крок годинника назад) затискаються до `now`, тож
    блокування ніколи не довше за вікно, а `retry_after` ≤ `ceil(window)`.
    """

    def __init__(self, limit: int, window_seconds: float,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if type(limit) is not int or limit < 1:
            raise ValueError(f"limit must be an int >= 1, got {limit!r}")
        if (type(window_seconds) not in (int, float) or not math.isfinite(window_seconds)
                or window_seconds <= 0):
            raise ValueError(f"window_seconds must be a finite number > 0, got {window_seconds!r}")
        if not callable(clock):
            raise ValueError("clock must be callable")
        self._limit = limit
        self._window = window_seconds
        self._max_retry = math.ceil(window_seconds)
        self._clock = clock
        self._lock = threading.Lock()
        self._hits: dict[Hashable, deque[float]] = {}
        self._last_sweep: float | None = None

    def hit(self, key: Hashable) -> Decision:
        """Атомарно: порахувати запит ключа й повернути рішення."""
        with self._lock:
            now = self._clock()
            self._sweep(now)
            stamps = self._hits.get(key)
            if stamps is not None:
                self._normalize(stamps, now)
            if stamps is None or len(stamps) < self._limit:
                if stamps is None:
                    stamps = self._hits[key] = deque()
                stamps.append(now)
                return Decision(True, 0)
            return Decision(False, self._retry_after(stamps[0], now))

    def _normalize(self, stamps: deque[float], now: float) -> None:
        """Відкинути мітки `≤ now − window`; мітки `> now` затиснути до `now`."""
        cutoff = now - self._window
        while stamps and stamps[0] <= cutoff:
            stamps.popleft()
        if stamps and stamps[-1] > now:
            clamped = deque(min(s, now) for s in stamps)
            stamps.clear()
            stamps.extend(clamped)

    def _retry_after(self, oldest: float, now: float) -> int:
        remaining = oldest + self._window - now
        if not math.isfinite(remaining):  # oldest + window переповнилось
            return self._max_retry
        return min(max(math.ceil(remaining), 1), self._max_retry)

    def _sweep(self, now: float) -> None:
        """Видалити ключі без живих міток; не частіше разу на `window` секунд."""
        last = self._last_sweep
        if last is None:
            self._last_sweep = now
            return
        if last > now:  # годинник пішов назад: відлік прибирання — від спостережуваного часу
            last = self._last_sweep = now
        if now - last < self._window:
            return
        self._last_sweep = now
        for key in list(self._hits):
            stamps = self._hits[key]
            self._normalize(stamps, now)
            if not stamps:
                del self._hits[key]



class Lease:
    """Утримуваний слот `HeavySlots`: контекст-менеджер, `release()` ідемпотентний.

    Створюється лише `HeavySlots.try_acquire()` (прямий виклик конструктора лізи без слота
    порушив би лічильник). Одна ліза — один слот, а не лічильник входів: вихід із будь-якого
    `with` (чи явний `release()`) звільняє його, усі наступні звільнення — no-op. Прапорець
    «вже звільнено» читається й пишеться під тим самим замком, що й лічильник, тож два потоки,
    які звільняють одну лізу, зменшують його рівно раз і не забирають чужий слот.
    """

    __slots__ = ("_slots", "_released")

    def __init__(self, slots: "HeavySlots") -> None:
        self._slots = slots
        self._released = False

    @property
    def released(self) -> bool:
        with self._slots._lock:
            return self._released

    def release(self) -> None:
        """Звільнити слот; повторні виклики нічого не роблять."""
        slots = self._slots
        with slots._lock:
            if self._released:
                return
            self._released = True
            slots._in_use -= 1

    def __enter__(self) -> "Lease":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        # `finally`-семантика для будь-якого виключення, включно з BaseException
        # (KeyboardInterrupt, SystemExit): виняток не ковтається (повертаємо None).
        self.release()


class HeavySlots:
    """Не більше `capacity` одночасних важких аналізів; `try_acquire` не чекає (FR-008-02).

    Інваріант `0 ≤ in_use ≤ capacity` тримає один `threading.Lock`: перевірка зайнятості й
    інкремент — одна критична секція. Черги очікування немає навмисно (спека: «не вічна
    черга») — викликач отримує `None` і віддає клієнту `busy` із `retry_after_seconds`.
    Слот, який не звільнили (ліза загубилась без `release`/`with`), не повертається сам:
    викликач зобов'язаний користуватися `with lease:` (FR-008-06).
    """

    def __init__(self, capacity: int, busy_retry_after_seconds: int) -> None:
        if type(capacity) is not int or capacity < 1:
            raise ValueError(f"capacity must be an int >= 1, got {capacity!r}")
        if type(busy_retry_after_seconds) is not int or busy_retry_after_seconds < 1:
            raise ValueError(
                f"busy_retry_after_seconds must be an int >= 1, got {busy_retry_after_seconds!r}")
        self._capacity = capacity
        self._retry_after = busy_retry_after_seconds
        self._lock = threading.Lock()
        self._in_use = 0

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def retry_after_seconds(self) -> int:
        """Скільки секунд радити клієнтові почекати, коли вільних слотів немає (≥ 1)."""
        return self._retry_after

    @property
    def in_use(self) -> int:
        with self._lock:
            return self._in_use

    def try_acquire(self) -> Lease | None:
        """Атомарно взяти слот або повернути `None` (без очікування й побічних ефектів)."""
        with self._lock:
            if self._in_use >= self._capacity:
                return None
            lease = Lease(self)  # якщо конструктор впаде — лічильник ще не зачеплено
            self._in_use += 1
            return lease


_GATE_KINDS = ("ok", "in_progress", "rate_limited")


@dataclass(frozen=True)
class GateDecision:
    """Рішення воріт користувача: `kind` ∈ {ok, in_progress, rate_limited}.

    `retry_after_seconds` = 0 для `ok`/`in_progress` (чекати треба завершення власного аналізу,
    а не таймера) і ціле ≥ 1 для `rate_limited`.
    """

    kind: str
    retry_after_seconds: int

    def __post_init__(self) -> None:
        kind, r = self.kind, self.retry_after_seconds
        if type(kind) is not str or kind not in _GATE_KINDS:
            raise ValueError(f"GateDecision.kind must be one of {_GATE_KINDS}, got {kind!r}")
        if type(r) is not int:
            raise ValueError("GateDecision.retry_after_seconds must be an int")
        if kind == "rate_limited":
            if r < 1:
                raise ValueError("a rate_limited GateDecision must have retry_after_seconds >= 1")
        elif r != 0:
            raise ValueError(f"a {kind} GateDecision must have retry_after_seconds == 0")


_OK = GateDecision("ok", 0)
_IN_PROGRESS = GateDecision("in_progress", 0)


class UserGate:
    """Ворота користувача бота: один одночасний аналіз + ковзне вікно «важких» перевірок (FR-008-07).

    `try_enter` атомарно (один `threading.Lock` на всю перевірку й позначку) віддає рішення в
    порядку `in_progress` → `rate_limited` → `ok`:

    1. користувач уже активний -> `in_progress`; вікно НЕ чіпається (повтор під час аналізу не
       їсть квоту);
    2. `counts=True` і вікно вичерпане -> `rate_limited` з чесним `retry_after`; відмова у вікно
       не пишеться (`SlidingWindowLimiter.hit`), користувач активним не стає;
    3. інакше вікно рахує запит (лише при `counts=True`: кешований токен чи невалідна адреса
       квоту не їдять, але правило «один одночасний» діє й для них) і користувач стає активним.

    `leave` ідемпотентний; квоту не повертає. Замок вікна вкладений у замок воріт (порядок
    завжди ворота -> вікно, зворотного немає), тож взаємного блокування не буде. Ключ користувача
    — довільний hashable (Telegram `from.id` / `chat.id`); `1` і `"1"` — різні ключі.
    Виклик `leave` лежить на викликачі (`finally` воркера): гейт сам активність не скидає.
    """

    def __init__(self, checks_per_window: int, window_seconds: float,
                 clock: Callable[[], float] = time.monotonic) -> None:
        # валідацію (int ≥ 1, скінченне вікно > 0, callable) робить SlidingWindowLimiter
        self._window = SlidingWindowLimiter(checks_per_window, window_seconds, clock)
        self._lock = threading.Lock()
        self._active: set[Hashable] = set()

    def try_enter(self, user: Hashable, *, counts: bool) -> GateDecision:
        """Атомарно: допустити користувача, відмовити через активний аналіз чи через вікно."""
        if type(counts) is not bool:  # `counts=1`/`None` тихо вимкнуло б або ввімкнуло квоту
            raise TypeError(f"counts must be a bool, got {counts!r}")
        with self._lock:
            if user in self._active:
                return _IN_PROGRESS
            if counts:
                decision = self._window.hit(user)
                if not decision.allowed:
                    return GateDecision("rate_limited", decision.retry_after_seconds)
            self._active.add(user)
            return _OK

    def leave(self, user: Hashable) -> None:
        """Зняти позначку активності; для неактивного (або повторно) — нічого не робить."""
        with self._lock:
            self._active.discard(user)


@dataclass(frozen=True)
class Protection:
    """Один регулятор на процес: збирається `build_protection` раз у `main.py` (data-model)."""

    api: SlidingWindowLimiter
    slots: HeavySlots
    users: UserGate
    trusted_proxies: tuple[IPv4Network | IPv6Network, ...]


def build_protection(cfg: "ProtectionConfig", clock: Callable[[], float] = time.monotonic) -> Protection:
    """Зібрати `Protection` із `config/protection.yaml` (FR-008-08): усі межі — з конфігу."""
    return Protection(
        api=SlidingWindowLimiter(cfg.api_requests_per_window, cfg.api_window_seconds, clock),
        slots=HeavySlots(cfg.max_concurrent_analyses, cfg.busy_retry_after_seconds),
        users=UserGate(cfg.bot_checks_per_window, cfg.bot_window_seconds, clock),
        trusted_proxies=tuple(ipaddress.ip_network(item, strict=False)
                              for item in cfg.trusted_proxies),
    )
