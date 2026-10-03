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
   (A→D→A) завершується, а спільний фінансувальник розгортається один раз. Рівень `depth`
   перевиводиться після розгортання рівня `depth-1` з переказів його повністю розгорнутих вершин
   (`_reconcile_level`), тож resume дає той самий frontier, що й свіжий прогін.

Неповнота не мовчки (принцип V, FR-001-09): відмова джерела (`RpcRateLimited`/`RpcTimeout`/
`RpcUnavailable`) на будь-якому кроці, `None` замість транзакції (`unavailable`) чи `CorruptRecord`
(`corrupt_data`) дають `MissingHistory(wallet=W, depth=рівень W, reason, detail)` у `state.missing`
(один запис на `(wallet, reason)`, деталі доповнюються). Решта вершин рівня розгортається далі; усі
перекази, зібрані для W до збою, зберігаються (відправники W до повного розгортання не реєструються — нижче); з `CorruptRecord.partial` перекази
беруться (як у `buyers.py`). Вершина з проблемою **не** потрапляє в `state.expanded` — повторний
виклик спробує її знову (пошкоджена транзакція в `tx_cache` не кладеться), а при успіху її записи в
`missing` видаляються. Розгорнуті вершини повторно не запитуються: виклик із тим самим станом
ідемпотентний.

Межі розгортання (research R-3, FR-001-08, T-013):
- `max_signatures_per_wallet` — спільний ліміт для гаманця й усіх його токен-рахунків: підписи (без
  err, строго раніше за межу; спільний підпис — один раз) зливаються від найновішого за `(slot, signature)`
  і переглядаються лише перші `max_signatures_per_wallet`. Історія довша → `signatures_truncated`; кожне
  джерело гортається лише до `max+1` придатних записів (досить, щоб знати про обрізання). Вершина
  позначається `signature_cap`, знайдені відправники розгортаються нормально.
- `counterparty_threshold` — унікальні відправники прийнятих переказів лічаться в `_NodeScan.accept`
  інкрементально, від найновішої транзакції до межі. Порогу дорівнює — не перевищено; щойно відправник
  став (поріг+1)-м унікальним, перегляд зупиняється: його переказ і все старіше не збирається, транзакції
  далі не запитуються, `counterparties_seen = поріг + 1`. Зібране до цього зберігається, а відправники
  вершини НЕ стають вершинами наступного рівня (`high_degree`, має пріоритет над `signature_cap`).
- `signatures_seen` — скільки підписів вікна переглянуто від найновішого (при `high_degree` — включно з
  підписом перевищувача); від розміру сторінки/пакета не залежить.
- Вершина зі збоєм у цьому проході (будь-який запис у `missing`) переглянута частково: хаб вона чи ні,
  невідомо, тому її відправники, як і при `high_degree`, НЕ реєструються (рішення власника процесу за
  T-013; змінює контракт T-012). Зібрані до збою перекази лишаються доказами. На resume вершина
  розгортається повністю й лише тоді реєструє відправників. Вершина наступного рівня, що з'явилась,
  піднялась із глибшого рівня (коротший шлях), змінила межу чи зникла, інвалідується (перекази в неї,
  `expanded`, `unexpanded`, `missing`) і розгортається заново з новою межею й мінімальною глибиною;
  її відправники перевиводяться тим самим правилом (каскад). Успішна повторна спроба вершини з
  `missing` замінює її перекази результатом повного перегляду.
- `UnexpandedNode(depth = рівень вершини)` пишеться в `state.unexpanded` (один запис на гаманець; при
  повторному розгортанні вершини з `missing` замінюється, порядок `(depth, wallet)`). На `missing` і
  `state.expanded` не впливає: позначена вершина без збоїв вважається розгорнутою.

`deadline` лише передається в кожен виклик джерела (структурний тип, як у `buyers.py`); перевірки
`expired()` і `budget_exhausted` — T-015.

