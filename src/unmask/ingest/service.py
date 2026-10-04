# impl: FR-001-11, FR-001-12, FR-001-13
"""Публічний вхід фічі 001: `IngestService` (contracts/ingest-service.md).

Що робить: `IngestService(config, source, clock, cache).collect(mint) -> IngestOutcome` — один
повторно використовуваний вхід, що поєднує збір (`collector`) і кеш (`cache.ResultCache`). Кроки
`collect` — у порядку контракту:

1. Некоректна адреса (не рядок, порожній, не base58, не рівно 32 байти — `addresses.is_valid_address`)
   → `Rejection(invalid_address)` ДО будь-якого звернення до кешу чи джерела (FR-001-11). Для не-рядка
   `Rejection.mint = ""` (поле — рядок), тип — у `detail`.
2. `complete` у кеші → копія з `metadata.served_from_cache=true`, решта полів ідентична першому
   поверненню (зокрема `rpc_calls`, `analyzed_at`, `resumed`); нуль звернень до `source` (FR-001-12).
   Копія — `dataclasses.replace` двох верхніх рівнів: `IngestResult` глибоко незмінний, тож кортежі
   покупців/переказів діляться, а вартість копії не залежить від розміру результату.
3. `partial` поточної `config.version` → `collector.resume` (лише недоотримане, FR-001-13). Стан
   іншої версії відкидається ТУТ (`drop_partial`) і збір іде з нового порожнього стану: сервіс ніколи
   не передає застарілий стан у `collector.collect` (там це гучний `ValueError` — дефект викликача).
4. Існування токена: `get_account_info(mint)` — на КОЖНОМУ шляху, що не віддається з кешу (і новий
   збір, і resume: інакше збій на цьому кроці назавжди лишив би токен неперевіреним). `None` →
   `Rejection(token_not_found, "account_missing")`; власник не Token/Token-2022 або
   `data.parsed.type != "mint"` (зокрема відсутня чи крива форма `data`) → `…, "not_a_mint"`
   (`_is_mint` захисний до форми — ніколи не виняток). Відмова не кешується й `partial` не чіпає:
   наступний виклик знову питає джерело. Збій джерела тут — НЕ відмова й не виняток: будь-який
   `RpcError` (захисний шар T-016: три класи контракту за `isinstance`, базовий/невідомий підклас →
   `unavailable`) або вичерпаний бюджет → `IngestResult` зі `status=incomplete`, `buyers.complete=false`,
   причиною за класом і `detail` «getAccountInfo <mint>: <Клас>: <текст>», без покупців/переказів
   (`_unverified`, будується напряму: збору не було). Стан (новий порожній або збережений `partial`,
   незмінний) кладеться в `partial` — наступний виклик повторить крок 4 і продовжить. Не-`RpcError`
   виняток джерела — дефект адаптера: не ловиться й виходить (contracts/rpc-source.md).
5. Збір: `collector.collect`/`resume` з ТИМ САМИМ `Deadline`, що й крок 4 (див. «Бюджет»).
6. `complete` → `cache.put_complete` (той сам виклик прибирає `partial`); `incomplete` → лише
   `cache.put_partial(state)`. Результат повертається в обох випадках із чесним статусом
   (FR-001-09/10): неповний ніколи не стає остаточним (FR-001-13), наступний виклик продовжує його.

Бюджет (FR-001-16, рішення власника процесу за T-018/T-019): сервіс створює один
`Deadline(clock, config.time_budget_seconds)` після кроку 3 і передає його і в крок 4, і в
`collect`/`resume` (необов'язковий `deadline=`; без нього колектор, як і раніше, створює власний —
прямі виклики колектора не змінились). Тож прогін сервісу блокує не довше бюджету + хвіст одного
запиту. Наслідок: бюджет, коротший за одне звернення, не дає збору жодного звернення після кроку 4.

Метадані прогону, що не віддається з кешу: `analyzed_at` — `clock.wall()` на початку прогону сервісу,
`elapsed_seconds` — від нього до кінця, `rpc_calls` — звернення колектора + 1 (крок 4; 0, лише якщо
бюджет сплив до звернення — тоді збору й не було); решта — від
колектора. Тож журнал джерела для сервісу = `[getAccountInfo(mint)] + журнал колектора`, а
`rpc_calls == len(журналу)` для свіжого прогону. Усі три поля volatile (контракт, детермінізм).

Рішення поза буквою контракту: `complete`-результат іншої `config_version`, ніж у сервісу (можливо
лише з переданим ззовні спільним кешем), не віддається — це промах кешу, збір іде заново, новий повний
результат замінює старий (принцип III: прогони різних версій не змішуються; те саме правило, що й для
`partial` у кроці 3).

Гарантії (контракт): винятки через дані з `collect` не виходять — некоректна адреса й відсутній токен
дають `Rejection`, збої джерела й вичерпаний бюджет — неповноту (крок 4 тут, решту — `collector`).
Дефекти програми (`CacheInvariantError`, `ConfigError`, не-`RpcError` з адаптера) не ловляться і падають
гучно. Сервіс не читає файлів/середовища, не пише на диск, у мережу — лише через `source`. Один
екземпляр = один кеш (`cache=None` → новий `ResultCache`); кеш живе з процесом.

Залежить від: `collector` (`CollectionState`, `collect`, `resume`), `cache` (`ResultCache`),
`budget` (`Clock`, `SystemClock`, `Deadline`, `ensure_time`, `deadline_timeouts`), `addresses`,
`buyers` (мапа винятку джерела на причину), `parse` (id програм Token), `config`, `model`, `rpc.protocol`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from unmask.ingest.addresses import is_valid_address
from unmask.ingest.budget import BudgetExhausted, Clock, Deadline, SystemClock, deadline_timeouts, ensure_time
from unmask.ingest.buyers import _rpc_detail, _rpc_reason  # одна мапа «клас винятку → причина» (T-016)
from unmask.ingest.cache import ResultCache
from unmask.ingest.collector import CollectionState, collect, resume
from unmask.ingest.config import IngestConfig
from unmask.ingest.model import (
    BuyersCompleteness,
    Completeness,
    CompletenessStatus,
    IngestOutcome,
    IngestResult,
    MissingReason,
    RejectKind,
    Rejection,
    RunMetadata,
)
from unmask.ingest.parse import TOKEN_2022_PROGRAM, TOKEN_PROGRAM
from unmask.ingest.rpc.protocol import RpcError, RpcSource

# Критерій «токен існує» (contracts/rpc-source.md, getAccountInfo): власник — Token або Token-2022.
TOKEN_PROGRAMS = frozenset({TOKEN_PROGRAM, TOKEN_2022_PROGRAM})

ACCOUNT_MISSING = "account_missing"
NOT_A_MINT = "not_a_mint"


class IngestService:
    def __init__(
        self,
        config: IngestConfig,
        source: RpcSource,
        clock: Clock = SystemClock(),  # без стану, тож спільний екземпляр за замовчуванням безпечний
        cache: ResultCache | None = None,
    ) -> None:
        self._config = config
        self._source = source
        self._clock = clock
        self._cache = cache if cache is not None else ResultCache()

    def collect(self, mint: str) -> IngestOutcome:
        """Результат збору для `mint` або явна відмова; порядок кроків — див. модуль."""
        # 1. Адреса: до будь-якого звернення до кешу чи джерела.
        rejection = self._reject_invalid_address(mint)
        if rejection is not None:
            return rejection

        # 2. Повний результат із кешу — нуль звернень до джерела.
        cached = self._cache.get_complete(mint)
        if cached is not None and cached.metadata.config_version == self._config.version:
            return replace(cached, metadata=replace(cached.metadata, served_from_cache=True))

        # 3. Партиційний стан поточної версії — продовжити; іншої — відкинути.
        state = self._cache.get_partial(mint)
        if state is not None and state.config_version != self._config.version:
            self._cache.drop_partial(mint)
            state = None

        # Прогін сервісу: один бюджет на крок 4 і збір (хвіст — один запит), метадані — з цього моменту.
        started = self._clock.monotonic()
        analyzed_at = self._clock.wall()
        deadline = Deadline(self._clock, self._config.time_budget_seconds)

        # 4. Існування токена — на кожному шляху, що не віддається з кешу (і новий збір, і resume).
        check, step4_calls = self._reject_missing_token(mint, deadline)
        if isinstance(check, Rejection):
            return check  # відмова не кешується й partial не чіпає
        run = collect if state is None else resume
        if state is None:
            state = CollectionState(mint=mint, config_version=self._config.version)
        if check is not None:
            # Збій джерела на кроці 4: токен не перевірено, збору не було — чесна неповнота; стан
            # (порожній або збережений) іде в partial, наступний виклик повторить крок 4 і продовжить.
            result = self._unverified(mint, check, analyzed_at, started, rpc_calls=step4_calls)
        else:
            # 5. Збір під тим самим дедлайном; метадані — за весь прогін сервісу (з кроком 4).
            result = run(state, self._source, self._config, self._clock, deadline=deadline)
            result = self._run_metadata(result, analyzed_at, started, extra_calls=step4_calls)

        # 6. Повний — у complete (partial чиститься); неповний — лише partial.
        self._store(state, result)
        return result

    def _store(self, state: CollectionState, result: IngestResult) -> None:
        if result.completeness.status is CompletenessStatus.COMPLETE:
            self._cache.put_complete(result)
        else:
            self._cache.put_partial(state)

    def _run_metadata(self, result: IngestResult, analyzed_at: int, started: float, *,
                      extra_calls: int) -> IngestResult:
        """Час і звернення — за весь прогін сервісу: колектор міряє лише свою частину (без кроку 4)."""
        meta = replace(
            result.metadata,
            analyzed_at=analyzed_at,
            elapsed_seconds=self._elapsed(started),
            rpc_calls=result.metadata.rpc_calls + extra_calls,
        )
        return replace(result, metadata=meta)

    def _elapsed(self, started: float) -> float:
        return max(0.0, self._clock.monotonic() - started)

    def _unverified(self, mint: str, failure: BuyersCompleteness, analyzed_at: int, started: float, *,
                    rpc_calls: int) -> IngestResult:
        """Результат прогону, обірваного збоєм джерела на кроці 4: нічого не зібрано, `buyers.complete=false`.

        Будується напряму (не через `collect`): колектор не має що робити без перевіреного токена, а
        порожній результат із причиною в `buyers` — рівно те, що сталося. Статус — лише з `derive`."""
        cfg = self._config
        metadata = RunMetadata(
            mint=mint,
            analyzed_at=analyzed_at,
            wallets_analyzed=0,
            config_version=cfg.version,
            first_buyers_n=cfg.first_buyers_n,
            funding_depth=cfg.funding_depth,
            counterparty_threshold=cfg.counterparty_threshold,
            max_signatures_per_wallet=cfg.max_signatures_per_wallet,
            collect_spl_inbound=cfg.collect_spl_inbound,
            time_budget_seconds=cfg.time_budget_seconds,
            elapsed_seconds=self._elapsed(started),
            rpc_calls=rpc_calls,  # звернення кроку 4 (0 — бюджет сплив до нього)
            transactions_scanned=0,
            source=self._source.name,
            resumed=False,  # стан не продовжувався
            served_from_cache=False,
        )
        return IngestResult(
            metadata=metadata,
            completeness=Completeness.derive((), failure),
            buyers=(),
            transfers=(),
            unexpanded=(),
        )

    # --- Кроки 1 і 4 (FR-001-11) ---------------------------------------------------------------

    def _reject_invalid_address(self, mint: object) -> Rejection | None:
        """Крок 1: не рядок, порожній, не base58 чи не 32 байти → `Rejection(invalid_address)`.

        Без звернень до кешу чи джерела. `Rejection.mint` — рядок: не-рядок не відтворюється (`""`),
        його тип — у `detail`."""
        if not isinstance(mint, str):
            return Rejection(RejectKind.INVALID_ADDRESS, "", f"not a string: {type(mint).__name__}")
        if not mint:
            return Rejection(RejectKind.INVALID_ADDRESS, mint, "empty")
        if not is_valid_address(mint):
            return Rejection(RejectKind.INVALID_ADDRESS, mint, "not a base58 string of 32 bytes")
        return None

    def _reject_missing_token(
        self, mint: str, deadline: Deadline
    ) -> tuple[Rejection | BuyersCompleteness | None, int]:
        """Крок 4: `get_account_info(mint)` під бюджетом прогону.

        - `None` → `Rejection(token_not_found, account_missing)`; не mint → `…, not_a_mint` (`_is_mint`);
        - mint (Token або Token-2022) → `None`, збір іде далі;
        - будь-який `RpcError` (захисний шар T-016: невідомий клас → `unavailable`, клас — у `detail`) або
          вичерпаний бюджет → `BuyersCompleteness(complete=false)` з причиною: не відмова, а неповнота.
        Не-`RpcError` виняток джерела — дефект адаптера: не ловиться (contracts/rpc-source.md).
        Друге значення — скільки звернень зроблено (0, якщо бюджет сплив до звернення; інакше 1)."""
        what = f"getAccountInfo {mint}"
        calls = 0
        try:
            ensure_time(deadline, what)  # перед зверненням, як і в колекторі
            calls = 1
            with deadline_timeouts(deadline, what):
                account = self._source.get_account_info(mint, deadline=deadline)
        except BudgetExhausted as exc:
            return BuyersCompleteness(complete=False, reason=MissingReason.BUDGET_EXHAUSTED, detail=exc.detail), calls
        except RpcError as exc:
            return BuyersCompleteness(complete=False, reason=_rpc_reason(exc), detail=_rpc_detail(what, exc)), calls
        if account is None:
            return Rejection(RejectKind.TOKEN_NOT_FOUND, mint, ACCOUNT_MISSING), calls
        if not _is_mint(account):
            return Rejection(RejectKind.TOKEN_NOT_FOUND, mint, NOT_A_MINT), calls
        return None, calls


def _is_mint(account: Any) -> bool:
    """`owner` ∈ {Token, Token-2022} і `data.parsed.type == "mint"`; будь-яка інша форма — не mint.

    Захисно до форми (дані джерела, не контракт типів): не-словник, відсутні ключі, `data` як
    `[base64, "base64"]`, нехешовані чи нерядкові значення — `False`, ніколи не виняток."""
    if not isinstance(account, Mapping):
        return False
    owner = account.get("owner")
    data = account.get("data")
    parsed = data.get("parsed") if isinstance(data, Mapping) else None
    kind = parsed.get("type") if isinstance(parsed, Mapping) else None
    return isinstance(owner, str) and owner in TOKEN_PROGRAMS and isinstance(kind, str) and kind == "mint"
