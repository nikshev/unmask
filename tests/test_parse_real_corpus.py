# verifies: FR-001-04, FR-001-06, FR-001-09
"""Парсер на реальному корпусі mainnet і ефемерні токен-рахунки (T-050).

Корпус `fixtures/real/mainnet_txs_2026-10-04.json` — 55 реальних відповідей `getTransaction`
(jsonParsed; версії 0, 1, legacy). Близько третини — свопи (Jupiter тощо), у яких тимчасовий
токен-рахунок (переважно wrapped SOL) створюється й закривається в тій самій транзакції: його
немає ні в `preTokenBalances`, ні в `postTokenBalances`, а власник і mint відомі лише з
`initializeAccount*` тієї ж транзакції.

Оракули тут незалежні від парсера: дельти токенів і правило купівлі (R-2) рахуються простим
підсумовуванням сирих `pre/postTokenBalances` / `pre/postBalances`; очікувані власники переказів —
прямим читанням сирих інструкцій.
"""

import copy
import json
from collections import defaultdict
from pathlib import Path

import pytest

from unmask.ingest.model import Asset
from unmask.ingest.parse import CorruptRecord, ParsedTx, parse_transaction
from unmask.ingest.purchases import detect_purchases

CORPUS_PATH = Path(__file__).parent / "fixtures" / "real" / "mainnet_txs_2026-10-04.json"
CORPUS: dict[str, dict] = json.loads(CORPUS_PATH.read_text())["transactions"]

TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

# Реальна транзакція з двома ефемерними рахунками (WSOL і USDC) одного власника; USDC-рахунок
# віддає токени legacy `transfer` (без decimals), а decimals відомі з його ж `transferChecked` (5.11).
SIG_TWO_EPHEMERAL = "4zVkrrimxxSz9JSVA6tDYX6ZMjWEsQe1uYaW7tWKLm5ah812HigmRU4u4PNHVzietySXSdH8nGtiBDLzVEbjLcPD"
EPH_WSOL = "C1kyUi8Ej1iqr7ZvcFYr7DRsJVB91YFFbRQWDBgwXaiu"
EPH_USDC = "5fWFCfbp6TAjQroMiQmJoMyKDNjX4Yn8aVsgbMYwVJrN"

# Синтетичні адреси (валідний base58).
A = "BG2CcmGWP9wJoXEmRqqud4ZjB4C8ppDfrCm1VzRmgYAc"   # власник ефемерного рахунку
B = "8WK4XexU2EH65Tpb3a3xvmvuPmNiYRsh6vGSNwd7ytqY"   # власник рахунку з балансами
C = "DwrpsQRTn5PczMvrN4dN4hgvCKFbKafjqH2au8VTJmrK"   # сторонній власник
TA = "7mYdFSYRwhWoZhGetuJ6TUDDp8NWteV31JwDtxDCGFmD"  # ефемерний токен-рахунок
TB = "AjEdPqzov4xPXJhXbGHZos8cVsEuv3bxBU658c4aYxSu"  # токен-рахунок з балансами
TC = "BRRh8msu86ax1G4Wfsjc7kHGwhjuVAqXShWSHGAX96k5"  # другий ефемерний рахунок
MINT = "FeC5ErmXHtwwsjQWYXasUPNRyWFQuY7XBHMR2yspcLPs"  # не native
KEYS = (A, TA, TB, TC, MINT, WSOL, TOKEN)


# ---------------------------------------------------------------------------- допоміжне


def _pubkey(entry) -> str:
    return entry["pubkey"] if isinstance(entry, dict) else entry


def _raw_instructions(raw: dict):
    """(шлях, інструкція) — верхній рівень і inner, напряму з сирого JSON."""
    for i, ix in enumerate(raw["transaction"]["message"]["instructions"]):
        yield str(i), ix
    for group in raw["meta"].get("innerInstructions") or ():
        for j, ix in enumerate(group["instructions"]):
            yield f"{group['index']}.{j}", ix


def _token_parsed(ix: dict):
    if ix.get("programId") in (TOKEN, TOKEN_2022) and isinstance(ix.get("parsed"), dict):
        return ix["parsed"].get("type"), ix["parsed"].get("info") or {}
    return None, {}


def _raw_token_rows(raw: dict, key: str) -> list[dict]:
    return list(raw["meta"].get(key) or ())


