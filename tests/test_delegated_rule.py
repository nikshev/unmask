# verifies: FR-002-15, FR-002-16
"""Правило делегованої купівлі (research R-2, T-045): `detect_delegated(parsed, mint)`.

Для успішної транзакції T з mint M і власниками O (`account_keys` ∪ власники токен-балансів; самі
токен-рахунки — не власники, див. `delegated.py`):
- `spent(O)` — як у правилі купівлі 001: `purchases.spent_sol(O) > 0` або від'ємна дельта іншого mint;
- платники P = {Δ(M) == 0 і spent}, отримувачі R = {Δ(M) > 0 і не spent};
- 1:1 → `DelegatedLink`; обидві сторони непорожні й не 1:1 → `UnpairedCandidate` на кожного з
  `detail="payers=<p> receivers=<r>"`; одна сторона порожня чи `failed` → `[]`.

Еталон — `scenarios/swapsend` (генератор T-043 складає `delegated` з ДЕКЛАРАЦІЇ ролей, а не з цього
коду): по всіх його транзакціях правило дає рівно задекларовані `links`/`unpaired`, а `no_link` — `[]`.
Межові випадки — синтетичні `ParsedTx` з явними балансами, зібрані тут.
"""

import dataclasses
import json
from pathlib import Path

import pytest

from unmask.ingest.delegated import detect_delegated
from unmask.ingest.model import (
    BuyersCompleteness,
    DelegatedAnalysis,
    DelegatedLink,
    DelegatedSide,
    UnpairedCandidate,
    link_sort_key,
    unpaired_sort_key,
)
from unmask.ingest.parse import CorruptRecord, ParsedTx, TokenBalance, parse_transaction
from unmask.ingest.purchases import detect_purchases

SWAPSEND = Path(__file__).parent / "fixtures" / "scenarios" / "swapsend"
RPC = json.loads((SWAPSEND / "rpc.json").read_text(encoding="utf-8"))
EXPECTED = json.loads((SWAPSEND / "expected.json").read_text(encoding="utf-8"))
DECLARED = EXPECTED["delegated"]
W = EXPECTED["wallets"]
M = EXPECTED["mint"]

WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SYSTEM = "11111111111111111111111111111111"
SOL = 10**9
FEE = 5000
RENT = 2_039_280

A, B, CREATOR, POOL, VAULT = W["A"], W["B"], W["CREATOR"], W["POOL"], W["SOL_VAULT"]
C1, E, D1, F1, AIR = W["C1"], W["E"], W["D1"], W["F1"], W["AIR"]
TIP = W["DEX"]          # будь-яка адреса, що лише отримує SOL (чайові/комісія)
X = W["P3"]             # сторонній співпідписант


# --- фікстура swapsend --------------------------------------------------------------


def _parsed(signature: str) -> ParsedTx:
    parsed = parse_transaction(RPC["getTransaction"][signature])
    assert isinstance(parsed, ParsedTx), parsed
    return parsed


def _by_reason(reason: str) -> list[dict]:
    rows = [r for r in DECLARED["no_link"] if r["reason"] == reason]
    assert rows, reason
    return rows


def _link_dict(link: DelegatedLink) -> dict:
    return {"signature": link.signature, "slot": link.slot, "block_time": link.block_time,
            "payer": link.payer, "receiver": link.receiver}


def _cand_dict(cand: UnpairedCandidate) -> dict:
    return {"signature": cand.signature, "slot": cand.slot, "block_time": cand.block_time,
            "wallet": cand.wallet, "side": cand.side.value, "detail": cand.detail}


def _declared_unpaired(signature: str) -> list[dict]:
    rows = [r for r in DECLARED["unpaired"] if r["signature"] == signature]
    assert rows, signature
    return rows


# --- синтетичні транзакції ----------------------------------------------------------


