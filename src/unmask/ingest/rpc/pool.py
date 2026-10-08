# impl: FR-007-01, FR-007-02, FR-007-03, FR-007-04, FR-007-05, FR-007-06, FR-007-07, FR-007-08, FR-007-12, FR-007-13
"""Пул RPC-провайдерів: джерело за протоколом `RpcSource`, що ділить запити між кількома провайдерами
(contracts/pooled-rpc-source.md, research R-1…R-4).

Збір (`IngestService`, колектор) працює з пулом як з одним джерелом і не змінюється. Паралелізм — лише
всередині `get_transactions`: виклик ділиться між провайдерами пропорційно вазі (`rate_per_second` профілю,
метод найбільших залишків — `apportion`), частки виконуються ОДНОЧАСНО через `runner` (за замовчуванням
`ThreadRunner` на пулі потоків; у тестах — `SequentialRunner` з довільним порядком завершення), а відповідь
збирається за ПОЗИЦІЄЮ підпису. Тож результат залежить лише від вхідних даних і складу пулу, а не від порядку
завершення потоків чи від того, який провайдер що обслужив (FR-007-04).

Статус реалізації (T-132, T-133): `Provider`, `apportion`, `ThreadRunner`, конструктор, `get_transactions`
одним раундом БЕЗ перебирання, одиночні методи (`get_account_info`, `get_signatures_for_address`,
`get_token_accounts_by_owner`) і `name`/`__repr__`. Одиночний виклик — один провайдер за згладженим зваженим
round-robin (SWRR, стан спільний для трьох методів: кошик адаптера глобальний, тож навантаження рахується
разом), у викликаючому потоці, під локом провайдера. Будь-який `RpcError` піднімається як є (той самий об'єкт
адаптера, поза `except`: `__cause__`/`__context__` порожні). Ще не реалізовано (наступні задачі):
маршрутизація за архівністю й підтвердження `None`/короткої сторінки (T-135, T-136), перебирання, паузи після
429 і класи помилок (T-140), лічильники й `close()` (T-141). До T-135/T-136 відповідь `None` / сторінка від
будь-якого провайдера вважається остаточною — безпечно лише для архівних провайдерів.

Потоки: адаптер `HttpRpcSource` однопотоковий, тож кожен провайдер викликається ЛИШЕ під власним
`threading.Lock` (кошик лімітера провайдера — один на всіх викликачів). Шматок роботи раунду — одна задача на
провайдера; обробка результатів — у викликаючому потоці, у порядку конфігу. Бухгалтерія пулу (стан SWRR)
захищена `_state_lock`; він береться лише на мить вибору й НІКОЛИ не тримається разом із локом провайдера
(інакше виклик повільного провайдера блокував би вибір для всіх).

Секретність: `Provider.__repr__` — лише ім'я; тексти помилок конструктора — назва поля й правило, без значень;
власні тексти пулу — константи. Модуль не імпортує транспорт (`httpx`, `rpc.http`), YAML і `os` (T-142).

Залежить від: `rpc.protocol`, `budget` (`Clock`).
"""

from __future__ import annotations

import math
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Callable, Sequence

from unmask.ingest.budget import Clock
from unmask.ingest.rpc.protocol import (
    AccountInfo,
    Deadline,
    RawTransaction,
    RpcBudgetTimeout,
    RpcError,
    RpcUnavailable,
    SignatureInfo,
    TokenAccountInfo,
)

# Чотири методи протоколу за іменами JSON-RPC (ті самі імена — у `skip_methods` і в журналі дублів).
METHODS = ("getAccountInfo", "getSignaturesForAddress", "getTransaction", "getTokenAccountsByOwner")

Outcome = tuple[Any, BaseException | None]  # (результат, виняток) однієї задачі
Runner = Callable[[Sequence[Callable[[], Any]]], list[Outcome]]  # outcomes — у ПОРЯДКУ ЗАДАЧ

# Власні тексти пулу — лише константи (політика `detail` адаптера: жодного вільного тексту ззовні).
_SHAPE_ERROR = "unexpected response shape: pool share length"


@dataclass(frozen=True, repr=False)
class Provider:
    """Один провайдер пулу. `repr` — лише ім'я: `source` може нести URL із ключем."""

    name: str
    source: Any  # RpcSource
    weight: float  # > 0, скінченне (rate_per_second профілю)
    archival: bool
    retention_seconds: float | None  # None ⇔ archival
    skip_methods: frozenset[str] = frozenset()

    def __repr__(self) -> str:
        return f"Provider(name={self.name!r})"


