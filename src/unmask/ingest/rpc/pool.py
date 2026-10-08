# impl: FR-007-01, FR-007-02, FR-007-03, FR-007-04, FR-007-05, FR-007-06, FR-007-07, FR-007-08, FR-007-12, FR-007-13
"""Пул RPC-провайдерів: джерело за протоколом `RpcSource`, що ділить запити між кількома провайдерами
(contracts/pooled-rpc-source.md, research R-1…R-4).

Збір (`IngestService`, колектор) працює з пулом як з одним джерелом і не змінюється. Паралелізм — лише
всередині `get_transactions`: виклик ділиться між провайдерами пропорційно вазі (`rate_per_second` профілю,
метод найбільших залишків — `apportion`), частки виконуються ОДНОЧАСНО через `runner` (за замовчуванням
`ThreadRunner` на пулі потоків; у тестах — `SequentialRunner` з довільним порядком завершення), а відповідь
збирається за ПОЗИЦІЄЮ підпису. Тож результат залежить лише від вхідних даних і складу пулу, а не від порядку
завершення потоків чи від того, який провайдер що обслужив (FR-007-04).

Статус реалізації (T-132, T-133, T-135): `Provider`, `apportion`, `ThreadRunner`, конструктор, `get_transactions`
раундами БЕЗ перебирання, одиночні методи (`get_account_info`, `get_signatures_for_address`,
`get_token_accounts_by_owner`) і `name`/`__repr__`. Одиночний виклик — один провайдер за згладженим зваженим
round-robin (SWRR, стан спільний для трьох методів: кошик адаптера глобальний, тож навантаження рахується
разом), у викликаючому потоці, під локом провайдера. Будь-який `RpcError` піднімається як є (той самий об'єкт
адаптера, поза `except`: `__cause__`/`__context__` порожні).

Вік і підтвердження «не знайдено» (T-135, FR-007-06/07, принцип V). Вік транзакції пул знає з `blockTime`
відповідей `getSignaturesForAddress`, що пройшли через нього (кеш `підпис → blockTime`, FIFO ≤
`age_cache_entries`, лише справжній `int`). Придатність провайдера для підпису: метод ∉ `skip_methods` і
(архівний, або вік невідомий, або `вік <= retention_seconds` — межа ВКЛЮЧНО). Позиції групуються за множиною
придатних провайдерів, кожна група ділиться окремо за вагами членів, тож частка провайдера — НЕ суцільний
відрізок, а відповідь збирається строго за ПОЗИЦІЄЮ підпису. `None` від неархівного провайдера — не остаточне:
позиція йде в наступний раунд «лише архівні» (`not_found_escalations`), і лише `None` від архівного
остаточний; транзакція з даними від будь-кого остаточна. Немає придатного провайдера (відомо старша за
глибину всіх неархівних, а придатного архівного немає; або нікому підтвердити `None`) —
`RpcUnavailable("history beyond provider retention")` без звернень цього раунду. Кеш віку — лише оптимізація
маршруту: правильність (чесна неповнота) тримається на правилі підтвердження, а не на кеші.
Ще не реалізовано (наступні задачі): правила `getSignaturesForAddress` для неархівних — до T-136 сторінка від
будь-якого провайдера вважається остаточною (безпечно лише для архівних); перебирання, паузи після 429 і класи
помилок (T-140); лічильники й `close()` (T-141). Відмова частки (будь-якого раунду) піднімається як є.

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
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Callable, Mapping, Sequence

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
_RETENTION_ERROR = "history beyond provider retention"


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
        self._age: OrderedDict[str, int] = OrderedDict()  # підпис -> blockTime, FIFO ≤ age_cache_entries (_state_lock)
        self._not_found_escalations = [0] * len(providers)  # позицій, переданих архівному після None (_state_lock)
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
        page = self._single(
            "getSignaturesForAddress",
            deadline,
            lambda s: s.get_signatures_for_address(
                address, before=before, until=until, limit=limit, deadline=deadline
            ),
        )
        self._remember_ages(page)  # після виклику: лок провайдера вже відпущено; відмова сюди не доходить
        return page

    def get_token_accounts_by_owner(self, owner: str, *, deadline: Deadline) -> list[TokenAccountInfo]:
        return self._single(
            "getTokenAccountsByOwner", deadline, lambda s: s.get_token_accounts_by_owner(owner, deadline=deadline)
        )

    def get_transactions(
        self, signatures: Sequence[str], *, deadline: Deadline
    ) -> list[RawTransaction | None]:
        """Раунди: поділ за придатністю й вагами → частки одночасно → збирання за позицією. «Все або нічого».

        Раунд 1 — усі позиції; позиція, на яку неархівний відповів `None`, іде в раунд «лише архівні», де `None`
        вже остаточний. Без перебирання: відмова частки будь-якого раунду піднімається (T-140 її перебере).
        """
        sigs = list(signatures)
        if not sigs:
            return []  # нуль звернень, дедлайн не перевіряється
        ages, now = self._age_snapshot(sigs)  # один знімок на виклик: розподіл не залежить від переплетення потоків
        results: list[RawTransaction | None] = [None] * len(sigs)
        pending = list(range(len(sigs)))
        confirm: set[int] = set()  # позиції, на які неархівний сказав None: питати лише архівних

        while pending:
            if deadline.expired():
                raise RpcBudgetTimeout()  # нуль звернень у цьому раунді
            eligible = {
                position: self._eligible_for_transaction(ages.get(sigs[position]), now, position in confirm)
                for position in pending
            }
            if any(not providers for providers in eligible.values()):
                raise RpcUnavailable(_RETENTION_ERROR)  # нікому віддати (або підтвердити): до звернень цього раунду

            shares = self._plan_round(pending, eligible)
            tasks = [
                self._share_task(index, [sigs[position] for position in positions], deadline)
                for index, positions in shares
            ]
            outcomes = self._runner(tasks)
            if len(outcomes) != len(tasks):
                raise RuntimeError("runner: must return one outcome per task")  # дефект runner, не відмова RPC

            failures: list[tuple[int, BaseException]] = []  # (найменша позиція частки, виняток)
            defect: BaseException | None = None
            unconfirmed: list[tuple[int, int]] = []  # (провайдер, позиція): None від неархівного
            for (index, positions), (value, exc) in zip(shares, outcomes):  # порядок конфігу, не завершення
                if exc is None and (not isinstance(value, list) or len(value) != len(positions)):
                    exc = RpcUnavailable(_SHAPE_ERROR)  # частка не відповідає запиту: ніколи не вгадуємо
                if exc is None:
                    archival = self._providers[index].archival
                    for position, raw in zip(positions, value):
                        results[position] = raw  # за ПОЗИЦІЄЮ підпису, не за порядком провайдерів
                        if raw is None and not archival:
                            unconfirmed.append((index, position))  # None неархівного — не остаточне
                elif isinstance(exc, RpcError):
                    failures.append((positions[0], exc))
                elif defect is None:
                    defect = exc  # не-RpcError — дефект: не маскується відмовою іншого провайдера
            # Усе `raise` — поза `except`: `__cause__`/`__context__` порожні (httpx-виняток тримав би URL).
            if defect is not None:
                raise defect
            if failures:
                raise min(failures, key=lambda failure: failure[0])[1]  # частка з найменшою позицією

            if unconfirmed:
                with self._state_lock:
                    for index, _ in unconfirmed:
                        self._not_found_escalations[index] += 1
                confirm.update(position for _, position in unconfirmed)
            pending = sorted(position for _, position in unconfirmed)  # лише архівні, де None остаточний: ≤ 2 раунди
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

    def _age_snapshot(self, sigs: Sequence[str]) -> tuple[dict[str, int], int]:
        """Відомі `blockTime` підписів виклику й «зараз» пулу (`clock.wall()`) — одним знімком під `_state_lock`."""
        with self._state_lock:
            ages = {sig: self._age[sig] for sig in sigs if sig in self._age}
        return ages, self._clock.wall()

    def _remember_ages(self, page: object) -> None:
        """Занести `blockTime` записів сторінки в кеш віку: лише `str` підпис і справжній `int` (не bool/None/float).

        Кеш — оптимізація маршруту (R-2), не джерело істини: сміття в сторінці мовчки пропускається (розбір записів —
        справа ядра), відповідь повертається викликачу як є. FIFO: наявний підпис оновлюється на місці, новий —
        в кінець, а понад `age_cache_entries` витісняється найстаріший.
        """
        if not isinstance(page, (list, tuple)):
            return
        with self._state_lock:
            for entry in page:
                if not isinstance(entry, Mapping):
                    continue
                signature, block_time = entry.get("signature"), entry.get("blockTime")
                if type(signature) is not str or type(block_time) is not int:
                    continue
                self._age[signature] = block_time  # наявний ключ зберігає позицію в черзі FIFO
                while len(self._age) > self._age_cache_entries:
                    self._age.popitem(last=False)

    def _eligible_for_transaction(self, block_time: int | None, now: int, confirming: bool) -> tuple[int, ...]:
        """Індекси провайдерів (порядок конфігу), яким можна віддати `getTransaction` підпису з відомим `blockTime`.

        Архівний придатний завжди; неархівний — коли вік невідомий або `вік <= retention_seconds` (межа включно),
        і ніколи у підтверджувальному раунді (там `None` має підтвердити лише архівний).
        """
        out = []
        for index, provider in enumerate(self._providers):
            if "getTransaction" in provider.skip_methods:
                continue
            if provider.archival:
                out.append(index)
            elif not confirming and (block_time is None or now - block_time <= provider.retention_seconds):
                out.append(index)
        return tuple(out)

    def _plan_round(
        self, positions: list[int], eligible: Mapping[int, tuple[int, ...]]
    ) -> list[tuple[int, list[int]]]:
        """Частки раунду: `(індекс провайдера, зростаючі позиції)` у порядку конфігу; порожні — без запису.

        Позиції групуються за множиною придатних провайдерів (групи — у порядку першої позиції); кожна група
        ділиться окремо — `apportion` за вагами своїх членів, суцільними відрізками СВОЇХ позицій. Тож частка
        провайдера — об'єднання відрізків із різних груп і, взагалі кажучи, не суцільна.
        """
        groups: dict[tuple[int, ...], list[int]] = {}
        for position in positions:
            groups.setdefault(eligible[position], []).append(position)
        shares: dict[int, list[int]] = {}
        for members, group in groups.items():
            counts = apportion(len(group), [self._providers[i].weight for i in members])
            offset = 0
            for index, count in zip(members, counts):
                if count:
                    shares.setdefault(index, []).extend(group[offset : offset + count])
                offset += count
        return [(index, sorted(shares[index])) for index in sorted(shares)]

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
