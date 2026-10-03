# impl: FR-001-15
"""Межа зовнішнього світу (contracts/rpc-source.md).

Ядро збору викликає лише `RpcSource`. Типи-значення дослівно повторюють поле
`result` відповідних методів Solana JSON-RPC, тож записані з живого RPC фікстури
підходять без перетворень. Адаптер піднімає лише три винятки нижче.
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol, Sequence, TypeAlias, TypedDict

# T-015 замінить на `budget.Deadline`; до того фікстурне джерело його не перевіряє.
Deadline: TypeAlias = Any


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
