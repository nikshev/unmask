# impl: FR-001-03, FR-001-04, FR-001-06, FR-001-09
"""Розбір сирої транзакції (`getTransaction`, jsonParsed) у `ParsedTx` або `CorruptRecord`.

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
пошкодження, не округлення. Переказ 0 lamports нічого не фінансує й не видається.

Система JSON-RPC розбирає System-інструкції успішної транзакції завжди (інакше
транзакція впала б), тож сира форма System-інструкції в успішній транзакції не
трапляється й окремо не декодується.

Розпізнається (SPL, T-008): spl-token і spl-token-2022 `transfer` / `transferChecked`
(верхній рівень і inner) → `asset="spl:<mint>"`. `sender`/`receiver` — **власники**
токен-рахунків джерела й призначення: позиція рахунку в `accountKeys` (у v0 jsonParsed
там уже є адреси lookup-таблиць, `meta.loadedAddresses` вдруге не додається) →
`pre/postTokenBalances[accountIndex].owner`. Рахунок, створений у транзакції, є лише в
post, закритий — лише в pre; тому шукаються обидва, і вони мусять узгоджуватись.
`mint` і `decimals` (`uiTokenAmount.decimals`) — з токен-балансів (legacy `transfer` поля
`mint` не має); якщо інструкція їх теж називає, вони мусять збігтися. Сума — з
`tokenAmount.amount` / `amount`: рядок лише з ASCII-цифр або точне `int`; інше —
пошкодження. WSOL лишається `spl:So111…112`, у SOL не перетворюється.

Невстановлюваний власник не пропускається мовчки і не підміняється адресою
токен-рахунку: такий переказ потрапляє в `ParsedTx.unresolved` з причиною
(`UNRESOLVED_REASONS`), а сусідні перекази лишаються. Так само токен-інструкція, що
переказує токени, але не підтримана (`transferCheckedWithFee`, confidential transfer,
виведення утриманих комісій; сира інструкція токен-програми з тегом переказу або з
даними, які не декодуються) → `unresolved` з причиною `unsupported_instruction`.
`mintTo`, `burn`, `closeAccount` тощо — не перекази за data-model і не позначаються.

Пошкодження (T-009, принцип V): `parse_transaction` не кидає виняток і не пропускає
мовчки, а повертає `CorruptRecord(signature, reason, detail, partial)`, якщо немає
підпису, `meta`, `slot` чи `accountKeys`; ціле поле (сума, slot, blockTime, fee,
баланси) не є точним цілим; бракує полів у розпізнаній структурі (`malformed`); або
`unresolved` непорожній (`partial` — `ParsedTx` з розібраними сусідами й переліком
`unresolved`, щоб нічого не губилось). `None` (транзакцію не отримано) — не пошкоджений
запис, а `unavailable`; його трактує викликач, тут це `TypeError`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, fields
from typing import Any, Iterator, Mapping

from unmask.ingest.model import Asset, Transfer

SYSTEM_PROGRAM = "11111111111111111111111111111111"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
TOKEN_PROGRAMS = frozenset({TOKEN_PROGRAM, TOKEN_2022_PROGRAM})

_SPL_TRANSFER_TYPES = frozenset({"transfer", "transferChecked"})
# jsonParsed-типи токен-інструкцій, що переміщують токени між рахунками, але не розбираються.
_UNSUPPORTED_TRANSFER_TYPES = frozenset({
    "transferCheckedWithFee",              # token-2022 transfer-fee extension
    "withdrawWithheldTokensFromMint",      # утримані комісії -> рахунок-призначення
    "withdrawWithheldTokensFromAccounts",
    "confidentialTransfer",                # confidential-transfer extension (сума прихована)
    "confidentialTransferWithFee",
    "confidentialTransferWithSplitProofs",
})
# Перший байт даних сирої токен-інструкції: Transfer=3, TransferChecked=12.
_RAW_TRANSFER_TAGS = frozenset({3, 12})
_RAW_TRANSFER_FEE_EXTENSION, _RAW_TRANSFER_CHECKED_WITH_FEE = 26, 1
_RAW_CONFIDENTIAL_TRANSFER_EXTENSION = 27  # уся гілка: суми приховані, власників не довести
_DIGITS_RE = re.compile(r"^[0-9]+$")
_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(_B58_ALPHABET)}

# Причини, з яких токен-переказ не можна приписати власникам (`UnresolvedTransfer.reason`).
UNRESOLVED_REASONS = frozenset({
    "account_not_in_keys",      # токен-рахунку немає в accountKeys
    "no_token_balance",         # рахунок є, але не згаданий ні в pre-, ні в postTokenBalances
    "owner_missing",            # запис балансу без поля owner
    "owner_conflict",           # pre і post називають різних власників
    "mint_mismatch",            # mint джерела/призначення/інструкції не збігаються
    "decimals_mismatch",        # decimals джерела/призначення/інструкції не збігаються
    "unsupported_instruction",  # токен-інструкція переказує токени, але не підтримана парсером
})

# Причини пошкодженого запису (`CorruptRecord.reason`). Усі → `MissingReason.CORRUPT_DATA` (T-014).
CORRUPT_REASONS = frozenset({
    "missing_signature",     # transaction.signatures[0] відсутній
    "missing_meta",          # meta відсутня або null
    "missing_slot",          # slot відсутній або null
    "missing_account_keys",  # transaction.message.accountKeys відсутній або порожній
    "non_integer",           # ціле поле (сума, slot, blockTime, fee, баланс) не є точним цілим
    "malformed",             # бракує поля / неочікуваний тип у розпізнаній структурі
    "unresolved_transfer",   # непорожній ParsedTx.unresolved: перекази неповні
})

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


class _Corrupt(Exception):
    """Внутрішній сигнал пошкодження сирих даних; назовні стає `CorruptRecord`."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason, self.detail = reason, detail