def _tx(*, fee_payer: str, sol: dict[str, tuple[int, int]], tokens=(), failed=False,
        sig="SyntheticDelegated111", slot=77, block_time=1759400077, fee=FEE) -> ParsedTx:
    """`ParsedTx` з явними балансами.

    `sol`: ключ -> (pre, post) lamports (не токен-рахунки; fee payer — першим ключем).
    `tokens`: (ключ_рахунку, власник, mint, pre, post[, pre_lamports, post_lamports]); `None` у pre — рахунок
    створений у транзакції, у post — закритий. Lamports за умовчанням: наявний RENT, відсутній 0.
    """
    keys, pre_bal, post_bal = [fee_payer], [sol[fee_payer][0]], [sol[fee_payer][1]]
    for key, (pre, post) in sol.items():
        if key != fee_payer:
            keys.append(key), pre_bal.append(pre), post_bal.append(post)
    pre_tok, post_tok = [], []
    for row in tokens:
        account, owner, mint, pre, post = row[:5]
        pre_lam = row[5] if len(row) > 5 else (0 if pre is None else RENT)
        post_lam = row[6] if len(row) > 6 else (0 if post is None else RENT)
        index = len(keys)
        keys.append(account), pre_bal.append(pre_lam), post_bal.append(post_lam)
        if pre is not None:
            pre_tok.append(TokenBalance(index, mint, owner, pre, 6))
        if post is not None:
            post_tok.append(TokenBalance(index, mint, owner, post, 6))
    return ParsedTx(
        signature=sig, slot=slot, block_time=block_time, failed=failed, fee_payer=fee_payer, fee=fee,
        programs=(SYSTEM,), transfers=(), account_keys=tuple(keys),
        pre_balances=tuple(pre_bal), post_balances=tuple(post_bal),
        pre_token_balances=tuple(pre_tok), post_token_balances=tuple(post_tok),
    )


def _link(tx: ParsedTx, payer: str, receiver: str) -> DelegatedLink:
    return DelegatedLink(tx.signature, tx.slot, tx.block_time, payer, receiver)


def _swap_and_send(*, failed=False) -> ParsedTx:
    """A (fee payer) платить пулу 2 SOL і створює ATA отримувача B; пул віддає B 3000 M."""
    return _tx(
        fee_payer=A, failed=failed,
        sol={A: (5 * SOL, 5 * SOL - FEE - RENT - 2 * SOL), VAULT: (SOL, 3 * SOL)},
        tokens=[("PoolAtaM", POOL, M, 10_000, 7_000), ("BAtaM", B, M, None, 3_000)],
    )


# --- тести, названі в T-045 ---------------------------------------------------------


def test_payer_without_token_and_receiver_without_spend_gives_link():
    (declared,) = DECLARED["links"]
    assert (declared["payer"], declared["receiver"]) == (A, B)
    parsed = _parsed(declared["signature"])

    result = detect_delegated(parsed, M)

    assert [_link_dict(r) for r in result] == [declared]
    assert all(isinstance(r, DelegatedLink) for r in result)
    # і в синтетичній формі того ж сценарію
    tx = _swap_and_send()
    assert detect_delegated(tx, M) == [_link(tx, A, B)]


def test_normal_purchase_gives_nothing():
    rows = _by_reason("ordinary_purchase")
    assert len(rows) == 3
    for row in rows:
        parsed = _parsed(row["signature"])
        buyers = [p.wallet for p in detect_purchases(parsed, M)]
        assert len(buyers) == 1  # покупець є (Δ>0 і витратив) — він ні платник, ні отримувач
        assert detect_delegated(parsed, M) == [], row


def test_airdrop_with_creator_paying_rent_gives_nothing():
    # фікстура: творець переказує свій токен (Δ<0) і платить комісію + ренту ATA отримувача
    (row,) = _by_reason("airdrop_creator_pays_rent")
    assert detect_delegated(_parsed(row["signature"]), M) == []

    # ейрдроп через mintTo: у творця Δ(M) == 0, він платить лише комісію й ренту ATA отримувача.
    # Рента створених рахунків віднімається з витрат fee payer — платника немає.
    airdrop = _tx(
        fee_payer=CREATOR,
        sol={CREATOR: (10 * SOL, 10 * SOL - FEE - RENT)},
        tokens=[("AirAtaM", AIR, M, None, 5_000)],
    )
    assert detect_delegated(airdrop, M) == []

    # контроль дискримінації: та сама форма, але творець ще й заплатив 0.01 SOL — це вже витрата
    paid = _tx(
        fee_payer=CREATOR,
        sol={CREATOR: (10 * SOL, 10 * SOL - FEE - RENT - 10_000_000), TIP: (SOL, SOL + 10_000_000)},
        tokens=[("AirAtaM", AIR, M, None, 5_000)],
    )
    assert detect_delegated(paid, M) == [_link(paid, CREATOR, AIR)]


def test_mint_to_creator_gives_nothing():
    (row,) = _by_reason("mint_to_creation")
    parsed = _parsed(row["signature"])
    # творець і пул отримали токен (R непорожня), але платників без токена немає
    assert parsed.token_delta(CREATOR, M) > 0
    assert detect_delegated(parsed, M) == []