Залежить від: `rpc.protocol` (джерело, винятки), `parse`, `model`, `collector.CollectionState`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from unmask.ingest.buyers import DeadlineLike
from unmask.ingest.collector import CollectionState
from unmask.ingest.config import IngestConfig
from unmask.ingest.model import Asset, MissingHistory, MissingReason, Transfer, UnexpandedNode, UnexpandedReason
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
    threshold: int  # counterparty_threshold
    transfers: list[Transfer] = field(default_factory=list)
    senders: dict[str, tuple[int, str]] = field(default_factory=dict)  # відправник -> найпізніше ребро
    problems: list[Problem] = field(default_factory=list)
    high_degree: bool = False  # поріг перевищено: перегляд зупинено, відправники не розгортаються
    signatures_seen: int = 0
    signatures_truncated: bool = False

    def accept(self, transfer: ParsedTransfer) -> bool:
        """Прийняти переказ; False — новий відправник перевищив поріг (переказ не прийнято, стоп)."""
        known = self.senders.get(transfer.sender)
        if known is None and len(self.senders) >= self.threshold:
            self.high_degree = True
            return False
        self.transfers.append(transfer.at_depth(self.depth))
        edge = (transfer.slot, transfer.signature)
        if known is None or edge > known:
            self.senders[transfer.sender] = edge
        return True

    def unexpanded(self, level: int) -> UnexpandedNode | None:
        if self.high_degree:
            reason = UnexpandedReason.HIGH_DEGREE
        elif self.signatures_truncated:
            reason = UnexpandedReason.SIGNATURE_CAP
        else:
            return None
        return UnexpandedNode(
            wallet=self.wallet, depth=level, reason=reason,
            counterparties_seen=len(self.senders) + (1 if self.high_degree else 0),
            signatures_seen=self.signatures_seen, signatures_truncated=self.signatures_truncated,
        )


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


def _enough(valid: list[tuple[str, int]], last_slot: int, cap: int) -> bool:
    """Чи досить гортати джерело: є `cap+1` придатних і група слота `cap+1`-го вже вся отримана."""
    return len(valid) > cap and last_slot < valid[cap][1]


def _node_signatures(source: RpcSource, state: CollectionState, wallet: str, cutoff: Cutoff,
                     config: IngestConfig, deadline: DeadlineLike,
                     problems: list[Problem]) -> tuple[list[str], bool]:
    """Вікно підписів (без err) строго раніше за межу: гаманець + токен-рахунки, від найновішого.

    Повертає (не більше `max_signatures_per_wallet` найновіших підписів, чи історія довша за ліміт) —
    рівно те, що дав би оракул «перегорнути все → фільтр межі → злиття за (slot, signature) спадно → max».

    Кожне джерело (гаманець, кожен токен-рахунок) гортається, доки в ньому є `max+1` придатних записів
    І слот останнього отриманого запису строго менший за слот `max+1`-го придатного (`_enough`). Порядок
    усередині слота в RPC — позиція в блоці, не підпис, тож група одного слота на межі добирається
    повністю: у злитті будь-який запис джерела зі слотом, меншим за слот його `max+1`-го придатного,
    поступається щонайменше `max+1` різним підписам того ж джерела, отже в найновіші `max` не потрапляє,
    а всі записи зі слотом не меншим — уже отримані. Результат не залежить від `rpc.page_size`.
    `_token_account_entries` перераховується на кожній сторінці — квадратично за кількістю записів,
    новіших за межу (виміряно ≈0,5 с на 80 тис.), свідомо лишено.
    """
    page_size = config.rpc.page_size
    cap = config.max_signatures_per_wallet
    found: dict[str, int] = {}

    before = cutoff.signature
    wallet_valid: list[tuple[str, int]] = []
    try:
        for page in _page(source, state, wallet, cutoff.signature, page_size, deadline):
            for entry in page:
                if entry.get("err") is None and entry["signature"] != cutoff.signature:
                    if entry["signature"] not in found:
                        wallet_valid.append((entry["signature"], entry["slot"]))
                    found.setdefault(entry["signature"], entry["slot"])
            before = page[-1]["signature"]
            if _enough(wallet_valid, page[-1]["slot"], cap):
                break
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
                    # Історія — від найновішого за слотом. Понад `cap` придатних записів означає, що межу
                    # вже пройдено: або її підпис знайдено, або є записи зі слотом, меншим за її слот
                    # (а записи того ж слота, що й межа, правило R-8 без підпису межі відкидає).
                    if _enough(_token_account_entries(entries, cutoff), page[-1]["slot"], cap):
                        break
            except _RPC_ERRORS as exc:
                problems.append((_rpc_reason(exc), f"getSignaturesForAddress {pubkey} (token account of {wallet}): {exc}"))
            for sig, slot in _token_account_entries(entries, cutoff):
                found.setdefault(sig, slot)

    found.pop(cutoff.signature, None)  # межа строга на рівні транзакції
    newest_first = sorted(found, key=lambda s: (found[s], s), reverse=True)
    return newest_first[:cap], len(newest_first) > cap