def _independent_token_deltas(raw: dict) -> dict[tuple[str, str], int]:
    """(owner, mint) -> Σpost − Σpre, лише з сирих токен-балансів."""
    out: dict[tuple[str, str], int] = defaultdict(int)
    for sign, key in ((1, "postTokenBalances"), (-1, "preTokenBalances")):
        for row in _raw_token_rows(raw, key):
            out[(row.get("owner"), row["mint"])] += sign * int(row["uiTokenAmount"]["amount"])
    return dict(out)


def _independent_buyers(raw: dict, mint: str) -> dict[str, int]:
    """R-2 незалежно від `purchases.py`: власник -> отримана кількість `mint`."""
    meta = raw["meta"]
    if meta.get("err") is not None:
        return {}
    keys = [_pubkey(k) for k in raw["transaction"]["message"]["accountKeys"]]
    deltas = _independent_token_deltas(raw)
    created = sum(post for pre, post in zip(meta["preBalances"], meta["postBalances"]) if pre == 0)
    buyers = {}
    for (owner, m), received in deltas.items():
        if m != mint or owner is None or received <= 0:
            continue
        spent = -sum(post - pre for k, pre, post in zip(keys, meta["preBalances"], meta["postBalances"]) if k == owner)
        if owner == keys[0]:
            spent -= meta["fee"] + created
        sold_other = any(o == owner and m2 != mint and d < 0 for (o, m2), d in deltas.items())
        if spent > 0 or sold_other:
            buyers[owner] = received
    return buyers


def _initialized(raw: dict) -> dict[str, tuple[str, str]]:
    """account -> (owner, mint) з усіх initializeAccount* сирої транзакції (верхній рівень + inner)."""
    out = {}
    for _, ix in _raw_instructions(raw):
        type_, info = _token_parsed(ix)
        if type_ in ("initializeAccount", "initializeAccount2", "initializeAccount3"):
            out[info["account"]] = (info["owner"], info["mint"])
    return out


def _expected_spl_transfers(raw: dict) -> dict[str, tuple[str, str, str, int]]:
    """path -> (sender, receiver, asset, decimals) для кожного SPL-переказу, прямо з сирого JSON.

    Власник/mint/decimals — з токен-балансів; якщо рахунку там немає — з initializeAccount* тієї ж
    транзакції, decimals — з transferChecked, що торкається цього рахунку, або 9 для WSOL.
    """
    keys = [_pubkey(k) for k in raw["transaction"]["message"]["accountKeys"]]
    rows = _raw_token_rows(raw, "preTokenBalances") + _raw_token_rows(raw, "postTokenBalances")
    by_account = {keys[r["accountIndex"]]: (r["owner"], r["mint"], r["uiTokenAmount"]["decimals"]) for r in rows}
    init = _initialized(raw)
    checked: dict[str, int] = {}
    for _, ix in _raw_instructions(raw):
        type_, info = _token_parsed(ix)
        if type_ == "transferChecked":
            for acc in (info["source"], info["destination"]):
                checked[acc] = info["tokenAmount"]["decimals"]

    def account(acc: str) -> tuple[str, str, int]:
        if acc in by_account:
            return by_account[acc]
        owner, mint = init[acc]
        return owner, mint, checked[acc] if acc in checked else (9 if mint == WSOL else -1)

    out = {}
    for path, ix in _raw_instructions(raw):
        type_, info = _token_parsed(ix)
        if type_ not in ("transfer", "transferChecked"):
            continue
        amount = info["tokenAmount"]["amount"] if type_ == "transferChecked" else info["amount"]
        if int(amount) == 0:
            continue
        sender, mint, decimals = account(info["source"])
        receiver, _, _ = account(info["destination"])
        out[path] = (sender, receiver, Asset.spl(mint), decimals)
    return out


def _parsed(sig: str) -> ParsedTx:
    tx = parse_transaction(copy.deepcopy(CORPUS[sig]))
    assert isinstance(tx, ParsedTx), (sig, tx)
    return tx


# Синтетичні транзакції у формі getTransaction jsonParsed.


def _bal(index: int, owner: str, amount: str, *, mint: str = WSOL, decimals: int = 9) -> dict:
    return {"accountIndex": index, "mint": mint, "owner": owner, "programId": TOKEN,
            "uiTokenAmount": {"amount": amount, "decimals": decimals, "uiAmount": None, "uiAmountString": amount}}