def test_two_payers_two_receivers_gives_four_unpaired_candidates_no_link():
    declared = _declared_unpaired(next(r["signature"] for r in DECLARED["unpaired"]
                                       if r["detail"] == "payers=2 receivers=2"))
    result = detect_delegated(_parsed(declared[0]["signature"]), M)

    assert len(result) == 4
    assert all(isinstance(r, UnpairedCandidate) for r in result)
    assert [_cand_dict(r) for r in result] == declared
    assert {r.side for r in result} == {DelegatedSide.PAYER, DelegatedSide.RECEIVER}


def test_one_payer_two_receivers_gives_three_candidates_no_link():
    declared = _declared_unpaired(next(r["signature"] for r in DECLARED["unpaired"]
                                       if r["detail"] == "payers=1 receivers=2"))
    result = detect_delegated(_parsed(declared[0]["signature"]), M)

    assert len(result) == 3
    assert not any(isinstance(r, DelegatedLink) for r in result)
    assert [_cand_dict(r) for r in result] == declared
    payer = [r.wallet for r in result if r.side is DelegatedSide.PAYER]
    assert payer == [E]


def test_seller_with_negative_delta_is_not_a_payer():
    # S віддає 500 M отримувачу B (створює йому ATA) і платить 0.01 SOL чайових: S витратив, але Δ(M) < 0.
    tx = _tx(
        fee_payer=A,
        sol={A: (5 * SOL, 5 * SOL - FEE - RENT - 10_000_000), TIP: (SOL, SOL + 10_000_000)},
        tokens=[("SAtaM", A, M, 1_000, 500), ("BAtaM", B, M, None, 500)],
    )
    assert tx.token_delta(A, M) < 0
    assert detect_delegated(tx, M) == []


def test_pool_pda_receiving_mint_on_user_sell_is_purchase_not_receiver():
    # U продає 400 M пулу за 0.3 SOL; співпідписант X платить чайові (X — платник без токена).
    # Пул отримав mint і заплатив SOL — це «купівля» 001, не отримувач, тож пари для X немає.
    user = W["P1"]
    tx = _tx(
        fee_payer=user,
        sol={user: (2 * SOL, 2 * SOL + 300_000_000 - FEE), POOL: (50 * SOL, 50 * SOL - 300_000_000),
             X: (SOL, SOL - 10_000_000), TIP: (SOL, SOL + 10_000_000)},
        tokens=[("UserAtaM", user, M, 400, 0), ("PoolAtaM", POOL, M, 1_000, 1_400)],
    )
    assert [p.wallet for p in detect_purchases(tx, M)] == [POOL]
    assert detect_delegated(tx, M) == []


def test_failed_tx_gives_nothing():
    (declared,) = DECLARED["links"]
    parsed = _parsed(declared["signature"])
    assert detect_delegated(dataclasses.replace(parsed, failed=True), M) == []
    assert detect_delegated(_swap_and_send(failed=True), M) == []


def _permuted(tx: ParsedTx) -> ParsedTx:
    """Та сама транзакція з оберненим порядком ключів (індекси балансів перераховано) і токен-балансів.

    fee payer лишається першим ключем (так влаштований Solana), решта ключів — у зворотному порядку.
    """
    order = [0] + list(range(len(tx.account_keys) - 1, 0, -1))
    new_index = {old: new for new, old in enumerate(order)}

    def remap(balances):
        return tuple(reversed([dataclasses.replace(b, account_index=new_index[b.account_index]) for b in balances]))

    return dataclasses.replace(
        tx,
        account_keys=tuple(tx.account_keys[i] for i in order),
        pre_balances=tuple(tx.pre_balances[i] for i in order),
        post_balances=tuple(tx.post_balances[i] for i in order),
        pre_token_balances=remap(tx.pre_token_balances),
        post_token_balances=remap(tx.post_token_balances),
    )


def test_result_order_is_data_only():
    for signature in sorted({r["signature"] for r in DECLARED["unpaired"]}):
        parsed = _parsed(signature)
        permuted = _permuted(parsed)
        assert permuted.account_keys != parsed.account_keys
        result = detect_delegated(parsed, M)
        assert detect_delegated(permuted, M) == result
        assert result == sorted(result, key=unpaired_sort_key)
        assert [_cand_dict(r) for r in result] == _declared_unpaired(signature)
    (declared,) = DECLARED["links"]
    parsed = _parsed(declared["signature"])
    assert detect_delegated(_permuted(parsed), M) == detect_delegated(parsed, M)


# --- повна декларація swapsend і інваріанти моделі ----------------------------------


