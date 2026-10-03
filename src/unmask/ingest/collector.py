# impl: FR-001-01, FR-001-02, FR-001-09, FR-001-10, FR-001-13, FR-001-14
"""Стан збору та оркестрація (data-model.md, «Стан збору і кеш»; contracts/ingest-service.md, крок 5).

Що робить:
- `CollectionState` — змінний спільний контракт кроків збору: перелічення покупців (`buyers.py`)
  заповнює курсор, історію mint, купівлі й відібраних покупців; BFS фінансування (`funding.py`) —
  frontier, перекази, `unexpanded`, `missing`. Живе лише всередині збору й у партиційному сховищі
  кешу; у результат (`IngestResult`) не потрапляє.
- `collect(state, source, config, clock) -> IngestResult`: `enumerate_buyers` → `expand_level` для
  глибин 1…`funding_depth` → `Completeness.derive(state.missing, buyers_completeness)` → `RunMetadata`.

Статус повноти ніколи не задається тут вручну: його виводить `Completeness.derive` з даних —
`complete` лише коли `missing` порожній **і** перелічення покупців повне (FR-001-09/10, принцип V).
`unexpanded[]` на статус не впливає (свідома межа конфігурації, не збій).

Упорядкування — лише ключами з `model.py` (R-6): покупці `buyer_sort_key`, перекази
`transfer_sort_key`, `unexpanded` — `(depth, wallet)`, `missing` упорядковує сам `Completeness`.
Порядок відповідей RPC на результат не впливає.

Метадані (FR-001-14): використані значення конфігу й `config_version`; `wallets_analyzed =
len(buyers)`; `source = source.name`; `analyzed_at = clock.wall()` на початку збору;
`elapsed_seconds` — різниця `clock.monotonic()` від початку до кінця цього виклику; `rpc_calls` —
звернень до джерела **за цей виклик** (приріст `state.rpc_calls`, data-model «за цей прогін»);
`transactions_scanned` — скільки транзакцій mint пропущено через правило купівлі в останньому
проході `enumerate_buyers` (і запитаних, і взятих із кешу), тож детермінований: після будь-якого
повтору дорівнює свіжому прогону. `resumed` і
`served_from_cache` тут завжди `false` — їх виставляють `resume` (T-017) і сервіс (T-018).

Повторний виклик на тому самому `state` (зокрема після збою чи обірваного перелічення) дає той самий
результат, що й свіжий прогін, за всіма полями, крім volatile-полів metadata (`rpc_calls` — реальна
робота за прогін): `enumerate_buyers` перевиводить купівлі від найстаріших (кеш транзакцій
відтворюється, решта дозапитується), `expand_level(1)` перевиводить рівень 0 з поточних покупців
(`funding._reconcile_buyers`) — нові, змінені й зниклі покупці проходять ту саму інвалідацію й
каскад, що й вершини глибших рівнів (T-013), а записи `missing` вершини перебудовуються з її
останнього розгортання (причини лише актуальні; перекази попередніх спроб лишаються доказами).
Розгорнуті вершини й закешовані транзакції повторно не запитуються, транзакції mint за слотом N-го
покупця на повторі не дозапитуються (ліниві пакети в `buyers.py`): на повному стані — нуль звернень. Що лишається T-017: партиційний
кеш стану, політика відкидання стану іншої версії конфігу (тут — гучна відмова) і `resumed=true`.

Бюджет часу: ТОЧКА ПІДСТАНОВКИ T-015 — `_deadline_for(clock, config)`. Зараз повертає
`_NeverExpires` (структурний `DeadlineLike`, ніколи не спливає), який передається в
`enumerate_buyers` і кожен `expand_level`, а звідти — у кожен виклик джерела. T-015 замінить його на
`budget.Deadline(clock, config.time_budget_seconds)` і додасть перевірки `expired()` між кроками.

Залежить від: `buyers`, `funding` (імпортуються в тілі `collect`: вони самі імпортують
`CollectionState` звідси), `budget.Clock`, `config`, `model`, `rpc.protocol`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from unmask.ingest.model import (
    Buyer,
    Completeness,
    IngestResult,
    MissingHistory,
    MissingReason,
    RunMetadata,
    Transfer,
    UnexpandedNode,
    buyer_sort_key,
    transfer_sort_key,
)
from unmask.ingest.parse import ParsedTx
from unmask.ingest.purchases import Purchase

if TYPE_CHECKING:
    from unmask.ingest.budget import Clock
    from unmask.ingest.config import IngestConfig
    from unmask.ingest.rpc.protocol import RpcSource

# Запис історії mint: (signature, slot, block_time, err) — рівно те, що зберігає перегортання (R-7).
MintSignature = tuple[str, int, int | None, Any]


@dataclass
class CollectionState:
    mint: str
    config_version: int
    # перелічення покупців (R-7): останній підпис перегортання історії mint або None
    signature_cursor: str | None = None
    mint_signatures: list[MintSignature] = field(default_factory=list)
    mint_history_exhausted: bool = False
    purchases_by_wallet: dict[str, Purchase] = field(default_factory=dict)
    buyers: tuple[Buyer, ...] = ()
    # BFS фінансування (R-1): глибина -> {гаманець: підпис-межа}
    frontier_by_depth: dict[int, dict[str, str]] = field(default_factory=dict)
    expanded: set[str] = field(default_factory=set)
    transfers: dict[tuple[str, str], Transfer] = field(default_factory=dict)
    unexpanded: list[UnexpandedNode] = field(default_factory=list)
    missing: dict[tuple[str, MissingReason], MissingHistory] = field(default_factory=dict)
    tx_cache: dict[str, ParsedTx] = field(default_factory=dict)
    rpc_calls: int = 0
    transactions_scanned: int = 0


class _NeverExpires:
    """Заглушка дедлайну до T-015: структурно `DeadlineLike`, ніколи не спливає."""

    def expired(self) -> bool:
        return False

    def remaining(self) -> float:
        return math.inf


def _deadline_for(clock: Clock, config: IngestConfig) -> _NeverExpires:
    """ТОЧКА ПІДСТАНОВКИ T-015: тут стане `budget.Deadline(clock, config.time_budget_seconds)`."""
    return _NeverExpires()


def collect(state: CollectionState, source: RpcSource, config: IngestConfig, clock: Clock) -> IngestResult:
    """Зібрати покупців і їхнє фінансування в `state`; повернути результат із виведеною повнотою."""
    from unmask.ingest.buyers import enumerate_buyers  # цикл імпорту: buyers/funding імпортують
    from unmask.ingest.funding import expand_level      # CollectionState з цього модуля

    if state.config_version != config.version:
        raise ValueError(
            f"state.config_version={state.config_version} != config.version={config.version}: "
            "стан іншої версії конфігу не продовжується (принцип III)"
        )
    started = clock.monotonic()
    analyzed_at = clock.wall()
    calls_before = state.rpc_calls
    deadline = _deadline_for(clock, config)

    buyers_completeness = enumerate_buyers(source, state.mint, state, config, deadline)
    for depth in range(1, config.funding_depth + 1):
        expand_level(source, state, depth, config, deadline)

    buyers = tuple(sorted(state.buyers, key=buyer_sort_key))
    metadata = RunMetadata(
        mint=state.mint,
        analyzed_at=analyzed_at,
        wallets_analyzed=len(buyers),
        config_version=config.version,
        first_buyers_n=config.first_buyers_n,
        funding_depth=config.funding_depth,
        counterparty_threshold=config.counterparty_threshold,
        max_signatures_per_wallet=config.max_signatures_per_wallet,
        collect_spl_inbound=config.collect_spl_inbound,
        time_budget_seconds=config.time_budget_seconds,
        elapsed_seconds=max(0.0, clock.monotonic() - started),
        rpc_calls=state.rpc_calls - calls_before,
        transactions_scanned=state.transactions_scanned,
        source=source.name,
        resumed=False,
        served_from_cache=False,
    )
    return IngestResult(
        metadata=metadata,
        completeness=Completeness.derive(state.missing.values(), buyers_completeness),
        buyers=buyers,
        transfers=tuple(sorted(state.transfers.values(), key=transfer_sort_key)),
        unexpanded=tuple(sorted(state.unexpanded, key=lambda u: (u.depth, u.wallet))),
    )
