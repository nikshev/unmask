# impl: FR-001-03, FR-001-04, FR-001-06, FR-001-09
"""Розбір сирої транзакції (`getTransaction`, jsonParsed) у `ParsedTx`.

Що робить: витягує вхідні перекази (доказ FR-001-06) з інструкцій верхнього рівня й
`meta.innerInstructions`, програми верхнього рівня та балансові факти для правила
купівлі (research R-2). Мережі не торкається.

Перекази тут — `ParsedTransfer`: усі поля `model.Transfer`, крім `depth`. Глибину
знає лише BFS (`funding.py`), тож вона додається там через `.at_depth(d)`; парсер не
вигадує підставного значення. Правила включення (`sender != receiver`, межа часу)
теж застосовує `funding.py` — парсер передає факти як є.

Розпізнається (SOL, T-007): System `transfer`, `transferWithSeed`, `createAccount`,
`createAccountWithSeed` → `asset="sol"`, `decimals=None`. Транзакція з `meta.err`
дає `failed=True` і жодного переказу. Суми — лише точні `int`: float/bool/рядок —
гучна помилка, не округлення. Переказ 0 lamports нічого не фінансує й не видається.

Система JSON-RPC розбирає System-інструкції успішної транзакції завжди (інакше
транзакція впала б), тож сира форма System-інструкції в успішній транзакції не
трапляється й окремо не декодується.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, fields
from typing import Any, Iterator, Mapping

from unmask.ingest.model import Asset, Transfer

SYSTEM_PROGRAM = "11111111111111111111111111111111"

_PATH_RE = re.compile(r"^[0-9]+(\.[0-9]+)?$")

# тип System-інструкції -> (поле відправника, поле отримувача)
_SYSTEM_TRANSFERS: dict[str, tuple[str, str]] = {
    "transfer": ("source", "destination"),
    "transferWithSeed": ("source", "destination"),
    "createAccount": ("source", "newAccount"),
    "createAccountWithSeed": ("source", "newAccount"),
}


def _exact_int(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name}: expected exact int, got {value!r}")
    return value


@dataclass(frozen=True)
class ParsedTransfer:
    """Переказ без глибини. Асет і decimals узгоджені з `Transfer` (придатний і для SPL)."""

    signature: str
    slot: int
    block_time: int | None
    instruction_path: str
    sender: str
    receiver: str
    asset: Asset
    amount: int
    decimals: int | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "asset", Asset(self.asset))
        _exact_int("parsed_transfer.amount", self.amount)
        if self.amount < 1:
            raise ValueError(f"parsed_transfer.amount: {self.amount} < 1")
        if self.decimals is not None:
            _exact_int("parsed_transfer.decimals", self.decimals)
        if self.asset == Asset.SOL and self.decimals is not None:
            raise ValueError("parsed_transfer.decimals: must be None for sol")
        if not isinstance(self.instruction_path, str) or not _PATH_RE.match(self.instruction_path):
            raise ValueError(f"parsed_transfer.instruction_path: {self.instruction_path!r} is not 'i' or 'i.j'")
        for name in ("signature", "sender", "receiver"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"parsed_transfer.{name}: expected non-empty str, got {value!r}")

    def at_depth(self, depth: int) -> Transfer:
        """Повний `Transfer` на глибині `depth` (≥ 1; перевіряє `Transfer`)."""
        values = {f.name: getattr(self, f.name) for f in fields(self)}
        return Transfer(**values, depth=depth)


@dataclass(frozen=True)
class ParsedTx:
    signature: str
    slot: int
    block_time: int | None
    failed: bool
    fee_payer: str
    fee: int
    programs: tuple[str, ...]
    transfers: tuple[ParsedTransfer, ...]
    account_keys: tuple[str, ...]
    pre_balances: tuple[int, ...]
    post_balances: tuple[int, ...]

    @property
    def created_accounts_lamports(self) -> int:
        """Сума `postBalances` рахунків, створених у транзакції (`preBalance == 0`); R-2."""
        return sum(post for pre, post in zip(self.pre_balances, self.post_balances) if pre == 0)

    def sol_delta(self, owner: str) -> int:
        """`postBalance − preBalance` рахунку `owner` у lamports; 0, якщо його в транзакції немає."""
        return sum(
            post - pre
            for key, pre, post in zip(self.account_keys, self.pre_balances, self.post_balances)
            if key == owner
        )


def _pubkey(entry: Any) -> str:
    # jsonParsed: {"pubkey": ..., "signer": ...}; форма json: просто рядок.
    return entry["pubkey"] if isinstance(entry, Mapping) else entry


def _instructions(message: Mapping[str, Any], meta: Mapping[str, Any]) -> Iterator[tuple[str, Mapping[str, Any]]]:
    """(instruction_path, інструкція): спершу верхній рівень, потім inner за зростанням (i, j)."""
    for i, ix in enumerate(message["instructions"]):
        yield str(i), ix
    groups = sorted(meta.get("innerInstructions") or (), key=lambda g: _exact_int("inner.index", g["index"]))
    for group in groups:
        for j, ix in enumerate(group["instructions"]):
            yield f"{group['index']}.{j}", ix


def _system_transfer(ix: Mapping[str, Any]) -> tuple[str, str, int] | None:
    if ix.get("programId") != SYSTEM_PROGRAM:
        return None
    parsed = ix.get("parsed")
    if not isinstance(parsed, Mapping):
        return None
    roles = _SYSTEM_TRANSFERS.get(parsed.get("type"))
    if roles is None:
        return None
    info = parsed["info"]
    return info[roles[0]], info[roles[1]], _exact_int("lamports", info["lamports"])


def parse_transaction(raw: Mapping[str, Any]) -> ParsedTx:
    meta = raw["meta"]
    message = raw["transaction"]["message"]
    signature = raw["transaction"]["signatures"][0]
    slot = _exact_int("slot", raw["slot"])
    block_time = raw.get("blockTime")
    failed = meta.get("err") is not None

    transfers: list[ParsedTransfer] = []
    if not failed:
        for path, ix in _instructions(message, meta):
            found = _system_transfer(ix)
            if found is None:
                continue
            sender, receiver, lamports = found
            if lamports == 0:
                continue
            transfers.append(ParsedTransfer(
                signature=signature, slot=slot, block_time=block_time, instruction_path=path,
                sender=sender, receiver=receiver, asset=Asset.SOL, amount=lamports, decimals=None,
            ))

    account_keys = tuple(_pubkey(k) for k in message["accountKeys"])
    return ParsedTx(
        signature=signature,
        slot=slot,
        block_time=block_time,
        failed=failed,
        fee_payer=account_keys[0],
        fee=_exact_int("fee", meta["fee"]),
        programs=tuple(ix["programId"] for ix in message["instructions"]),
        transfers=tuple(transfers),
        account_keys=account_keys,
        pre_balances=tuple(_exact_int("preBalances", b) for b in meta["preBalances"]),
        post_balances=tuple(_exact_int("postBalances", b) for b in meta["postBalances"]),
    )