def _init(account: str, owner: str, mint: str, type_: str = "initializeAccount3") -> dict:
    info = {"account": account, "mint": mint, "owner": owner}
    if type_ == "initializeAccount":
        info["rentSysvar"] = "SysvarRent111111111111111111111111111111111"
    return {"program": "spl-token", "programId": TOKEN, "parsed": {"type": type_, "info": info}}


def _close(account: str, owner: str) -> dict:
    return {"program": "spl-token", "programId": TOKEN,
            "parsed": {"type": "closeAccount", "info": {"account": account, "destination": owner, "owner": owner}}}


def _legacy(source: str, destination: str, amount: str = "25", authority: str = A) -> dict:
    return {"program": "spl-token", "programId": TOKEN, "parsed": {"type": "transfer", "info": {
        "source": source, "destination": destination, "authority": authority, "amount": amount}}}


def _checked(source: str, destination: str, amount: str = "25", *, mint: str = WSOL, decimals: int = 9,
             authority: str = A) -> dict:
    return {"program": "spl-token", "programId": TOKEN, "parsed": {"type": "transferChecked", "info": {
        "source": source, "destination": destination, "authority": authority, "mint": mint,
        "tokenAmount": {"amount": amount, "decimals": decimals, "uiAmount": None, "uiAmountString": amount}}}}


def _tx(instructions, inner=(), *, pre_tok=(), post_tok=(), sig="SyntheticEph1111", keys=KEYS) -> dict:
    n = len(keys)
    return {
        "slot": 11, "blockTime": 1759400011, "version": 0,
        "transaction": {"signatures": [sig], "message": {
            "accountKeys": [{"pubkey": k, "signer": i == 0, "writable": True, "source": "transaction"}
                            for i, k in enumerate(keys)],
            "instructions": list(instructions), "recentBlockhash": "11111111111111111111111111111111",
        }},
        "meta": {"err": None, "fee": 5000, "preBalances": [10**9] * n, "postBalances": [10**9] * n,
                 "preTokenBalances": list(pre_tok), "postTokenBalances": list(post_tok),
                 "innerInstructions": list(inner), "logMessages": []},
    }


# TB (власник B, WSOL) — рахунок з балансами; індекс 2 у KEYS.
TB_PRE, TB_POST = _bal(2, B, "100"), _bal(2, B, "75")


def _transfers(raw: dict) -> list[tuple]:
    tx = parse_transaction(raw)
    assert isinstance(tx, ParsedTx), tx
    return [(t.instruction_path, t.sender, t.receiver, t.asset, t.amount, t.decimals) for t in tx.transfers]


def _unresolved(raw: dict) -> list[tuple[str, str, str]]:
    rec = parse_transaction(raw)
    assert isinstance(rec, CorruptRecord) and rec.reason == "unresolved_transfer", rec
    return [(u.instruction_path, u.account, u.reason) for u in rec.partial.unresolved]


# ---------------------------------------------------------------------------- корпус


def test_corpus_shape():
    # Захист від тихої підміни фікстури: 55 транзакцій, усі три версії, є ефемерні рахунки.
    assert len(CORPUS) == 55
    assert {raw.get("version") for raw in CORPUS.values()} == {0, 1, "legacy"}
    ephemeral = [sig for sig, raw in CORPUS.items()
                 if any(acc not in {_pubkey(raw["transaction"]["message"]["accountKeys"][r["accountIndex"]])
                                    for r in _raw_token_rows(raw, "preTokenBalances")
                                    + _raw_token_rows(raw, "postTokenBalances")}
                        for acc in _initialized(raw))]
    assert len(ephemeral) >= 15


def test_all_real_transactions_parse_without_corrupt_record():
    corrupt = {}
    for sig, raw in CORPUS.items():
        rec = parse_transaction(copy.deepcopy(raw))
        if not isinstance(rec, ParsedTx):
            corrupt[sig] = (rec.reason, rec.detail[:120])
    assert corrupt == {}, f"{len(corrupt)} of {len(CORPUS)} corrupt"


def test_token_delta_conservation_matches_independent_sum_on_real_corpus():
    for sig, raw in CORPUS.items():
        tx = _parsed(sig)
        expected = _independent_token_deltas(raw)
        for (owner, mint), delta in expected.items():
            assert tx.token_delta(owner, mint) == delta, (sig, owner, mint)
        # по кожному mint сума дельт власників — та сама, що й проста сума по рядках
        for mint in {m for _, m in expected}:
            owners = {o for o, m in expected if m == mint}
            assert sum(tx.token_delta(o, mint) for o in owners) == sum(
                d for (_, m), d in expected.items() if m == mint), (sig, mint)


