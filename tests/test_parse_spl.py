# verifies: FR-001-04, FR-001-06
"""Розбір вхідних SPL-переказів із сирої транзакції (T-008).

Еталони — `scenarios/basic/expected.json` (незалежний генератор) і сирі транзакції basic;
межові форми — мінімальні транзакції у формі `getTransaction` jsonParsed, зібрані тут.

Відправник/отримувач SPL — **власники** токен-рахунків (`pre/postTokenBalances[accountIndex].owner`,
індекс — позиція в `accountKeys`), а не самі токен-рахунки. Токен-рахунок, власника якого
встановити не можна, не пропускається мовчки: він потрапляє в `ParsedTx.unresolved` з причиною,
а з T-009 `parse_transaction` повертає для такої транзакції `CorruptRecord(reason="unresolved_transfer")`,
чий `partial` — той самий `ParsedTx` з розібраними сусідами й переліком `unresolved`.
"""

import copy
import json
from pathlib import Path

import pytest

from unmask.ingest.model import Asset, Transfer
from unmask.ingest.parse import CorruptRecord, ParsedTransfer, ParsedTx, UnresolvedTransfer, parse_transaction

BASIC = Path(__file__).parent / "fixtures" / "scenarios" / "basic"
RPC = json.loads((BASIC / "rpc.json").read_text())
EXPECTED = json.loads((BASIC / "expected.json").read_text())
W = EXPECTED["wallets"]
M = EXPECTED["mint"]
USDC = W["USDC"]

SYSTEM = "11111111111111111111111111111111"
TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
WSOL = "So11111111111111111111111111111111111111112"

# basic: S -> P2 USDC (transferChecked на існуючий рахунок; та сама пара — ще й після createIdempotent)
SIG_SPL_EXISTING = "wWN58uBjAhiaBLVXW4LNjhXvbcJ3CXg2X3hayDAcnBebNTTAcSTRH5zzHBzuY6FntHSRxsuE7ZNsfNGLgc5Z2SD"
SIG_SPL_IDEMPOTENT = "5UkNdVVFxpBZBdmfoSgDhjYmja2YA6qbDihQtJAESP5QLc8GC1MYRgKxnJAKmTFCdAm2MhhbaQZ5pH6KVuVi7UhX"
# basic: CREATOR -> U, legacy `transfer` (без mint) на щойно створений ATA
SIG_LEGACY = "2AQ2vzbsH3mGc4z97d71gdBo2qyRXb9aVvgMRGvyTb8ggZjFJUBhS2T75erB9SQB5Do1QWog4PxxtVFZ6c2iEXQT"
# basic: перша купівля P1 — inner transferChecked пул(P5) -> новий ATA P1 (рахунок лише в post)
SIG_BUY_P1 = "5fEbVTvxkGmu92KwPLYihyQypiKGSbnpnqm5m1hUSQuZWsrYPNdmdwytshQwYR8i2dZanqqmJpbY4U5T9mS9o71D"
P1_ATA = "JAJZEftFv4rj93cyLRrxnSYbf9BkhHd9oMDuvVsRzXKx"

# Синтетичні адреси (валідний base58, ролі задаються тут).
A = "BG2CcmGWP9wJoXEmRqqud4ZjB4C8ppDfrCm1VzRmgYAc"   # власник-відправник
B = "8WK4XexU2EH65Tpb3a3xvmvuPmNiYRsh6vGSNwd7ytqY"   # власник-отримувач
C = "DwrpsQRTn5PczMvrN4dN4hgvCKFbKafjqH2au8VTJmrK"   # третій власник
TA = "7mYdFSYRwhWoZhGetuJ6TUDDp8NWteV31JwDtxDCGFmD"  # токен-рахунок A
TB = "AjEdPqzov4xPXJhXbGHZos8cVsEuv3bxBU658c4aYxSu"  # токен-рахунок B
TA2 = "BRRh8msu86ax1G4Wfsjc7kHGwhjuVAqXShWSHGAX96k5"  # другий токен-рахунок A
MINT = "FeC5ErmXHtwwsjQWYXasUPNRyWFQuY7XBHMR2yspcLPs"
OTHER_PROGRAM = "FdZFJ3hSSzkY22KG3WaVb8zSTnTKWonoRdvmoei3WYus"


def _raw(sig: str) -> dict:
    return copy.deepcopy(RPC["getTransaction"][sig])


