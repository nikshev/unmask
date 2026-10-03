# verifies: FR-001-01, FR-001-02
"""Правило купівлі (research R-2, T-010): `detect_purchases(parsed, mint) -> list[Purchase]`.

Власник O купив mint M у транзакції T ⇔ `token_delta(O, M) > 0` **і**
(`spent_sol(O) > 0` **або** ∃ m≠M: `token_delta(O, m) < 0`), де
`spent_sol = −Δsol(O) − fee·[O=fee_payer] − created_accounts_lamports·[O=fee_payer]`.
Списку DEX немає (Q2): правило дивиться лише на балансові дельти.

Еталон — `scenarios/basic` (незалежний генератор): по всіх транзакціях basic правило дає рівно
5 покупців expected.json і жодного зайвого (творець і отримувач `mintTo`, отримувач ейрдропу,
пул-продавець у чужих купівлях, отримувач USDC). Межові випадки — синтетичні `ParsedTx`, зібрані тут.
"""

import json
from pathlib import Path

import pytest

from unmask.ingest.model import AddressType, Asset, Spend
from unmask.ingest.parse import CorruptRecord, ParsedTx, TokenBalance, parse_transaction
from unmask.ingest.purchases import Purchase, detect_purchases

BASIC = Path(__file__).parent / "fixtures" / "scenarios" / "basic"
RPC = json.loads((BASIC / "rpc.json").read_text())
EXPECTED = json.loads((BASIC / "expected.json").read_text())
W = EXPECTED["wallets"]
M = EXPECTED["mint"]

WSOL = "So11111111111111111111111111111111111111112"
USDC = W["USDC"]
OTHER = W["ROUTER"]          # інший mint (не цільовий)
DEX = W["DEX"]               # програма свопу — лише ідентифікатор, не список
SYSTEM = "11111111111111111111111111111111"
ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"

# Ролі (усі — валідні адреси; P5 — off-curve, як пул/PDA).
BUYER, BUYER2, SENDER, SPONSOR, CREATOR = W["B"], W["C"], W["A"], W["F"], W["CREATOR"]
POOL = W["P5"]
FEE = 5000
RENT = 2_039_280
SOL = 10**9


# --- Синтетичні транзакції ----------------------------------------------------------


def _tx(*, fee_payer: str, sol: dict[str, tuple[int, int]], tokens=(), created=(), failed=False,
        programs=(ATA_PROGRAM, SYSTEM, DEX), sig="SyntheticBuy1111", slot=42, block_time=1759400042) -> ParsedTx:
    """`ParsedTx` із явними балансами.

    `sol`: власник -> (pre, post) lamports (fee payer — першим ключем).
    `tokens`: (власник, mint, pre, post) токен-рахунку; `None` — рахунку немає в pre (створений) чи post
    (закритий). Створений токен-рахунок має lamports (0, RENT), наявний — (RENT, RENT).
    `created`: (ключ, post_lamports) інших рахунків, створених у транзакції (mint-рахунок тощо).
    """
    keys, pre_bal, post_bal = [fee_payer], [sol[fee_payer][0]], [sol[fee_payer][1]]
    for owner, (pre, post) in sol.items():
        if owner != fee_payer:
            keys.append(owner), pre_bal.append(pre), post_bal.append(post)
    pre_tok, post_tok = [], []
    for i, (owner, mint, pre, post) in enumerate(tokens):
        index = len(keys)
        keys.append(f"TokenAccount{i}")
        pre_bal.append(0 if pre is None else RENT)
        post_bal.append(0 if post is None else RENT)
        if pre is not None:
            pre_tok.append(TokenBalance(index, mint, owner, pre, 6))
        if post is not None:
            post_tok.append(TokenBalance(index, mint, owner, post, 6))
    for key, post in created:
        keys.append(key), pre_bal.append(0), post_bal.append(post)
    return ParsedTx(
        signature=sig, slot=slot, block_time=block_time, failed=failed, fee_payer=fee_payer, fee=FEE,
        programs=tuple(programs), transfers=(), account_keys=tuple(keys),
        pre_balances=tuple(pre_bal), post_balances=tuple(post_bal),
        pre_token_balances=tuple(pre_tok), post_token_balances=tuple(post_tok),
    )


