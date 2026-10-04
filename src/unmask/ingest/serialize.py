# impl: FR-001-06, FR-001-14, FR-002-20, FR-002-21
"""Серіалізація результату збору за `contracts/ingest-result.schema.json` (T-020).

Що робить: `to_dict(outcome) -> dict` відображає `IngestResult` або `Rejection` у структуру,
валідну проти схеми контракту; `to_json(outcome) -> str` — той самий вміст як канонічний JSON
(`sort_keys=True`, `ensure_ascii=False`, без зайвих пробілів, `allow_nan=False`). Вихід
детермінований і обертається без втрат: `json.loads(to_json(o)) == to_dict(o)`.

Як користуватись: `to_json(service.collect(mint))`. Для `Rejection` — об'єкт
`{"kind", "mint", "detail"}`; для `IngestResult` — `{"metadata", "completeness", "buyers",
"transfers", "unexpanded", "delegated"}` (схема 1.1, T-044: ключ `delegated` емітується ЗАВЖДИ,
зокрема `NOT_ANALYZED`; решта ключів і значень — побайтово ті самі, що в схемі 1.0, SC-006).

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
не береться на віру, а звіряється з виведеним (`Completeness.status`). Ключ `delegated`
(схема 1.1, T-044) необов'язковий: відсутній → `DelegatedAnalysis.NOT_ANALYZED`; присутній —
`complete`/`reason`/`detail` не беруться на віру, а звіряються з `DelegatedAnalysis.derive(links, unpaired,
completeness.buyers)` (або з константою `NOT_ANALYZED` при `reason == "not_analyzed"`), а `links`/
`unpaired` мають бути в канонічному порядку контракту — інакше `ValueError` (жодного мовчазного
пересортування, тож `to_dict(from_dict(d)) == d`).
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
    DelegatedAnalysis,
    DelegatedLink,
    DelegatedSide,
    IngestOutcome,
    IngestResult,
    MissingHistory,
    MissingReason,
    NOT_ANALYZED_REASON,
    RejectKind,
    Rejection,
    RunMetadata,
    Spend,
    Transfer,
    UnexpandedNode,
    UnexpandedReason,
    UnpairedCandidate,
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


def _link(link: DelegatedLink) -> dict[str, Any]:
    return {
        "signature": link.signature,
        "slot": link.slot,
        "block_time": link.block_time,
        "payer": link.payer,
        "receiver": link.receiver,
    }


def _unpaired(cand: UnpairedCandidate) -> dict[str, Any]:
    return {
        "signature": cand.signature,
        "slot": cand.slot,
        "block_time": cand.block_time,
        "wallet": cand.wallet,
        "side": cand.side.value,
        "detail": cand.detail,
    }


def _delegated(analysis: DelegatedAnalysis) -> dict[str, Any]:
    reason = analysis.reason
    return {
        "links": [_link(link) for link in analysis.links],
        "unpaired": [_unpaired(c) for c in analysis.unpaired],
        "complete": analysis.complete,
        "reason": reason.value if isinstance(reason, MissingReason) else reason,
        "detail": analysis.detail,
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
            "delegated": _delegated(outcome.delegated),
        }
    raise TypeError(f"serialize: expected IngestResult or Rejection, got {type(outcome).__name__}")


def to_json(outcome: IngestResult | Rejection) -> str:
    """Канонічний JSON: ключі відсортовані, кирилиця не екранується, без зайвих пробілів."""
    return json.dumps(
        to_dict(outcome), sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


# --- from_dict: читання контракту (T-026) -----------------------------------------------------


def _obj(where: str, value: Any, keys: tuple[str, ...], optional: tuple[str, ...] = ()) -> dict[str, Any]:
    """Об'єкт із РІВНО цими ключами (плюс, можливо, `optional`); ключі лише рядки."""
    if not isinstance(value, dict):
        raise TypeError(f"{where}: expected object, got {type(value).__name__}")
    unknown = sorted(str(k) for k in value if k not in keys and k not in optional)
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
_RESULT_OPTIONAL_KEYS = ("delegated",)  # схема 1.1: необов'язковий
_DELEGATED_KEYS = ("links", "unpaired", "complete", "reason", "detail")
_LINK_KEYS = ("signature", "slot", "block_time", "payer", "receiver")
_UNPAIRED_KEYS = ("signature", "slot", "block_time", "wallet", "side", "detail")
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