def _partial(raw: dict) -> ParsedTx:
    """Транзакція з непорожнім unresolved: з T-009 це CorruptRecord, розібрана частина — у partial."""
    rec = parse_transaction(raw)
    assert isinstance(rec, CorruptRecord), rec
    assert rec.reason == "unresolved_transfer" and rec.partial.unresolved
    return rec.partial


def _spl(tx: ParsedTx) -> list[ParsedTransfer]:
    return [t for t in tx.transfers if t.asset != "sol"]


def _by_path(tx: ParsedTx) -> dict[str, ParsedTransfer]:
    return {t.instruction_path: t for t in _spl(tx)}


def _fields(t: ParsedTransfer) -> dict:
    return {
        "signature": t.signature, "slot": t.slot, "block_time": t.block_time,
        "instruction_path": t.instruction_path, "sender": t.sender, "receiver": t.receiver,
        "asset": t.asset, "amount": t.amount, "decimals": t.decimals,
    }


def _bal(index: int, owner: str | None, amount: str, *, mint: str = MINT, decimals: int = 6,
         program: str = TOKEN) -> dict:
    entry = {
        "accountIndex": index, "mint": mint, "programId": program,
        "uiTokenAmount": {"amount": amount, "decimals": decimals, "uiAmount": None, "uiAmountString": amount},
    }
    if owner is not None:
        entry["owner"] = owner
    return entry


def _checked(source=TA, destination=TB, amount="25", *, mint=MINT, decimals=6, program=TOKEN, authority=A) -> dict:
    return {"program": "spl-token" if program == TOKEN else "spl-token-2022", "programId": program,
            "parsed": {"type": "transferChecked", "info": {
                "source": source, "destination": destination, "authority": authority, "mint": mint,
                "tokenAmount": {"amount": amount, "decimals": decimals, "uiAmount": None, "uiAmountString": amount},
            }}}


def _legacy(source=TA, destination=TB, amount="25", *, program=TOKEN, authority=A) -> dict:
    return {"program": "spl-token" if program == TOKEN else "spl-token-2022", "programId": program,
            "parsed": {"type": "transfer", "info": {
                "source": source, "destination": destination, "authority": authority, "amount": amount,
            }}}


def _tx(instructions, inner=(), *, keys=(A, TA, TB, MINT, TOKEN), pre_tok=(), post_tok=(), err=None,
        sig="SyntheticSpl1111", slot=9, block_time=1759400009) -> dict:
    """Мінімальна транзакція у формі `getTransaction` jsonParsed з токен-балансами."""
    n = len(keys)
    return {
        "slot": slot, "blockTime": block_time, "version": 0,
        "transaction": {"signatures": [sig], "message": {
            "accountKeys": [{"pubkey": k, "signer": i == 0, "writable": True, "source": "transaction"}
                            for i, k in enumerate(keys)],
            "instructions": list(instructions),
            "recentBlockhash": "11111111111111111111111111111111",
        }},
        "meta": {
            "err": err, "fee": 5000,
            "preBalances": [10**9] * n, "postBalances": [10**9] * n,
            "preTokenBalances": list(pre_tok), "postTokenBalances": list(post_tok),
            "innerInstructions": list(inner), "logMessages": [],
        },
    }


# Типова пара: TA (власник A, 100) -> TB (власник B, 0); індекси за keys=(A, TA, TB, MINT, TOKEN).
PRE = (_bal(1, A, "100"), _bal(2, B, "0"))
POST = (_bal(1, A, "75"), _bal(2, B, "25"))


# --- Задані тести T-008 ------------------------------------------------------------


def test_transfer_checked_resolves_owners_from_token_balances():
    # Верхній рівень: S -> P2, обидва рахунки є і в pre, і в post. sender/receiver — власники, не рахунки.
    tx = parse_transaction(_raw(SIG_SPL_EXISTING))
    (t,) = _spl(tx)
    assert (t.sender, t.receiver) == (W["S"], W["P2"])
    assert t.sender != "AnHZYYptiP8vm42r6Bc5Z7SD78UTSpRuEyT8SnmmETXU"  # токен-рахунок S
    assert (t.asset, t.amount, t.decimals, t.instruction_path) == (Asset.spl(USDC), 25000000, 6, "0")
    assert tx.unresolved == ()

    # Inner (CPI DEX): пул P5 -> новий ATA P1, якого ще немає в preTokenBalances (лише post).
    tx = parse_transaction(_raw(SIG_BUY_P1))
    (t,) = _spl(tx)
    assert (t.instruction_path, t.sender, t.receiver) == ("2.0", W["P5"], W["P1"])
    assert (t.asset, t.amount, t.decimals) == (Asset.spl(M), EXPECTED["buyers"][0]["received_amount"], 6)
    assert tx.unresolved == ()


