# impl: FR-001-01, FR-001-02
"""Правило купівлі (research R-2): хто купив цільовий mint в одній розібраній транзакції.

Що робить: `detect_purchases(parsed, mint)` повертає `Purchase` для кожного власника O, у якого
`token_delta(O, mint) > 0` **і** (`spent_sol(O) > 0` **або** ∃ m≠mint: `token_delta(O, m) < 0`), де

    spent_sol(O) = −Δsol(O) − fee·[O = fee_payer] − created_accounts_lamports·[O = fee_payer]

Поправка на комісію й ренту створених рахунків прибирає хибні купівлі: отримувач ейрдропу,
що сам платив за свій ATA; творець токена (`mintTo` з оплатою ренти); простий переказ, де
отримувач — fee payer. Списку DEX немає (Q2): невідомий лаунчпад не дає тихого «нуль покупців».
Пул/PDA, що отримує mint при продажу користувачем, — теж купівля, позначена `off_curve`
(відсікання хабів — окремий модуль, принцип VI).

Як користуватись: на вхід лише `ParsedTx` (не `CorruptRecord` — це рішення викликача);
`failed=True` → порожній список. Порядок результату — за `wallet` (детерміновано).
`spent`: спершу `sol` (якщо `spent_sol > 0`), далі SPL-активи з від'ємною дельтою за зростанням
`spl:<mint>`. WSOL — звичайний SPL-актив (`spl:So11…112`), у SOL не перетворюється.
Залежить від: `parse.ParsedTx` (дельти), `addresses.address_type`, `model.Spend`. Мережі не торкається.
"""

from __future__ import annotations

from dataclasses import dataclass

from unmask.ingest.addresses import address_type
from unmask.ingest.model import AddressType, Asset, Spend
from unmask.ingest.parse import ParsedTx


@dataclass(frozen=True)
class Purchase:
    """Проміжний запис купівлі (data-model: з нього `buyers.py` будує `Buyer` після відбору перших N)."""

    wallet: str
    signature: str
    slot: int
    block_time: int | None
    received_amount: int
    spent: tuple[Spend, ...]
    programs: tuple[str, ...]
    address_type: AddressType

    def __post_init__(self) -> None:
        if not isinstance(self.wallet, str) or not self.wallet:
            raise ValueError(f"purchase.wallet: expected non-empty str, got {self.wallet!r}")
        if isinstance(self.received_amount, bool) or not isinstance(self.received_amount, int):
            raise TypeError(f"purchase.received_amount: expected int, got {self.received_amount!r}")
        if self.received_amount < 1:
            raise ValueError(f"purchase.received_amount: {self.received_amount} < 1")
        spent = tuple(self.spent)
        if not spent:
            raise ValueError("purchase.spent: must not be empty")
        if not all(isinstance(s, Spend) for s in spent):
            raise TypeError(f"purchase.spent: expected Spend items, got {spent!r}")
        object.__setattr__(self, "spent", spent)
        object.__setattr__(self, "programs", tuple(self.programs))
        object.__setattr__(self, "address_type", AddressType(self.address_type))


def spent_sol(parsed: ParsedTx, owner: str) -> int:
    """Витрачені власником lamports без комісії й ренти створених рахунків (їх несе fee payer)."""
    spent = -parsed.sol_delta(owner)
    if owner == parsed.fee_payer:
        spent -= parsed.fee + parsed.created_accounts_lamports
    return spent


def detect_purchases(parsed: ParsedTx, mint: str) -> list[Purchase]:
    """Купівлі `mint` у транзакції `parsed` за правилом R-2, по одній на власника, за зростанням `wallet`."""
    if not isinstance(parsed, ParsedTx):
        raise TypeError(f"detect_purchases: expected ParsedTx, got {type(parsed).__name__}")
    if parsed.failed:
        return []
    balances = parsed.pre_token_balances + parsed.post_token_balances
    owners = sorted({b.owner for b in balances if b.mint == mint and b.owner is not None})
    other_mints = sorted({b.mint for b in balances if b.mint != mint})

    purchases = []
    for owner in owners:
        received = parsed.token_delta(owner, mint)
        if received <= 0:
            continue
        spent: list[Spend] = []
        sol = spent_sol(parsed, owner)
        if sol > 0:
            spent.append(Spend(Asset.SOL, sol))
        for other in other_mints:
            delta = parsed.token_delta(owner, other)
            if delta < 0:
                spent.append(Spend(Asset.spl(other), -delta))
        if not spent:
            continue
        purchases.append(Purchase(
            wallet=owner, signature=parsed.signature, slot=parsed.slot, block_time=parsed.block_time,
            received_amount=received, spent=tuple(spent), programs=parsed.programs,
            address_type=address_type(owner),
        ))
    return purchases
