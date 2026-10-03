# impl: FR-001-06, FR-001-09, FR-001-10, FR-001-14
"""Типи результату збору (data-model.md) та інваріант повноти.

Усі сутності незмінні (`frozen=True`) і перевіряють себе при побудові: некоректний
стан падає гучно (TypeError/ValueError), а не потрапляє в результат.

Ключовий інваріант (FR-001-09, FR-001-10, принцип V): статус повноти не
зберігається, а обчислюється з даних — `complete` тоді й лише тоді, коли
`missing` порожній і `buyers.complete`. `Completeness` будується лише через
`Completeness.derive`; поле `status`, яке можна було б виставити вручну, не існує.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Iterable


# --- Перелічення -----------------------------------------------------------------


class AddressType(StrEnum):
    WALLET = "wallet"
    OFF_CURVE = "off_curve"


class CompletenessStatus(StrEnum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"


class MissingReason(StrEnum):
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    CORRUPT_DATA = "corrupt_data"
    BUDGET_EXHAUSTED = "budget_exhausted"


class UnexpandedReason(StrEnum):
    HIGH_DEGREE = "high_degree"
    SIGNATURE_CAP = "signature_cap"


class RejectKind(StrEnum):
    INVALID_ADDRESS = "invalid_address"
    TOKEN_NOT_FOUND = "token_not_found"


class Asset(str):
    """Актив переказу: `"sol"` або `"spl:<mint>"`. Рядок, тож порівнюється і серіалізується як є."""

    SOL: Asset

    def __new__(cls, value: object) -> Asset:
        if not isinstance(value, str):
            raise TypeError(f"asset: expected str, got {value!r}")
        if value != "sol" and not (value.startswith("spl:") and len(value) > len("spl:")):
            raise ValueError(f"asset: {value!r} is neither 'sol' nor 'spl:<mint>'")
        return super().__new__(cls, value)

    @classmethod
    def spl(cls, mint: str) -> Asset:
        return cls("spl:" + mint)

    @property
    def mint(self) -> str | None:
        return None if self == "sol" else self[len("spl:"):]


Asset.SOL = Asset("sol")


class CacheInvariantError(Exception):
    """Спроба покласти в кеш повних результатів неповний результат — дефект програми."""


# --- Перевірки -------------------------------------------------------------------

_PATH_RE = re.compile(r"^[0-9]+(\.[0-9]+)?$")


def _set(obj: object, name: str, value: Any) -> None:
    object.__setattr__(obj, name, value)


def _int(name: str, value: Any, lo: int | None = None) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name}: expected int, got {value!r}")
    if lo is not None and value < lo:
        raise ValueError(f"{name}: {value} < {lo}")


def _opt_int(name: str, value: Any, lo: int | None = None) -> None:
    if value is not None:
        _int(name, value, lo)


def _num(name: str, value: Any, lo: float) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name}: expected number, got {value!r}")
    if value < lo:
        raise ValueError(f"{name}: {value} < {lo}")


def _bool(name: str, value: Any) -> None:
    if not isinstance(value, bool):
        raise TypeError(f"{name}: expected bool, got {value!r}")


def _str(name: str, value: Any, *, nonempty: bool) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name}: expected str, got {value!r}")
    if nonempty and not value:
        raise ValueError(f"{name}: must not be empty")


def _enum(obj: object, name: str, enum: type[StrEnum]) -> None:
    value = getattr(obj, name)
    if not isinstance(value, enum):
        _set(obj, name, enum(value))  # невідоме значення -> ValueError


def _tuple_of(obj: object, name: str, cls: type) -> tuple:
    value = getattr(obj, name)
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise TypeError(f"{name}: expected a sequence of {cls.__name__}")
    items = tuple(value)
    for item in items:
        if not isinstance(item, cls):
            raise TypeError(f"{name}: expected {cls.__name__}, got {item!r}")
    _set(obj, name, items)
    return items


def _path_key(path: str) -> tuple[int, ...]:
    return tuple(int(part) for part in path.split("."))


# --- Сутності --------------------------------------------------------------------


@dataclass(frozen=True)
class Spend:
    asset: Asset
    amount: int

    def __post_init__(self) -> None:
        _set(self, "asset", Asset(self.asset))
        _int("spend.amount", self.amount, lo=1)


@dataclass(frozen=True)
class Buyer:
    wallet: str
    rank: int
    first_buy_signature: str
    first_buy_slot: int
    first_buy_time: int | None
    received_amount: int
    spent: tuple[Spend, ...]
    programs: tuple[str, ...]
    address_type: AddressType

    def __post_init__(self) -> None:
        _str("buyer.wallet", self.wallet, nonempty=True)
        _int("buyer.rank", self.rank, lo=1)
        _str("buyer.first_buy_signature", self.first_buy_signature, nonempty=True)
        _int("buyer.first_buy_slot", self.first_buy_slot, lo=0)
        _opt_int("buyer.first_buy_time", self.first_buy_time, lo=0)
        _int("buyer.received_amount", self.received_amount, lo=1)
        if not _tuple_of(self, "spent", Spend):
            raise ValueError("buyer.spent: must not be empty")
        _tuple_of(self, "programs", str)
        _enum(self, "address_type", AddressType)


@dataclass(frozen=True)
class Transfer:
    """Вхідний переказ — доказ (FR-001-06). Жодне поле не має умовчання."""

    signature: str
    slot: int
    block_time: int | None
    instruction_path: str
    sender: str
    receiver: str
    asset: Asset
    amount: int
    decimals: int | None
    depth: int

    def __post_init__(self) -> None:
        _str("transfer.signature", self.signature, nonempty=True)
        _int("transfer.slot", self.slot, lo=0)
        _opt_int("transfer.block_time", self.block_time, lo=0)
        _str("transfer.instruction_path", self.instruction_path, nonempty=True)
        if not _PATH_RE.match(self.instruction_path):
            raise ValueError(f"transfer.instruction_path: {self.instruction_path!r} is not 'i' or 'i.j'")
        _str("transfer.sender", self.sender, nonempty=True)
        _str("transfer.receiver", self.receiver, nonempty=True)
        _set(self, "asset", Asset(self.asset))
        _int("transfer.amount", self.amount, lo=1)
        _opt_int("transfer.decimals", self.decimals, lo=0)
        if self.asset == Asset.SOL and self.decimals is not None:
            raise ValueError("transfer.decimals: must be None for sol")
        _int("transfer.depth", self.depth, lo=1)


@dataclass(frozen=True)
class UnexpandedNode:
    wallet: str
    depth: int
    reason: UnexpandedReason
    counterparties_seen: int
    signatures_seen: int
    signatures_truncated: bool

    def __post_init__(self) -> None:
        _str("unexpanded.wallet", self.wallet, nonempty=True)
        _int("unexpanded.depth", self.depth, lo=0)
        _enum(self, "reason", UnexpandedReason)
        _int("unexpanded.counterparties_seen", self.counterparties_seen, lo=0)
        _int("unexpanded.signatures_seen", self.signatures_seen, lo=0)
        _bool("unexpanded.signatures_truncated", self.signatures_truncated)


@dataclass(frozen=True)
class MissingHistory:
    wallet: str
    depth: int
    reason: MissingReason
    detail: str

    def __post_init__(self) -> None:
        _str("missing.wallet", self.wallet, nonempty=True)
        _int("missing.depth", self.depth, lo=0)
        _enum(self, "reason", MissingReason)
        _str("missing.detail", self.detail, nonempty=False)


@dataclass(frozen=True)
class BuyersCompleteness:
    """Чи перелічено історію mint до кінця. `complete` і `reason` узгоджені: причина є лише в неповноти."""

    complete: bool
    reason: MissingReason | None
    detail: str

    def __post_init__(self) -> None:
        _bool("buyers.complete", self.complete)
        if self.reason is not None:
            _enum(self, "reason", MissingReason)
        if self.complete and self.reason is not None:
            raise ValueError("buyers: complete=True must not carry a reason")
        if not self.complete and self.reason is None:
            raise ValueError("buyers: complete=False requires a reason")
        _str("buyers.detail", self.detail, nonempty=False)


_DERIVE_TOKEN = object()


def _missing_order(m: MissingHistory) -> tuple[int, str, str]:
    return (m.depth, m.wallet, m.reason.value)


@dataclass(frozen=True)
class Completeness:
    """Повнота результату. Будується лише через `derive`; `status` — властивість, не поле."""

    missing: tuple[MissingHistory, ...]
    buyers: BuyersCompleteness
    _token: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._token is not _DERIVE_TOKEN:
            raise TypeError("Completeness is built only via Completeness.derive(missing, buyers)")
        if not isinstance(self.buyers, BuyersCompleteness):
            raise TypeError(f"completeness.buyers: expected BuyersCompleteness, got {self.buyers!r}")
        items = _tuple_of(self, "missing", MissingHistory)
        seen: set[tuple[str, MissingReason]] = set()
        for m in items:
            key = (m.wallet, m.reason)
            if key in seen:
                raise ValueError(f"completeness.missing: duplicate entry for {key}")
            seen.add(key)
        _set(self, "missing", tuple(sorted(items, key=_missing_order)))

    @classmethod
    def derive(cls, missing: Iterable[MissingHistory], buyers: BuyersCompleteness) -> Completeness:
        return cls(tuple(missing), buyers, _DERIVE_TOKEN)

    @property
    def status(self) -> CompletenessStatus:
        if not self.missing and self.buyers.complete:
            return CompletenessStatus.COMPLETE
        return CompletenessStatus.INCOMPLETE


@dataclass(frozen=True)
class RunMetadata:
    mint: str
    analyzed_at: int
    wallets_analyzed: int
    config_version: int
    first_buyers_n: int
    funding_depth: int
    counterparty_threshold: int
    max_signatures_per_wallet: int
    collect_spl_inbound: bool
    time_budget_seconds: float
    elapsed_seconds: float
    rpc_calls: int
    transactions_scanned: int
    source: str
    resumed: bool
    served_from_cache: bool

    def __post_init__(self) -> None:
        _str("metadata.mint", self.mint, nonempty=True)
        _str("metadata.source", self.source, nonempty=True)
        for name in ("analyzed_at", "wallets_analyzed", "rpc_calls", "transactions_scanned"):
            _int(f"metadata.{name}", getattr(self, name), lo=0)
        for name in ("config_version", "first_buyers_n", "funding_depth",
                     "counterparty_threshold", "max_signatures_per_wallet"):
            _int(f"metadata.{name}", getattr(self, name), lo=1)
        for name in ("collect_spl_inbound", "resumed", "served_from_cache"):
            _bool(f"metadata.{name}", getattr(self, name))
        _num("metadata.time_budget_seconds", self.time_budget_seconds, lo=0)
        if self.time_budget_seconds <= 0:
            raise ValueError("metadata.time_budget_seconds: must be > 0")
        _num("metadata.elapsed_seconds", self.elapsed_seconds, lo=0)


@dataclass(frozen=True)
class IngestResult:
    metadata: RunMetadata
    completeness: Completeness
    buyers: tuple[Buyer, ...]
    transfers: tuple[Transfer, ...]
    unexpanded: tuple[UnexpandedNode, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.metadata, RunMetadata):
            raise TypeError(f"result.metadata: expected RunMetadata, got {self.metadata!r}")
        if not isinstance(self.completeness, Completeness):
            raise TypeError(
                f"result.completeness: expected Completeness.derive(...), got {self.completeness!r}"
            )
        buyers = _tuple_of(self, "buyers", Buyer)
        transfers = _tuple_of(self, "transfers", Transfer)
        _tuple_of(self, "unexpanded", UnexpandedNode)
        if len({b.wallet for b in buyers}) != len(buyers):
            raise ValueError("result.buyers: duplicate wallet")
        if len({(t.signature, t.instruction_path) for t in transfers}) != len(transfers):
            raise ValueError("result.transfers: duplicate (signature, instruction_path)")
        if self.metadata.wallets_analyzed != len(buyers):
            raise ValueError(
                f"result: metadata.wallets_analyzed={self.metadata.wallets_analyzed} "
                f"!= len(buyers)={len(buyers)}"
            )


@dataclass(frozen=True)
class Rejection:
    """Явна відмова без збою (FR-001-11); повертається значенням, не винятком."""

    kind: RejectKind
    mint: str
    detail: str

    def __post_init__(self) -> None:
        _enum(self, "kind", RejectKind)
        _str("rejection.mint", self.mint, nonempty=False)
        _str("rejection.detail", self.detail, nonempty=False)


IngestOutcome = IngestResult | Rejection


# --- Ключі порядку (research R-6): лише з даних транзакцій -----------------------


def buyer_sort_key(buyer: Buyer) -> tuple[int, str, str]:
    return (buyer.first_buy_slot, buyer.first_buy_signature, buyer.wallet)


def transfer_sort_key(transfer: Transfer) -> tuple[int, str, tuple[int, ...]]:
    """`(slot, signature, instruction_path)`; шлях порівнюється числово: "2" < "2.1" < "10"."""
    return (transfer.slot, transfer.signature, _path_key(transfer.instruction_path))
