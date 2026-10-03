# impl: FR-001-01, FR-001-02
"""Перші N покупців токена з курсором (research R-6, R-7).

Що робить: `enumerate_buyers(source, mint, state, config, deadline) -> BuyersCompleteness`
1. перегортає `get_signatures_for_address(mint)` від найновішого через `before` сторінками
   `rpc.page_size`, доки джерело не поверне порожню сторінку (кінець історії за контрактом);
   у `state` зберігає `(signature, slot, block_time, err)` і `signature_cursor`;
2. лише після вичерпання історії бере транзакції **від найстаріших** — упорядковані за
   `(slot, signature)`, а не за порядком відповіді, — пакетами `rpc.page_size`; записи з
   `err != null` не запитуються; кожну розібрану транзакцію пропускає через `detect_purchases`;
3. для кожного гаманця тримає першу купівлю за ключем `(slot, signature)` (повтор не займає
   місця в N); коли унікальних покупців стало N, **добиває поточний слот до кінця**, далі не йде;
4. сортує за `(first_buy_slot, first_buy_signature, wallet)`, бере рівно N, проставляє `rank`
   (`select_first_n`). Менше за N покупців — усі, `complete=true`.

Покупці з `address_type=off_curve` (пули, PDA) рахуються в N нарівні з іншими: відсікання
хабів і пулів — окремий модуль (принцип VI); тут вершина лише позначена.

Неповнота не мовчки (принцип V): результат — `BuyersCompleteness`.
- Відмова джерела (`RpcRateLimited`/`RpcTimeout`/`RpcUnavailable`) посеред перегортання —
  `complete=false` з причиною й курсором у `detail`; покупці не відбираються (найстаріших записів
  не бачили — перших N не вигадуємо), `state.mint_history_exhausted` лишається `false`.
- Відмова посеред завантаження транзакцій — `complete=false`; відбираються лише покупці з уже
  розібраних транзакцій (від найстаріших), без вигаданих.
- `None` від `get_transactions` → `unavailable`; `CorruptRecord` → `corrupt_data`; підпис — у
  `detail`. Розбір решти триває. Пошкоджена транзакція в `tx_cache` не потрапляє (повтор спробує
  знову); з `CorruptRecord.partial` (нерозпізнаний переказ при цілих балансах) купівлі все одно
  беруться — дані не губляться, але неповнота позначена.
Перша з таких причин (у порядку обробки) потрапляє в `reason`, усі підписи — у `detail`.

Лічильники в `state`: `rpc_calls` — кожне звернення до джерела (й невдале), накопичується;
`transactions_scanned` — скільки транзакцій mint пропущено через правило купівлі в ОСТАННЬОМУ виклику
(скидається на початку; рахується кожна розібрана правилом транзакція — і запитана, і взята з
`state.tx_cache`), тож детермінований: після будь-якого повтору дорівнює свіжому прогону.
Транзакції з `state.tx_cache` повторно не запитуються. `purchases_by_wallet` на кожному виклику
перевиводиться з нуля (кеш відтворюється, решта дозапитується), тож повтор після збою дає ту саму
вибірку, що й свіжий прогін (T-014).

Запити транзакцій ліниві: пакет (до `rpc.page_size` ще не закешованих підписів від поточного, а після
досягнення N — лише зі слота N-го покупця) запитується, лише коли розбір дійшов до незакешованого
підпису. У свіжому прогоні пакети ті самі, що й «вікно `page_size` від поточного»; на повторі,
коли N досягається на закешованих транзакціях, транзакції за межею слота (отримані, але не розібрані
першим проходом) не запитуються — повторний виклик на повному стані не звертається до джерела.

`deadline` лише передається в кожен виклик джерела; перевірки `expired()` і мапування в
`budget_exhausted` додає T-015 (`budget.Deadline`).

Залежить від: `rpc.protocol` (джерело, винятки), `parse`, `purchases`, `model`, `collector.CollectionState`.
"""

from __future__ import annotations

from typing import Iterable, Protocol

from unmask.ingest.collector import CollectionState
from unmask.ingest.config import IngestConfig
from unmask.ingest.model import Buyer, BuyersCompleteness, MissingReason
from unmask.ingest.parse import CorruptRecord, ParsedTx, parse_transaction
from unmask.ingest.purchases import Purchase, detect_purchases
from unmask.ingest.rpc.protocol import RpcRateLimited, RpcSource, RpcTimeout, RpcUnavailable


class DeadlineLike(Protocol):
    """Структурний тип дедлайну; реалізація — `budget.Deadline` (T-015)."""

    def expired(self) -> bool: ...

    def remaining(self) -> float: ...


_RPC_REASONS: tuple[tuple[type[Exception], MissingReason], ...] = (
    (RpcRateLimited, MissingReason.RATE_LIMITED),
    (RpcTimeout, MissingReason.TIMEOUT),
    (RpcUnavailable, MissingReason.UNAVAILABLE),
)
_RPC_ERRORS = tuple(exc for exc, _ in _RPC_REASONS)


def _rpc_reason(exc: Exception) -> MissingReason:
    return next(reason for cls, reason in _RPC_REASONS if isinstance(exc, cls))


def _purchase_key(p: Purchase) -> tuple[int, str, str]:
    return (p.slot, p.signature, p.wallet)


def select_first_n(purchases: Iterable[Purchase], n: int) -> list[Buyer]:
    """Перші купівлі → рівно `min(n, len)` покупців за `(slot, signature, wallet)` з rank від 1."""
    ordered = sorted(purchases, key=_purchase_key)[:n]
    return [
        Buyer(
            wallet=p.wallet, rank=rank, first_buy_signature=p.signature, first_buy_slot=p.slot,
            first_buy_time=p.block_time, received_amount=p.received_amount, spent=p.spent,
            programs=p.programs, address_type=p.address_type,
        )
        for rank, p in enumerate(ordered, start=1)
    ]


