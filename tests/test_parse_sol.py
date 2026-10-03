# verifies: FR-001-03, FR-001-06
"""Розбір вхідних SOL-переказів із сирої транзакції (T-007).

Еталони беруться з `scenarios/basic/expected.json` (породжений незалежним генератором),
крайові форми — з мінімальних транзакцій, зібраних тут же у формі jsonParsed.
Перевірки SOL фільтрують `asset == "sol"`: SPL-перекази з тих самих транзакцій — T-008.
"""

import copy
import json
from pathlib import Path

import pytest

from unmask.ingest.model import Asset, Transfer
from unmask.ingest.parse import ParsedTransfer, ParsedTx, parse_transaction

BASIC = Path(__file__).parent / "fixtures" / "scenarios" / "basic"
RPC = json.loads((BASIC / "rpc.json").read_text())
EXPECTED = json.loads((BASIC / "expected.json").read_text())
SYSTEM = "11111111111111111111111111111111"
TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"

# Підписи з basic (за expected.json: transfer_notes / excluded).
SIG_PLAIN = "4Au5MBM8hicf1LYx9YbSUHZ6tA98hcBdtfj8tdvVKcfhdR6zqGaCkuBMD2yrZ5zutNPk2dxB7qN6pzDwSL6AbJ6n"
SIG_CPI = "47HaJrgcKAvwQe8pSgYJvG23bDWfy6xipH8yGYX8QtHKTdg9tXtkomw5kmLkD5bHNjxX67mxDMZRNYhf91UDou25"
SIG_DOUBLE = "RafQBMEz5i5W5BDU2iBZ39PcaZtocQD4KNCDfStDbrHvT954QMHEfx83vj84tNEewm7tWnc3UMJmsQ9y7qxepHy"
SIG_FAILED = "4PDeYe9R8HR3qwC353ZNqXD3Uir9USorr4FTCBAr37p2aL48KimBTZNc2BP9XiCnUVAtibW7Jap9Kh9JPKxVBY9k"
SIG_SELF = "64Z8bupj6L755tTD8wmatQU2A4yaGMmCSJWBh77gHerXJp9mDkkyRnzjLFht1CTgPm92Ezp8i7zrqKtCSCeEdVWs"
SIG_BUY_P1 = "5fEbVTvxkGmu92KwPLYihyQypiKGSbnpnqm5m1hUSQuZWsrYPNdmdwytshQwYR8i2dZanqqmJpbY4U5T9mS9o71D"

W = EXPECTED["wallets"]
P1 = EXPECTED["buyers"][0]["wallet"]  # 8Ce2…, покупець першої купівлі SIG_BUY_P1

A = "BG2CcmGWP9wJoXEmRqqud4ZjB4C8ppDfrCm1VzRmgYAc"
B = "8WK4XexU2EH65Tpb3a3xvmvuPmNiYRsh6vGSNwd7ytqY"
C = "DwrpsQRTn5PczMvrN4dN4hgvCKFbKafjqH2au8VTJmrK"
NEW = "7mYdFSYRwhWoZhGetuJ6TUDDp8NWteV31JwDtxDCGFmD"
BASE = "AjEdPqzov4xPXJhXbGHZos8cVsEuv3bxBU658c4aYxSu"
OTHER_PROGRAM = "FdZFJ3hSSzkY22KG3WaVb8zSTnTKWonoRdvmoei3WYus"


def _raw(sig: str) -> dict:
    return copy.deepcopy(RPC["getTransaction"][sig])


def _sol(tx: ParsedTx) -> list[ParsedTransfer]:
    return [t for t in tx.transfers if t.asset == "sol"]


def _expected_transfer(sig: str, path: str) -> dict:
    (row,) = [t for t in EXPECTED["transfers"] if t["signature"] == sig and t["instruction_path"] == path]
    return row


def _sys(type_: str, **info) -> dict:
    return {"program": "system", "programId": SYSTEM, "parsed": {"type": type_, "info": info}}