def _sol_buy(*, failed=False) -> ParsedTx:
    # BUYER (fee payer) створює свій ATA і платить пулу 0.6 SOL; пул віддає 400 M.
    return _tx(
        fee_payer=BUYER, failed=failed,
        sol={BUYER: (10 * SOL, 10 * SOL - 600_000_000 - FEE - RENT), POOL: (50 * SOL, 50 * SOL + 600_000_000)},
        tokens=[(POOL, M, 1000, 600), (BUYER, M, None, 400)],
    )


def _purchase(wallet, tx: ParsedTx, received, spent, address_type=AddressType.WALLET) -> Purchase:
    return Purchase(
        wallet=wallet, signature=tx.signature, slot=tx.slot, block_time=tx.block_time,
        received_amount=received, spent=tuple(spent), programs=tx.programs, address_type=address_type,
    )


# --- Задані тести T-010 -------------------------------------------------------------


def test_swap_sol_for_token_is_purchase_with_spent_sol():
    tx = _sol_buy()
    # Пул (віддав M) не покупець; витрата покупця — без комісії й ренти власного ATA.
    assert detect_purchases(tx, M) == [_purchase(BUYER, tx, 400, [Spend(Asset.SOL, 600_000_000)])]


def test_swap_usdc_for_token_is_purchase_with_spent_spl():
    # BUYER віддає 25 USDC з наявного рахунку; SOL витрачено лише на комісію.
    tx = _tx(
        fee_payer=BUYER,
        sol={BUYER: (SOL, SOL - FEE), POOL: (SOL, SOL)},
        tokens=[(BUYER, USDC, 100, 75), (POOL, USDC, 0, 25), (BUYER, M, 10, 410), (POOL, M, 1000, 600)],
    )
    assert detect_purchases(tx, M) == [_purchase(BUYER, tx, 400, [Spend(Asset.spl(USDC), 25)])]


@pytest.mark.parametrize("fee_payer", [SENDER, BUYER], ids=["sender_pays", "recipient_pays_fee"])
def test_plain_token_transfer_recipient_is_not_purchase(fee_payer):
    sol = {SENDER: (SOL, SOL), BUYER: (SOL, SOL)}
    sol[fee_payer] = (SOL, SOL - FEE)
    tx = _tx(fee_payer=fee_payer, sol=sol, tokens=[(SENDER, M, 100, 75), (BUYER, M, 5, 30)], programs=(TOKEN,))
    assert detect_purchases(tx, M) == []


def test_airdrop_recipient_paying_own_ata_rent_is_not_purchase():
    # Отримувач — fee payer, сам створює свій ATA: його SOL зменшився рівно на fee + rent.
    tx = _tx(
        fee_payer=BUYER,
        sol={BUYER: (SOL, SOL - FEE - RENT), SENDER: (SOL, SOL)},
        tokens=[(SENDER, M, 100, 50), (BUYER, M, None, 50)],
        programs=(ATA_PROGRAM, TOKEN),
    )
    assert detect_purchases(tx, M) == []


def test_mint_to_creator_paying_rent_is_not_purchase():
    # Творець створює mint-рахунок і два ATA, `mintTo` собі та пулу; платить лише fee + рента.
    mint_rent = 1_461_600
    tx = _tx(
        fee_payer=CREATOR,
        sol={CREATOR: (5 * SOL, 5 * SOL - FEE - mint_rent - 2 * RENT), POOL: (0 + SOL, SOL)},
        tokens=[(CREATOR, M, None, 100), (POOL, M, None, 900)],
        created=[(M, mint_rent)],
        programs=(SYSTEM, TOKEN, ATA_PROGRAM, ATA_PROGRAM, TOKEN, TOKEN),
    )
    assert detect_purchases(tx, M) == []


def test_pool_pda_receiving_tokens_on_user_sell_is_purchase_off_curve():
    # Користувач (fee payer) продає 2000 M пулу за 2.5 SOL: пул «купує» — і позначений off_curve.
    tx = _tx(
        fee_payer=BUYER,
        sol={BUYER: (SOL, SOL + 2_500_000_000 - FEE), POOL: (50 * SOL, 50 * SOL - 2_500_000_000)},
        tokens=[(BUYER, M, 2000, 0), (POOL, M, 1000, 3000)],
        programs=(DEX,),
    )
    assert detect_purchases(tx, M) == [
        _purchase(POOL, tx, 2000, [Spend(Asset.SOL, 2_500_000_000)], AddressType.OFF_CURVE)
    ]


def test_sponsored_swap_non_fee_payer_is_purchase():
    # Спонсор платить комісію й ренту ATA покупця; покупець віддає пулу 0.3 SOL.
    tx = _tx(
        fee_payer=SPONSOR,
        sol={SPONSOR: (SOL, SOL - FEE - RENT), BUYER: (SOL, SOL - 300_000_000), POOL: (SOL, SOL + 300_000_000)},
        tokens=[(POOL, M, 1000, 850), (BUYER, M, None, 150)],
    )
    assert detect_purchases(tx, M) == [_purchase(BUYER, tx, 150, [Spend(Asset.SOL, 300_000_000)])]


def test_failed_tx_yields_nothing():
    assert detect_purchases(_sol_buy(failed=True), M) == []


# --- Golden: увесь basic --------------------------------------------------------------


def _expected_purchase(row: dict) -> Purchase:
    return Purchase(
        wallet=row["wallet"], signature=row["first_buy_signature"], slot=row["first_buy_slot"],
        block_time=row["first_buy_time"], received_amount=row["received_amount"],
        spent=tuple(Spend(Asset(s["asset"]), s["amount"]) for s in row["spent"]),
        programs=tuple(row["programs"]), address_type=AddressType(row["address_type"]),
    )


def test_golden_basic_all_transactions_yield_exactly_expected_buyers():
    parsed = [parse_transaction(raw) for raw in RPC["getTransaction"].values()]
    assert not [p for p in parsed if isinstance(p, CorruptRecord)]  # basic чистий: нічого не губиться
    found = [purchase for tx in parsed for purchase in detect_purchases(tx, M)]
    expected = [_expected_purchase(row) for row in EXPECTED["buyers"]]
    key = lambda p: (p.slot, p.signature, p.wallet)  # noqa: E731
    # Рівно ці 5 (кожен — один раз), без творця, отримувачів mintTo/ейрдропу і пулу-продавця.
    assert sorted(found, key=key) == sorted(expected, key=key)
    assert {p.wallet for p in found}.isdisjoint({W["CREATOR"], W["U"], W["S"]})


def test_golden_basic_creator_mint_and_airdrop_txs_have_no_purchase():
    # Явно: у транзакціях творення токена й ейрдропу токен-дельта > 0 є, купівлі — немає.
    seen_positive = 0
    for raw in RPC["getTransaction"].values():
        tx = parse_transaction(raw)
        receivers = {b.owner for b in tx.post_token_balances if b.mint == M and tx.token_delta(b.owner, M) > 0}
        if receivers and DEX not in tx.programs:
            seen_positive += 1
            assert detect_purchases(tx, M) == [], tx.signature
    assert seen_positive >= 2  # mintTo творцю/пулу та ейрдроп U справді є в basic


# --- Межові випадки ---------------------------------------------------------------------


def test_several_owners_in_one_tx_each_judged_separately():
    # BUYER (fee payer) і BUYER2 купують в одній транзакції; SENDER отримує M «у подарунок» без витрат.
    tx = _tx(
        fee_payer=BUYER,
        sol={
            BUYER: (10 * SOL, 10 * SOL - 100_000_000 - FEE - 3 * RENT),
            BUYER2: (SOL, SOL - 200_000_000),
            SENDER: (SOL, SOL),
            POOL: (SOL, SOL + 300_000_000),
        },
        tokens=[(POOL, M, 1000, 400), (BUYER, M, None, 100), (BUYER2, M, None, 200), (SENDER, M, None, 300)],
    )
    expected = [
        _purchase(BUYER, tx, 100, [Spend(Asset.SOL, 100_000_000)]),
        _purchase(BUYER2, tx, 200, [Spend(Asset.SOL, 200_000_000)]),
    ]
    assert detect_purchases(tx, M) == sorted(expected, key=lambda p: p.wallet)


def test_spent_wsol_counts_as_spl_spend():
    # WSOL лишається SPL-активом: від'ємна дельта WSOL — витрата `spl:So11…112`, не SOL.
    tx = _tx(
        fee_payer=BUYER,
        sol={BUYER: (SOL, SOL - FEE), POOL: (SOL, SOL)},
        tokens=[(BUYER, WSOL, 500, 200), (POOL, WSOL, 0, 300), (BUYER, M, 0, 40), (POOL, M, 100, 60)],
    )
    assert detect_purchases(tx, M) == [_purchase(BUYER, tx, 40, [Spend(Asset.spl(WSOL), 300)])]


def test_wrap_sol_partially_left_in_new_wsol_account_is_not_double_counted():
    # BUYER створює WSOL-рахунок, кладе 1 SOL, свопає 0.6 WSOL, 0.4 лишає у відкритому рахунку.
    # Рахунок створений → його post (rent + 0.4 SOL) у created_accounts_lamports; витрата — рівно 0.6 SOL.
    keys = (BUYER, "WsolAccount", "MAccount", POOL)
    tx = ParsedTx(
        signature="SyntheticWrap1111", slot=50, block_time=None, failed=False, fee_payer=BUYER, fee=FEE,
        programs=(SYSTEM, TOKEN, DEX), transfers=(), account_keys=keys,
        pre_balances=(10 * SOL, 0, 0, SOL),
        post_balances=(10 * SOL - SOL - 2 * RENT - FEE, RENT + 400_000_000, RENT, SOL),
        pre_token_balances=(TokenBalance(3, M, POOL, 1000, 6),),
        post_token_balances=(
            TokenBalance(1, WSOL, BUYER, 400_000_000, 9), TokenBalance(2, M, BUYER, 70, 6),
            TokenBalance(3, M, POOL, 930, 6),
        ),
    )
    assert detect_purchases(tx, M) == [_purchase(BUYER, tx, 70, [Spend(Asset.SOL, 600_000_000)])]


def test_wsol_as_target_mint_is_judged_like_any_mint():
    # Купівля WSOL за USDC — купівля mint=WSOL; для M у цій транзакції купівлі немає.
    tx = _tx(
        fee_payer=BUYER,
        sol={BUYER: (SOL, SOL - FEE)},
        tokens=[(BUYER, USDC, 100, 0), (POOL, USDC, 0, 100), (BUYER, WSOL, 0, 7), (POOL, WSOL, 10, 3)],
    )
    assert detect_purchases(tx, WSOL) == [_purchase(BUYER, tx, 7, [Spend(Asset.spl(USDC), 100)])]
    assert detect_purchases(tx, M) == []


def test_spent_sol_and_spl_together_sol_first_then_spl_by_asset():
    tx = _tx(
        fee_payer=BUYER,
        sol={BUYER: (SOL, SOL - 1000 - FEE), POOL: (SOL, SOL + 1000)},
        tokens=[(BUYER, WSOL, 9, 4), (BUYER, USDC, 9, 7), (BUYER, M, 0, 3), (POOL, M, 3, 0)],
    )
    [purchase] = detect_purchases(tx, M)
    assert USDC < WSOL  # "spl:71WL…" < "spl:So11…"
    assert purchase.spent == (Spend(Asset.SOL, 1000), Spend(Asset.spl(USDC), 2), Spend(Asset.spl(WSOL), 5))


def test_zero_net_token_delta_is_not_purchase():
    # BUYER отримує 100 M і в тій самій транзакції віддає 100 M далі, витративши SOL.
    tx = _tx(
        fee_payer=BUYER,
        sol={BUYER: (SOL, SOL - 50_000 - FEE), POOL: (SOL, SOL + 50_000), SENDER: (SOL, SOL)},
        tokens=[(POOL, M, 1000, 900), (BUYER, M, 20, 20), (SENDER, M, 0, 100)],
    )
    assert [p.wallet for p in detect_purchases(tx, M)] == []


def test_negative_token_delta_is_not_purchase():
    # BUYER продає M за USDC: віддав M (дельта < 0) — не купівля, хоч інший актив і змінився.
    tx = _tx(
        fee_payer=BUYER,
        sol={BUYER: (SOL, SOL - FEE)},
        tokens=[(BUYER, M, 100, 0), (BUYER, USDC, 0, 50), (POOL, USDC, 50, 0), (POOL, M, 0, 100)],
    )
    assert [p.wallet for p in detect_purchases(tx, M)] == [POOL]


def test_positive_delta_without_any_spend_is_not_purchase():
    # Не fee payer отримує M; SOL не змінився, інших активів не віддав.
    tx = _tx(
        fee_payer=SENDER,
        sol={SENDER: (SOL, SOL - FEE), BUYER: (SOL, SOL)},
        tokens=[(SENDER, M, 10, 0), (BUYER, M, 0, 10)],
    )
    assert detect_purchases(tx, M) == []


def test_received_sol_with_positive_token_delta_is_not_purchase():
    # Отримав і M, і SOL (spent_sol < 0), нічого не віддав — не купівля.
    tx = _tx(
        fee_payer=SENDER,
        sol={SENDER: (SOL, SOL - 1000 - FEE), BUYER: (SOL, SOL + 1000)},
        tokens=[(SENDER, M, 10, 0), (BUYER, M, 0, 10)],
    )
    assert detect_purchases(tx, M) == []


def test_other_mint_purchase_is_not_target_purchase():
    tx = _tx(
        fee_payer=BUYER,
        sol={BUYER: (SOL, SOL - 1000 - FEE), POOL: (SOL, SOL + 1000)},
        tokens=[(POOL, OTHER, 50, 40), (BUYER, OTHER, 0, 10)],
    )
    assert detect_purchases(tx, M) == []
    assert detect_purchases(tx, OTHER) == [_purchase(BUYER, tx, 10, [Spend(Asset.SOL, 1000)])]


def test_token_amount_summed_across_owner_accounts():
    # Два рахунки M одного власника: +30 і −10 → дельта +20.
    tx = _tx(
        fee_payer=BUYER,
        sol={BUYER: (SOL, SOL - 1000 - FEE), POOL: (SOL, SOL + 1000)},
        tokens=[(BUYER, M, 0, 30), (BUYER, M, 50, 40), (POOL, M, 100, 80)],
    )
    assert detect_purchases(tx, M) == [_purchase(BUYER, tx, 20, [Spend(Asset.SOL, 1000)])]


def test_token_balance_without_owner_is_never_a_buyer():
    tx = _tx(fee_payer=BUYER, sol={BUYER: (SOL, SOL - 1000 - FEE)}, tokens=[(None, M, 0, 10), (POOL, M, 10, 0)])
    assert detect_purchases(tx, M) == []


def test_rejects_non_parsed_tx():
    with pytest.raises(TypeError):
        detect_purchases(CorruptRecord("sig", "malformed", "x"), M)


def test_purchase_validates_itself():
    tx = _sol_buy()
    with pytest.raises(ValueError):
        _purchase(BUYER, tx, 0, [Spend(Asset.SOL, 1)])
    with pytest.raises(ValueError):
        _purchase(BUYER, tx, 1, [])
