# impl: FR-007-15
"""Тестові дублі провайдерів пулу (contracts/pooled-rpc-source.md «Тестові дублі»).

`ScriptedProvider` обгортає власний `FixtureRpcSource` і додає керовані відхилення від реальної
поведінки вузла: горизонт історії (неархівний вузол), збої за номером виклику, бар'єр перед
`getTransaction`, журнал викликів. `SequentialRunner` виконує задачі в заданому порядку без потоків.
Не потокобезпечний `FixtureRpcSource` не ділиться між дублями.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Sequence

Outcome = tuple[Any, BaseException | None]


@dataclass
class ScriptedFailure:
    """Збій методу на номерах викликів цього методу на цьому провайдері (з 1); None — кожен виклик."""

    method: str
    calls: frozenset[int] | None
    exc: Exception


class ScriptedProvider:
    """RpcSource поверх `inner` з горизонтом історії, збоями, бар'єром, затримкою і журналом.

    `horizon_block_time`: транзакція з blockTime < horizon -> None; записи сторінки підписів з
    blockTime < horizon відкидаються після ліміту сторінки (сторінка коротшає). Запис без blockTime
    не відкидається. Порядок у кожному виклику: журнал -> `barrier.wait()` (лише `get_transactions`)
    -> `delay_seconds` -> збій -> обслуговування. `log` — `(метод JSON-RPC, ключі)`; `getTransaction` ->
    кортеж підписів пакета. Номери викликів — окремо для кожного методу. `max_concurrent` — максимум
    викликів, що одночасно перебувають усередині провайдера (під локом; для T-133).
    """

    def __init__(
        self,
        inner: Any,
        *,
        horizon_block_time: int | None = None,
        failures: Sequence[ScriptedFailure] = (),
        barrier: threading.Barrier | None = None,
        delay_seconds: float = 0.0,
    ) -> None:
        if delay_seconds < 0:
            raise ValueError("delay_seconds must be >= 0")
        self._inner = inner
        self._horizon = horizon_block_time
        self._failures = tuple(failures)
        self._barrier = barrier
        self._delay = float(delay_seconds)
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}
        self._inflight = 0
        self.max_concurrent = 0
        self.log: list[tuple[str, tuple[str, ...]]] = []

    @property
    def name(self) -> str:
        return self._inner.name

    # --- службове ---------------------------------------------------------------

    @contextmanager
    def _call(self, method: str, keys: Sequence[str], *, barrier: bool = False) -> Iterator[None]:
        with self._lock:
            self.log.append((method, tuple(keys)))
            number = self._counts.get(method, 0) + 1
            self._counts[method] = number
            self._inflight += 1
            self.max_concurrent = max(self.max_concurrent, self._inflight)
        try:
            if barrier and self._barrier is not None:
                self._barrier.wait()
            if self._delay:
                time.sleep(self._delay)
            for failure in self._failures:
                if failure.method == method and (failure.calls is None or number in failure.calls):
                    raise failure.exc
            yield
        finally:
            with self._lock:
                self._inflight -= 1

    def _visible(self, block_time: int | None) -> bool:
        return self._horizon is None or block_time is None or block_time >= self._horizon

    # --- RpcSource --------------------------------------------------------------

    def get_account_info(self, address: str, *, deadline: Any) -> Any:
        with self._call("getAccountInfo", (address,)):
            return self._inner.get_account_info(address, deadline=deadline)

    def get_signatures_for_address(
        self,
        address: str,
        *,
        before: str | None,
        until: str | None,
        limit: int,
        deadline: Any,
    ) -> list[Any]:
        with self._call("getSignaturesForAddress", (address,)):
            page = self._inner.get_signatures_for_address(
                address, before=before, until=until, limit=limit, deadline=deadline
            )
        if self._horizon is None:
            return page
        return [entry for entry in page if self._visible(entry.get("blockTime"))]

    def get_transactions(self, signatures: Sequence[str], *, deadline: Any) -> list[Any]:
        with self._call("getTransaction", tuple(signatures), barrier=True):
            results = self._inner.get_transactions(signatures, deadline=deadline)
        if self._horizon is None:
            return results
        return [
            raw if raw is None or self._visible(raw.get("blockTime")) else None
            for raw in results
        ]

    def get_token_accounts_by_owner(self, owner: str, *, deadline: Any) -> Any:
        with self._call("getTokenAccountsByOwner", (owner,)):
            return self._inner.get_token_accounts_by_owner(owner, deadline=deadline)


class SequentialRunner:
    """Runner без потоків: задачі виконуються в порядку `order`, outcomes — у порядку задач.

    `order`: "forward" | "reverse" | перестановка індексів `0..n-1` (довжина має збігтися з кількістю
    задач). Виняток задачі (BaseException) потрапляє в Outcome як `(None, exc)`.
    """

    def __init__(self, order: str | Sequence[int] = "forward") -> None:
        if isinstance(order, str):
            if order not in ("forward", "reverse"):
                raise ValueError("order must be 'forward', 'reverse' or a permutation of indices")
            self._mode: str | None = order
            self._perm: tuple[int, ...] | None = None
        else:
            perm = tuple(order)
            if sorted(perm) != list(range(len(perm))):
                raise ValueError("order must be a permutation of 0..n-1")
            self._mode = None
            self._perm = perm

    def __call__(self, tasks: Sequence[Callable[[], Any]]) -> list[Outcome]:
        tasks = list(tasks)
        if self._perm is not None:
            if len(self._perm) != len(tasks):
                raise ValueError("permutation length must equal the number of tasks")
            run_order: Sequence[int] = self._perm
        elif self._mode == "reverse":
            run_order = range(len(tasks) - 1, -1, -1)
        else:
            run_order = range(len(tasks))
        outcomes: list[Outcome] = [(None, None)] * len(tasks)
        for index in run_order:
            try:
                outcomes[index] = (tasks[index](), None)
            except BaseException as exc:  # noqa: BLE001 — контракт: виняток задачі йде в Outcome
                outcomes[index] = (None, exc)
        return outcomes