def _fetch_batch(source: RpcSource, state: CollectionState, batch: list[str], deadline: DeadlineLike,
                 problems: list[Problem]) -> dict[str, object] | None:
    """Один пакет `get_transactions`; на відмові — проблема і None."""
    state.rpc_calls += 1
    try:
        raws = source.get_transactions(batch, deadline=deadline)
    except _RPC_ERRORS as exc:
        problems.append((_rpc_reason(exc), f"getTransaction from {batch[0]}: {exc}"))
        return None
    return dict(zip(batch, raws))


def _expand_node(source: RpcSource, state: CollectionState, wallet: str, depth: int, cutoff: Cutoff,
                 config: IngestConfig, deadline: DeadlineLike) -> _NodeScan:
    scan = _NodeScan(wallet=wallet, depth=depth, threshold=config.counterparty_threshold)
    newest_first, scan.signatures_truncated = _node_signatures(
        source, state, wallet, cutoff, config, deadline, scan.problems,
    )
    # Транзакції, яких немає в кеші, запитуються пакетами `page_size` (у порядку від найновішого)
    # ліниво — лише коли перегляд до них дійшов; після відмови пакети більше не запитуються.
    page_size = config.rpc.page_size
    to_fetch = [sig for sig in newest_first if sig not in state.tx_cache]
    fetched: dict[str, object] = {}
    next_batch = 0
    fetch_failed = False

    for sig in newest_first:
        scan.signatures_seen += 1
        parsed: ParsedTx | None = state.tx_cache.get(sig)
        if parsed is None:
            if sig not in fetched and not fetch_failed and next_batch < len(to_fetch):
                batch = to_fetch[next_batch:next_batch + page_size]
                next_batch += page_size
                got = _fetch_batch(source, state, batch, deadline, scan.problems)
                if got is None:
                    fetch_failed = True
                else:
                    fetched.update(got)
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
            if not scan.accept(transfer):
                return scan  # high_degree: перевищувач і все старіше не збирається
    return scan


def _forget_missing(state: CollectionState, wallet: str) -> bool:
    """Прибрати записи `missing` гаманця за O(кількості причин); True — якщо такі були."""
    return any([state.missing.pop((wallet, reason), None) is not None for reason in MissingReason])


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
    slots = _slot_index(state)
    into = _StateIndex(state)

    try:
        for wallet in sorted(frontier[level]):
            if wallet in state.expanded:
                continue
            cutoff_sig = frontier[level][wallet]
            if cutoff_sig not in slots:
                raise ValueError(f"cutoff {cutoff_sig!r} of {wallet!r} is neither a first buy nor a collected edge")
            scan = _expand_node(source, state, wallet, depth, Cutoff(cutoff_sig, slots[cutoff_sig]),
                                config, deadline)

            if not scan.problems and any((wallet, reason) in state.missing for reason in MissingReason):
                # повторна спроба вдалася: перекази у вершину — рівно ті, що дає повний перегляд
                into.drop(wallet)
            for transfer in scan.transfers:
                key = (transfer.signature, transfer.instruction_path)
                known = state.transfers.get(key)
                if known is None or transfer.depth < known.depth:
                    into.put(key, transfer)
                slots.setdefault(transfer.signature, transfer.slot)

            into.set_unexpanded(wallet, scan.unexpanded(level))
            if scan.problems:
                _record_missing(state, wallet, level, scan.problems)
            else:
                state.expanded.add(wallet)
                _forget_missing(state, wallet)
    finally:
        into.flush()  # state.unexpanded узгоджено навіть при винятку; _derive_level читає хабів звідти
    if depth + 1 <= config.funding_depth:
        _reconcile_level(state, depth, into)
        into.flush()


