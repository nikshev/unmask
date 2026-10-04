# impl: FR-001-03, FR-001-04, FR-001-05, FR-001-07, FR-001-08, FR-001-09, FR-001-10, FR-001-13, FR-001-16
"""BFS джерел фінансування по рівнях (research R-1, R-8, R-9).

Що робить: `expand_level(source, state, depth, config, deadline)` збирає вхідні перекази
глибини `depth` (1 — безпосередньо в покупця), розгортаючи вершини рівня `depth-1`
з `state.frontier_by_depth[depth-1]` (рівень 0 — покупці; перевиводиться з поточних `state.buyers` на кожному `expand_level(1)` через
`_reconcile_buyers` — новий/змінений/зниклий покупець інвалідується, як і вершини глибших рівнів).
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
(один запис на `(wallet, reason)`; при кожному розгортанні вершини її записи перебудовуються з
результату цього проходу — причини лише актуальні, тоді як перекази попередніх спроб лишаються доказами). Решта вершин рівня розгортається далі; усі
перекази, зібрані для W до збою, зберігаються (відправники W до повного розгортання не реєструються — нижче); з `CorruptRecord.partial` перекази
беруться (як у `buyers.py`). Вершина з проблемою **не** потрапляє в `state.expanded` — повторний
виклик спробує її знову (пошкоджена транзакція в `tx_cache` не кладеться), а при успіху її записи в
`missing` видаляються. Розгорнуті вершини повторно не запитуються: виклик із тим самим станом
ідемпотентний.
Будь-який інший `RpcError` (базовий чи невідомий підклас — дефект адаптера) теж стає `unavailable` і з
`collect` не виходить; `detail` збою джерела — метод, адреса/підпис і `<КласВинятку>: <текст>` (T-016).

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

Бюджет часу (FR-001-16): перед кожною сторінкою історії (гаманця й токен-рахунку), запитом
токен-рахунків і пакетом транзакцій — `budget.ensure_time(deadline)`; після спливу звернень немає;
`RpcTimeout`, спричинений дедлайном, — теж `BudgetExhausted` (`budget.deadline_timeouts`). Вичерпання
зупиняє вершину негайно (`_expand_node`): `MissingHistory(reason=budget_exhausted)` з рівнем вершини,
зібрані до того перекази лишаються, вершина не в `expanded` і відправників не реєструє (як при збої).
Решта вершин рівня й усі вже відомі вершини наступних рівнів (колектор викликає `expand_level` далі)
отримують `budget_exhausted` без жодного звернення. Повтор розгортає їх як після збою.

Мемо сканувань джерел (T-022, FR-001-13, known-issues §1): `state.scan_memo: dict[ScanKey, ScanRecord]`.
Джерела вершини — історія гаманця до межі, список токен-рахунків власника, історія кожного токен-рахунку
до межі. `_node_signatures` спершу шукає знімок за ключем і лише за його відсутності звертається до
джерела; ЗАВЕРШЕНЕ сканування (`_enough` або порожня сторінка; список — успішна відповідь) кладеться в
мемо одразу, до наступного джерела, тож `BudgetExhausted` чи збій посеред вершини не губить уже завершених.
Сканування, обірване збоєм чи бюджетом, не кладеться (атомарність: повний знімок або нічого), а його
частково отримані записи, як і раніше, лише йдуть у вікно цього проходу. Гарантія прогресу: повтор із
бюджетом, не меншим за (звернення, що повторюються завжди, — крок 4 сервісу, пошкоджені транзакції) +
(найдорожче одне сканування джерела), завершує щонайменше одне сканування; бюджет, менший за одне
сканування (наприклад, історія з двох сторінок при бюджеті на одне звернення), — чесний `incomplete` назавжди.
- Ключ (`ScanKey`) — усе, від чого залежить знімок: тип джерела (`wallet`/`token_account`/
  `token_accounts_listing`), адреса джерела, межа (`Cutoff`: підпис — `before=` гаманця й позиційна межа R-8;
  слот — межа R-8 без підпису в історії рахунку), `max_signatures_per_wallet` (точка зупинки `_enough` і
  нормалізація), `collect_spl_inbound`, `commitment` (дані джерела залежать від нього; як і решта, він під
  версією конфігу — у ключі як захист від зміни без підняття версії). `owner` — вершина, для якої сканували
  (для чистки; на знімок не впливає). Не в ключі: `page_size` — знімок нормалізовано (`_history_record`):
  завершене сканування отримало всі придатні записи зі слотом ≥ слоту `max+1`-го, і лише вони потрібні
  злиттю (аргумент T-013 вище: запис джерела зі слотом, меншим за слот його `max+1`-го придатного,
  поступається `max+1` іншим підписам того ж джерела), тож знімок — функція лише даних, межі й ліміту, а
  вікно з нормалізованих знімків == вікну з повних (перевіряє тест на page_size 1…1000 і оракул);
  `counterparty_threshold` (застосовується до транзакцій після вікна), рівень/глибина вершини (вікно від
  них не залежить: вершина, що змінила рівень, але не межу, бере знімок).
- Зміна межі (пізніше ребро на resume) → інший ключ → перескан; truncated-ознака вікна виводиться зі
  знімків (`truncated` джерела ⇒ у знімку ≥ `max+1` записів) ідентично свіжому прогону, як і
  `signatures_seen`/`UnexpandedNode` (рахуються з вікна). Кількість переглянутих записів джерела у знімку
  НЕ зберігається: вона залежить від `page_size` і на результат не впливає.
- Список токен-рахунків — один раз за життя партиційного стану (ключ без межі й ліміту). Свідомо:
  рахунок, створений після межі, не має історії до межі; закритий рахунок не видно й у свіжому прогоні
  (R-8). Застарілість у межах життя стану (секунди–хвилини повторів) допустима.
- Чистка (`_sweep_memo`, кінець кожного `expand_level`): історія лишається, лише поки її вершина з цією
  межею є у frontier і не розгорнута; розгорнута, зникла (каскад `_invalidate`) чи зі зміненою межею —
  прибирається (ключ однаково не влучив би; вершина, що повернулась, сканується заново). Списки не
  чистяться. Пам'ять: історій — щонайбільше (недорозгорнуті вершини) × (1 + K токен-рахунків) × (`max+1`
  + група слота) пар `(підпис, слот)`; на практиці — вершини, обірвані збоєм/бюджетом; списків — по одному
  на кожного власника, що дійшов до кроку 3 (K адрес). Значення незмінні, тож `ResultCache` копіює лише
  словник. Свіжий прогін стартує з порожнього мемо і в ньому не влучає (кожна вершина сканується раз, а
  `owner` у ключі розводить навіть спільний для двох власників рахунок), тож журнал викликів свіжого
  прогону той самий, що до T-022; stale `config_version` у `resume` скидає й мемо (усі поля стану).

Залежить від: `rpc.protocol` (джерело, винятки, `Deadline`), `budget`, `parse`, `model`, `collector.CollectionState`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from unmask.ingest.budget import BudgetExhausted, deadline_timeouts, ensure_time
from unmask.ingest.collector import CollectionState
from unmask.ingest.config import IngestConfig
from unmask.ingest.model import Asset, MissingHistory, MissingReason, Transfer, UnexpandedNode, UnexpandedReason
from unmask.ingest.parse import CorruptRecord, ParsedTransfer, ParsedTx, parse_transaction
from unmask.ingest.rpc.protocol import Deadline, RpcError, RpcRateLimited, RpcSource, RpcTimeout, RpcUnavailable

_RPC_REASONS: tuple[tuple[type[Exception], MissingReason], ...] = (
    (RpcRateLimited, MissingReason.RATE_LIMITED),
    (RpcTimeout, MissingReason.TIMEOUT),
    (RpcUnavailable, MissingReason.UNAVAILABLE),
)
# Ловиться вся ієрархія `RpcError`: базовий чи невідомий підклас (поза контрактом — дефект адаптера)
# із збору не виходить і стає `unavailable` — «не вдалось дізнатись», клас винятку видно в detail (T-016).
_RPC_ERRORS = (RpcError,)

Problem = tuple[MissingReason, str]


def _rpc_reason(exc: Exception) -> MissingReason:
    return next((reason for cls, reason in _RPC_REASONS if isinstance(exc, cls)), MissingReason.UNAVAILABLE)


def _rpc_detail(what: str, exc: Exception) -> str:
    """detail збою джерела: що запитували (метод, адреса/підпис) і клас винятку з його текстом."""
    return f"{what}: {type(exc).__name__}: {exc}"


@dataclass(frozen=True)
class Cutoff:
    """Межа вершини: транзакція, строго раніше за яку беруться вхідні перекази (R-1)."""

    signature: str
    slot: int


# Типи джерел вершини, сканування яких запам'ятовується (T-022).
SCAN_WALLET = "wallet"                                # getSignaturesForAddress(W, before=межа)
SCAN_TOKEN_ACCOUNT = "token_account"                  # getSignaturesForAddress(рахунок) + межа R-8
SCAN_TOKEN_ACCOUNTS_LISTING = "token_accounts_listing"  # getTokenAccountsByOwner(W)


@dataclass(frozen=True)
class ScanKey:
    """Ключ мемо: УСІ параметри, від яких залежить знімок сканування (див. модуль, «Мемо»).

    `address` — адреса джерела (гаманець, токен-рахунок, власник для списку); `owner` — вершина, для якої
    сканували (== `address` для гаманця й списку): потрібна чистці; `cutoff` (підпис і слот межі) і
    `max_signatures` — `None` лише для списку, який від них не залежить. `page_size` у ключі немає свідомо:
    знімок від нього не залежить (нормалізація `_history_record`, правило `_enough`).
    """

    kind: str
    address: str
    owner: str
    cutoff: Cutoff | None
    max_signatures: int | None
    collect_spl_inbound: bool
    commitment: str


@dataclass(frozen=True)
class HistoryScan:
    """Знімок ЗАВЕРШЕНОГО сканування історії (гаманця чи токен-рахунку) до межі.

    `entries` — придатні записи `(signature, slot)` (без err, строго раніше за межу за R-1/R-8) у порядку
    джерела (від найновішого), нормалізовані: якщо їх понад `max_signatures`, береться все зі слотом, не
    меншим за слот `max+1`-го (група слота на межі повністю) — рівно стільки, скільки потрібно злиттю
    вікна; `truncated` — джерело має понад `max_signatures` придатних записів.
    """

    entries: tuple[tuple[str, int], ...]
    truncated: bool


@dataclass(frozen=True)
class TokenAccountsListing:
    """Знімок завершеного `getTokenAccountsByOwner(owner)`: унікальні адреси рахунків за зростанням."""

    accounts: tuple[str, ...]


ScanRecord = HistoryScan | TokenAccountsListing


def _scan_key(kind: str, address: str, owner: str, cutoff: Cutoff | None, config: IngestConfig) -> ScanKey:
    return ScanKey(
        kind=kind, address=address, owner=owner, cutoff=cutoff,
        max_signatures=None if kind == SCAN_TOKEN_ACCOUNTS_LISTING else config.max_signatures_per_wallet,
        collect_spl_inbound=config.collect_spl_inbound, commitment=config.commitment,
    )


def _history_record(valid: list[tuple[str, int]], cap: int) -> HistoryScan:
    """Нормалізований знімок завершеного сканування (`valid` — усі отримані придатні, від найновішого).

    Завершене сканування (`_enough` або кінець історії) отримало всі придатні записи зі слотом, не меншим
    за слот `cap+1`-го придатного, — і лише їх потребує злиття (`_node_signatures`). Решта залежить від
    `page_size` (скільки зайвого захопила остання сторінка) і відкидається, тож знімок — функція лише
    даних джерела, межі й `cap`."""
    if len(valid) > cap:
        threshold = valid[cap][1]
        return HistoryScan(entries=tuple(e for e in valid if e[1] >= threshold), truncated=True)
    return HistoryScan(entries=tuple(valid), truncated=False)


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
          page_size: int, deadline: Deadline):
    """Сторінки історії адреси від `before` до кінця (порожня сторінка). Винятки й `BudgetExhausted` — викликачу."""
    while True:
        what = f"getSignaturesForAddress {address} before={before}"
        ensure_time(deadline, what)  # перед кожною сторінкою
        state.rpc_calls += 1
        with deadline_timeouts(deadline, what):
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
                     config: IngestConfig, deadline: Deadline,
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
    memo = state.scan_memo
    found: dict[str, int] = {}

    wallet_key = _scan_key(SCAN_WALLET, wallet, wallet, cutoff, config)
    wallet_record = memo.get(wallet_key)
    if wallet_record is not None:
        wallet_valid = list(wallet_record.entries)
    else:
        before = cutoff.signature
        wallet_valid = []
        seen: set[str] = set()
        try:
            for page in _page(source, state, wallet, cutoff.signature, page_size, deadline):
                for entry in page:
                    if entry.get("err") is None and entry["signature"] != cutoff.signature:
                        if entry["signature"] not in seen:
                            seen.add(entry["signature"])
                            wallet_valid.append((entry["signature"], entry["slot"]))
                before = page[-1]["signature"]
                if _enough(wallet_valid, page[-1]["slot"], cap):
                    break
            memo[wallet_key] = _history_record(wallet_valid, cap)  # завершено: одразу в мемо
        except _RPC_ERRORS as exc:
            problems.append((_rpc_reason(exc), _rpc_detail(f"getSignaturesForAddress {wallet} before={before}", exc)))
    for sig, slot in wallet_valid:
        found.setdefault(sig, slot)

    if config.collect_spl_inbound:
        listing_key = _scan_key(SCAN_TOKEN_ACCOUNTS_LISTING, wallet, wallet, None, config)
        listing = memo.get(listing_key)
        if listing is None:
            try:
                what = f"getTokenAccountsByOwner {wallet}"
                ensure_time(deadline, what)
                state.rpc_calls += 1
                with deadline_timeouts(deadline, what):
                    accounts = source.get_token_accounts_by_owner(wallet, deadline=deadline)
                listing = TokenAccountsListing(accounts=tuple(sorted({acc["pubkey"] for acc in accounts})))
                memo[listing_key] = listing
            except _RPC_ERRORS as exc:
                problems.append((_rpc_reason(exc), _rpc_detail(what, exc)))
                listing = TokenAccountsListing(accounts=())
        for pubkey in listing.accounts:
            account_key = _scan_key(SCAN_TOKEN_ACCOUNT, pubkey, wallet, cutoff, config)
            account_record = memo.get(account_key)
            if account_record is not None:
                kept = account_record.entries
            else:
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
                    memo[account_key] = _history_record(_token_account_entries(entries, cutoff), cap)
                except _RPC_ERRORS as exc:
                    problems.append((_rpc_reason(exc), _rpc_detail(
                        f"getSignaturesForAddress {pubkey} (token account of {wallet})", exc)))
                kept = _token_account_entries(entries, cutoff)
            for sig, slot in kept:
                found.setdefault(sig, slot)

    found.pop(cutoff.signature, None)  # межа строга на рівні транзакції
    newest_first = sorted(found, key=lambda s: (found[s], s), reverse=True)
    return newest_first[:cap], len(newest_first) > cap


def _fetch_batch(source: RpcSource, state: CollectionState, batch: list[str], deadline: Deadline,
                 problems: list[Problem]) -> dict[str, object] | None:
    """Один пакет `get_transactions`; на відмові — проблема і None; `BudgetExhausted` — викликачу."""
    what = f"getTransaction from {batch[0]}"
    ensure_time(deadline, what)  # перед кожним пакетом
    state.rpc_calls += 1
    try:
        with deadline_timeouts(deadline, what):
            raws = source.get_transactions(batch, deadline=deadline)
    except _RPC_ERRORS as exc:
        problems.append((_rpc_reason(exc), _rpc_detail(what, exc)))
        return None
    return dict(zip(batch, raws))


def _expand_node(source: RpcSource, state: CollectionState, wallet: str, depth: int, cutoff: Cutoff,
                 config: IngestConfig, deadline: Deadline) -> _NodeScan:
    """Розгорнути вершину. Вичерпаний бюджет зупиняє її негайно: `budget_exhausted` у проблемах вершини,
    зібране до того лишається (як при збої джерела), далі звернень немає."""
    scan = _NodeScan(wallet=wallet, depth=depth, threshold=config.counterparty_threshold)
    try:
        _scan_node(source, state, scan, cutoff, config, deadline)
    except BudgetExhausted as exc:
        scan.problems.append((MissingReason.BUDGET_EXHAUSTED, exc.detail))
    return scan


def _scan_node(source: RpcSource, state: CollectionState, scan: _NodeScan, cutoff: Cutoff,
               config: IngestConfig, deadline: Deadline) -> None:
    wallet = scan.wallet
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
                return  # high_degree: перевищувач і все старіше не збирається


def _forget_missing(state: CollectionState, wallet: str) -> bool:
    """Прибрати записи `missing` гаманця за O(кількості причин); True — якщо такі були."""
    return any([state.missing.pop((wallet, reason), None) is not None for reason in MissingReason])


def _record_missing(state: CollectionState, wallet: str, level: int, problems: list[Problem]) -> None:
    """Записати проблеми ОДНОГО проходу вершини (викликач спершу прибирає її старі записи)."""
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
                 deadline: Deadline) -> None:
    """Зібрати перекази глибини `depth`, розгорнувши вершини рівня `depth-1` (див. модуль)."""
    if isinstance(depth, bool) or not isinstance(depth, int) or not 1 <= depth <= config.funding_depth:
        raise ValueError(f"depth: {depth!r} must be an int in 1..funding_depth={config.funding_depth}")
    frontier = state.frontier_by_depth
    into = _StateIndex(state)
    if depth == 1:
        _reconcile_buyers(state, into)
    if depth - 1 not in frontier:
        raise ValueError(f"depth {depth}: level {depth - 1} has not been expanded yet")

    level = depth - 1
    slots = _slot_index(state)

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
            # причини вершини — лише з цього проходу: старі прибираються (докази-перекази лишаються)
            _forget_missing(state, wallet)
            if scan.problems:
                _record_missing(state, wallet, level, scan.problems)
            else:
                state.expanded.add(wallet)
    finally:
        into.flush()  # state.unexpanded узгоджено навіть при винятку; _derive_level читає хабів звідти
    if depth + 1 <= config.funding_depth:
        _reconcile_level(state, depth, into)
        into.flush()
    _sweep_memo(state)


def _sweep_memo(state: CollectionState) -> None:
    """Прибрати з мемо історії, що вже не знадобляться (пам'ять; на результат не впливає).

    Історія потрібна, лише поки вершина `owner` з межею `cutoff` є у frontier і ще не розгорнута:
    розгорнута повторно не сканується; вершина, що зникла чи змінила межу, сканується з новим ключем.
    Списки токен-рахунків лишаються на все життя стану (див. модуль). O(мемо + frontier) на рівень."""
    memo = state.scan_memo
    if not memo:
        return
    live = {(wallet, cutoff) for level in state.frontier_by_depth.values() for wallet, cutoff in level.items()
            if wallet not in state.expanded}
    dead = [key for key in memo
            if key.kind != SCAN_TOKEN_ACCOUNTS_LISTING and (key.owner, key.cutoff.signature) not in live]
    for key in dead:
        del memo[key]


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


def _reconcile_buyers(state: CollectionState, into: _StateIndex) -> None:
    """Перевивести рівень 0 з ПОТОЧНИХ `state.buyers` (на кожному `expand_level(1)`).

    Той самий принцип, що й `_reconcile_level` для глибших рівнів: покупець, що з'явився, зник або
    змінив першу купівлю (межу), інвалідується (`_invalidate`). Новий розгорнеться на цьому ж виклику;
    зниклий лишається без своїх переказів/`expanded`/`unexpanded`/`missing`, а його піддерево, що
    більше ніким не досяжне, прибирає каскад `_reconcile_level` наступних рівнів. Без цього рівень 0,
    зафіксований після перелічення, що збоїло чи обірвалось, лишався б порожнім чи застарілим — і
    повторний збір повернув би `complete` без переказів нових покупців.
    """
    frontier = state.frontier_by_depth
    old = frontier.get(0, {})
    new = {b.wallet: b.first_buy_signature for b in state.buyers}
    for wallet in old.keys() | new.keys():
        if old.get(wallet) != new.get(wallet):
            _invalidate(state, wallet, into)
    frontier[0] = new


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