def _raw_int(name: str, value: Any) -> int:
    """Ціле з сирої відповіді RPC: точне `int` (не bool/float/рядок), інакше — пошкодження."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise _Corrupt("non_integer", f"{name}: expected exact int, got {value!r}")
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


def _token_decimals(name: str, value: Any) -> int:
    """`uiTokenAmount.decimals` — u8 у SPL: 0..255; інше — пошкодження (`non_integer`), не виняток."""
    _raw_int(name, value)
    if not 0 <= value <= 255:
        raise _Corrupt("non_integer", f"{name}: {value} is outside 0..255 (SPL decimals are u8)")
    return value


def _token_amount(name: str, value: Any) -> int:
    """Сума SPL у базових одиницях: рядок ASCII-цифр (jsonParsed) або точне невідʼємне `int`."""
    if isinstance(value, str):
        if not _DIGITS_RE.match(value):
            raise _Corrupt("non_integer", f"{name}: {value!r} is not a non-negative integer string")
        return int(value)
    _raw_int(name, value)
    if value < 0:
        raise _Corrupt("non_integer", f"{name}: {value} < 0")
    return value


@dataclass(frozen=True)
class TokenBalance:
    """Рядок `pre/postTokenBalances`: стан токен-рахунку `account_keys[account_index]`."""

    account_index: int
    mint: str
    owner: str | None
    amount: int
    decimals: int


@dataclass(frozen=True)
class UnresolvedTransfer:
    """Токен-переказ, який не вдалося приписати власникам. Не пропуск — явний факт (T-009)."""

    signature: str
    instruction_path: str
    program_id: str
    account: str
    reason: str
    note: str = ""  # для unsupported_instruction: тип jsonParsed або тег сирої інструкції

    def __post_init__(self) -> None:
        if self.reason not in UNRESOLVED_REASONS:
            raise ValueError(f"unresolved_transfer.reason: unknown {self.reason!r}")


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
    unresolved: tuple[UnresolvedTransfer, ...] = ()
    pre_token_balances: tuple[TokenBalance, ...] = ()
    post_token_balances: tuple[TokenBalance, ...] = ()

    def token_delta(self, owner: str, mint: str) -> int:
        """Δ балансу `mint` для власника `owner` у базових одиницях (сума по його токен-рахунках, R-2).

        Рахунок, якого немає в pre (створений) чи post (закритий), там рахується як 0.
        """
        def total(balances: tuple[TokenBalance, ...]) -> int:
            return sum(b.amount for b in balances if b.owner == owner and b.mint == mint)

        return total(self.post_token_balances) - total(self.pre_token_balances)

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


@dataclass(frozen=True)
class CorruptRecord:
    """Пошкоджена транзакція: отримана, але її не можна розібрати повністю (FR-001-09).

    `signature` — None лише коли підпису в записі немає (викликач знає, що запитував).
    `partial` — розібрана частина, коли пошкоджено лише окремі перекази (`unresolved_transfer`);
    вона не є повним набором переказів транзакції.
    """

    signature: str | None
    reason: str
    detail: str
    partial: ParsedTx | None = None

    def __post_init__(self) -> None:
        if self.reason not in CORRUPT_REASONS:
            raise ValueError(f"corrupt_record.reason: unknown {self.reason!r}")


def _pubkey(entry: Any) -> str:
    # jsonParsed: {"pubkey": ..., "signer": ...}; форма json: просто рядок.
    return entry["pubkey"] if isinstance(entry, Mapping) else entry


def _instructions(message: Mapping[str, Any], meta: Mapping[str, Any]) -> Iterator[tuple[str, Mapping[str, Any]]]:
    """(instruction_path, інструкція): спершу верхній рівень, потім inner за зростанням (i, j)."""
    for i, ix in enumerate(message["instructions"]):
        yield str(i), ix
    groups = sorted(meta.get("innerInstructions") or (), key=lambda g: _raw_int("inner.index", g["index"]))
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
    return info[roles[0]], info[roles[1]], _raw_int("lamports", info["lamports"])


def _token_balances(name: str, entries: Any) -> tuple[TokenBalance, ...]:
    return tuple(
        TokenBalance(
            account_index=_raw_int(f"{name}.accountIndex", e["accountIndex"]),
            mint=e["mint"],
            owner=e.get("owner") or None,
            amount=_token_amount(f"{name}.uiTokenAmount.amount", e["uiTokenAmount"]["amount"]),
            decimals=_token_decimals(f"{name}.uiTokenAmount.decimals", e["uiTokenAmount"]["decimals"]),
        )
        for e in entries or ()
    )


class _Unresolved(Exception):
    def __init__(self, account: str, reason: str) -> None:
        self.account, self.reason = account, reason


class _TokenAccounts:
    """Власник/mint/decimals токен-рахунку за адресою через accountKeys і pre/post токен-баланси."""

    def __init__(self, account_keys: tuple[str, ...], balances: tuple[TokenBalance, ...]) -> None:
        self._index: dict[str, int] = {}
        for i, key in enumerate(account_keys):
            self._index.setdefault(key, i)
        self._balances = balances

    def resolve(self, account: str) -> tuple[str, str, int]:
        index = self._index.get(account)
        if index is None:
            raise _Unresolved(account, "account_not_in_keys")
        entries = [b for b in self._balances if b.account_index == index]
        if not entries:
            raise _Unresolved(account, "no_token_balance")
        owners = {b.owner for b in entries}
        if None in owners:
            raise _Unresolved(account, "owner_missing")
        if len(owners) > 1:
            raise _Unresolved(account, "owner_conflict")
        if len({b.mint for b in entries}) > 1:
            raise _Unresolved(account, "mint_mismatch")
        if len({b.decimals for b in entries}) > 1:
            raise _Unresolved(account, "decimals_mismatch")
        return entries[0].owner, entries[0].mint, entries[0].decimals  # type: ignore[return-value]


def _b58decode(text: str) -> bytes | None:
    """Base58 (алфавіт Bitcoin/Solana) → байти; None, якщо рядок не base58."""
    n = 0
    for ch in text:
        digit = _B58_INDEX.get(ch)
        if digit is None:
            return None
        n = n * 58 + digit
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\0" * (len(text) - len(text.lstrip("1"))) + body


def _raw_token_transfer_note(data: Any) -> str | None:
    """Опис сирої (нерозібраної RPC) токен-інструкції, що може переказувати токени; None — не може.

    Консервативно: дані, які не декодуються, теж «можуть» — довести протилежне нема чим.
    """
    decoded = _b58decode(data) if isinstance(data, str) else None
    if not decoded:
        return "raw undecodable data"
    tag = decoded[0]
    if tag in _RAW_TRANSFER_TAGS or tag == _RAW_CONFIDENTIAL_TRANSFER_EXTENSION:
        return f"raw tag {tag}"
    if tag == _RAW_TRANSFER_FEE_EXTENSION and (len(decoded) < 2 or decoded[1] == _RAW_TRANSFER_CHECKED_WITH_FEE):
        return f"raw tag {tag}.{decoded[1] if len(decoded) > 1 else '?'}"
    return None


def _unsupported_token_transfer(ix: Mapping[str, Any]) -> tuple[str, str] | None:
    """(рахунок-джерело або "", опис) токен-інструкції, яка переказує токени, але не підтримана."""
    if ix.get("programId") not in TOKEN_PROGRAMS:
        return None
    parsed = ix.get("parsed")
    if isinstance(parsed, Mapping):
        type_ = parsed.get("type")
        if type_ not in _UNSUPPORTED_TRANSFER_TYPES:
            return None
        info = parsed.get("info")
        source = info.get("source") if isinstance(info, Mapping) else None
        return (source if isinstance(source, str) else ""), type_
    note = _raw_token_transfer_note(ix.get("data"))
    if note is None:
        return None
    accounts = ix.get("accounts") or ()
    return (accounts[0] if accounts and isinstance(accounts[0], str) else ""), note


def _spl_transfer(ix: Mapping[str, Any]) -> tuple[str, str, int, Mapping[str, Any]] | None:
    """(source, destination, amount, info) токен-переказу або None, якщо це не він."""
    if ix.get("programId") not in TOKEN_PROGRAMS:
        return None
    parsed = ix.get("parsed")
    if not isinstance(parsed, Mapping) or parsed.get("type") not in _SPL_TRANSFER_TYPES:
        return None
    info = parsed["info"]
    if parsed["type"] == "transferChecked":
        amount = _token_amount("tokenAmount.amount", info["tokenAmount"]["amount"])
    else:
        amount = _token_amount("amount", info["amount"])
    return info["source"], info["destination"], amount, info


def _resolve_spl(accounts: _TokenAccounts, source: str, destination: str,
                 info: Mapping[str, Any]) -> tuple[str, str, Asset, int]:
    sender, mint, decimals = accounts.resolve(source)
    receiver, dest_mint, dest_decimals = accounts.resolve(destination)
    if dest_mint != mint:
        raise _Unresolved(destination, "mint_mismatch")
    if info.get("mint", mint) != mint:
        raise _Unresolved(source, "mint_mismatch")
    if dest_decimals != decimals:
        raise _Unresolved(destination, "decimals_mismatch")
    token_amount = info.get("tokenAmount")
    if isinstance(token_amount, Mapping) and token_amount.get("decimals", decimals) != decimals:
        raise _Unresolved(source, "decimals_mismatch")
    return sender, receiver, Asset.spl(mint), decimals


def _signature(raw: Mapping[str, Any]) -> str | None:
    transaction = raw.get("transaction")
    signatures = transaction.get("signatures") if isinstance(transaction, Mapping) else None
    if isinstance(signatures, (list, tuple)) and signatures and isinstance(signatures[0], str) and signatures[0]:
        return signatures[0]
    return None


def _account_keys(raw: Mapping[str, Any]) -> Any:
    message = raw.get("transaction", {}).get("message")
    return message.get("accountKeys") if isinstance(message, Mapping) else None


def parse_transaction(raw: Mapping[str, Any]) -> ParsedTx | CorruptRecord:
    """Сира транзакція → `ParsedTx`, або `CorruptRecord`, якщо її не можна розібрати повністю."""
    if not isinstance(raw, Mapping):
        # None = транзакцію не отримано (`unavailable`, вирішує викликач), а не пошкоджений запис.
        raise TypeError(f"parse_transaction: expected a transaction mapping, got {raw!r}")
    signature = _signature(raw)
    if signature is None:
        return CorruptRecord(None, "missing_signature", "transaction.signatures[0] is absent")
    if raw.get("meta") is None:
        return CorruptRecord(signature, "missing_meta", "meta is absent or null")
    if raw.get("slot") is None:
        return CorruptRecord(signature, "missing_slot", "slot is absent or null")
    if not _account_keys(raw):
        return CorruptRecord(signature, "missing_account_keys", "transaction.message.accountKeys is absent or empty")
    try:
        tx = _parse(raw, signature)
    except _Corrupt as e:
        return CorruptRecord(signature, e.reason, e.detail)
    except (KeyError, IndexError, TypeError, ValueError, AttributeError) as e:
        # Розпізнана структура без потрібного поля чи з чужим типом: запис не відкидається мовчки.
        return CorruptRecord(signature, "malformed", f"{type(e).__name__}: {e}")
    if tx.unresolved:
        detail = "; ".join(
            f"{u.instruction_path} {u.account} {u.reason}" + (f" ({u.note})" if u.note else "") for u in tx.unresolved
        )
        return CorruptRecord(signature, "unresolved_transfer", detail, partial=tx)
    return tx


def _parse(raw: Mapping[str, Any], signature: str) -> ParsedTx:
    meta = raw["meta"]
    message = raw["transaction"]["message"]
    slot = _raw_int("slot", raw["slot"])
    block_time = raw.get("blockTime")
    if block_time is not None:
        _raw_int("blockTime", block_time)
    failed = meta.get("err") is not None

    account_keys = tuple(_pubkey(k) for k in message["accountKeys"])
    pre_tokens = _token_balances("preTokenBalances", meta.get("preTokenBalances"))
    post_tokens = _token_balances("postTokenBalances", meta.get("postTokenBalances"))
    accounts = _TokenAccounts(account_keys, pre_tokens + post_tokens)

    transfers: list[ParsedTransfer] = []
    unresolved: list[UnresolvedTransfer] = []

    def flag(path: str, ix: Mapping[str, Any], account: str, reason: str, note: str = "") -> None:
        unresolved.append(UnresolvedTransfer(
            signature=signature, instruction_path=path, program_id=ix["programId"], account=account, reason=reason,
            note=note,
        ))

    if not failed:
        for path, ix in _instructions(message, meta):
            common = dict(signature=signature, slot=slot, block_time=block_time, instruction_path=path)
            sol = _system_transfer(ix)
            if sol is not None:
                sender, receiver, lamports = sol
                if lamports:
                    transfers.append(ParsedTransfer(
                        **common, sender=sender, receiver=receiver, asset=Asset.SOL, amount=lamports, decimals=None,
                    ))
                continue
            spl = _spl_transfer(ix)
            if spl is None:
                unsupported = _unsupported_token_transfer(ix)
                if unsupported is not None:
                    flag(path, ix, unsupported[0], "unsupported_instruction", unsupported[1])
                continue
            source, destination, amount, info = spl
            if amount == 0:
                continue
            try:
                sender, receiver, asset, decimals = _resolve_spl(accounts, source, destination, info)
            except _Unresolved as e:
                flag(path, ix, e.account, e.reason)
                continue
            transfers.append(ParsedTransfer(
                **common, sender=sender, receiver=receiver, asset=asset, amount=amount, decimals=decimals,
            ))

    return ParsedTx(
        signature=signature,
        slot=slot,
        block_time=block_time,
        failed=failed,
        fee_payer=account_keys[0],
        fee=_raw_int("fee", meta["fee"]),
        programs=tuple(ix["programId"] for ix in message["instructions"]),
        transfers=tuple(transfers),
        account_keys=account_keys,
        pre_balances=tuple(_raw_int("preBalances", b) for b in meta["preBalances"]),
        post_balances=tuple(_raw_int("postBalances", b) for b in meta["postBalances"]),
        unresolved=tuple(unresolved),
        pre_token_balances=pre_tokens,
        post_token_balances=post_tokens,
    )