def test_sol_delta_consistent_with_fee_on_real_corpus():
    # Лампорти зберігаються: Σ sol_delta усіх рахунків = −fee; SOL-переказ — між рахунками транзакції.
    for sig, raw in CORPUS.items():
        tx = _parsed(sig)
        assert len(set(tx.account_keys)) == len(tx.account_keys)
        assert sum(tx.sol_delta(k) for k in tx.account_keys) == -tx.fee == -raw["meta"]["fee"], sig
        for t in tx.transfers:
            if t.asset == Asset.SOL:
                assert t.sender in tx.account_keys and t.receiver in tx.account_keys, (sig, t)


def test_spl_transfers_match_raw_instructions_on_real_corpus():
    # Кожен SPL-переказ корпусу — з власниками, mint і decimals, прочитаними з сирого JSON напряму;
    # жодного переказу «з порожнечі»: відправник завжди розв'язаний до власника, названого в цій транзакції.
    resolved_via_init = 0
    for sig, raw in CORPUS.items():
        tx = _parsed(sig)
        expected = _expected_spl_transfers(raw)
        got = {t.instruction_path: (t.sender, t.receiver, t.asset, t.decimals)
               for t in tx.transfers if t.asset != Asset.SOL}
        assert got == expected, sig
        init = _initialized(raw)
        # власник — завжди той, кого називають баланси чи initializeAccount* цієї ж транзакції
        known_owners = {o for o, _ in init.values()} | {r.get("owner") for r in _raw_token_rows(
            raw, "preTokenBalances") + _raw_token_rows(raw, "postTokenBalances")}
        for path, (sender, receiver, _, _) in got.items():
            assert sender in known_owners and receiver in known_owners, (sig, path)
        for path, ix in _raw_instructions(raw):
            type_, info = _token_parsed(ix)
            if path in got and (info.get("source") in init or info.get("destination") in init):
                resolved_via_init += 1
    assert resolved_via_init >= 40


def test_two_ephemeral_accounts_in_real_transaction():
    # Вирізано з корпусу: WSOL- і USDC-ефемерні рахунки одного власника (inner initializeAccount3).
    raw = CORPUS[SIG_TWO_EPHEMERAL]
    owner, mint = _initialized(raw)[EPH_USDC]
    assert mint == USDC and _initialized(raw)[EPH_WSOL] == (owner, WSOL)
    by_path = {t.instruction_path: t for t in _parsed(SIG_TWO_EPHEMERAL).transfers}
    assert (by_path["5.3"].receiver, by_path["5.3"].asset, by_path["5.3"].decimals) == (owner, Asset.spl(WSOL), 9)
    assert (by_path["5.10"].sender, by_path["5.10"].decimals) == (owner, 9)
    assert (by_path["5.11"].receiver, by_path["5.11"].asset, by_path["5.11"].decimals) == (owner, Asset.spl(USDC), 6)
    # legacy transfer з ефемерного USDC: decimals — із transferChecked цього ж рахунку (5.11), не 9
    assert (by_path["5.13"].sender, by_path["5.13"].asset, by_path["5.13"].amount, by_path["5.13"].decimals) == (
        owner, Asset.spl(USDC), 155327227, 6)


def test_purchase_detection_unchanged_by_ephemeral_resolution_on_real_corpus():
    # Ефемерні рахунки відсутні в pre/post балансах, тож правило купівлі (R-2 на балансах) не змінюється:
    # множина покупців кожного mint кожної транзакції — та сама, що й обчислена незалежно з сирих балансів.
    buyers_total = 0
    for sig, raw in CORPUS.items():
        tx = _parsed(sig)
        mints = {r["mint"] for r in _raw_token_rows(raw, "preTokenBalances") + _raw_token_rows(raw, "postTokenBalances")}
        for mint in mints:
            got = {p.wallet: p.received_amount for p in detect_purchases(tx, mint)}
            assert got == _independent_buyers(raw, mint), (sig, mint)
            buyers_total += len(got)
    assert buyers_total > 0


# ---------------------------------------------------------------------------- ефемерні: синтетика


