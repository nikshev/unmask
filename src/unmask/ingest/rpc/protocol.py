# impl: FR-001-15, FR-001-16
"""Межа зовнішнього світу (contracts/rpc-source.md).

Ядро збору викликає лише `RpcSource`. Типи-значення дослівно повторюють поле
`result` відповідних методів Solana JSON-RPC, тож записані з живого RPC фікстури
підходять без перетворень. Адаптер піднімає лише три винятки нижче (і `RpcBudgetTimeout` —
підклас `RpcTimeout`, T-051).
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol, Sequence, TypedDict


class Deadline(Protocol):
    """Бюджет часу, що передається в кожен виклик джерела (contracts/rpc-source.md «Deadline»).

    Структурний тип: реалізація — `budget.Deadline(clock, seconds)`; протокол тут, щоб межа джерела
    не імпортувала `budget` (той сам імпортує цей модуль). Адаптер перед кожним запитом:
    `if deadline.expired(): raise RpcBudgetTimeout()`, таймаут запиту — `deadline.request_timeout(cap)`.
    """

    def remaining(self) -> float: ...

    def expired(self) -> bool: ...

    def request_timeout(self, cap: float) -> float: ...


class SignatureInfo(TypedDict):
    """Елемент `getSignaturesForAddress`."""

    signature: str
    slot: int
    err: Any
    memo: str | None
    blockTime: int | None
    confirmationStatus: str


class AccountInfo(TypedDict, total=False):
    """`result.value` методу `getAccountInfo` (jsonParsed)."""

    lamports: int
    owner: str
    executable: bool
    rentEpoch: int
    space: int
    data: Mapping[str, Any]


class RawTransaction(TypedDict, total=False):
    """Результат `getTransaction` (jsonParsed, version 0).

    Усі ключі необов'язкові на рівні типу: пошкоджений запис — це дані, а не помилка
    типізації; його розпізнає `parse_transaction` (T-009).
    """

    slot: int
    blockTime: int | None
    version: Any
    transaction: Mapping[str, Any]
    meta: Mapping[str, Any] | None


class TokenAccountInfo(TypedDict):
    """Елемент `getTokenAccountsByOwner`: `pubkey` і `account.data.parsed.info`."""

    pubkey: str
    account: Mapping[str, Any]


class RpcError(Exception):
    """База винятків адаптера."""


class RpcRateLimited(RpcError):
    def __init__(self, retry_after: float | None = None) -> None:
        super().__init__(f"rate limited (retry_after={retry_after})")
        self.retry_after = retry_after


class RpcTimeout(RpcError):
    pass


class RpcBudgetTimeout(RpcTimeout):
    """Таймаут, у якому винен бюджет (T-051): адаптер вирішив, що звернення не вміститься в дедлайн.

    Піднімається адаптером замість запиту: `deadline.expired()` перед запитом; `request_timeout() <= 0`
    (або NaN); пейсер — очікування токенів не вміщається в `deadline.remaining()` (тоді `expired()` ще
    хибне). Підклас `RpcTimeout` — зворотно сумісний (`except RpcTimeout` його ловить). Текст фіксований —
    `budget`, без жодного тексту ззовні (політика `detail`). Ядро (`budget.deadline_timeouts`) перетворює
    його на `budget_exhausted` безумовно; звичайний `RpcTimeout` — лише коли дедлайн уже сплив.
    """

    def __init__(self) -> None:
        super().__init__("budget")


class RpcUnavailable(RpcError):
    def __init__(self, detail: str = "") -> None:
        super().__init__(detail)
        self.detail = detail


class RpcSource(Protocol):
    def get_account_info(self, address: str, *, deadline: Deadline) -> AccountInfo | None: ...

    def get_signatures_for_address(
        self,
        address: str,
        *,
        before: str | None,
        until: str | None,
        limit: int,
        deadline: Deadline,
    ) -> list[SignatureInfo]: ...

    def get_transactions(
        self, signatures: Sequence[str], *, deadline: Deadline
    ) -> list[RawTransaction | None]: ...

    def get_token_accounts_by_owner(
        self, owner: str, *, deadline: Deadline
    ) -> list[TokenAccountInfo]: ...

    @property
    def name(self) -> str: ...