def test_legacy_transfer_without_mint_field_uses_token_balance_mint():
    raw = _raw(SIG_LEGACY)
    assert "mint" not in raw["transaction"]["message"]["instructions"][1]["parsed"]["info"]
    tx = parse_transaction(raw)
    (t,) = _spl(tx)
    assert (t.instruction_path, t.sender, t.receiver) == ("1", W["CREATOR"], W["U"])
    assert (t.asset, t.amount, t.decimals) == (Asset.spl(M), 50000000000, 6)
    assert type(t.amount) is int and type(t.decimals) is int
    assert tx.unresolved == ()


def test_token_2022_program_recognized():
    keys = (A, TA, TB, MINT, TOKEN_2022, OTHER_PROGRAM)
    pre = [_bal(1, A, "100", program=TOKEN_2022), _bal(2, B, "0", program=TOKEN_2022)]
    post = [_bal(1, A, "60", program=TOKEN_2022), _bal(2, B, "40", program=TOKEN_2022)]
    raw_dex = {"programId": OTHER_PROGRAM, "accounts": [TA, TB], "data": "3Bxs4h24hBtQy9rw"}
    inner = [{"index": 1, "instructions": [_legacy(amount="15", program=TOKEN_2022)]}]
    tx = parse_transaction(_tx([_checked(amount="25", program=TOKEN_2022), raw_dex], inner,
                               keys=keys, pre_tok=pre, post_tok=post))
    got = [(t.instruction_path, t.sender, t.receiver, t.asset, t.amount, t.decimals) for t in tx.transfers]
    assert got == [
        ("0", A, B, Asset.spl(MINT), 25, 6),
        ("1.0", A, B, Asset.spl(MINT), 15, 6),
    ]
    assert tx.programs == (TOKEN_2022, OTHER_PROGRAM)
    assert tx.unresolved == ()


def test_wsol_is_spl_not_sol():
    pre = [_bal(1, A, "2000000000", mint=WSOL, decimals=9), _bal(2, B, "0", mint=WSOL, decimals=9)]
    post = [_bal(1, A, "500000000", mint=WSOL, decimals=9), _bal(2, B, "1500000000", mint=WSOL, decimals=9)]
    tx = parse_transaction(_tx([_checked(amount="1500000000", mint=WSOL, decimals=9)],
                               keys=(A, TA, TB, WSOL, TOKEN), pre_tok=pre, post_tok=post))
    (t,) = tx.transfers
    assert t.asset == "spl:" + WSOL and t.asset != Asset.SOL and t.asset.mint == WSOL
    assert t.decimals == 9 and t.amount == 1500000000
    assert [x for x in tx.transfers if x.asset == "sol"] == []
    # Legacy transfer без mint на WSOL-рахунках — так само spl.
    tx = parse_transaction(_tx([_legacy(amount="1500000000")], keys=(A, TA, TB, WSOL, TOKEN),
                               pre_tok=pre, post_tok=post))
    (t,) = tx.transfers
    assert t.asset == Asset.spl(WSOL)
    # WSOL у token_delta, не в sol_delta.
    assert tx.token_delta(B, WSOL) == 1500000000 and tx.sol_delta(B) == 0


def test_token_delta_per_owner_and_mint():
    # basic, купівля P1: P1 отримав M (рахунок створено в транзакції — лише post), пул P5 віддав.
    tx = parse_transaction(_raw(SIG_BUY_P1))
    received = EXPECTED["buyers"][0]["received_amount"]
    assert tx.token_delta(W["P1"], M) == received
    assert tx.token_delta(W["P5"], M) == -received
    assert tx.token_delta(W["P1"], USDC) == 0
    assert tx.token_delta("NotInThisTransaction1111111111111111111111", M) == 0

    # Синтетика: два рахунки одного власника (сумуються), інший mint окремо, закритий рахунок (лише pre).
    keys = (A, TA, TA2, TB, MINT, USDC, TOKEN)
    pre = [_bal(1, A, "100"), _bal(2, A, "7", mint=USDC), _bal(3, B, "30")]
    post = [_bal(1, A, "70"), _bal(3, B, "50"), _bal(4, C, "10")]
    tx = parse_transaction(_tx([], keys=keys, pre_tok=pre, post_tok=post))
    assert tx.token_delta(A, MINT) == -30
    assert tx.token_delta(A, USDC) == -7          # рахунок закрито: post відсутній = 0
    assert tx.token_delta(B, MINT) == 20
    assert tx.token_delta(C, MINT) == 10          # рахунок створено: pre відсутній = 0
    assert tx.token_delta(C, USDC) == 0
    for v in (tx.token_delta(A, MINT), tx.token_delta(C, MINT)):
        assert type(v) is int


