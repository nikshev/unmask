# impl: FR-001-06, FR-001-14, FR-002-20
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

`from_dict(data) -> IngestOutcome` (T-026, FR-002-20) — обернене до `to_dict`: читає документ за
контрактом 001 назад у типи моделі (`IngestResult`/`Rejection`), так що `from_dict(to_dict(o)) == o`
і `to_dict(from_dict(d)) == d`. Читач СУВОРИЙ: об'єкт має рівно ключі схеми (зайвий чи відсутній —
`ValueError` з назвою ключа), значення — свого JSON-типу (чужий — `TypeError`, зокрема `bool` на
місці числа, `list` на місці `tuple`-поля не підміняється `dict`/`str`), перелічення поза значеннями —
`ValueError`; усі інваріанти моделі 001 (конструктори типів) спрацьовують як є; `completeness.status`
не береться на віру, а звіряється з виведеним (`Completeness.status`). Поле `delegated` на цьому
етапі НЕ існує — документ із ним відхиляється як невідоме поле (його додає T-044, схема 1.1).
Читач не перевіряє документ проти JSON-схеми (шаблони адрес, межі `depth`/`first_buyers_n` — справа
тестів схеми) і нічого не пише; вхід не змінюється, структури з ним не діляться.

Залежить від: `model`. Не валідує проти схеми сам (схема — еталон у тестах) і не
пише на диск. Невідомий тип результату — `TypeError`, а не мовчазне `{}`.
"""

from __future__ import annotations

import json
import math
from typing import Any, Mapping

from unmask.ingest.model import (
    AddressType,
    Asset,
    Buyer,
    BuyersCompleteness,
    Completeness,
    IngestOutcome,
    IngestResult,
    MissingHistory,
    MissingReason,
    RejectKind,
    Rejection,
    RunMetadata,
    Spend,
    Transfer,
    UnexpandedNode,
    UnexpandedReason,
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


# --- from_dict: читання контракту (T-026) -----------------------------------------------------


def _obj(where: str, value: Any, keys: tuple[str, ...]) -> dict[str, Any]:
    """Об'єкт із РІВНО цими ключами; ключі лише рядки."""
    if not isinstance(value, dict):
        raise TypeError(f"{where}: expected object, got {type(value).__name__}")
    unknown = sorted(str(k) for k in value if k not in keys)
    missing = [k for k in keys if k not in value]
    if unknown or missing:
        raise ValueError(f"{where}: unknown keys {unknown}, missing keys {missing}")
    return value