def _tx(instructions, inner=(), *, keys=(A, B, SYSTEM), err=None, sig="SyntheticSig1111", slot=7,
        block_time=1759400000, pre=None, post=None, fee=5000) -> dict:
    """Мінімальна транзакція у формі `getTransaction` jsonParsed."""
    n = len(keys)
    return {
        "slot": slot,
        "blockTime": block_time,
        "version": 0,
        "transaction": {
            "signatures": [sig],
            "message": {
                "accountKeys": [
                    {"pubkey": k, "signer": i == 0, "writable": True, "source": "transaction"}
                    for i, k in enumerate(keys)
                ],
                "instructions": list(instructions),
                "recentBlockhash": "11111111111111111111111111111111",
            },
        },
        "meta": {
            "err": err,
            "fee": fee,
            "preBalances": list(pre if pre is not None else [10**9] * n),
            "postBalances": list(post if post is not None else [10**9] * n),
            "preTokenBalances": [],
            "postTokenBalances": [],
            "innerInstructions": list(inner),
            "logMessages": [],
        },
    }


def _fields(t: ParsedTransfer) -> dict:
    return {
        "signature": t.signature,
        "slot": t.slot,
        "block_time": t.block_time,
        "instruction_path": t.instruction_path,
        "sender": t.sender,
        "receiver": t.receiver,
        "asset": t.asset,
        "amount": t.amount,
        "decimals": t.decimals,
    }


# --- Задані тести T-007 ------------------------------------------------------------


def test_top_level_and_inner_sol_transfers_extracted_with_all_fields():
    for sig, path in ((SIG_PLAIN, "0"), (SIG_CPI, "0.0")):
        tx = parse_transaction(_raw(sig))
        assert isinstance(tx, ParsedTx)
        assert tx.signature == sig
        assert not tx.failed
        (got,) = _sol(tx)
        row = _expected_transfer(sig, path)
        want = {k: v for k, v in row.items() if k != "depth"}
        assert _fields(got) == want
        assert type(got.amount) is int
        # Глибину знає лише BFS; у парсера її немає, і `.at_depth` дає рівно еталонний Transfer.
        assert not hasattr(got, "depth")
        assert got.at_depth(row["depth"]) == Transfer(**{**row, "asset": Asset(row["asset"])})


def test_create_account_counts_as_sol_transfer():
    # Купівельна транзакція P1: ATA `create` породжує inner `createAccount` P1 -> новий токен-рахунок.
    tx = parse_transaction(_raw(SIG_BUY_P1))
    by_path = {t.instruction_path: t for t in _sol(tx)}
    created = by_path["0.0"]
    assert (created.sender, created.receiver, created.amount) == (
        P1, "JAJZEftFv4rj93cyLRrxnSYbf9BkhHd9oMDuvVsRzXKx", 2039280,
    )
    assert created.asset == Asset.SOL and created.decimals is None


def test_failed_transaction_yields_no_transfers():
    tx = parse_transaction(_raw(SIG_FAILED))
    assert tx.failed is True
    assert tx.transfers == ()
    # Факти, які не залежать від успіху, лишаються: комісію знято, програму викликано.
    assert tx.fee == 5000 and tx.fee_payer == "APKbo7jWjahuPAFr7eeXCrGpsrwQDUpTesJ8Ju6c1AfP"
    assert tx.programs == (SYSTEM,)


def test_two_identical_transfers_get_distinct_instruction_paths():
    tx = parse_transaction(_raw(SIG_DOUBLE))
    got = _sol(tx)
    assert [t.instruction_path for t in got] == ["0", "1"]
    assert {(t.sender, t.receiver, t.amount) for t in got} == {(A, "5jEmd1XJ8MsTqbdjYjkfGguo7Gs1Paq8yZZn4f3LQftT", 10**9)}
    for t in got:
        row = _expected_transfer(SIG_DOUBLE, t.instruction_path)
        assert _fields(t) == {k: v for k, v in row.items() if k != "depth"}


def test_non_transfer_instructions_ignored_but_program_recorded():
    tx = parse_transaction(_raw(SIG_BUY_P1))
    # programs — programId верхнього рівня, у порядку транзакції (еталон — buyer.programs).
    assert list(tx.programs) == EXPECTED["buyers"][0]["programs"]
    # SOL: лише top-level System transfer (індекс 1) і inner createAccount під ATA (0.0);
    # ATA create, initializeAccount3, сирий виклик DEX і spl-token інструкції SOL-переказами не є.
    assert sorted(t.instruction_path for t in _sol(tx)) == ["0.0", "1"]
    assert by_path(tx)["1"].receiver == W["SOL_VAULT"] and by_path(tx)["1"].amount == 600000000


def by_path(tx: ParsedTx) -> dict[str, ParsedTransfer]:
    return {t.instruction_path: t for t in _sol(tx)}


