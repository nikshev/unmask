# verifies: FR-001-06, FR-001-09
"""Пошкоджені записи не маскуються (T-009, принцип V).

`parse_transaction` на пошкодженій сирій транзакції повертає `CorruptRecord`, а не кидає
виняток і не пропускає мовчки: немає `meta` / `slot` / `accountKeys`, неціла сума,
токен-переказ, який не вдалося приписати власникам (`unresolved`), або непідтримана
токен-інструкція, що переказує токени (`unsupported_instruction`). `None` від
`get_transactions` — це `unavailable`, його трактує викликач (T-014), не парсер.
"""

import copy
import json
from pathlib import Path

import pytest

from unmask.ingest.parse import (
    CORRUPT_REASONS,
    UNRESOLVED_REASONS,
    CorruptRecord,
    ParsedTx,
    parse_transaction,
)

SCENARIOS = Path(__file__).parent / "fixtures" / "scenarios"
CORRUPT = json.loads((SCENARIOS / "corrupt" / "rpc.json").read_text())
BASIC = json.loads((SCENARIOS / "basic" / "rpc.json").read_text())

SYSTEM = "11111111111111111111111111111111"
TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
A = "BG2CcmGWP9wJoXEmRqqud4ZjB4C8ppDfrCm1VzRmgYAc"
B = "8WK4XexU2EH65Tpb3a3xvmvuPmNiYRsh6vGSNwd7ytqY"
TA = "7mYdFSYRwhWoZhGetuJ6TUDDp8NWteV31JwDtxDCGFmD"
TB = "AjEdPqzov4xPXJhXbGHZos8cVsEuv3bxBU658c4aYxSu"
TA2 = "BRRh8msu86ax1G4Wfsjc7kHGwhjuVAqXShWSHGAX96k5"
MINT = "FeC5ErmXHtwwsjQWYXasUPNRyWFQuY7XBHMR2yspcLPs"
OTHER_PROGRAM = "FdZFJ3hSSzkY22KG3WaVb8zSTnTKWonoRdvmoei3WYus"
SIG = "SyntheticCorrupt111"

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58(data: bytes) -> str:
    """Незалежний від парсера base58-кодер (для сирих інструкцій без `parsed`)."""
    n = int.from_bytes(data, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    return "1" * (len(data) - len(data.lstrip(b"\0"))) + out


def _bal(index, owner, amount, *, decimals=6, mint=MINT):
    return {"accountIndex": index, "mint": mint, "owner": owner, "programId": TOKEN,
            "uiTokenAmount": {"amount": amount, "decimals": decimals, "uiAmount": None, "uiAmountString": amount}}


PRE = (_bal(1, A, "100"), _bal(2, B, "0"))
POST = (_bal(1, A, "75"), _bal(2, B, "25"))


def _sys_transfer(lamports=5, source=A, destination=B):
    return {"program": "system", "programId": SYSTEM,
            "parsed": {"type": "transfer", "info": {"source": source, "destination": destination, "lamports": lamports}}}


def _checked(amount="25", *, program=TOKEN, type_="transferChecked", source=TA, **extra):
    info = {"source": source, "destination": TB, "authority": A, "mint": MINT,
            "tokenAmount": {"amount": amount, "decimals": 6, "uiAmount": None, "uiAmountString": amount}, **extra}
    return {"program": "spl-token", "programId": program, "parsed": {"type": type_, "info": info}}


def _legacy(amount="25", *, source=TA, destination=TB):
    return {"program": "spl-token", "programId": TOKEN, "parsed": {"type": "transfer", "info": {
        "source": source, "destination": destination, "authority": A, "amount": amount}}}


def _raw_ix(program, data: bytes, accounts=(TA, MINT, TB, A)):
    return {"programId": program, "accounts": list(accounts), "data": _b58(data)}


def _tx(instructions=(), inner=(), *, keys=(A, TA, TB, MINT, TOKEN, SYSTEM), pre_tok=PRE, post_tok=POST,
        err=None, sig=SIG):
    n = len(keys)
    return {
        "slot": 11, "blockTime": 1759400011, "version": 0,
        "transaction": {"signatures": [sig], "message": {
            "accountKeys": [{"pubkey": k, "signer": i == 0, "writable": True, "source": "transaction"}
                            for i, k in enumerate(keys)],
            "instructions": list(instructions),
            "recentBlockhash": "11111111111111111111111111111111",
        }},
        "meta": {"err": err, "fee": 5000, "preBalances": [10**9] * n, "postBalances": [10**9] * n,
                 "preTokenBalances": list(pre_tok), "postTokenBalances": list(post_tok),
                 "innerInstructions": list(inner), "logMessages": []},
    }


def _corrupt(raw) -> CorruptRecord:
    rec = parse_transaction(raw)
    assert isinstance(rec, CorruptRecord), rec
    assert rec.reason in CORRUPT_REASONS
    return rec


# --- Задані тести T-009 ------------------------------------------------------------


def test_missing_meta_is_corrupt_record():
    # Фікстура corrupt: транзакція переказу в K3 прийшла без meta.
    (sig,) = CORRUPT["_meta"]["defects"]["missing_meta"]
    raw = CORRUPT["getTransaction"][sig]
    assert "meta" not in raw
    rec = _corrupt(copy.deepcopy(raw))
    assert (rec.signature, rec.reason) == (sig, "missing_meta")
    assert rec.partial is None
    # meta: null (форма живого RPC) — те саме.
    raw = _tx([_sys_transfer()])
    raw["meta"] = None
    assert (_corrupt(raw).signature, _corrupt(raw).reason) == (SIG, "missing_meta")


def test_missing_slot_is_corrupt_record():
    raw = _tx([_sys_transfer()])
    del raw["slot"]
    rec = _corrupt(raw)
    assert (rec.signature, rec.reason) == (SIG, "missing_slot")
    raw = _tx([_sys_transfer()])
    raw["slot"] = None
    assert _corrupt(raw).reason == "missing_slot"


def test_missing_account_keys_is_corrupt_record():
    raw = _tx([_sys_transfer()])
    del raw["transaction"]["message"]["accountKeys"]
    rec = _corrupt(raw)
    assert (rec.signature, rec.reason) == (SIG, "missing_account_keys")
    raw = _tx([_sys_transfer()])
    raw["transaction"]["message"]["accountKeys"] = []
    assert _corrupt(raw).reason == "missing_account_keys"


@pytest.mark.parametrize(
    "keys, pre, post, reason, account",
    [
        ((A, TA, TB, MINT, TOKEN), [_bal(1, A, "100")], [_bal(1, A, "75")], "no_token_balance", TB),
        ((A, TB, MINT, TOKEN), [_bal(1, B, "0")], [_bal(1, B, "25")], "account_not_in_keys", TA),
        ((A, TA, TB, MINT, TOKEN), [_bal(1, A, "100"), {**_bal(2, B, "0"), "owner": None}],
         [_bal(1, A, "75"), {**_bal(2, B, "25"), "owner": None}], "owner_missing", TB),
    ],
    ids=["no_token_balance", "account_not_in_keys", "owner_missing"],
)
def test_unresolvable_token_account_owner_is_corrupt_record(keys, pre, post, reason, account):
    rec = _corrupt(_tx([_checked()], keys=keys, pre_tok=pre, post_tok=post))
    assert (rec.signature, rec.reason) == (SIG, "unresolved_transfer")
    (u,) = rec.partial.unresolved
    assert (u.instruction_path, u.account, u.reason) == ("0", account, reason)
    assert reason in rec.detail and account in rec.detail


@pytest.mark.parametrize(
    "ix",
    [
        _sys_transfer(lamports=1.5e9),
        _sys_transfer(lamports="1000000000"),
        _sys_transfer(lamports=True),
        _sys_transfer(lamports=None),
        _checked(amount="1.5"),
        _checked(amount="-25"),
        _checked(amount=25.0),
        _legacy(amount="2.5e1"),
        _legacy(amount=None),
    ],
    ids=["sol_float", "sol_str", "sol_bool", "sol_none", "spl_decimal_str", "spl_negative", "spl_float",
         "legacy_exp", "legacy_none"],
)
def test_non_integer_amount_is_corrupt_record(ix):
    rec = _corrupt(_tx([ix]))
    assert (rec.signature, rec.reason) == (SIG, "non_integer")
    assert rec.partial is None


def test_valid_transaction_is_not_flagged():
    # Кожна непошкоджена транзакція corrupt і basic — ParsedTx, без unresolved.
    defects = CORRUPT["_meta"]["defects"]
    bad = set(defects["null_transaction"]) | set(defects["missing_meta"])
    checked = 0
    for txs, skip in ((CORRUPT["getTransaction"], bad), (BASIC["getTransaction"], set())):
        for sig, raw in txs.items():
            if sig in skip:
                continue
            tx = parse_transaction(copy.deepcopy(raw))
            assert isinstance(tx, ParsedTx), (sig, tx)
            assert tx.signature == sig and tx.unresolved == ()
            checked += 1
    assert checked > 10
    tx = parse_transaction(_tx([_sys_transfer(), _checked()]))
    assert isinstance(tx, ParsedTx) and len(tx.transfers) == 2


# --- Межові випадки (critical) -----------------------------------------------------


def test_null_transaction_is_not_parsed_here():
    # `None` — це «транзакцію не отримано» (unavailable, T-014), а не «отримано пошкоджену».
    (sig,) = CORRUPT["_meta"]["defects"]["null_transaction"]
    assert CORRUPT["getTransaction"][sig] is None
    with pytest.raises(TypeError):
        parse_transaction(None)


def test_missing_signature_is_corrupt_record_without_signature():
    raw = _tx([_sys_transfer()])
    raw["transaction"]["signatures"] = []
    rec = _corrupt(raw)
    assert (rec.signature, rec.reason) == (None, "missing_signature")


@pytest.mark.parametrize("field, value", [
    ("fee", 5000.0), ("preBalances", [1.0, 2, 3, 4, 5, 6]), ("postBalances", ["1", 2, 3, 4, 5, 6]),
])
def test_non_integer_balance_facts_are_corrupt(field, value):
    raw = _tx()
    raw["meta"][field] = value
    rec = _corrupt(raw)
    assert rec.reason == "non_integer" and field in rec.detail


def test_non_integer_slot_block_time_and_token_balance_are_corrupt():
    raw = _tx()
    raw["slot"] = "11"
    assert _corrupt(raw).reason == "non_integer"
    raw = _tx()
    raw["blockTime"] = 1759400011.5
    assert _corrupt(raw).reason == "non_integer"
    raw = _tx(pre_tok=(_bal(1, A, "1.5"), _bal(2, B, "0")))
    assert _corrupt(raw).reason == "non_integer"
    # null blockTime — легальна форма RPC, не пошкодження.
    raw = _tx()
    raw["blockTime"] = None
    assert isinstance(parse_transaction(raw), ParsedTx)


def test_structurally_malformed_instruction_is_corrupt_not_exception():
    # Відсутні поля всередині розпізнаної інструкції: не KeyError, а запис з причиною.
    broken = _sys_transfer()
    del broken["parsed"]["info"]["destination"]
    rec = _corrupt(_tx([broken]))
    assert (rec.signature, rec.reason) == (SIG, "malformed")
    assert "destination" in rec.detail
    raw = _tx()
    del raw["transaction"]["message"]["instructions"]
    assert _corrupt(raw).reason == "malformed"
    raw = _tx(inner=[{"instructions": [_sys_transfer()]}])  # група без index
    assert _corrupt(raw).reason == "malformed"


def test_unresolved_keeps_resolvable_neighbours_in_partial():
    # Пошкодження не губить сусідні факти: вони лишаються в partial, але запис — CorruptRecord.
    keys = (A, TA, TB, TA2, MINT, TOKEN, SYSTEM)
    rec = _corrupt(_tx([_checked(), _checked(source=TA2, amount="5"), _sys_transfer()], keys=keys,
                       pre_tok=PRE, post_tok=POST))
    assert rec.reason == "unresolved_transfer"
    assert isinstance(rec.partial, ParsedTx) and rec.partial.signature == SIG
    assert [t.instruction_path for t in rec.partial.transfers] == ["0", "2"]
    assert [(u.instruction_path, u.account, u.reason) for u in rec.partial.unresolved] == [
        ("1", TA2, "no_token_balance")]


def test_failed_transaction_with_unresolvable_transfer_is_not_corrupt():
    # Упала — перекази не відбулися; нерозібраність її інструкцій нічого не приховує.
    tx = parse_transaction(_tx([_checked(source=TA2), _checked(type_="transferCheckedWithFee", fee={})],
                               err={"InstructionError": [0, "Custom"]}))
    assert isinstance(tx, ParsedTx) and tx.failed and tx.unresolved == ()


# --- (a) Непідтримані токен-інструкції не губляться мовчки -------------------------


def test_unsupported_instruction_is_an_unresolved_reason():
    assert "unsupported_instruction" in UNRESOLVED_REASONS


@pytest.mark.parametrize("type_", [
    "transferCheckedWithFee", "withdrawWithheldTokensFromMint", "withdrawWithheldTokensFromAccounts",
    "confidentialTransfer", "confidentialTransferWithFee",
])
def test_parsed_unsupported_token_transfer_is_corrupt_not_skipped(type_):
    ix = _checked(program=TOKEN_2022, type_=type_,
                  feeAmount={"amount": "1", "decimals": 6, "uiAmount": None, "uiAmountString": "1"})
    inner = [{"index": 0, "instructions": [ix]}]
    keys = (A, TA, TB, MINT, TOKEN_2022, OTHER_PROGRAM)
    rec = _corrupt(_tx([{"programId": OTHER_PROGRAM, "accounts": [], "data": ""}], inner, keys=keys))
    assert rec.reason == "unresolved_transfer"
    (u,) = rec.partial.unresolved
    assert (u.instruction_path, u.program_id, u.account, u.reason) == ("0.0", TOKEN_2022, TA, "unsupported_instruction")
    assert type_ in rec.detail


@pytest.mark.parametrize("program, data", [
    (TOKEN, bytes([3]) + (25).to_bytes(8, "little")),                       # Transfer
    (TOKEN, bytes([12]) + (25).to_bytes(8, "little") + bytes([6])),          # TransferChecked
    (TOKEN_2022, bytes([12]) + (25).to_bytes(8, "little") + bytes([6])),
    (TOKEN_2022, bytes([26, 1]) + (25).to_bytes(8, "little") + bytes([6]) + (1).to_bytes(8, "little")),
    (TOKEN_2022, bytes([27, 7]) + bytes(32)),                               # ConfidentialTransfer
], ids=["transfer", "transferChecked", "transferChecked_2022", "transferCheckedWithFee", "confidential"])
def test_raw_token_instruction_that_looks_like_transfer_is_corrupt(program, data):
    keys = (A, TA, TB, MINT, program)
    rec = _corrupt(_tx([_raw_ix(program, data)], keys=keys))
    assert rec.reason == "unresolved_transfer"
    (u,) = rec.partial.unresolved
    assert (u.instruction_path, u.program_id, u.account, u.reason) == ("0", program, TA, "unsupported_instruction")


@pytest.mark.parametrize("data", ["", "0OIl"], ids=["empty", "not_base58"])
def test_raw_token_instruction_with_undecodable_data_is_corrupt(data):
    # Неможливо довести, що це не переказ — консервативно: не «чисто».
    ix = {"programId": TOKEN, "accounts": [TA, TB, A], "data": data}
    rec = _corrupt(_tx([ix]))
    (u,) = rec.partial.unresolved
    assert u.reason == "unsupported_instruction"


@pytest.mark.parametrize("data", [
    bytes([7]) + (25).to_bytes(8, "little"),   # MintTo
    bytes([8]) + (25).to_bytes(8, "little"),   # Burn
    bytes([9]),                                # CloseAccount
    bytes([26, 5]) + bytes(10),                # SetTransferFee
], ids=["mintTo", "burn", "closeAccount", "setTransferFee"])
def test_raw_non_transfer_token_instruction_is_not_flagged(data):
    keys = (A, TA, TB, MINT, TOKEN_2022)
    tx = parse_transaction(_tx([_raw_ix(TOKEN_2022, data)], keys=keys))
    assert isinstance(tx, ParsedTx) and tx.transfers == () and tx.unresolved == ()


def test_raw_instruction_of_non_token_program_is_not_flagged():
    tx = parse_transaction(_tx([_raw_ix(OTHER_PROGRAM, bytes([3]) + bytes(8))],
                               keys=(A, TA, TB, MINT, OTHER_PROGRAM)))
    assert isinstance(tx, ParsedTx) and tx.unresolved == ()


def test_corrupt_record_validates_reason():
    with pytest.raises(ValueError):
        CorruptRecord(signature=SIG, reason="whatever", detail="")


# --- decimals токен-балансу: u8 (0..255), інакше пошкодження (T-020, узгодження з контрактом) ---------


def _tx_with_decimals(decimals):
    ix = _checked()
    ix["parsed"]["info"]["tokenAmount"]["decimals"] = decimals
    pre = tuple(_bal(b["accountIndex"], b["owner"], b["uiTokenAmount"]["amount"], decimals=decimals) for b in PRE)
    post = tuple(_bal(b["accountIndex"], b["owner"], b["uiTokenAmount"]["amount"], decimals=decimals) for b in POST)
    return _tx([ix], pre_tok=pre, post_tok=post)


@pytest.mark.parametrize("decimals", [0, 6, 19, 255])
def test_spl_decimals_within_u8_are_parsed(decimals):
    tx = parse_transaction(_tx_with_decimals(decimals))
    assert isinstance(tx, ParsedTx)
    (t,) = tx.transfers
    assert t.decimals == decimals and type(t.decimals) is int


@pytest.mark.parametrize("decimals", [256, 1000, -1])
def test_spl_decimals_outside_u8_are_corrupt_record_not_exception(decimals):
    rec = _corrupt(_tx_with_decimals(decimals))
    assert rec.reason == "non_integer" and "decimals" in rec.detail and rec.signature == SIG