def _list(where: str, value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise TypeError(f"{where}: expected array, got {type(value).__name__}")
    return value


def _finite(where: str, value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{where}: non-finite number {value!r}")
    return value


def _enum_value(where: str, value: Any, enum: type) -> Any:
    if not isinstance(value, str):
        raise TypeError(f"{where}: expected str, got {value!r}")
    try:
        return enum(value)
    except ValueError:
        raise ValueError(f"{where}: {value!r} is not a valid {enum.__name__}") from None


_SPEND_KEYS = ("asset", "amount")
_BUYER_KEYS = ("wallet", "rank", "first_buy_signature", "first_buy_slot", "first_buy_time",
               "received_amount", "spent", "programs", "address_type")
_TRANSFER_KEYS = ("signature", "slot", "block_time", "instruction_path", "sender", "receiver",
                  "asset", "amount", "decimals", "depth")
_UNEXPANDED_KEYS = ("wallet", "depth", "reason", "counterparties_seen", "signatures_seen",
                    "signatures_truncated")
_MISSING_KEYS = ("wallet", "depth", "reason", "detail")
_BUYERS_COMPLETENESS_KEYS = ("complete", "reason", "detail")
_COMPLETENESS_KEYS = ("status", "missing", "buyers")
_METADATA_KEYS = (
    "mint", "analyzed_at", "wallets_analyzed", "config_version", "first_buyers_n", "funding_depth",
    "counterparty_threshold", "max_signatures_per_wallet", "collect_spl_inbound",
    "time_budget_seconds", "elapsed_seconds", "rpc_calls", "transactions_scanned", "source",
    "resumed", "served_from_cache",
)
_RESULT_KEYS = ("metadata", "completeness", "buyers", "transfers", "unexpanded")
_REJECTION_KEYS = ("kind", "mint", "detail")


def _read_spend(where: str, data: Any) -> Spend:
    d = _obj(where, data, _SPEND_KEYS)
    return Spend(asset=d["asset"], amount=d["amount"])


def _read_buyer(where: str, data: Any) -> Buyer:
    d = _obj(where, data, _BUYER_KEYS)
    spent = _list(f"{where}.spent", d["spent"])
    programs = _list(f"{where}.programs", d["programs"])
    return Buyer(
        wallet=d["wallet"],
        rank=d["rank"],
        first_buy_signature=d["first_buy_signature"],
        first_buy_slot=d["first_buy_slot"],
        first_buy_time=d["first_buy_time"],
        received_amount=d["received_amount"],
        spent=tuple(_read_spend(f"{where}.spent[{i}]", s) for i, s in enumerate(spent)),
        programs=tuple(programs),
        address_type=_enum_value(f"{where}.address_type", d["address_type"], AddressType),
    )


def _read_transfer(where: str, data: Any) -> Transfer:
    d = _obj(where, data, _TRANSFER_KEYS)
    return Transfer(**{k: d[k] for k in _TRANSFER_KEYS})


def _read_unexpanded(where: str, data: Any) -> UnexpandedNode:
    d = _obj(where, data, _UNEXPANDED_KEYS)
    return UnexpandedNode(
        wallet=d["wallet"],
        depth=d["depth"],
        reason=_enum_value(f"{where}.reason", d["reason"], UnexpandedReason),
        counterparties_seen=d["counterparties_seen"],
        signatures_seen=d["signatures_seen"],
        signatures_truncated=d["signatures_truncated"],
    )


def _read_missing(where: str, data: Any) -> MissingHistory:
    d = _obj(where, data, _MISSING_KEYS)
    return MissingHistory(
        wallet=d["wallet"],
        depth=d["depth"],
        reason=_enum_value(f"{where}.reason", d["reason"], MissingReason),
        detail=d["detail"],
    )


def _read_completeness(where: str, data: Any) -> Completeness:
    d = _obj(where, data, _COMPLETENESS_KEYS)
    b = _obj(f"{where}.buyers", d["buyers"], _BUYERS_COMPLETENESS_KEYS)
    reason = b["reason"]
    buyers = BuyersCompleteness(
        complete=b["complete"],
        reason=None if reason is None else _enum_value(f"{where}.buyers.reason", reason, MissingReason),
        detail=b["detail"],
    )
    missing = tuple(
        _read_missing(f"{where}.missing[{i}]", m) for i, m in enumerate(_list(f"{where}.missing", d["missing"]))
    )
    completeness = Completeness.derive(missing, buyers)
    status = d["status"]
    if not isinstance(status, str):
        raise TypeError(f"{where}.status: expected str, got {status!r}")
    if status != completeness.status.value:
        raise ValueError(
            f"{where}.status: document says {status!r}, but missing[] and buyers.complete "
            f"give {completeness.status.value!r}"
        )
    return completeness


def _read_metadata(where: str, data: Any) -> RunMetadata:
    d = _obj(where, data, _METADATA_KEYS)
    for name in ("time_budget_seconds", "elapsed_seconds"):
        _finite(f"{where}.{name}", d[name])
    return RunMetadata(**{k: d[k] for k in _METADATA_KEYS})


def from_dict(data: Mapping[str, Any]) -> IngestOutcome:
    """Документ за контрактом 001 → `IngestResult` або `Rejection` (обернене до `to_dict`)."""
    if not isinstance(data, dict):
        raise TypeError(f"from_dict: expected dict, got {type(data).__name__}")
    if "kind" in data:
        d = _obj("rejection", data, _REJECTION_KEYS)
        return Rejection(
            kind=_enum_value("rejection.kind", d["kind"], RejectKind), mint=d["mint"], detail=d["detail"]
        )
    d = _obj("result", data, _RESULT_KEYS)
    return IngestResult(
        metadata=_read_metadata("metadata", d["metadata"]),
        completeness=_read_completeness("completeness", d["completeness"]),
        buyers=tuple(_read_buyer(f"buyers[{i}]", b) for i, b in enumerate(_list("buyers", d["buyers"]))),
        transfers=tuple(
            _read_transfer(f"transfers[{i}]", t) for i, t in enumerate(_list("transfers", d["transfers"]))
        ),
        unexpanded=tuple(
            _read_unexpanded(f"unexpanded[{i}]", u) for i, u in enumerate(_list("unexpanded", d["unexpanded"]))
        ),
    )