# --- Golden проти незалежного еталону ------------------------------------------------


def test_every_expected_spl_transfer_is_reproduced_by_parser():
    rows = [t for t in EXPECTED["transfers"] if t["asset"] != "sol"]
    assert len(rows) == 2  # S->P2 USDC двічі; якщо еталон зміниться — тест має це помітити
    for row in rows:
        tx = parse_transaction(_raw(row["signature"]))
        assert tx.unresolved == ()
        got = _by_path(tx)[row["instruction_path"]]
        assert _fields(got) == {k: v for k, v in row.items() if k != "depth"}
        assert type(got.amount) is int
        assert got.at_depth(row["depth"]) == Transfer(**{**row, "asset": Asset(row["asset"])})


def test_every_token_delivery_in_basic_buys_parses_from_pool_owner_to_buyer():
    # Кожна купівля basic: SPL-переказ M від власника пулу до покупця на received_amount.
    for buyer in EXPECTED["buyers"]:
        tx = parse_transaction(_raw(buyer["first_buy_signature"]))
        assert tx.unresolved == ()
        deliveries = [t for t in _spl(tx) if t.asset == Asset.spl(M) and t.receiver == buyer["wallet"]]
        assert [t.amount for t in deliveries] == [buyer["received_amount"]]
        assert tx.token_delta(buyer["wallet"], M) == buyer["received_amount"]


# --- Межові випадки (critical) -----------------------------------------------------


@pytest.mark.parametrize(
    "keys, pre, post, reason, account",
    [
        # destination TB є в accountKeys, але немає в жодному з pre/postTokenBalances
        ((A, TA, TB, MINT, TOKEN), [_bal(1, A, "100")], [_bal(1, A, "75")], "no_token_balance", TB),
        # source TA відсутній у accountKeys узагалі
        ((A, TB, MINT, TOKEN), [_bal(1, B, "0")], [_bal(1, B, "25")], "account_not_in_keys", TA),
        # запис балансу без поля owner (старі RPC) — власника не знаємо
        ((A, TA, TB, MINT, TOKEN), [_bal(1, A, "100"), _bal(2, None, "0")],
         [_bal(1, A, "75"), _bal(2, None, "25")], "owner_missing", TB),
        # pre і post називають різних власників одного рахунку — не вгадуємо
        ((A, TA, TB, MINT, TOKEN), [_bal(1, A, "100"), _bal(2, B, "0")],
         [_bal(1, A, "75"), _bal(2, C, "25")], "owner_conflict", TB),
    ],
    ids=["no_token_balance", "account_not_in_keys", "owner_missing", "owner_conflict"],
)
def test_unresolvable_token_account_owner_is_reported_not_silently_skipped(keys, pre, post, reason, account):
    inner = [{"index": 0, "instructions": [_checked(amount="25")]}]
    tx = _partial(_tx([{"programId": OTHER_PROGRAM, "accounts": [], "data": ""}], inner,
                      keys=keys + (OTHER_PROGRAM,), pre_tok=pre, post_tok=post, sig="SigUnresolved1"))
    assert _spl(tx) == []  # власника не вигадано, токен-рахунок не підставлено як «гаманець»
    (u,) = tx.unresolved
    assert isinstance(u, UnresolvedTransfer)
    assert (u.signature, u.instruction_path, u.program_id, u.account, u.reason) == (
        "SigUnresolved1", "0.0", TOKEN, account, reason,
    )
    assert not tx.failed


