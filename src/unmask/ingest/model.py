# impl: FR-001-06, FR-001-09, FR-001-10, FR-001-14, FR-002-19, FR-002-21
"""Типи результату збору (data-model.md) та інваріант повноти.

Усі сутності незмінні (`frozen=True`) і перевіряють себе при побудові: некоректний
стан падає гучно (TypeError/ValueError), а не потрапляє в результат.

Ключовий інваріант (FR-001-09, FR-001-10, принцип V): статус повноти не
зберігається, а обчислюється з даних — `complete` тоді й лише тоді, коли
`missing` порожній і `buyers.complete`. `Completeness` будується лише через
`Completeness.derive`; поле `status`, яке можна було б виставити вручну, не існує.

Розширення фічі 002 для swap-and-send (T-044, FR-002-19, FR-002-21; research R-2…R-4):
`DelegatedLink`, `UnpairedCandidate`, `DelegatedAnalysis` і поле `IngestResult.delegated`.
Повнота аналізу теж ПОХІДНА: `DelegatedAnalysis.derive(links, unpaired, buyers)` копіює
`complete`/`reason`/`detail` з `BuyersCompleteness` (те саме вікно транзакцій, той самий розбір),
тож другого джерела істини немає. Результат, побудований без аналізу, несе чесне умовчання
`DelegatedAnalysis.NOT_ANALYZED` (`complete=False`, `reason="not_analyzed"`), а не «зв'язків немає».
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, ClassVar, Iterable


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


class DelegatedSide(StrEnum):
    PAYER = "payer"
    RECEIVER = "receiver"


NOT_ANALYZED_REASON = "not_analyzed"
"""`DelegatedAnalysis.reason` результату, побудованого без аналізу swap-and-send (не `MissingReason`)."""


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


def _opt_int(name: str, value: Any, lo: int | None = None, hi: int | None = None) -> None:
    if value is not None:
        _int(name, value, lo)
        if hi is not None and value > hi:
            raise ValueError(f"{name}: {value} > {hi}")


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
        _opt_int("transfer.decimals", self.decimals, lo=0, hi=255)  # SPL decimals — u8
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


# --- Розширення 002: swap-and-send (T-044; data-model «Розширення фічі 001») -------

_UNPAIRED_DETAIL_RE = re.compile(r"payers=([0-9]+) receivers=([0-9]+)")  # лише через fullmatch: `$` приймає "\n"


def _unpaired_counts(detail: str) -> tuple[int, int]:
    match = _UNPAIRED_DETAIL_RE.fullmatch(detail)
    if match is None:
        raise ValueError(f"unpaired.detail: {detail!r} is not 'payers=<p> receivers=<r>'")
    return int(match.group(1)), int(match.group(2))


@dataclass(frozen=True)
class DelegatedLink:
    """Делегована купівля (FR-002-15): платник витратив, отримувач отримав токен. Одна на транзакцію."""

    signature: str
    slot: int
    block_time: int | None
    payer: str
    receiver: str

    def __post_init__(self) -> None:
        _str("delegated.link.signature", self.signature, nonempty=True)
        _int("delegated.link.slot", self.slot, lo=0)
        _opt_int("delegated.link.block_time", self.block_time, lo=0)
        _str("delegated.link.payer", self.payer, nonempty=True)
        _str("delegated.link.receiver", self.receiver, nonempty=True)
        if self.payer == self.receiver:
            raise ValueError(f"delegated.link: payer == receiver ({self.payer})")


@dataclass(frozen=True)
class UnpairedCandidate:
    """Учасник неоднозначної транзакції (FR-002-16): не 1:1, тож пару не вгадано.

    `detail == "payers=<p> receivers=<r>"` з p ≥ 1, r ≥ 1 і (p, r) != (1, 1) — інакше це або
    зв'язок (1:1), або взагалі не кандидат (одна сторона порожня; research R-2).
    """

    signature: str
    slot: int
    block_time: int | None
    wallet: str
    side: DelegatedSide
    detail: str

    def __post_init__(self) -> None:
        _str("delegated.unpaired.signature", self.signature, nonempty=True)
        _int("delegated.unpaired.slot", self.slot, lo=0)
        _opt_int("delegated.unpaired.block_time", self.block_time, lo=0)
        _str("delegated.unpaired.wallet", self.wallet, nonempty=True)
        _enum(self, "side", DelegatedSide)
        _str("delegated.unpaired.detail", self.detail, nonempty=True)
        payers, receivers = _unpaired_counts(self.detail)
        if payers < 1 or receivers < 1:
            raise ValueError(f"unpaired.detail: {self.detail!r} — both sides must be non-empty")
        if (payers, receivers) == (1, 1):
            raise ValueError(f"unpaired.detail: {self.detail!r} — a 1:1 transaction is a link, not a candidate")


def link_sort_key(link: DelegatedLink) -> tuple[int, str, str, str]:
    return (link.slot, link.signature, link.payer, link.receiver)


def unpaired_sort_key(cand: UnpairedCandidate) -> tuple[int, str, str, str]:
    return (cand.slot, cand.signature, cand.wallet, cand.side.value)


def _check_links(links: tuple[DelegatedLink, ...]) -> None:
    seen: set[str] = set()
    for link in links:
        if link.signature in seen:
            raise ValueError(f"delegated.links: duplicate link for signature {link.signature}")
        seen.add(link.signature)


def _check_unpaired(unpaired: tuple[UnpairedCandidate, ...], link_signatures: set[str]) -> None:
    """Без дублів за `(signature, wallet, side)`; кожна транзакція — повна група за своїм `detail`."""
    keys: set[tuple[str, str, DelegatedSide]] = set()
    groups: dict[str, list[UnpairedCandidate]] = {}
    for cand in unpaired:
        key = (cand.signature, cand.wallet, cand.side)
        if key in keys:
            raise ValueError(f"delegated.unpaired: duplicate candidate {key}")
        keys.add(key)
        groups.setdefault(cand.signature, []).append(cand)
    for signature, group in groups.items():
        if signature in link_signatures:
            raise ValueError(f"delegated: signature {signature} is both a link and unpaired candidates")
        wallets = [c.wallet for c in group]
        if len(set(wallets)) != len(wallets):  # платник: Δ(M) == 0, отримувач: Δ(M) > 0 — сторони не перетинаються
            raise ValueError(f"delegated.unpaired: a wallet of {signature} is on both sides")
        first = group[0]
        if any((c.slot, c.block_time, c.detail) != (first.slot, first.block_time, first.detail) for c in group):
            raise ValueError(f"delegated.unpaired: candidates of {signature} disagree on slot/block_time/detail")
        payers, receivers = _unpaired_counts(first.detail)
        got_p = sum(1 for c in group if c.side is DelegatedSide.PAYER)
        got_r = len(group) - got_p
        if (got_p, got_r) != (payers, receivers):
            raise ValueError(
                f"delegated.unpaired: {signature} says {first.detail!r}, "
                f"but has payers={got_p} receivers={got_r}"
            )


@dataclass(frozen=True)
class DelegatedAnalysis:
    """Аналіз swap-and-send (FR-002-19). Лише `derive(...)` або `NOT_ANALYZED`; повнота похідна."""

    links: tuple[DelegatedLink, ...]
    unpaired: tuple[UnpairedCandidate, ...]
    complete: bool
    reason: MissingReason | str | None
    detail: str
    _token: object = field(default=None, repr=False, compare=False)

    NOT_ANALYZED: ClassVar[DelegatedAnalysis]

    def __post_init__(self) -> None:
        # Спершу інваріанти (найконкретніша помилка), потім — спосіб побудови.
        _bool("delegated.complete", self.complete)
        if self.reason is not None:
            if self.reason == NOT_ANALYZED_REASON:
                _set(self, "reason", NOT_ANALYZED_REASON)
            else:
                _enum(self, "reason", MissingReason)
        if self.complete and self.reason is not None:
            raise ValueError("delegated: complete=True must not carry a reason")
        if not self.complete and self.reason is None:
            raise ValueError("delegated: complete=False requires a reason")
        _str("delegated.detail", self.detail, nonempty=False)
        links = _tuple_of(self, "links", DelegatedLink)
        unpaired = _tuple_of(self, "unpaired", UnpairedCandidate)
        if self.reason == NOT_ANALYZED_REASON and (links or unpaired or self.detail != _NOT_ANALYZED_DETAIL):
            raise ValueError("delegated: reason='not_analyzed' admits no links/unpaired and only its own detail")
        _check_links(links)
        _check_unpaired(unpaired, {link.signature for link in links})
        _set(self, "links", tuple(sorted(links, key=link_sort_key)))
        _set(self, "unpaired", tuple(sorted(unpaired, key=unpaired_sort_key)))
        if self._token is not _DERIVE_TOKEN:
            raise TypeError(
                "DelegatedAnalysis is built only via DelegatedAnalysis.derive(links, unpaired, buyers) "
                "or DelegatedAnalysis.NOT_ANALYZED"
            )

    @classmethod
    def derive(
        cls,
        links: Iterable[DelegatedLink],
        unpaired: Iterable[UnpairedCandidate],
        buyers: BuyersCompleteness,
    ) -> DelegatedAnalysis:
        """Повнота = повнота перелічення покупців: той самий набір транзакцій mint, той самий розбір."""
        if not isinstance(buyers, BuyersCompleteness):
            raise TypeError(f"delegated.derive: expected BuyersCompleteness, got {buyers!r}")
        for name, value in (("links", links), ("unpaired", unpaired)):
            if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
                raise TypeError(f"delegated.derive: {name} must be a sequence")
        return cls(tuple(links), tuple(unpaired), buyers.complete, buyers.reason, buyers.detail, _DERIVE_TOKEN)

    @property
    def analyzed(self) -> bool:
        return self.reason != NOT_ANALYZED_REASON


_NOT_ANALYZED_DETAIL = "swap-and-send analysis was not performed for this result"
DelegatedAnalysis.NOT_ANALYZED = DelegatedAnalysis(
    (), (), False, NOT_ANALYZED_REASON, _NOT_ANALYZED_DETAIL, _DERIVE_TOKEN
)


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
    delegated: DelegatedAnalysis = DelegatedAnalysis.NOT_ANALYZED

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
        if not isinstance(self.delegated, DelegatedAnalysis):
            raise TypeError(f"result.delegated: expected DelegatedAnalysis, got {self.delegated!r}")
        if self.delegated.analyzed:  # FR-002-19: повнота аналізу дзеркалить повноту перелічення
            b, d = self.completeness.buyers, self.delegated
            if (d.complete, d.reason, d.detail) != (b.complete, b.reason, b.detail):
                raise ValueError(
                    f"result.delegated: complete/reason/detail=({d.complete}, {d.reason!r}, {d.detail!r}) "
                    f"!= completeness.buyers ({b.complete}, {b.reason!r}, {b.detail!r})"
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