def test_every_expected_sol_transfer_is_reproduced_by_parser():
    # Golden: кожен SOL-переказ незалежного еталону basic відтворюється полями й шляхом.
    rows = [t for t in EXPECTED["transfers"] if t["asset"] == "sol"]
    assert rows
    for row in rows:
        got = by_path(parse_transaction(_raw(row["signature"])))[row["instruction_path"]]
        assert got.at_depth(row["depth"]) == Transfer(**{**row, "asset": Asset(row["asset"])})


# --- Межові випадки (critical) -----------------------------------------------------


@pytest.mark.parametrize(
    "ix, sender, receiver",
    [
        (_sys("transfer", source=A, destination=B, lamports=123), A, B),
        (
            _sys("transferWithSeed", source=NEW, sourceBase=BASE, sourceSeed="s", sourceOwner=SYSTEM,
                 destination=B, lamports=123),
            NEW, B,
        ),
        (_sys("createAccount", source=A, newAccount=NEW, lamports=123, space=0, owner=SYSTEM), A, NEW),
        (
            _sys("createAccountWithSeed", source=A, newAccount=NEW, base=BASE, seed="s", lamports=123,
                 space=0, owner=SYSTEM),
            A, NEW,
        ),
    ],
    ids=["transfer", "transferWithSeed", "createAccount", "createAccountWithSeed"],
)
def test_every_system_form_maps_source_and_destination(ix, sender, receiver):
    # transferWithSeed: кошти списуються з похідного рахунку `source`, не з `sourceBase`.
    tx = parse_transaction(_tx([ix], keys=(A, B, NEW, BASE, SYSTEM)))
    (t,) = tx.transfers
    assert (t.sender, t.receiver, t.amount, t.asset, t.decimals, t.instruction_path) == (
        sender, receiver, 123, "sol", None, "0",
    )


@pytest.mark.parametrize("type_", ["assign", "allocate", "advanceNonce", "withdrawFromNonce_unknown"])
def test_other_system_instructions_are_not_transfers(type_):
    tx = parse_transaction(_tx([_sys(type_, account=A)]))
    assert tx.transfers == ()
    assert tx.programs == (SYSTEM,)


def test_transfer_typed_instruction_of_other_program_is_not_sol():
    spl = {"program": "spl-token", "programId": TOKEN,
           "parsed": {"type": "transfer", "info": {"source": A, "destination": B, "authority": A, "amount": "5"}}}
    tx = parse_transaction(_tx([spl], keys=(A, B, TOKEN)))
    assert _sol(tx) == []


def test_nested_inner_instructions_get_index_dot_position_paths():
    # innerInstructions групуються за `index` (позиція top-level), j — позиція в сплощеному списку CPI
    # (вкладеність stackHeight>2 RPC сплощує). Порядок груп у масиві не має значення.
    raw_ix = {"programId": OTHER_PROGRAM, "accounts": [A, B], "data": "3Bxs4h24hBtQy9rw"}
    inner = [
        {"index": 2, "instructions": [
            {**raw_ix, "stackHeight": 2},
            {**_sys("transfer", source=A, destination=B, lamports=5), "stackHeight": 3},
            {**_sys("createAccount", source=A, newAccount=NEW, lamports=7, space=0, owner=SYSTEM),
             "stackHeight": 3},
        ]},
        {"index": 0, "instructions": [_sys("transfer", source=B, destination=A, lamports=11)]},
    ]
    tx = parse_transaction(_tx([raw_ix, raw_ix, raw_ix], inner, keys=(A, B, NEW, SYSTEM, OTHER_PROGRAM)))
    assert [(t.instruction_path, t.sender, t.receiver, t.amount) for t in tx.transfers] == [
        ("0.0", B, A, 11),
        ("2.1", A, B, 5),
        ("2.2", A, NEW, 7),
    ]
    assert tx.programs == (OTHER_PROGRAM,) * 3


def test_amount_is_exact_int_for_u64_beyond_float_precision():
    big = 2**64 - 1  # u64::MAX: у float втратив би молодші розряди
    tx = parse_transaction(json.loads(json.dumps(_tx([_sys("transfer", source=A, destination=B, lamports=big)]))))
    (t,) = tx.transfers
    assert type(t.amount) is int and t.amount == big