def test_swapsend_every_transaction_matches_declaration_exactly():
    links, unpaired = [], []
    by_signature = {}
    for signature in RPC["getTransaction"]:
        result = detect_delegated(_parsed(signature), M)
        by_signature[signature] = result
        links += [r for r in result if isinstance(r, DelegatedLink)]
        unpaired += [r for r in result if isinstance(r, UnpairedCandidate)]

    assert [_link_dict(r) for r in sorted(links, key=link_sort_key)] == DECLARED["links"]
    assert [_cand_dict(r) for r in sorted(unpaired, key=unpaired_sort_key)] == DECLARED["unpaired"]
    for row in DECLARED["no_link"]:
        assert by_signature[row["signature"]] == [], row
    declared_sigs = ({r["signature"] for r in DECLARED["links"]} | {r["signature"] for r in DECLARED["unpaired"]}
                     | {r["signature"] for r in DECLARED["no_link"]})
    assert declared_sigs == set(RPC["getTransaction"])  # жодна транзакція сценарію не лишилась без вердикту

    # інваріанти T-044 (один зв'язок на підпис, повні групи кандидатів) виконуються без підгонки
    analysis = DelegatedAnalysis.derive(links, unpaired, BuyersCompleteness(True, None, ""))
    assert [_link_dict(r) for r in analysis.links] == DECLARED["links"]


# --- межові випадки -----------------------------------------------------------------


def test_wsol_token_account_lamports_are_not_a_second_payer():
    # A платить 2 WSOL зі свого постійного WSOL-рахунку: lamports рахунку падають разом із WSOL-балансом.
    # Витрата вже видна як Δ(WSOL) < 0 власника A; сам токен-рахунок — не власник і не другий платник.
    tx = _tx(
        fee_payer=A,
        sol={A: (5 * SOL, 5 * SOL - FEE - RENT)},
        tokens=[
            ("AWsolAta", A, WSOL, 2 * SOL, 0, RENT + 2 * SOL, RENT),
            ("PoolWsol", POOL, WSOL, 10 * SOL, 12 * SOL, RENT + 10 * SOL, RENT + 12 * SOL),
            ("PoolAtaM", POOL, M, 10_000, 7_000),
            ("BAtaM", B, M, None, 3_000),
        ],
    )
    assert detect_delegated(tx, M) == [_link(tx, A, B)]


def test_pool_vault_paying_out_on_sell_is_not_a_payer_for_a_fee_receiver():
    # U продає M у пул Raydium-форми: WSOL-сховище пулу втрачає lamports (виплата WSOL), а комісію в M
    # отримує гаманець REF (Δ(M) > 0, нічого не платив). Власник сховища (POOL) отримав M і віддав WSOL —
    # покупець, а не платник. Якби сховище рахувалося власником, вийшов би хибний зв'язок сховище → REF.
    user, ref = W["P1"], W["F2"]
    tx = _tx(
        fee_payer=user,
        sol={user: (2 * SOL, 2 * SOL - FEE)},
        tokens=[
            ("UserAtaM", user, M, 1_000, 0),
            ("PoolAtaM", POOL, M, 10_000, 10_990),
            ("RefAtaM", ref, M, 0, 10),
            ("PoolWsol", POOL, WSOL, 10 * SOL, 9 * SOL, RENT + 10 * SOL, RENT + 9 * SOL),
            ("UserWsol", user, WSOL, 0, SOL, RENT, RENT + SOL),
        ],
    )
    assert detect_delegated(tx, M) == []


def test_closed_token_account_rent_refund_is_not_a_payer():
    # A платить 2 SOL за токен для B і заодно закриває свій порожній USDC-рахунок (рента повертається A).
    # Закритий рахунок втрачає lamports, але це не «витрата власника».
    tx = _tx(
        fee_payer=A,
        sol={A: (5 * SOL, 5 * SOL - FEE - RENT - 2 * SOL + RENT), VAULT: (SOL, 3 * SOL)},
        tokens=[
            ("AOldUsdc", A, USDC, 0, None, RENT, 0),
            ("PoolAtaM", POOL, M, 10_000, 7_000),
            ("BAtaM", B, M, None, 3_000),
        ],
    )
    assert detect_delegated(tx, M) == [_link(tx, A, B)]


def test_payer_spending_other_mint_counts_as_payer():
    # A платить 100 USDC (SOL — лише комісія й рента ATA отримувача): це витрата за R-2.
    tx = _tx(
        fee_payer=A,
        sol={A: (5 * SOL, 5 * SOL - FEE - RENT)},
        tokens=[("AUsdc", A, USDC, 500, 400), ("PoolUsdc", POOL, USDC, 0, 100),
                ("PoolAtaM", POOL, M, 10_000, 7_000), ("BAtaM", B, M, None, 3_000)],
    )
    assert detect_delegated(tx, M) == [_link(tx, A, B)]


