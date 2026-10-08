# impl: FR-007-01, FR-007-02, FR-007-03, FR-007-04, FR-007-05, FR-007-06, FR-007-07, FR-007-08, FR-007-12, FR-007-13
"""Пул RPC-провайдерів: джерело за протоколом `RpcSource`, що ділить запити між кількома провайдерами
(contracts/pooled-rpc-source.md, research R-1…R-4).

Збір (`IngestService`, колектор) працює з пулом як з одним джерелом і не змінюється. Паралелізм — лише
всередині `get_transactions`: виклик ділиться між провайдерами пропорційно вазі (`rate_per_second` профілю,
метод найбільших залишків — `apportion`), частки виконуються ОДНОЧАСНО через `runner` (за замовчуванням
`ThreadRunner` на пулі потоків; у тестах — `SequentialRunner` з довільним порядком завершення), а відповідь
збирається за ПОЗИЦІЄЮ підпису. Тож результат залежить лише від вхідних даних і складу пулу, а не від порядку
завершення потоків чи від того, який провайдер що обслужив (FR-007-04).

Статус реалізації (T-132, T-133, T-135, T-136, T-140): `Provider`, `apportion`, `ThreadRunner`, конструктор,
`get_transactions` раундами з перебиранням, одиночні методи (`get_account_info`, `get_signatures_for_address`,
`get_token_accounts_by_owner`) і `name`/`__repr__`. Одиночний виклик — один провайдер за згладженим зваженим
round-robin (SWRR, стан спільний для трьох методів: кошик адаптера глобальний, тож навантаження рахується
разом), у викликаючому потоці, під локом провайдера.

Перебирання й класи помилок (T-140, FR-007-05/08/12, research R-4). Відмова провайдера (`RpcRateLimited`,
`RpcTimeout`, `RpcUnavailable`, а також хибна форма частки) переводить частку/запит на іншого придатного; провайдер,
що відмовив, виключений до кінця ВИКЛИКУ (набір `excluded` лише зростає → завершення гарантоване, плюс жорсткий
запобіжник на кількість раундів). `RpcRateLimited` додатково ставить провайдера на паузу
`max(rate_limit_cooldown_seconds, retry_after)` за `clock.monotonic()` пулу: на паузі він не отримує нового, поки є
інші (усі на паузі → пауза ігнорується). Коли придатних не лишилось, піднімається ОСТАННЯ помилка найменшої
нерозвʼязаної позиції — той самий обʼєкт адаптера (клас і фіксований текст), поза `except`: `__cause__`/`__context__`
порожні, пул тексту не дописує. Дефект (не-`RpcError`) пріоритетніший за все й не перебирається. `RpcBudgetTimeout`
частки — це стан кошика КОНКРЕТНОГО ключа (пейсер не вмістив запит у дедлайн), тож перебирається як інші `RpcError`
(інший ключ може мати повний кошик; SC-005), без паузи; прострочений дедлайн перед раундом/спробою — свіжий
`RpcBudgetTimeout` без звернень.
Придатність (вік, `skip_methods`, «лише архівні» для підтвердження) діє й при перебиранні: неархівний не стає
запасним для давніх даних.

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

Підписи (T-136, FR-007-06/07, research R-5). Коротка сторінка неархівного вузла каже «у моєму сховищі більше нічого»,
а не «в адреси більше нічого» (а ядро читає порожню сторінку як кінець історії). Тому `get_signatures_for_address`
іде ЛИШЕ архівним, поки є придатний архівний (архівний, що не пропускає метод); неархівний — запасний, і тоді
приймається лише повна сторінка (`len >= limit`); коротка/порожня — `RpcUnavailable("history beyond provider
retention")` (чесна неповнота, а не «історія скінчилась»), що рахується в `not_found_escalations`. Сторінка архівного
остаточна, яка б не була. Решта одиночних методів обирає серед усіх, хто не пропускає метод (`skip_methods`).

Ще не реалізовано (наступні задачі): лічильники й `close()` (T-141).

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

import logging
import math
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from fractions import Fraction
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence

from unmask.ingest.budget import Clock
from unmask.ingest.rpc.protocol import (
    AccountInfo,
    Deadline,
    RawTransaction,
    RpcBudgetTimeout,
    RpcError,
    RpcRateLimited,
    RpcTimeout,
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
_NO_PROVIDER_ERROR = "no provider available"

_log = logging.getLogger("unmask.ingest.rpc.pool")
# Класи відмов для лічильників (contracts/pooled-rpc-source.md «Лічильники»).
FAILURE_CLASSES = ("budget", "timeout", "rate_limited", "unavailable", "other")


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


@dataclass(frozen=True)
class ProviderStats:
    """Знімок лічильників одного провайдера (FR-007-13). Без URL і ключів; `failures` — незмінний mapping."""

    requests: int  # звернень до провайдера (будь-який метод; частка = 1 звернення), включно з невдалими
    units: int  # одиниць: частка get_transactions = к-сть її підписів; інші звернення = 1
    failures: Mapping[str, int]  # класи FAILURE_CLASSES, завжди всі п'ять ключів
    not_found_escalations: int  # позицій, переданих архівному після None/короткої сторінки
    cooldowns: int  # постановок паузи після RpcRateLimited


def _failure_class(exc: BaseException) -> str:
    """Клас відмови: `budget` перевіряється раніше за `timeout` (RpcBudgetTimeout є підкласом RpcTimeout)."""
    if isinstance(exc, RpcBudgetTimeout):
        return "budget"
    if isinstance(exc, RpcTimeout):
        return "timeout"
    if isinstance(exc, RpcRateLimited):
        return "rate_limited"
    if isinstance(exc, RpcUnavailable):
        return "unavailable"
    return "other"


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
        self._requests = [0] * len(providers)  # звернень до провайдера, включно з невдалими (_state_lock)
        self._units = [0] * len(providers)  # одиниць: частка = к-сть підписів, інше = 1 (_state_lock)
        self._failures: list[dict[str, int]] = [dict.fromkeys(FAILURE_CLASSES, 0) for _ in providers]  # (_state_lock)
        self._cooldown_count = [0] * len(providers)  # постановок паузи (_state_lock)
        self._closed = False  # close() ідемпотентний (_state_lock)
        self._cooldown_until: dict[int, float] = {}  # індекс провайдера -> clock.monotonic() кінця паузи (_state_lock)
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

    # --- лічильники і закриття (T-141, FR-007-13, FR-007-12) -------------------------------

    def stats(self) -> Mapping[str, ProviderStats]:
        """Знімок лічильників за іменами з конфігу, у порядку конфігу. Подальші виклики пулу знімок не змінюють."""
        with self._state_lock:
            return MappingProxyType(
                {
                    provider.name: ProviderStats(
                        requests=self._requests[index],
                        units=self._units[index],
                        failures=MappingProxyType(dict(self._failures[index])),
                        not_found_escalations=self._not_found_escalations[index],
                        cooldowns=self._cooldown_count[index],
                    )
                    for index, provider in enumerate(self._providers)
                }
            )

    def stats_line(self) -> str:
        """Один рядок для логу/друку: `rpc_pool <ім'я> requests=… units=… failures=… escalations=… cooldowns=…; …`.

        `failures` — сума за класами. Лише імена з конфігу й цілі числа: ні URL, ні ключів.
        """
        parts = []
        for name, entry in self.stats().items():
            parts.append(
                f"{name} requests={entry.requests} units={entry.units} "
                f"failures={sum(entry.failures.values())} escalations={entry.not_found_escalations} "
                f"cooldowns={entry.cooldowns}"
            )
        return "rpc_pool " + "; ".join(parts)

    def close(self) -> None:
        """Закриває runner і джерела провайдерів (кожне джерело — один раз), пише `stats_line()` у лог рівно раз.

        Ідемпотентно: повторний виклик нічого не робить. Виняток закриття одного компонента не перериває решту:
        усі закриваються, лог пишеться, і лише потім піднімається перший виняток (обʼєкт як є, поза `except`).
        """
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
        closers: list[Callable[[], Any]] = []
        runner_close = getattr(self._runner, "close", None)
        if callable(runner_close):
            closers.append(runner_close)
        seen: set[int] = set()
        for provider in self._providers:
            if id(provider.source) in seen:  # одне джерело під кількома іменами — закрити один раз
                continue
            seen.add(id(provider.source))
            source_close = getattr(provider.source, "close", None)
            if callable(source_close):
                closers.append(source_close)
        errors: list[Exception] = []
        for closer in closers:
            try:
                closer()
            except Exception as exc:  # noqa: BLE001 — решта закриттів і лог виконуються; перший виняток піде далі
                errors.append(exc)
        _log.info(self.stats_line())
        if errors:
            raise errors[0]  # поза `except`: без `__context__`

    def __enter__(self) -> PooledRpcSource:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        """Закриває пул; виняток із блоку `with` не ковтає (повертає None)."""
        self.close()

    def get_account_info(self, address: str, *, deadline: Deadline) -> AccountInfo | None:
        _, info = self._single("getAccountInfo", deadline, lambda s: s.get_account_info(address, deadline=deadline))
        return info

    def get_signatures_for_address(
        self,
        address: str,
        *,
        before: str | None,
        until: str | None,
        limit: int,
        deadline: Deadline,
    ) -> list[SignatureInfo]:
        index, page = self._single(
            "getSignaturesForAddress",
            deadline,
            lambda s: s.get_signatures_for_address(
                address, before=before, until=until, limit=limit, deadline=deadline
            ),
        )
        if not self._providers[index].archival and (
            not isinstance(page, (list, tuple)) or len(page) < limit
        ):
            # Запасний неархівний: коротка/порожня сторінка не доводить кінця історії адреси (R-5). Сторінку відкидаємо
            # (вік з неї не кешуємо); `raise` — поза `except` і після відпущених локів.
            with self._state_lock:
                self._not_found_escalations[index] += 1
            raise RpcUnavailable(_RETENTION_ERROR)
        self._remember_ages(page)  # після виклику: лок провайдера вже відпущено; відмова сюди не доходить
        return page

    def get_token_accounts_by_owner(self, owner: str, *, deadline: Deadline) -> list[TokenAccountInfo]:
        _, accounts = self._single(
            "getTokenAccountsByOwner", deadline, lambda s: s.get_token_accounts_by_owner(owner, deadline=deadline)
        )
        return accounts

    def get_transactions(
        self, signatures: Sequence[str], *, deadline: Deadline
    ) -> list[RawTransaction | None]:
        """Раунди: поділ за придатністю й вагами → частки одночасно → збирання за позицією. «Все або нічого».

        Раунд 1 — усі позиції; позиція, на яку неархівний відповів `None`, іде в раунд «лише архівні», де `None`
        вже остаточний. Перебирання (T-140, FR-007-08): частка, що впала з `RpcRateLimited`/`RpcTimeout`/
        `RpcUnavailable` (або з хибною формою), іде в наступний раунд без цього провайдера — він виключений до кінця
        ВИКЛИКУ (`excluded` лише зростає). Коли для позиції придатних не лишилось, піднімається ОСТАННЯ помилка цієї
        позиції (найменша серед нерозвʼязаних), той самий обʼєкт адаптера (тут може бути й `RpcBudgetTimeout`). Дефект
        (не-`RpcError`) не перебирається.
        """
        sigs = list(signatures)
        if not sigs:
            return []  # нуль звернень, дедлайн не перевіряється
        ages, now = self._age_snapshot(sigs)  # один знімок на виклик: розподіл не залежить від переплетення потоків
        results: list[RawTransaction | None] = [None] * len(sigs)
        pending = list(range(len(sigs)))
        confirm: set[int] = set()  # позиції, на які неархівний сказав None: питати лише архівних
        excluded: set[int] = set()  # провайдери, що відмовили в цьому виклику: лише зростає → завершення гарантоване
        last_error: dict[int, BaseException] = {}  # позиція -> помилка її останньої невдалої спроби
        said_none: dict[int, int] = {}  # позиція підтвердження -> неархівний, що сказав None (рахується при передачі)
        excluded_errors: dict[int, BaseException] = {}  # провайдер -> помилка, що його виключила (порядок виключення)
        rounds = 0

        while pending:
            # Жорсткий запобіжник. Кожен раунд із відмовою виключає ≥ 1 провайдера (≤ n таких раундів); раунд без відмов
            # або завершує все, або лишає лише позиції підтвердження (там і далі працюють лише архівні, `None` остаточний),
            # тож найгірший випадок — n + 2 раундів. Ліміт 2n + 2 недосяжний у коректному коді й семантики не змінює:
            # це гучний дефект замість зависання потоку, що тримає запит.
            rounds += 1
            if rounds > 2 * len(self._providers) + 2:
                raise RuntimeError("pool: failover round limit exceeded")
            if deadline.expired():
                raise RpcBudgetTimeout()  # нуль звернень у цьому раунді
            paused = self._paused_providers()
            eligible: dict[int, tuple[int, ...]] = {}
            for position in pending:
                usable = tuple(
                    index
                    for index in self._eligible_for_transaction(ages.get(sigs[position]), now, position in confirm)
                    if index not in excluded
                )
                awake = tuple(index for index in usable if index not in paused)
                eligible[position] = awake or usable  # усі на паузі → пауза ігнорується: політика не валить збір
            stuck = [position for position in pending if not eligible[position]]
            if stuck:
                # нікому віддати (або підтвердити): до звернень цього раунду; `raise` — поза `except`
                position = min(stuck)
                error = last_error.get(position)
                if error is None:
                    # Позиція сама не падала (напр. `None` неархівного чекає підтвердження), але придатні для неї
                    # провайдери виключені відмовою: чесна причина — остання відмова серед них, а не «історія за межею».
                    suitable = self._eligible_for_transaction(ages.get(sigs[position]), now, position in confirm)
                    error = next((e for i, e in reversed(excluded_errors.items()) if i in suitable), None)
                if error is not None:
                    raise error
                raise RpcUnavailable(_RETENTION_ERROR)  # нікого не було кому віддати: історія за межею всіх

            handed_over = [position for position in pending if position in said_none]
            if handed_over:
                with self._state_lock:  # лічильник — коли позиція справді передається архівному, а не коли про це дізналися
                    for position in handed_over:
                        self._not_found_escalations[said_none.pop(position)] += 1
            shares = self._plan_round(pending, eligible)
            tasks = [
                self._share_task(index, [sigs[position] for position in positions], deadline)
                for index, positions in shares
            ]
            outcomes = self._runner(tasks)
            if len(outcomes) != len(tasks):
                raise RuntimeError("runner: must return one outcome per task")  # дефект runner, не відмова RPC
            with self._state_lock:  # кожна частка — одне звернення; одиниць — підписи частки (FR-007-13)
                for index, positions in shares:
                    self._requests[index] += 1
                    self._units[index] += len(positions)

            failed: list[int] = []  # позиції часток, що впали (RpcError)
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
                    excluded.add(index)
                    excluded_errors[index] = exc
                    failed.extend(positions)
                    for position in positions:
                        last_error[position] = exc
                    self._record_failure(index, exc)
                    if isinstance(exc, RpcRateLimited):
                        self._cool_down(index, exc)
                elif defect is None:
                    defect = exc  # не-RpcError — дефект: не маскується відмовою іншого провайдера
            # Усе `raise` — поза `except`: `__cause__`/`__context__` порожні (httpx-виняток тримав би URL).
            if defect is not None:
                raise defect

            for index, position in unconfirmed:
                said_none[position] = index
                confirm.add(position)
            pending = sorted({*failed, *(position for _, position in unconfirmed)})
        return results

    # --- внутрішнє -----------------------------------------------------------------------

    def _single(self, method: str, deadline: Deadline, call: Callable[[Any], Any]) -> tuple[int, Any]:
        """Одиночний виклик: один провайдер за SWRR, у викликаючому потоці, під його локом, з перебиранням (T-140).

        Провайдер, що відмовив `RpcRateLimited`/`RpcTimeout`/`RpcUnavailable`, виключається до кінця виклику, і
        спроба йде далі; коли кандидатів не лишилось — піднімається ОСТАННЯ помилка (той самий обʼєкт, поза `except`).
        Дефект не перебирається; `RpcBudgetTimeout` ключа — перебирається (без паузи). Ітерацій ≤ n + 1: кожна невдала виключає ще одного провайдера.

        Повертає `(індекс провайдера, відповідь)`: `get_signatures_for_address` має знати, чия це сторінка.
        """
        excluded: set[int] = set()
        last: BaseException | None = None
        for _ in range(len(self._providers) + 1):
            if deadline.expired():
                raise RpcBudgetTimeout()  # нуль звернень; ротацію не зсуваємо
            candidates = self._candidates(method, excluded)
            if not candidates:
                if last is not None:
                    raise last
                raise RpcUnavailable(_NO_PROVIDER_ERROR)
            index = self._pick(candidates)
            failure: RpcError | None = None
            with self._state_lock:  # звернення рахується до виклику, невдале теж (FR-007-13)
                self._requests[index] += 1
                self._units[index] += 1
            try:
                with self._locks[index]:  # `_state_lock` тут уже відпущено
                    return index, call(self._providers[index].source)
            except RpcError as exc:
                failure = exc  # лише запамʼятати: усе `raise` — нижче, поза `except`
            self._record_failure(index, failure)
            excluded.add(index)
            last = failure
            if isinstance(failure, RpcRateLimited):
                self._cool_down(index, failure)
        raise RuntimeError("pool: failover attempt limit exceeded")  # недосяжно: кожна спроба виключає провайдера

    def _pick(self, candidates: Sequence[int]) -> int:
        """Згладжений зважений round-robin серед кандидатів; нічия — порядок конфігу.

        Класичний SWRR: кожному кандидату `+вага`, переможцю `−сума ваг кандидатів`. Стан — точні дроби.
        """
        with self._state_lock:
            total = sum(self._exact_weights[i] for i in candidates)
            for i in candidates:
                self._swrr_current[i] += self._exact_weights[i]
            best = max(candidates, key=lambda i: (self._swrr_current[i], -i))
            self._swrr_current[best] -= total
        return best

    def _candidates(self, method: str, excluded: set[int]) -> list[int]:
        """Індекси провайдерів, яким можна віддати одиночний виклик (порядок конфігу).

        Метод ∉ `skip_methods`, провайдер не виключений у цьому виклику. `getSignaturesForAddress`: лише архівні, поки
        лишається бодай один придатний архівний; неархівні — запасні (R-5). Провайдери на паузі відкидаються, якщо
        лишається хоч один не на паузі (усі на паузі → пауза ігнорується).
        """
        serving = [i for i, p in enumerate(self._providers) if method not in p.skip_methods and i not in excluded]
        if method == "getSignaturesForAddress":
            archival = [i for i in serving if self._providers[i].archival]
            if archival:
                serving = archival
        paused = self._paused_providers()
        return [i for i in serving if i not in paused] or serving

    def _paused_providers(self) -> frozenset[int]:
        """Провайдери на паузі після `RpcRateLimited` (`now < кінець паузи` за `clock.monotonic()` пулу)."""
        with self._state_lock:
            now = self._clock.monotonic()
            for index in [i for i, until in self._cooldown_until.items() if now >= until]:
                del self._cooldown_until[index]  # пауза скінчилась (межа `now == until` — вже не пауза)
            return frozenset(self._cooldown_until)

    def _cool_down(self, index: int, exc: BaseException) -> None:
        """Пауза після `RpcRateLimited`: `max(rate_limit_cooldown_seconds, retry_after)`.

        `retry_after` — лише справжнє скінченне число (NaN, inf, не-число і `bool` ігноруються: сервер не вирішує, що
        ключ мовчить вічно). Пауза не скорочується повторною відмовою. Кошик ліміту один на всі методи ключа, тож пауза —
        на провайдера, а не на метод.
        """
        pause = self._cooldown_seconds
        retry_after = getattr(exc, "retry_after", None)
        if _is_number(retry_after) and math.isfinite(retry_after) and retry_after > pause:
            pause = float(retry_after)
        with self._state_lock:
            until = self._clock.monotonic() + pause
            self._cooldown_until[index] = max(until, self._cooldown_until.get(index, until))
            self._cooldown_count[index] += 1  # кожна постановка паузи (FR-007-13)

    def _record_failure(self, index: int, exc: BaseException) -> None:
        """Лічильник відмови провайдера за класом (`budget` раніше за `timeout`)."""
        with self._state_lock:
            self._failures[index][_failure_class(exc)] += 1

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