def apportion(n: int, weights: Sequence[float]) -> list[int]:
    """Метод найбільших залишків (Гамільтона): `sum == n`, кожна частка в межах 1 від точної квоти.

    Нічия залишків — менший індекс (порядок конфігу); `n == 0` → нулі. Арифметика точна (`Fraction` від
    значень ваг), тож рівні ваги дають рівні залишки без шуму плаваючої коми, а результат детермінований.
    """
    if type(n) is not int or n < 0:
        raise ValueError("n: must be an int >= 0")
    if len(weights) == 0:
        raise ValueError("weights: at least one weight is required")
    if not all(_is_positive_finite(w) for w in weights):
        raise ValueError("weights: every weight must be a finite number > 0")
    exact = [Fraction(w) for w in weights]
    total = sum(exact)
    quotas = [n * w / total for w in exact]
    counts = [math.floor(q) for q in quotas]
    leftover = n - sum(counts)  # 0 <= leftover < len(weights)
    by_remainder = sorted(range(len(counts)), key=lambda i: (-(quotas[i] - counts[i]), i))
    for index in by_remainder[:leftover]:
        counts[index] += 1
    return counts


def _capture(task: Callable[[], Any]) -> Outcome:
    try:
        return task(), None
    except BaseException as exc:  # noqa: BLE001 — контракт runner: виняток задачі йде в Outcome
        return None, exc


class ThreadRunner:
    """Runner за замовчуванням: задачі раунду — на пулі потоків; outcomes — у порядку задач."""

    def __init__(self, max_workers: int) -> None:
        if type(max_workers) is not int or max_workers < 1:
            raise ValueError("max_workers: must be an int >= 1")
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="rpc-pool")

    def __call__(self, tasks: Sequence[Callable[[], Any]]) -> list[Outcome]:
        futures = [self._executor.submit(_capture, task) for task in tasks]
        return [future.result() for future in futures]  # `_capture` не піднімає: чекаємо всі задачі

    def close(self) -> None:
        self._executor.shutdown(wait=True)