@pytest.mark.parametrize("bad", [1.5e9, 1000000000.0, True, "1000000000", None])
def test_non_integer_lamports_fail_loudly_not_truncated(bad):
    # Жодних float/bool/рядків: сума або точне ціле, або гучна помилка (CorruptRecord — T-009).
    with pytest.raises((TypeError, ValueError)):
        parse_transaction(_tx([_sys("transfer", source=A, destination=B, lamports=bad)]))


def test_zero_lamport_transfer_is_not_funding():
    tx = parse_transaction(_tx([_sys("transfer", source=A, destination=B, lamports=0),
                                _sys("transfer", source=A, destination=B, lamports=1)]))
    assert [(t.instruction_path, t.amount) for t in tx.transfers] == [("1", 1)]


def test_self_transfer_kept_for_funding_to_filter():
    # Правило sender != receiver застосовує funding.py; парсер передає факт як є.
    (t,) = _sol(parse_transaction(_raw(SIG_SELF)))
    assert t.sender == t.receiver == "HzwnbdJkjXLNs4wZCUUKrZXSdx3JKdr35mWCRLbfpMKz"
    assert t.amount == 100000000


def test_failed_synthetic_transaction_drops_inner_transfers_too():
    inner = [{"index": 0, "instructions": [_sys("transfer", source=A, destination=B, lamports=9)]}]
    raw = _tx([_sys("transfer", source=A, destination=B, lamports=5)], inner,
              err={"InstructionError": [0, "Custom"]})
    tx = parse_transaction(raw)
    assert tx.failed is True and tx.transfers == ()


def test_balance_facts_for_purchase_rule():
    # Числа звіряються з R-2: spent_sol = -Δ - fee - rent_created має дати еталонні 0.6 SOL P1.
    tx = parse_transaction(_raw(SIG_BUY_P1))
    assert tx.fee_payer == P1
    assert tx.fee == 5000
    assert tx.sol_delta(P1) == 2407955720 - 3010000000
    assert tx.created_accounts_lamports == 2039280
    spent = -tx.sol_delta(P1) - tx.fee - tx.created_accounts_lamports
    assert spent == EXPECTED["buyers"][0]["spent"][0]["amount"]
    assert tx.sol_delta("NotInThisTransaction1111111111111111111111") == 0
    for value in (tx.fee, tx.sol_delta(P1), tx.created_accounts_lamports):
        assert type(value) is int


def test_slot_block_time_and_plain_string_account_keys():
    raw = _tx([_sys("transfer", source=A, destination=B, lamports=3)], block_time=None,
              pre=[10, 0, 1], post=[2, 5, 1], fee=5)
    raw["transaction"]["message"]["accountKeys"] = [A, B, SYSTEM]  # форма `json`, не `jsonParsed`
    tx = parse_transaction(raw)
    assert (tx.slot, tx.block_time, tx.fee_payer) == (7, None, A)
    (t,) = tx.transfers
    assert t.block_time is None and t.slot == 7
    assert tx.sol_delta(A) == -8 and tx.sol_delta(B) == 5
    assert tx.created_accounts_lamports == 5


# --- Проміжний тип ParsedTransfer ---------------------------------------------------


def test_at_depth_requires_positive_depth():
    (t,) = _sol(parse_transaction(_raw(SIG_PLAIN)))
    for bad in (0, -1):
        with pytest.raises(ValueError):
            t.at_depth(bad)
    with pytest.raises(TypeError):
        t.at_depth(True)
    assert t.at_depth(2).depth == 2


def test_parsed_transfer_ready_for_spl_and_validates_itself():
    mint = "FeC5ErmXHtwwsjQWYXasUPNRyWFQuY7XBHMR2yspcLPs"
    spl = ParsedTransfer(signature="s", slot=1, block_time=None, instruction_path="3.1", sender=A,
                         receiver=B, asset=Asset.spl(mint), amount=10, decimals=6)
    assert spl.at_depth(1).asset.mint == mint and spl.at_depth(1).decimals == 6
    common = dict(signature="s", slot=1, block_time=None, instruction_path="0", sender=A, receiver=B)
    with pytest.raises(ValueError):
        ParsedTransfer(**common, asset=Asset.SOL, amount=10, decimals=9)  # sol без decimals
    with pytest.raises(TypeError):
        ParsedTransfer(**common, asset=Asset.SOL, amount=10.0, decimals=None)
    with pytest.raises(ValueError):
        ParsedTransfer(**{**common, "instruction_path": "0.1.2"}, asset=Asset.SOL, amount=1, decimals=None)