class _StateIndex:
    """Індекси стану за гаманцем — щоб забути чи замінити записи вершини за O(її записів).

    `transfers`: ключі `state.transfers` за отримувачем (перекази з `receiver == W` дає лише перегляд
    самої W); `unexpanded`: позиція запису гаманця. Живе лише в межах одного виклику `expand_level`:
    будується одним проходом (O(переказів + unexpanded) на рівень) і оновлюється разом зі станом. У
    `CollectionState` не зберігається, тож resume і збереження стану його не стосуються — він завжди
    виводиться з `transfers` і `unexpanded`.
    """

    def __init__(self, state: CollectionState) -> None:
        self._state = state
        self._keys: dict[str, set[tuple[str, str]]] = {}
        for key, t in state.transfers.items():
            self._keys.setdefault(t.receiver, set()).add(key)
        self._unexpanded: dict[str, UnexpandedNode] = {u.wallet: u for u in state.unexpanded}
        self._unexpanded_dirty = False

    def put(self, key: tuple[str, str], transfer: Transfer) -> None:
        self._state.transfers[key] = transfer
        self._keys.setdefault(transfer.receiver, set()).add(key)

    def drop(self, wallet: str) -> None:
        for key in self._keys.pop(wallet, ()):
            del self._state.transfers[key]

    def set_unexpanded(self, wallet: str, node: UnexpandedNode | None) -> None:
        """Один запис на гаманець: повторне розгортання (resume) замінює, а не дублює."""
        if node is None:
            if self._unexpanded.pop(wallet, None) is None:
                return
        else:
            self._unexpanded[wallet] = node
        self._unexpanded_dirty = True

    def flush(self) -> None:
        """Записати `state.unexpanded` (порядок `(depth, wallet)`) — раз на виклик, не на вершину."""
        if self._unexpanded_dirty:
            self._state.unexpanded = sorted(self._unexpanded.values(), key=lambda u: (u.depth, u.wallet))
            self._unexpanded_dirty = False


def _invalidate(state: CollectionState, wallet: str, index: _StateIndex) -> None:
    """Забути розгортання вершини: її позиція чи межа змінилась (або вона більше не досяжна).

    O(записів цієї вершини); вершина без стану (щойно знайдена у свіжому прогоні) — O(1).
    """
    index.drop(wallet)
    index.set_unexpanded(wallet, None)
    state.expanded.discard(wallet)
    _forget_missing(state, wallet)


def _derive_level(state: CollectionState, level: int) -> dict[str, str]:
    """Вершини рівня `level` і їхні межі — лише з даних повністю розгорнутих вершин рівня `level-1`.

    Батько реєструє відправників, лише якщо розгорнутий без збоїв і не `high_degree` (FR-001-08; вершина
    зі збоєм переглянута частково — хаб вона чи ні, невідомо). Відправник, що вже є вершиною меншого
    рівня, пропускається (цикли, спільні фінансувальники). Межа — найпізніше за `(slot, signature)`
    ребро до вершини рівня `level-1`.
    """
    frontier = state.frontier_by_depth
    hubs = {u.wallet for u in state.unexpanded if u.reason == UnexpandedReason.HIGH_DEGREE}
    parents = {w for w in frontier[level - 1] if w in state.expanded and w not in hubs}
    shallower = set().union(*(frontier[d] for d in range(level)))
    edges: dict[str, tuple[int, str]] = {}
    for t in state.transfers.values():
        if t.receiver in parents and t.sender not in shallower:
            edge = (t.slot, t.signature)
            if edge > edges.get(t.sender, (-1, "")):
                edges[t.sender] = edge
    return {sender: edge[1] for sender, edge in edges.items()}


def _reconcile_level(state: CollectionState, level: int, into: _StateIndex) -> None:
    """Перевивести `frontier[level]` після розгортання рівня `level-1` (зокрема на resume).

    У свіжому прогоні рівень ще порожній — це звичайна реєстрація відправників. На resume вершина могла
    з'явитись (батько нарешті розгорнутий повністю), перейти з глибшого рівня на цей (коротший шлях),
    змінити межу (пізніше ребро) або зникнути (батька більше немає). Кожна така вершина інвалідується:
    її перекази, `expanded`, `unexpanded`, `missing` забуваються, і наступний рівень розгорне її заново —
    з новою межею й мінімальною глибиною, а її відправники перевиводяться тим самим правилом (каскад).
    Вершина, що піднялась на менший рівень, там уже інвалідована й розгорнута — тут лише прибирається.
    """
    frontier = state.frontier_by_depth
    old = frontier.get(level, {})
    new = _derive_level(state, level)
    shallower = set().union(*(frontier[d] for d in range(level)))
    for wallet, cutoff in old.items():
        if new.get(wallet) != cutoff and wallet not in shallower:
            _invalidate(state, wallet, into)
    for wallet, cutoff in new.items():
        if old.get(wallet) != cutoff:
            _invalidate(state, wallet, into)
    frontier[level] = new