class PooledRpcSource:
    """Реалізує `RpcSource` поверх кількох провайдерів; див. модуль. Потокобезпечний: частки `get_transactions` і одиночні методи ділять один лок на провайдера."""

    def __init__(
        self,
        providers: Sequence[Provider],
        *,
        clock: Clock,
        rate_limit_cooldown_seconds: float,
        age_cache_entries: int,
        runner: Runner | None = None,
    ) -> None:
        providers = tuple(providers)
        _validate_providers(providers)
        if not _is_positive_finite(rate_limit_cooldown_seconds):
            raise ValueError("rate_limit_cooldown_seconds: must be a finite number > 0")
        if type(age_cache_entries) is not int or age_cache_entries < 1:
            raise ValueError("age_cache_entries: must be an int >= 1")
        self._providers = providers  # порядок конфігу = порядок часток і нічиїх
        self._locks = tuple(threading.Lock() for _ in providers)  # по одному на провайдера
        self._state_lock = threading.Lock()  # стан SWRR (і далі: кеш віку, паузи, лічильники); не разом із _locks
        self._exact_weights = tuple(Fraction(p.weight) for p in providers)  # точні ваги: без шуму float
        self._swrr_current = [Fraction(0)] * len(providers)  # стан згладженого round-robin, за індексом
        self._clock = clock
        self._cooldown_seconds = float(rate_limit_cooldown_seconds)
        self._age_cache_entries = age_cache_entries
        self._runner: Runner = runner if runner is not None else ThreadRunner(len(providers))

    # --- RpcSource: одиночні методи (T-133) -------------------------------------------------

    @property
    def name(self) -> str:
        """Спільне `source.name` усіх провайдерів, інакше `"pool"` (метадані результату не змінюються)."""
        names = {provider.source.name for provider in self._providers}
        return next(iter(names)) if len(names) == 1 else "pool"

    def __repr__(self) -> str:
        return f"PooledRpcSource(providers={tuple(p.name for p in self._providers)!r})"  # лише імена

    def get_account_info(self, address: str, *, deadline: Deadline) -> AccountInfo | None:
        return self._single("getAccountInfo", deadline, lambda s: s.get_account_info(address, deadline=deadline))

    def get_signatures_for_address(
        self,
        address: str,
        *,
        before: str | None,
        until: str | None,
        limit: int,
        deadline: Deadline,
    ) -> list[SignatureInfo]:
        return self._single(
            "getSignaturesForAddress",
            deadline,
            lambda s: s.get_signatures_for_address(
                address, before=before, until=until, limit=limit, deadline=deadline
            ),
        )

    def get_token_accounts_by_owner(self, owner: str, *, deadline: Deadline) -> list[TokenAccountInfo]:
        return self._single(
            "getTokenAccountsByOwner", deadline, lambda s: s.get_token_accounts_by_owner(owner, deadline=deadline)
        )

    def get_transactions(
        self, signatures: Sequence[str], *, deadline: Deadline
    ) -> list[RawTransaction | None]:
        """Раунд: поділ за вагами → частки одночасно → збирання за позицією. «Все або нічого»."""
        sigs = list(signatures)
        if not sigs:
            return []  # нуль звернень, дедлайн не перевіряється
        if deadline.expired():
            raise RpcBudgetTimeout()  # нуль звернень у цьому раунді

        shares = self._plan_round(list(range(len(sigs))), self._serving("getTransaction"))
        tasks = [
            self._share_task(index, [sigs[position] for position in positions], deadline)
            for index, positions in shares
        ]
        outcomes = self._runner(tasks)
        if len(outcomes) != len(tasks):
            raise RuntimeError("runner: must return one outcome per task")  # дефект runner, не відмова RPC

        results: list[RawTransaction | None] = [None] * len(sigs)
        failures: list[tuple[int, BaseException]] = []  # (найменша позиція частки, виняток)
        defect: BaseException | None = None
        for (_, positions), (value, exc) in zip(shares, outcomes):  # порядок конфігу, не завершення
            if exc is None and (not isinstance(value, list) or len(value) != len(positions)):
                exc = RpcUnavailable(_SHAPE_ERROR)  # частка не відповідає запиту: ніколи не вгадуємо
            if exc is None:
                for position, raw in zip(positions, value):
                    results[position] = raw
            elif isinstance(exc, RpcError):
                failures.append((positions[0], exc))
            elif defect is None:
                defect = exc  # не-RpcError — дефект: не маскується відмовою іншого провайдера
        # Усе `raise` — поза `except`: `__cause__`/`__context__` порожні (httpx-виняток тримав би URL).
        if defect is not None:
            raise defect
        if failures:
            raise min(failures, key=lambda failure: failure[0])[1]  # частка з найменшою позицією
        return results

    # --- внутрішнє -----------------------------------------------------------------------

    def _single(self, method: str, deadline: Deadline, call: Callable[[Any], Any]) -> Any:
        """Одиночний виклик: один провайдер за SWRR, у викликаючому потоці, під його локом. Без перебирання."""
        if deadline.expired():
            raise RpcBudgetTimeout()  # нуль звернень; ротацію не зсуваємо
        index = self._pick(method)
        with self._locks[index]:  # `_state_lock` тут уже відпущено
            return call(self._providers[index].source)  # RpcError піднімається як є, поза `except`

    def _pick(self, method: str) -> int:
        """Згладжений зважений round-robin серед провайдерів, що обслуговують метод; нічия — порядок конфігу.

        Класичний SWRR: кожному кандидату `+вага`, переможцю `−сума ваг кандидатів`. Стан — точні дроби.
        """
        candidates = self._serving(method)
        with self._state_lock:
            total = sum(self._exact_weights[i] for i in candidates)
            for i in candidates:
                self._swrr_current[i] += self._exact_weights[i]
            best = max(candidates, key=lambda i: (self._swrr_current[i], -i))
            self._swrr_current[best] -= total
        return best

    def _serving(self, method: str) -> list[int]:
        """Індекси провайдерів, що обслуговують метод (порядок конфігу)."""
        return [i for i, p in enumerate(self._providers) if method not in p.skip_methods]

    def _plan_round(self, positions: list[int], eligible: list[int]) -> list[tuple[int, list[int]]]:
        """Частки раунду: `(індекс провайдера, зростаючі позиції)` у порядку конфігу; порожні — без запису.

        Частка — суцільний відрізок `positions`; розмір — `apportion` за вагами придатних провайдерів.
        """
        counts = apportion(len(positions), [self._providers[i].weight for i in eligible])
        shares: list[tuple[int, list[int]]] = []
        offset = 0
        for index, count in zip(eligible, counts):
            if count:
                shares.append((index, positions[offset : offset + count]))
            offset += count
        return shares

    def _share_task(self, index: int, batch: list[str], deadline: Deadline) -> Callable[[], Any]:
        provider, lock = self._providers[index], self._locks[index]

        def run() -> Any:
            with lock:  # адаптер однопотоковий: провайдер обслуговує один виклик за раз
                return provider.source.get_transactions(batch, deadline=deadline)

        return run


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_positive_finite(value: object) -> bool:
    return _is_number(value) and math.isfinite(value) and value > 0  # type: ignore[arg-type]


def _validate_providers(providers: tuple[Provider, ...]) -> None:
    """`ValueError` з назвою поля й правилом, без значень (значення можуть бути секретом)."""
    if not providers:
        raise ValueError("providers: at least one provider is required")
    names: set[str] = set()
    for provider in providers:
        if not isinstance(provider, Provider):
            raise ValueError("providers: every item must be a Provider")
        if not isinstance(provider.name, str) or not provider.name:
            raise ValueError("name: must be a non-empty string")
        if provider.name in names:
            raise ValueError("providers: names must be unique")
        names.add(provider.name)
        if not _is_positive_finite(provider.weight):
            raise ValueError("weight: must be a finite number > 0")
        if type(provider.archival) is not bool:
            raise ValueError("archival: must be a bool")
        if provider.archival != (provider.retention_seconds is None) or (
            provider.retention_seconds is not None and not _is_positive_finite(provider.retention_seconds)
        ):
            raise ValueError(
                "retention_seconds: must be None for an archival provider and a finite number > 0 otherwise"
            )
        if not isinstance(provider.skip_methods, frozenset) or not provider.skip_methods <= set(METHODS):
            raise ValueError("skip_methods: must be a frozenset of JSON-RPC method names of the source")
    for method in METHODS:
        if all(method in p.skip_methods for p in providers):
            raise ValueError("skip_methods: every method must be served by at least one provider")