def test_resolvable_transfers_kept_alongside_unresolved_one():
    # Один битий переказ не ховає сусідні: вони в transfers, битий — у unresolved.
    keys = (A, TA, TB, TA2, MINT, TOKEN)
    pre = [_bal(1, A, "100"), _bal(2, B, "0")]
    post = [_bal(1, A, "50"), _bal(2, B, "50")]
    tx = _partial(_tx([_checked(amount="25"), _checked(source=TA2, amount="5"), _legacy(amount="25")],
                      keys=keys, pre_tok=pre, post_tok=post))
    assert [(t.instruction_path, t.amount) for t in tx.transfers] == [("0", 25), ("2", 25)]
    assert [(u.instruction_path, u.account, u.reason) for u in tx.unresolved] == [("1", TA2, "no_token_balance")]


def test_mint_in_instruction_disagreeing_with_token_balance_is_unresolved():
    tx = _partial(_tx([_checked(mint=USDC)], keys=(A, TA, TB, MINT, USDC, TOKEN), pre_tok=PRE, post_tok=POST))
    assert tx.transfers == ()
    (u,) = tx.unresolved
    assert u.reason == "mint_mismatch"
    # І між джерелом та призначенням legacy-переказу (mint береться з балансів — мусить збігатись).
    post = (_bal(1, A, "75"), _bal(2, B, "25", mint=USDC))
    pre = (_bal(1, A, "100"), _bal(2, B, "0", mint=USDC))
    tx = _partial(_tx([_legacy()], keys=(A, TA, TB, MINT, USDC, TOKEN), pre_tok=pre, post_tok=post))
    assert tx.transfers == () and [u.reason for u in tx.unresolved] == ["mint_mismatch"]


def test_decimals_disagreeing_with_token_balance_is_unresolved():
    tx = _partial(_tx([_checked(decimals=9)], pre_tok=PRE, post_tok=POST))
    assert tx.transfers == () and [u.reason for u in tx.unresolved] == ["decimals_mismatch"]


def test_legacy_transfer_source_and_destination_decimals_disagree_is_unresolved():
    # Legacy `transfer` не несе ні mint, ні decimals: єдина перевірка — джерело проти призначення.
    # Той самий mint, але рахунки звітують різні decimals — суму не можна однозначно масштабувати.
    pre = (_bal(1, A, "100", decimals=6), _bal(2, B, "0", decimals=9))
    post = (_bal(1, A, "75", decimals=6), _bal(2, B, "25", decimals=9))
    tx = _partial(_tx([_legacy()], pre_tok=pre, post_tok=post))
    assert tx.transfers == ()
    assert [(u.account, u.reason) for u in tx.unresolved] == [(TB, "decimals_mismatch")]


def test_spl_amount_is_exact_int_for_u64_beyond_float_precision():
    big = str(2**64 - 1)
    pre = (_bal(1, A, big), _bal(2, B, "0"))
    post = (_bal(1, A, "0"), _bal(2, B, big))
    tx = parse_transaction(json.loads(json.dumps(_tx([_checked(amount=big)], pre_tok=pre, post_tok=post))))
    (t,) = tx.transfers
    assert type(t.amount) is int and t.amount == 2**64 - 1
    assert tx.token_delta(B, MINT) == 2**64 - 1


@pytest.mark.parametrize("bad", ["1.5", "-25", "", " 25", "2.5e1", "0x19", "２５", 25.0, True, None])
def test_non_integer_spl_amount_is_corrupt_not_truncated(bad):
    # Як і для SOL: або точне ціле, або явний CorruptRecord (контракт T-009; до T-009 — виняток).
    for ix in (_checked(amount=bad), _legacy(amount=bad)):
        rec = parse_transaction(_tx([ix], pre_tok=PRE, post_tok=POST))
        assert isinstance(rec, CorruptRecord)
        assert (rec.reason, rec.partial) == ("non_integer", None)
        assert "amount" in rec.detail


@pytest.mark.parametrize("bad", ["1.5", "-1", 7.0, None])
def test_non_integer_token_balance_amount_is_corrupt(bad):
    # Баланс — основа token_delta (R-2): неціле значення не округлюється, запис пошкоджений.
    pre = (_bal(1, A, "100"), {**_bal(2, B, "0"), "uiTokenAmount": {"amount": bad, "decimals": 6}})
    rec = parse_transaction(_tx([_checked()], pre_tok=pre, post_tok=POST))
    assert isinstance(rec, CorruptRecord)
    assert (rec.reason, rec.partial) == ("non_integer", None)
    assert "preTokenBalances" in rec.detail