def test_receiver_that_spent_other_mint_is_a_buyer_not_receiver():
    # A платить SOL, але B теж віддав 50 USDC: B — покупець (Δ>0 і витратив), отримувачів немає.
    tx = _tx(
        fee_payer=A,
        sol={A: (5 * SOL, 5 * SOL - FEE - RENT - SOL), VAULT: (SOL, 2 * SOL)},
        tokens=[("BUsdc", B, USDC, 50, 0), ("PoolUsdc", POOL, USDC, 0, 50),
                ("PoolAtaM", POOL, M, 10_000, 7_000), ("BAtaM", B, M, None, 3_000)],
    )
    assert detect_delegated(tx, M) == []


def test_owner_with_several_token_accounts_is_judged_by_net_delta():
    sol = {A: (5 * SOL, 5 * SOL - FEE - SOL), VAULT: (SOL, 2 * SOL)}
    # B лише переклав 100 M між своїми рахунками: Δ(M) == 0 — не отримувач
    shuffle = _tx(fee_payer=A, sol=sol, tokens=[("BAta1", B, M, 100, 0), ("BAta2", B, M, 0, 100)])
    assert detect_delegated(shuffle, M) == []
    # B отримав на два рахунки: один отримувач (не два кандидати)
    split = _tx(fee_payer=A, sol=sol, tokens=[("PoolAtaM", POOL, M, 1_000, 500),
                                              ("BAta1", B, M, 0, 300), ("BAta2", B, M, 0, 200)])
    assert detect_delegated(split, M) == [_link(split, A, B)]


def test_owner_known_only_from_token_balances_is_a_receiver():
    # власник рахунку-отримувача (напр. PDA) не є ключем транзакції — його видно лише з токен-балансів
    pda = W["POOL_ATA"]
    tx = _tx(fee_payer=A, sol={A: (5 * SOL, 5 * SOL - FEE - SOL), VAULT: (SOL, 2 * SOL)},
             tokens=[("PoolAtaM", POOL, M, 1_000, 500), ("PdaAtaM", pda, M, 0, 500)])
    assert pda not in tx.account_keys
    assert detect_delegated(tx, M) == [_link(tx, A, pda)]


def test_zero_mint_delta_is_neither_receiver_nor_buyer():
    # у B є рахунок M, але баланс не змінився: Δ == 0 і не витрачав — ні в P, ні в R
    tx = _tx(fee_payer=A, sol={A: (5 * SOL, 5 * SOL - FEE - SOL), VAULT: (SOL, 2 * SOL)},
             tokens=[("BAtaM", B, M, 700, 700)])
    assert detect_delegated(tx, M) == []


def test_transaction_without_token_balances_gives_nothing():
    # стара/усічена транзакція без pre/postTokenBalances: ніхто не має Δ(M) > 0 — отримувача не вигадуємо
    tx = _tx(fee_payer=A, sol={A: (5 * SOL, 5 * SOL - FEE - SOL), VAULT: (SOL, 2 * SOL)})
    assert detect_delegated(tx, M) == []


def test_payer_and_receiver_sets_are_disjoint_and_fee_payer_paying_only_fee_is_receiver():
    # B — fee payer, отримав токен і заплатив лише комісію й ренту свого ATA; платить A — зв'язок A → B
    tx = _tx(
        fee_payer=B,
        sol={B: (SOL, SOL - FEE - RENT), A: (5 * SOL, 3 * SOL), VAULT: (SOL, 3 * SOL)},
        tokens=[("PoolAtaM", POOL, M, 10_000, 7_000), ("BAtaM", B, M, None, 3_000)],
    )
    assert detect_delegated(tx, M) == [_link(tx, A, B)]


def test_inconsistent_balance_arrays_raise_instead_of_guessing():
    tx = _swap_and_send()
    with pytest.raises(ValueError, match="balances"):
        detect_delegated(dataclasses.replace(tx, post_balances=tx.post_balances[:-1]), M)
    bad_index = dataclasses.replace(tx.post_token_balances[0], account_index=len(tx.account_keys))
    with pytest.raises(ValueError, match="account_index"):
        detect_delegated(dataclasses.replace(tx, post_token_balances=(bad_index,) + tx.post_token_balances[1:]), M)


def test_rejects_non_parsed_input_and_bad_mint():
    tx = _swap_and_send()
    with pytest.raises(TypeError):
        detect_delegated(CorruptRecord(tx.signature, "unresolved_transfer", "x", partial=tx), M)
    with pytest.raises(ValueError):
        detect_delegated(tx, "")
    with pytest.raises(TypeError):
        detect_delegated(tx, None)
