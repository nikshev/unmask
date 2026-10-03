# impl: FR-001-03, FR-001-04, FR-001-05, FR-001-07, FR-001-08
"""BFS джерел фінансування по рівнях (research R-1, R-8, R-9).

Що робить: `expand_level(source, state, depth, config, deadline)` збирає вхідні перекази
глибини `depth` (1 — безпосередньо в покупця), розгортаючи вершини рівня `depth-1`
з `state.frontier_by_depth[depth-1]` (рівень 0 — покупці, засівається з `state.buyers`).
Колектор (T-014) викликає його для `depth = 1 … funding_depth` по черзі.

Для кожної ще не розгорнутої вершини W з межею `cutoff` (підпис транзакції):
1. `cutoff_for(W)` — підпис першої купівлі (рівень 0) або **найпізнішого** ребра W→X до вершини
   попереднього рівня (BFS по рівнях знає всі ребра рівня до розгортання наступного). «Найпізніше» —
   за `(slot, signature)`: позиції в блоці RPC не дає, тож у межах одного слота береться
   детермінований максимум за підписом.
2. Історія гаманця: `get_signatures_for_address(W, before=cutoff)` сторінками `rpc.page_size` до
   порожньої сторінки — лише строго раніші за межу записи (позиція в історії, не лише слот).
3. Якщо `collect_spl_inbound`: `get_token_accounts_by_owner(W)` і історія кожного токен-рахунку від
   найновішого; межа на токен-рахунку — за research R-8 (`_token_account_entries`): межа в історії →
   відкидається вона і все не старше за позицією; межі немає → лише `slot < slot(cutoff)`.
   При `collect_spl_inbound=false` токен-рахунки не запитуються і SPL-перекази не збираються взагалі
   (і з історії гаманця теж) — так визначено еталон `basic` («усі `spl:*` відсутні»).
4. Записи з `err != null` не запитуються; транзакції — через `state.tx_cache` (один запит на підпис за
   прогін; спільний підпис історії гаманця й токен-рахунку запитується раз). Транзакція з `meta.err`
   (`failed`) переказів не дає.
5. Беруться лише перекази з `receiver == W` і `sender != W`, з глибиною `depth`. Ключ дедупу —
   `(signature, instruction_path)` (R-9); при повторній зустрічі зберігається мінімальна глибина.
6. Відправники стають вершинами рівня `depth` (`frontier_by_depth[depth]`, межа — найпізніше ребро)
   лише якщо `depth + 1 <= funding_depth` і вони ще не є вершиною меншого/цього рівня — так цикл
   (A→D→A) завершується, а спільний фінансувальник розгортається один раз.

Неповнота не мовчки (принцип V, FR-001-09): відмова джерела (`RpcRateLimited`/`RpcTimeout`/
`RpcUnavailable`) на будь-якому кроці, `None` замість транзакції (`unavailable`) чи `CorruptRecord`
(`corrupt_data`) дають `MissingHistory(wallet=W, depth=рівень W, reason, detail)` у `state.missing`
(один запис на `(wallet, reason)`, деталі доповнюються). Решта вершин рівня розгортається далі; усе,
що зібрано для W до збою (зокрема відправники), зберігається; з `CorruptRecord.partial` перекази
беруться (як у `buyers.py`). Вершина з проблемою **не** потрапляє в `state.expanded` — повторний
виклик спробує її знову (пошкоджена транзакція в `tx_cache` не кладеться), а при успіху її записи в
`missing` видаляються. Розгорнуті вершини повторно не запитуються: виклик із тим самим станом
ідемпотентний.

Точка розширення для T-013 (межі R-3): `_NodeScan` переглядає транзакції вершини від найновішої до
межі, кожен прийнятий переказ іде через `_NodeScan.accept` — там лічитимуться унікальні відправники
(`counterparty_threshold`), а `_node_signatures` — місце для `max_signatures_per_wallet`.

`deadline` лише передається в кожен виклик джерела (структурний тип, як у `buyers.py`); перевірки
`expired()` і `budget_exhausted` — T-015.

Залежить від: `rpc.protocol` (джерело, винятки), `parse`, `model`, `collector.CollectionState`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from unmask.ingest.buyers import DeadlineLike
from unmask.ingest.collector import CollectionState
from unmask.ingest.config import IngestConfig
from unmask.ingest.model import Asset, MissingHistory, MissingReason, Transfer
from unmask.ingest.parse import CorruptRecord, ParsedTransfer, ParsedTx, parse_transaction
from unmask.ingest.rpc.protocol import RpcRateLimited, RpcSource, RpcTimeout, RpcUnavailable

_RPC_REASONS: tuple[tuple[type[Exception], MissingReason], ...] = (
    (RpcRateLimited, MissingReason.RATE_LIMITED),
    (RpcTimeout, MissingReason.TIMEOUT),
    (RpcUnavailable, MissingReason.UNAVAILABLE),
)
_RPC_ERRORS = tuple(exc for exc, _ in _RPC_REASONS)

Problem = tuple[MissingReason, str]


def _rpc_reason(exc: Exception) -> MissingReason:
    return next(reason for cls, reason in _RPC_REASONS if isinstance(exc, cls))


@dataclass(frozen=True)
class Cutoff:
    """Межа вершини: транзакція, строго раніше за яку беруться вхідні перекази (R-1)."""

    signature: str
    slot: int


@dataclass
class _NodeScan:
    """Результат розгортання однієї вершини. Перекази приймаються від найновішого до межі."""

    wallet: str
    depth: int  # глибина переказів у вершину = рівень вершини + 1
    transfers: list[Transfer] = field(default_factory=list)
    senders: dict[str, tuple[int, str]] = field(default_factory=dict)  # відправник -> найпізніше ребро
    problems: list[Problem] = field(default_factory=list)

    def accept(self, transfer: ParsedTransfer) -> None:
        self.transfers.append(transfer.at_depth(self.depth))
        edge = (transfer.slot, transfer.signature)
        known = self.senders.get(transfer.sender)
        if known is None or edge > known:
            self.senders[transfer.sender] = edge


def _slot_index(state: CollectionState) -> dict[str, int]:
    """Слот кожної відомої транзакції-межі: купівлі покупців і зібрані ребра."""
    slots = {b.first_buy_signature: b.first_buy_slot for b in state.buyers}
    slots.update({sig: t.slot for (sig, _path), t in state.transfers.items()})
    return slots


def _page(source: RpcSource, state: CollectionState, address: str, before: str | None,
          page_size: int, deadline: DeadlineLike):
    """Сторінки історії адреси від `before` до кінця (порожня сторінка). Винятки — викликачу."""
    while True:
        state.rpc_calls += 1
        page = source.get_signatures_for_address(
            address, before=before, until=None, limit=page_size, deadline=deadline,
        )
        if not page:
            return
        yield page
        before = page[-1]["signature"]


def _token_account_entries(entries: list[tuple[str, int]], cutoff: Cutoff) -> list[tuple[str, int]]:
    """Межа на історії токен-рахунку (research R-8). `entries` — від найновішого, без err.

    Підпис межі є в історії → відкидається він і все не старше за нього за позицією.
    Немає → лише записи зі слотом строго меншим за слот межі (порядок у слоті невідомий;
    консервативно: переказ після межі не потрапляє, FR-001-07).
    """
    for i, (sig, _slot) in enumerate(entries):
        if sig == cutoff.signature:
            return entries[i + 1:]
    return [(sig, slot) for sig, slot in entries if slot < cutoff.slot]


def _node_signatures(source: RpcSource, state: CollectionState, wallet: str, cutoff: Cutoff,
                     config: IngestConfig, deadline: DeadlineLike, problems: list[Problem]) -> dict[str, int]:
    """Підписи (без err) строго раніше за межу: історія гаманця + історії токен-рахунків."""
    page_size = config.rpc.page_size
    found: dict[str, int] = {}

    before = cutoff.signature
    try:
        for page in _page(source, state, wallet, cutoff.signature, page_size, deadline):
            for entry in page:
                if entry.get("err") is None:
                    found.setdefault(entry["signature"], entry["slot"])
            before = page[-1]["signature"]
    except _RPC_ERRORS as exc:
        problems.append((_rpc_reason(exc), f"getSignaturesForAddress {wallet} before={before}: {exc}"))

    if config.collect_spl_inbound:
        try:
            state.rpc_calls += 1
            accounts = source.get_token_accounts_by_owner(wallet, deadline=deadline)
        except _RPC_ERRORS as exc:
            problems.append((_rpc_reason(exc), f"getTokenAccountsByOwner {wallet}: {exc}"))
            accounts = []
        for pubkey in sorted({acc["pubkey"] for acc in accounts}):
            entries: list[tuple[str, int]] = []
            try:
                for page in _page(source, state, pubkey, None, page_size, deadline):
                    entries.extend(
                        (e["signature"], e["slot"]) for e in page
                        if e.get("err") is None
                    )
            except _RPC_ERRORS as exc:
                problems.append((_rpc_reason(exc), f"getSignaturesForAddress {pubkey} (token account of {wallet}): {exc}"))
            for sig, slot in _token_account_entries(entries, cutoff):
                found.setdefault(sig, slot)

    found.pop(cutoff.signature, None)  # межа строга на рівні транзакції
    return found


def _fetch(source: RpcSource, state: CollectionState, signatures: list[str], page_size: int,
           deadline: DeadlineLike, problems: list[Problem]) -> dict[str, object]:
    """Завантажити транзакції, яких немає в `tx_cache`; на відмові — проблема, решта не запитується."""
    fetched: dict[str, object] = {}
    missing = [sig for sig in signatures if sig not in state.tx_cache]
    for i in range(0, len(missing), page_size):
        batch = missing[i:i + page_size]
        state.rpc_calls += 1
        try:
            raws = source.get_transactions(batch, deadline=deadline)
        except _RPC_ERRORS as exc:
            problems.append((_rpc_reason(exc), f"getTransaction from {batch[0]}: {exc}"))
            break
        fetched.update(zip(batch, raws))
    return fetched


def _expand_node(source: RpcSource, state: CollectionState, wallet: str, depth: int, cutoff: Cutoff,
                 config: IngestConfig, deadline: DeadlineLike) -> _NodeScan:
    scan = _NodeScan(wallet=wallet, depth=depth)
    signatures = _node_signatures(source, state, wallet, cutoff, config, deadline, scan.problems)
    newest_first = sorted(signatures, key=lambda s: (signatures[s], s), reverse=True)
    fetched = _fetch(source, state, newest_first, config.rpc.page_size, deadline, scan.problems)

    for sig in newest_first:
        parsed: ParsedTx | None = state.tx_cache.get(sig)
        if parsed is None:
            if sig not in fetched:
                continue  # пакет не отримано — проблему вже зафіксовано
            raw = fetched[sig]
            if raw is None:
                scan.problems.append((MissingReason.UNAVAILABLE, sig))
                continue
            result = parse_transaction(raw)
            if isinstance(result, CorruptRecord):
                scan.problems.append((MissingReason.CORRUPT_DATA, f"{sig} ({result.reason})"))
                if result.partial is None:
                    continue
                parsed = result.partial  # розібрані сусіди не губляться; у кеш не кладеться
            else:
                parsed = result
                state.tx_cache[sig] = parsed
        if parsed.failed:
            continue
        for transfer in parsed.transfers:
            if transfer.receiver != wallet or transfer.sender == wallet:
                continue
            if transfer.signature == cutoff.signature:
                continue
            if not config.collect_spl_inbound and transfer.asset != Asset.SOL:
                continue
            scan.accept(transfer)
    return scan


def _record_missing(state: CollectionState, wallet: str, level: int, problems: list[Problem]) -> None:
    for reason, detail in problems:
        key = (wallet, reason)
        known = state.missing.get(key)
        if known is None:
            state.missing[key] = MissingHistory(wallet=wallet, depth=level, reason=reason, detail=detail)
        elif detail not in known.detail.split("; "):
            state.missing[key] = MissingHistory(
                wallet=wallet, depth=known.depth, reason=reason, detail=f"{known.detail}; {detail}",
            )


def expand_level(source: RpcSource, state: CollectionState, depth: int, config: IngestConfig,
                 deadline: DeadlineLike) -> None:
    """Зібрати перекази глибини `depth`, розгорнувши вершини рівня `depth-1` (див. модуль)."""
    if isinstance(depth, bool) or not isinstance(depth, int) or not 1 <= depth <= config.funding_depth:
        raise ValueError(f"depth: {depth!r} must be an int in 1..funding_depth={config.funding_depth}")
    frontier = state.frontier_by_depth
    if depth == 1 and 0 not in frontier:
        frontier[0] = {b.wallet: b.first_buy_signature for b in state.buyers}
    if depth - 1 not in frontier:
        raise ValueError(f"depth {depth}: level {depth - 1} has not been expanded yet")

    level = depth - 1
    register_next = depth + 1 <= config.funding_depth
    shallower = set().union(*(nodes for d, nodes in frontier.items() if d <= level))
    next_level = frontier.setdefault(depth, {}) if register_next else {}
    slots = _slot_index(state)

    for wallet in sorted(frontier[level]):
        if wallet in state.expanded:
            continue
        cutoff_sig = frontier[level][wallet]
        if cutoff_sig not in slots:
            raise ValueError(f"cutoff {cutoff_sig!r} of {wallet!r} is neither a first buy nor a collected edge")
        scan = _expand_node(source, state, wallet, depth, Cutoff(cutoff_sig, slots[cutoff_sig]),
                            config, deadline)

        for transfer in scan.transfers:
            key = (transfer.signature, transfer.instruction_path)
            known = state.transfers.get(key)
            if known is None or transfer.depth < known.depth:
                state.transfers[key] = transfer
            slots.setdefault(transfer.signature, transfer.slot)

        if register_next:
            for sender, edge in scan.senders.items():
                if sender in shallower or sender in state.expanded:
                    continue
                current = next_level.get(sender)
                if current is None or edge > (slots[current], current):
                    next_level[sender] = edge[1]

        if scan.problems:
            _record_missing(state, wallet, level, scan.problems)
        else:
            state.expanded.add(wallet)
            for key in [k for k in state.missing if k[0] == wallet]:
                del state.missing[key]