def _page_history(source: RpcSource, mint: str, state: CollectionState, page_size: int,
                  deadline: DeadlineLike) -> None:
    """Перегорнути історію mint від курсора до кінця. Винятки джерела летять викликачу."""
    while not state.mint_history_exhausted:
        state.rpc_calls += 1
        page = source.get_signatures_for_address(
            mint, before=state.signature_cursor, until=None, limit=page_size, deadline=deadline,
        )
        if not page:
            state.mint_history_exhausted = True
            break
        for entry in page:
            state.mint_signatures.append((entry["signature"], entry["slot"], entry.get("blockTime"), entry.get("err")))
        state.signature_cursor = page[-1]["signature"]


def _record_first(state: CollectionState, parsed: ParsedTx, mint: str) -> None:
    for purchase in detect_purchases(parsed, mint):
        known = state.purchases_by_wallet.get(purchase.wallet)
        if known is None or (purchase.slot, purchase.signature) < (known.slot, known.signature):
            state.purchases_by_wallet[purchase.wallet] = purchase


def _scan(state: CollectionState, mint: str, sig: str, raw: object) -> tuple[MissingReason, str] | None:
    """Розібрати нову транзакцію mint правилом купівлі; повернути проблему, якщо вона є."""
    if raw is None:
        return (MissingReason.UNAVAILABLE, sig)
    parsed = parse_transaction(raw)
    state.transactions_scanned += 1
    if isinstance(parsed, CorruptRecord):
        if parsed.partial is not None:
            _record_first(state, parsed.partial, mint)  # баланси цілі — купівлі не губимо
        return (MissingReason.CORRUPT_DATA, f"{sig} ({parsed.reason})")
    state.tx_cache[sig] = parsed
    _record_first(state, parsed, mint)
    return None


def _next_batch(eligible: list[tuple[int, str]], start: int, state: CollectionState, page_size: int,
                cut_slot: int | None) -> list[str]:
    """До `page_size` ще не закешованих підписів від позиції `start` (після N — лише слот `cut_slot`)."""
    batch: list[str] = []
    for slot, sig in eligible[start:]:
        if len(batch) == page_size or (cut_slot is not None and slot > cut_slot):
            break
        if sig not in state.tx_cache:
            batch.append(sig)
    return batch


def enumerate_buyers(source: RpcSource, mint: str, state: CollectionState, config: IngestConfig,
                     deadline: DeadlineLike) -> BuyersCompleteness:
    """Заповнити `state.buyers` першими N покупцями `mint`; повернути повноту перелічення."""
    n = config.first_buyers_n
    if isinstance(n, bool) or not isinstance(n, int) or n < 1:
        raise ValueError(f"first_buyers_n: {n!r} must be an int >= 1")
    page_size = config.rpc.page_size

    state.transactions_scanned = 0  # лічильник останнього проходу (детермінований)
    try:
        _page_history(source, mint, state, page_size, deadline)
    except _RPC_ERRORS as exc:
        state.buyers = ()
        return BuyersCompleteness(
            complete=False, reason=_rpc_reason(exc),
            detail=f"getSignaturesForAddress before={state.signature_cursor}: {exc}",
        )

    # Від найстаріших; порядок — лише з даних (slot, signature), дублікати відкинуто, err пропущено.
    eligible = sorted({(slot, sig) for sig, slot, _bt, err in state.mint_signatures if err is None})

    # Купівлі перевиводяться щоразу від найстаріших: закешовані транзакції лише відтворюються, тож
    # повторний виклик не запитує їх знову. Купівлі минулого проходу не переносяться: інакше вони
    # передчасно «заповнюють» N, і раніший покупець, недоступний минулого разу, у вибірку не потрапить.
    state.purchases_by_wallet = {}
    problems: list[tuple[MissingReason, str]] = []  # у порядку обробки
    cut_slot: int | None = None  # слот, у якому знайдено N-го покупця
    fetched: dict[str, object] = {}
    for i, (slot, sig) in enumerate(eligible):
        if cut_slot is not None and slot > cut_slot:
            break  # слот N-го покупця добито; далі не розбираємо (і не запитуємо)
        if sig in state.tx_cache:
            state.transactions_scanned += 1
            _record_first(state, state.tx_cache[sig], mint)
        else:
            if sig not in fetched:
                to_fetch = _next_batch(eligible, i, state, page_size, cut_slot)
                state.rpc_calls += 1
                try:
                    raws = source.get_transactions(to_fetch, deadline=deadline)
                except _RPC_ERRORS as exc:
                    problems.append((_rpc_reason(exc), f"getTransaction from {to_fetch[0]}: {exc}"))
                    break
                fetched.update(zip(to_fetch, raws))
            problem = _scan(state, mint, sig, fetched.pop(sig))
            if problem is not None:
                problems.append(problem)
        if cut_slot is None and len(state.purchases_by_wallet) >= n:
            cut_slot = slot  # N-й покупець знайдено: добиваємо цей слот до кінця

    state.buyers = tuple(select_first_n(state.purchases_by_wallet.values(), n))

    if not problems:
        return BuyersCompleteness(complete=True, reason=None, detail="")
    return BuyersCompleteness(
        complete=False, reason=problems[0][0],
        detail="; ".join(f"{reason.value}: {what}" for reason, what in problems),
    )