def test_spl_amount_given_as_json_int_is_accepted():
    # Деякі провайдери віддають amount числом; точне ціле приймається.
    tx = parse_transaction(_tx([_legacy(amount=25)], pre_tok=PRE, post_tok=POST))
    (t,) = tx.transfers
    assert t.amount == 25 and type(t.amount) is int


def test_spl_self_transfer_kept_for_funding_to_filter():
    # Між двома рахунками одного власника: sender == receiver, правило відсікання — у funding.py.
    keys = (A, TA, TA2, MINT, TOKEN)
    pre = (_bal(1, A, "100"), _bal(2, A, "0"))
    post = (_bal(1, A, "75"), _bal(2, A, "25"))
    tx = parse_transaction(_tx([_checked(source=TA, destination=TA2)], keys=keys, pre_tok=pre, post_tok=post))
    (t,) = tx.transfers
    assert t.sender == t.receiver == A and t.amount == 25
    assert tx.token_delta(A, MINT) == 0


def test_zero_amount_spl_transfer_is_not_funding():
    tx = parse_transaction(_tx([_checked(amount="0"), _legacy(amount="1")], pre_tok=PRE, post_tok=POST))
    assert [(t.instruction_path, t.amount) for t in tx.transfers] == [("1", 1)]


def test_failed_transaction_yields_no_spl_transfers_and_no_unresolved():
    inner = [{"index": 0, "instructions": [_checked(source=TA2)]}]
    tx = parse_transaction(_tx([_checked()], inner, pre_tok=PRE, post_tok=PRE, err={"InstructionError": [0, "Custom"]}))
    assert tx.failed is True and tx.transfers == () and tx.unresolved == ()
    assert tx.token_delta(B, MINT) == 0


@pytest.mark.parametrize("type_, info", [
    ("mintTo", {"mint": MINT, "account": TB, "mintAuthority": A, "amount": "25"}),
    ("mintToChecked", {"mint": MINT, "account": TB, "mintAuthority": A,
                       "tokenAmount": {"amount": "25", "decimals": 6}}),
    ("burn", {"account": TA, "mint": MINT, "authority": A, "amount": "25"}),
    ("approve", {"source": TA, "delegate": B, "owner": A, "amount": "25"}),
    ("closeAccount", {"account": TA, "destination": A, "owner": A}),
    ("initializeAccount3", {"account": TB, "mint": MINT, "owner": B}),
])
def test_other_token_instructions_are_not_transfers(type_, info):
    ix = {"program": "spl-token", "programId": TOKEN, "parsed": {"type": type_, "info": info}}
    tx = parse_transaction(_tx([ix], pre_tok=PRE, post_tok=POST))
    assert tx.transfers == () and tx.unresolved == ()


def test_transfer_typed_instruction_of_unknown_program_is_ignored():
    # Чужа програма з parsed.type == "transferChecked" не є токен-програмою.
    ix = _checked()
    ix["programId"] = OTHER_PROGRAM
    tx = parse_transaction(_tx([ix], keys=(A, TA, TB, MINT, OTHER_PROGRAM), pre_tok=PRE, post_tok=POST))
    assert tx.transfers == () and tx.unresolved == ()


def test_account_index_counts_lookup_table_addresses_once():
    # v0 jsonParsed: accountKeys уже містить адреси з lookup-таблиць (source="lookupTable"),
    # а meta.loadedAddresses їх дублює. accountIndex індексує accountKeys — без повторного додавання.
    raw = _tx([_checked()], keys=(A, TA, MINT, TOKEN, TB), pre_tok=(_bal(1, A, "100"), _bal(4, B, "0")),
              post_tok=(_bal(1, A, "75"), _bal(4, B, "25")))
    raw["transaction"]["message"]["accountKeys"][4]["source"] = "lookupTable"
    raw["meta"]["loadedAddresses"] = {"writable": [TB], "readonly": []}
    tx = parse_transaction(raw)
    (t,) = tx.transfers
    assert (t.sender, t.receiver) == (A, B)
    assert tx.account_keys == (A, TA, MINT, TOKEN, TB)


def test_sol_and_spl_transfers_interleave_by_instruction_path():
    # Порядок transfers — порядок інструкцій (верхній рівень, потім inner), незалежно від активу.
    tx = parse_transaction(_raw(SIG_BUY_P1))
    assert [(t.instruction_path, t.asset) for t in tx.transfers] == [
        ("1", Asset.SOL), ("0.0", Asset.SOL), ("2.0", Asset.spl(M)),
    ]