def _read_link(where: str, data: Any) -> DelegatedLink:
    d = _obj(where, data, _LINK_KEYS)
    return DelegatedLink(**{k: d[k] for k in _LINK_KEYS})


def _read_unpaired(where: str, data: Any) -> UnpairedCandidate:
    d = _obj(where, data, _UNPAIRED_KEYS)
    return UnpairedCandidate(
        signature=d["signature"],
        slot=d["slot"],
        block_time=d["block_time"],
        wallet=d["wallet"],
        side=_enum_value(f"{where}.side", d["side"], DelegatedSide),
        detail=d["detail"],
    )


def _read_delegated(where: str, data: Any, buyers: BuyersCompleteness) -> DelegatedAnalysis:
    """`delegated` → `DelegatedAnalysis`; повнота звіряється з виведеною, порядок — канонічний."""
    d = _obj(where, data, _DELEGATED_KEYS)
    links = tuple(_read_link(f"{where}.links[{i}]", x) for i, x in enumerate(_list(f"{where}.links", d["links"])))
    unpaired = tuple(
        _read_unpaired(f"{where}.unpaired[{i}]", x) for i, x in enumerate(_list(f"{where}.unpaired", d["unpaired"]))
    )
    complete, reason, detail = d["complete"], d["reason"], d["detail"]
    if not isinstance(complete, bool):
        raise TypeError(f"{where}.complete: expected bool, got {complete!r}")
    if reason is not None and not isinstance(reason, str):
        raise TypeError(f"{where}.reason: expected str or null, got {reason!r}")
    if not isinstance(detail, str):
        raise TypeError(f"{where}.detail: expected str, got {detail!r}")
    if reason is not None and reason != NOT_ANALYZED_REASON and reason not in {r.value for r in MissingReason}:
        raise ValueError(f"{where}.reason: {reason!r} is neither a MissingReason nor {NOT_ANALYZED_REASON!r}")
    if reason == NOT_ANALYZED_REASON:
        analysis = DelegatedAnalysis.NOT_ANALYZED
    else:
        analysis = DelegatedAnalysis.derive(links, unpaired, buyers)
    derived = _delegated(analysis)
    for key, value in (("complete", complete), ("reason", reason), ("detail", detail)):
        if derived[key] != value:
            raise ValueError(
                f"{where}.{key}: document says {value!r}, but "
                + ("the not_analyzed constant has" if reason == NOT_ANALYZED_REASON else "completeness.buyers gives")
                + f" {derived[key]!r}"
            )
    if links != analysis.links or unpaired != analysis.unpaired:
        if reason == NOT_ANALYZED_REASON:
            raise ValueError(f"{where}: reason='not_analyzed' admits no links/unpaired")
        raise ValueError(f"{where}: links/unpaired are not in the contract order (slot, signature, ...)")
    return analysis


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
    d = _obj("result", data, _RESULT_KEYS, _RESULT_OPTIONAL_KEYS)
    metadata = _read_metadata("metadata", d["metadata"])
    completeness = _read_completeness("completeness", d["completeness"])
    delegated = (
        _read_delegated("delegated", d["delegated"], completeness.buyers)
        if "delegated" in d
        else DelegatedAnalysis.NOT_ANALYZED
    )
    return IngestResult(
        metadata=metadata,
        completeness=completeness,
        buyers=tuple(_read_buyer(f"buyers[{i}]", b) for i, b in enumerate(_list("buyers", d["buyers"]))),
        transfers=tuple(
            _read_transfer(f"transfers[{i}]", t) for i, t in enumerate(_list("transfers", d["transfers"]))
        ),
        unexpanded=tuple(
            _read_unexpanded(f"unexpanded[{i}]", u) for i, u in enumerate(_list("unexpanded", d["unexpanded"]))
        ),
        delegated=delegated,
    )
