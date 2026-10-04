# impl: FR-001-06, FR-001-14
"""Серіалізація результату збору за `contracts/ingest-result.schema.json` (T-020).

Що робить: `to_dict(outcome) -> dict` відображає `IngestResult` або `Rejection` у структуру,
валідну проти схеми контракту; `to_json(outcome) -> str` — той самий вміст як канонічний JSON
(`sort_keys=True`, `ensure_ascii=False`, без зайвих пробілів, `allow_nan=False`). Вихід
детермінований і обертається без втрат: `json.loads(to_json(o)) == to_dict(o)`.

Як користуватись: `to_json(service.collect(mint))`. Для `Rejection` — об'єкт
`{"kind", "mint", "detail"}`; для `IngestResult` — `{"metadata", "completeness", "buyers",
"transfers", "unexpanded"}`.

Правила відображення (явне, поле за полем — НЕ `dataclasses.asdict`, бо схема має
`additionalProperties: false`, а `Completeness` несе службове `_token`):
- `Completeness.status` — властивість, не поле моделі; у вихід кладеться явно (`.value`);
- `StrEnum` → рядкове значення, `Asset` → точний `str`, `tuple` → `list`, `None` → `null`
  (там, де його дозволяє схема: `first_buy_time`, `block_time`, `decimals`, `buyers.reason`);
- порядок елементів списків — канонічний порядок моделі (buyers/transfers/missing/unexpanded),
  тут нічого не пересортовується; порядок ключів фіксує `to_json`.

Залежить від: `model`. Не валідує проти схеми сам (схема — еталон у тестах) і не
пише на диск. Невідомий тип результату — `TypeError`, а не мовчазне `{}`.
"""

from __future__ import annotations

import json
from typing import Any

from unmask.ingest.model import (
    Buyer,
    BuyersCompleteness,
    Completeness,
    IngestResult,
    MissingHistory,
    Rejection,
    RunMetadata,
    Spend,
    Transfer,
    UnexpandedNode,
)


def _spend(spend: Spend) -> dict[str, Any]:
    return {"asset": str(spend.asset), "amount": spend.amount}


def _buyer(buyer: Buyer) -> dict[str, Any]:
    return {
        "wallet": buyer.wallet,
        "rank": buyer.rank,
        "first_buy_signature": buyer.first_buy_signature,
        "first_buy_slot": buyer.first_buy_slot,
        "first_buy_time": buyer.first_buy_time,
        "received_amount": buyer.received_amount,
        "spent": [_spend(s) for s in buyer.spent],
        "programs": list(buyer.programs),
        "address_type": buyer.address_type.value,
    }


def _transfer(transfer: Transfer) -> dict[str, Any]:
    return {
        "signature": transfer.signature,
        "slot": transfer.slot,
        "block_time": transfer.block_time,
        "instruction_path": transfer.instruction_path,
        "sender": transfer.sender,
        "receiver": transfer.receiver,
        "asset": str(transfer.asset),
        "amount": transfer.amount,
        "decimals": transfer.decimals,
        "depth": transfer.depth,
    }


def _unexpanded(node: UnexpandedNode) -> dict[str, Any]:
    return {
        "wallet": node.wallet,
        "depth": node.depth,
        "reason": node.reason.value,
        "counterparties_seen": node.counterparties_seen,
        "signatures_seen": node.signatures_seen,
        "signatures_truncated": node.signatures_truncated,
    }


def _missing(item: MissingHistory) -> dict[str, Any]:
    return {
        "wallet": item.wallet,
        "depth": item.depth,
        "reason": item.reason.value,
        "detail": item.detail,
    }


def _buyers_completeness(buyers: BuyersCompleteness) -> dict[str, Any]:
    return {
        "complete": buyers.complete,
        "reason": None if buyers.reason is None else buyers.reason.value,
        "detail": buyers.detail,
    }


def _completeness(completeness: Completeness) -> dict[str, Any]:
    return {
        "status": completeness.status.value,
        "missing": [_missing(m) for m in completeness.missing],
        "buyers": _buyers_completeness(completeness.buyers),
    }


def _metadata(meta: RunMetadata) -> dict[str, Any]:
    return {
        "mint": meta.mint,
        "analyzed_at": meta.analyzed_at,
        "wallets_analyzed": meta.wallets_analyzed,
        "config_version": meta.config_version,
        "first_buyers_n": meta.first_buyers_n,
        "funding_depth": meta.funding_depth,
        "counterparty_threshold": meta.counterparty_threshold,
        "max_signatures_per_wallet": meta.max_signatures_per_wallet,
        "collect_spl_inbound": meta.collect_spl_inbound,
        "time_budget_seconds": meta.time_budget_seconds,
        "elapsed_seconds": meta.elapsed_seconds,
        "rpc_calls": meta.rpc_calls,
        "transactions_scanned": meta.transactions_scanned,
        "source": meta.source,
        "resumed": meta.resumed,
        "served_from_cache": meta.served_from_cache,
    }


def to_dict(outcome: IngestResult | Rejection) -> dict[str, Any]:
    """`IngestOutcome` → словник за схемою контракту (нова незалежна структура при кожному виклику)."""
    if isinstance(outcome, Rejection):
        return {"kind": outcome.kind.value, "mint": outcome.mint, "detail": outcome.detail}
    if isinstance(outcome, IngestResult):
        return {
            "metadata": _metadata(outcome.metadata),
            "completeness": _completeness(outcome.completeness),
            "buyers": [_buyer(b) for b in outcome.buyers],
            "transfers": [_transfer(t) for t in outcome.transfers],
            "unexpanded": [_unexpanded(u) for u in outcome.unexpanded],
        }
    raise TypeError(f"serialize: expected IngestResult or Rejection, got {type(outcome).__name__}")


def to_json(outcome: IngestResult | Rejection) -> str:
    """Канонічний JSON: ключі відсортовані, кирилиця не екранується, без зайвих пробілів."""
    return json.dumps(
        to_dict(outcome), sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )
