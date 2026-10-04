# impl: FR-002-15, FR-002-16, FR-002-19
"""Правило делегованої купівлі — swap-and-send (research R-2, T-045).

Що робить: `detect_delegated(parsed, mint)` для однієї розібраної транзакції mint M повертає
`[DelegatedLink]`, кандидатів `UnpairedCandidate` або `[]`. Правило працює лише на балансах:

    O        = (account_keys \\ токен-рахунки) ∪ власники з pre/postTokenBalances
    spent(O) = purchases.spent_sol(O) > 0  або  ∃ m ≠ M: token_delta(O, m) < 0
    P        = { O : token_delta(O, M) == 0 і spent(O) }      — платники
    R        = { O : token_delta(O, M) >  0 і не spent(O) }   — отримувачі

- |P| == 1 і |R| == 1 → один `DelegatedLink(payer, receiver)`;
- обидві сторони непорожні, але не 1:1 → `UnpairedCandidate` на кожного учасника з
  `detail="payers=<|P|> receivers=<|R|>"`: суми SOL і токена без ціни незіставні, пару не вгадуємо (FR-002-16);
- одна сторона порожня або `failed` → `[]`.

`spent_sol` — та сама функція, що в правилі купівлі 001 (комісія й рента рахунків, створених у
транзакції, віднімаються лише у fee payer), тож «витратив» означає те саме, що для покупця. Продавець
(Δ(M) < 0) і покупець (Δ(M) > 0 і spent) не є ні платниками, ні отримувачами; P і R не перетинаються за
побудовою (Δ == 0 проти Δ > 0), тому payer ≠ receiver і гаманець ніколи не стоїть на обох сторонах.

Чому токен-рахунки вилучено з `account_keys` (уточнення «власників» R-2). Адреса, на яку посилається
`accountIndex` хоч одного токен-балансу, — токен-рахунок, а не власник; його lamports — це рента (її
облік уже веде `created_accounts_lamports` fee payer) або WSOL, який і так видно як Δ(`So11…112`)
його власника. Якби такий рахунок рахувався власником, то: оплата з постійного WSOL-рахунку дала б
другого «платника» (хибний кандидат 2×1 замість зв'язку); виплата WSOL зі сховища пулу при продажу —
хибного платника-сховище, і будь-який отримувач токена (комісія в M) став би «зв'язком» сховище → гаманець;
закриття порожнього рахунку з поверненням ренти — теж «витрата». Ефемерні рахунки (T-050, створені й
закриті в тій самій транзакції) у балансах не значаться, але мають pre = post = 0 lamports і на
`spent` не впливають. Власники, що з'являються лише в токен-балансах (PDA поза ключами), — у O.

Межові випадки, що вирішуються самими даними:
- власник із кількома токен-рахунками — сумарна Δ (`ParsedTx.token_delta`): перекладання між своїми
  рахунками дає 0, отримання на два рахунки — один отримувач;
- нульові дельти — ні платник (якщо не витрачав), ні отримувач;
- транзакція без pre/postTokenBalances (стара/усічена) — Δ(M) ні в кого не > 0, R = ∅ → `[]`:
  без балансів токена отримувача довести нема чим, і зв'язок не вигадується; що історія неповна,
  каже повнота збору (`buyers`), а не це правило (R-3 п. 5 — одне джерело повноти);
- суперечливий `ParsedTx` (довжини `account_keys`/`pre_balances`/`post_balances` різні, `accountIndex`
  поза ключами) — `ValueError`: `zip` мовчки обрізав би рахунки й міг перетворити 2×1 на хибний 1:1.
  `parse_transaction` такого на реальних і фікстурних даних не породжує.

Порядок результату — лише з даних: зв'язки за `link_sort_key` (slot, signature, payer, receiver),
кандидати за `unpaired_sort_key` (slot, signature, wallet, side); від порядку ключів чи балансів не
залежить. `CorruptRecord.partial` викликач передає як `ParsedTx` (баланси цілі; R-2) — сам
`CorruptRecord` тут `TypeError`, як у `detect_purchases`.

Як користуватись: `detect_delegated(parsed, mint)`; збирання в `DelegatedAnalysis` і запис у стан
збору — справа викликача (`buyers`/`collector`, T-046). Склад і порядок покупців 001 не зачіпає.
Залежить від: `parse.ParsedTx`, `purchases.spent_sol`, `model` (типи й ключі сортування). Мережі не торкається.
"""

from __future__ import annotations

from unmask.ingest.model import (
    DelegatedLink,
    DelegatedSide,
    UnpairedCandidate,
    unpaired_sort_key,
)
from unmask.ingest.parse import ParsedTx
from unmask.ingest.purchases import spent_sol


def _check_consistent(parsed: ParsedTx) -> None:
    n = len(parsed.account_keys)
    if len(parsed.pre_balances) != n or len(parsed.post_balances) != n:
        raise ValueError(
            f"detect_delegated: {parsed.signature}: account_keys={n}, pre_balances={len(parsed.pre_balances)}, "
            f"post_balances={len(parsed.post_balances)} — inconsistent balances"
        )
    for b in parsed.pre_token_balances + parsed.post_token_balances:
        if not 0 <= b.account_index < n:
            raise ValueError(f"detect_delegated: {parsed.signature}: token balance account_index "
                             f"{b.account_index} is outside account_keys (0..{n - 1})")


def _owners(parsed: ParsedTx) -> list[str]:
    balances = parsed.pre_token_balances + parsed.post_token_balances
    token_accounts = {parsed.account_keys[b.account_index] for b in balances}
    owners = {key for key in parsed.account_keys if key not in token_accounts}
    owners |= {b.owner for b in balances if b.owner is not None}
    return sorted(owners)


def _spent(parsed: ParsedTx, owner: str, other_mints: list[str]) -> bool:
    if spent_sol(parsed, owner) > 0:
        return True
    return any(parsed.token_delta(owner, m) < 0 for m in other_mints)


def detect_delegated(parsed: ParsedTx, mint: str) -> list[DelegatedLink | UnpairedCandidate]:
    """Делегована купівля `mint` у транзакції `parsed` за правилом R-2 (див. докстрінг модуля)."""
    if not isinstance(parsed, ParsedTx):
        raise TypeError(f"detect_delegated: expected ParsedTx, got {type(parsed).__name__}")
    if not isinstance(mint, str):
        raise TypeError(f"detect_delegated: mint must be str, got {mint!r}")
    if not mint:
        raise ValueError("detect_delegated: mint must be non-empty")
    if parsed.failed:
        return []
    _check_consistent(parsed)

    balances = parsed.pre_token_balances + parsed.post_token_balances
    other_mints = sorted({b.mint for b in balances if b.mint != mint})
    payers: list[str] = []
    receivers: list[str] = []
    for owner in _owners(parsed):
        delta = parsed.token_delta(owner, mint)
        spent = _spent(parsed, owner, other_mints)
        # Δ < 0 (продавець / відправник токена) не потрапляє нікуди: платник — рівно нуль по M.
        if delta == 0 and spent:
            payers.append(owner)
        elif delta > 0 and not spent:
            receivers.append(owner)

    if not payers or not receivers:
        return []
    common = dict(signature=parsed.signature, slot=parsed.slot, block_time=parsed.block_time)
    if len(payers) == 1 and len(receivers) == 1:
        return [DelegatedLink(**common, payer=payers[0], receiver=receivers[0])]
    detail = f"payers={len(payers)} receivers={len(receivers)}"
    candidates = [UnpairedCandidate(**common, wallet=w, side=DelegatedSide.PAYER, detail=detail) for w in payers]
    candidates += [UnpairedCandidate(**common, wallet=w, side=DelegatedSide.RECEIVER, detail=detail)
                   for w in receivers]
    return sorted(candidates, key=unpaired_sort_key)