@pytest.mark.parametrize("init_type", ["initializeAccount", "initializeAccount2", "initializeAccount3"])
def test_ephemeral_account_owner_resolved_from_initialize_account_in_same_tx(init_type):
    # TA створено й закрито в цій транзакції (немає в токен-балансах); WSOL → decimals 9.
    raw = _tx([_init(TA, A, WSOL, init_type), _legacy(TA, TB), _close(TA, A)],
              pre_tok=[TB_PRE], post_tok=[_bal(2, B, "125")])
    assert _transfers(raw) == [("1", A, B, Asset.spl(WSOL), 25, 9)]


def test_ephemeral_non_native_decimals_from_transfer_checked():
    # Не native: decimals — з transferChecked.info.tokenAmount.decimals; mint перевіряється проти мапи.
    pre, post = _bal(2, B, "100", mint=MINT, decimals=6), _bal(2, B, "75", mint=MINT, decimals=6)
    raw = _tx([_init(TA, A, MINT), _checked(TB, TA, mint=MINT, decimals=6, authority=B), _close(TA, A)],
              pre_tok=[pre], post_tok=[post])
    assert _transfers(raw) == [("1", B, A, Asset.spl(MINT), 25, 6)]


def test_ephemeral_initialize_account_in_inner_instructions():
    inner = [{"index": 0, "instructions": [_init(TA, A, WSOL), _legacy(TA, TB)]}]
    raw = _tx([{"programId": "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4", "accounts": [], "data": "1"}],
              inner, pre_tok=[TB_PRE], post_tok=[_bal(2, B, "125")])
    assert _transfers(raw) == [("0.1", A, B, Asset.spl(WSOL), 25, 9)]


def test_several_ephemeral_accounts_in_one_transaction():
    # Два ефемерних рахунки різних власників: переказ між ними, обидва з мапи; плюс вихід до TB.
    raw = _tx([_init(TA, A, WSOL), _init(TC, C, WSOL), _legacy(TA, TC, "30"), _legacy(TC, TB, "10", authority=C),
               _close(TA, A), _close(TC, C)], pre_tok=[TB_PRE], post_tok=[_bal(2, B, "110")])
    assert _transfers(raw) == [
        ("2", A, C, Asset.spl(WSOL), 30, 9),
        ("3", C, B, Asset.spl(WSOL), 10, 9),
    ]


def test_ephemeral_non_native_unchecked_transfer_stays_unresolved():
    # Не native, лише legacy transfer: decimals узяти нізвідки — переказ лишається unresolved, причина та сама.
    # decimals контрагента (TB, 6) — не джерело для рахунку TA: не вгадуємо.
    pre, post = _bal(2, B, "100", mint=MINT, decimals=6), _bal(2, B, "125", mint=MINT, decimals=6)
    raw = _tx([_init(TA, A, MINT), _legacy(TA, TB), _close(TA, A)], pre_tok=[pre], post_tok=[post])
    assert _unresolved(raw) == [("1", TA, "no_token_balance")]
    # transferChecked іншого рахунку (TB -> TC) з decimals цього mint — теж не джерело для TA
    raw = _tx([_init(TA, A, MINT), _init(TC, C, MINT), _checked(TB, TC, "5", mint=MINT, decimals=6, authority=B),
               _legacy(TA, TB)], pre_tok=[pre], post_tok=[post])
    assert _unresolved(raw) == [("3", TA, "no_token_balance")]
    # transferChecked, що торкається TA, але з іншим mint — не джерело decimals для TA
    raw = _tx([_init(TA, A, MINT), _checked(TB, TA, "5", mint=WSOL, decimals=6, authority=B), _legacy(TA, TB)],
              pre_tok=[pre], post_tok=[post])
    assert _unresolved(raw) == [("1", TA, "no_token_balance"), ("2", TA, "no_token_balance")]


def test_ephemeral_decimals_conflict_is_unresolved():
    # WSOL-рахунок, але transferChecked називає інші decimals — суперечність, не вибір одного.
    raw = _tx([_init(TA, A, WSOL), _checked(TA, TB, decimals=6), _legacy(TA, TB)],
              pre_tok=[TB_PRE], post_tok=[_bal(2, B, "150")])
    assert _unresolved(raw) == [("1", TA, "decimals_mismatch"), ("2", TA, "decimals_mismatch")]


def test_initialize_map_never_overrides_conflicting_token_balances():
    # TB є в балансах (власник B): мапа, що називає іншого власника чи mint, не перемагає — unresolved.
    raw = _tx([_init(TB, C, WSOL), _checked(TB, TA, authority=B), _init(TA, A, WSOL)],
              pre_tok=[TB_PRE], post_tok=[TB_POST])
    assert _unresolved(raw) == [("1", TB, "owner_conflict")]
    raw = _tx([_init(TB, B, MINT), _legacy(TB, TA, authority=B), _init(TA, A, WSOL)],
              pre_tok=[TB_PRE], post_tok=[TB_POST])
    assert _unresolved(raw) == [("1", TB, "mint_mismatch")]
    # узгоджені мапа й баланси — звичайне розв'язання з балансів
    raw = _tx([_init(TB, B, WSOL), _legacy(TB, TA, authority=B), _init(TA, A, WSOL)],
              pre_tok=[TB_PRE], post_tok=[TB_POST])
    assert _transfers(raw) == [("1", B, A, Asset.spl(WSOL), 25, 9)]


def test_conflicting_initializations_of_same_ephemeral_account_are_unresolved():
    # Один рахунок ініціалізовано двічі з різними власниками / mint — не вибирати одного.
    raw = _tx([_init(TA, A, WSOL), _close(TA, A), _init(TA, C, WSOL), _legacy(TA, TB)],
              pre_tok=[TB_PRE], post_tok=[_bal(2, B, "125")])
    assert _unresolved(raw) == [("3", TA, "owner_conflict")]
    raw = _tx([_init(TA, A, WSOL), _close(TA, A), _init(TA, A, MINT), _legacy(TA, TB)],
              pre_tok=[TB_PRE], post_tok=[_bal(2, B, "125")])
    assert _unresolved(raw) == [("3", TA, "mint_mismatch")]


def test_ephemeral_map_is_per_transaction_only():
    # TA ініціалізовано в tx1; у tx2 TA знову ефемерний, але без initializeAccount — мапа tx1 не діє.
    tx1 = _tx([_init(TA, A, WSOL), _legacy(TA, TB), _close(TA, A)], pre_tok=[TB_PRE],
              post_tok=[_bal(2, B, "125")], sig="SyntheticEphOne1")
    tx2 = _tx([_legacy(TA, TB)], pre_tok=[TB_PRE], post_tok=[_bal(2, B, "125")], sig="SyntheticEphTwo1")
    assert _transfers(tx1) == [("1", A, B, Asset.spl(WSOL), 25, 9)]
    assert _unresolved(tx2) == [("0", TA, "no_token_balance")]
    # Закритий у tx1 і створений знову в tx3 іншим власником: власник — з tx3, не з tx1.
    tx3 = _tx([_init(TA, C, WSOL), _legacy(TA, TB, authority=C)], pre_tok=[TB_PRE],
              post_tok=[_bal(2, B, "125")], sig="SyntheticEphThree")
    assert _transfers(tx3) == [("1", C, B, Asset.spl(WSOL), 25, 9)]
    assert _transfers(tx1) == [("1", A, B, Asset.spl(WSOL), 25, 9)]
    assert _unresolved(tx2) == [("0", TA, "no_token_balance")]


def test_initialize_account_without_owner_field_does_not_resolve():
    # Неповна initializeAccount (без owner) — не джерело власника: лишається no_token_balance.
    init = _init(TA, A, WSOL)
    del init["parsed"]["info"]["owner"]
    raw = _tx([init, _legacy(TA, TB)], pre_tok=[TB_PRE], post_tok=[_bal(2, B, "125")])
    assert _unresolved(raw) == [("1", TA, "no_token_balance")]


def test_initialize_account_of_foreign_program_is_ignored():
    init = _init(TA, A, WSOL)
    init["programId"] = "FdZFJ3hSSzkY22KG3WaVb8zSTnTKWonoRdvmoei3WYus"
    raw = _tx([init, _legacy(TA, TB)], pre_tok=[TB_PRE], post_tok=[_bal(2, B, "125")])
    assert _unresolved(raw) == [("1", TA, "no_token_balance")]


def test_ephemeral_account_not_in_keys_is_still_account_not_in_keys():
    # Мапа не обходить перевірку accountKeys.
    raw = _tx([_init(TA, A, WSOL), _legacy(TA, TB)], pre_tok=[TB_PRE], post_tok=[_bal(2, B, "125")],
              keys=(A, C, TB, TC, MINT, WSOL, TOKEN))
    assert _unresolved(raw) == [("1", TA, "account_not_in_keys")]
