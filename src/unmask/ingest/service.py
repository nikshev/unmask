# impl: FR-001-12, FR-001-13
"""Публічний вхід фічі 001: `IngestService` (contracts/ingest-service.md).

Що робить: `IngestService(config, source, clock, cache).collect(mint) -> IngestOutcome` — один
повторно використовуваний вхід, що поєднує збір (`collector`) і кеш (`cache.ResultCache`). Кроки
`collect` — у порядку контракту:

1. Некоректна адреса → `Rejection(invalid_address)` до будь-якого звернення — **T-019** (точка
   розширення `_reject_invalid_address`; поки що повертає `None`).
2. `complete` у кеші → копія з `metadata.served_from_cache=true`, решта полів ідентична першому
   поверненню (зокрема `rpc_calls`, `analyzed_at`, `resumed`); нуль звернень до `source` (FR-001-12).
   Копія — `dataclasses.replace` двох верхніх рівнів: `IngestResult` глибоко незмінний, тож кортежі
   покупців/переказів діляться, а вартість копії не залежить від розміру результату.
3. `partial` поточної `config.version` → `collector.resume` (лише недоотримане, FR-001-13). Стан
   іншої версії відкидається ТУТ (`drop_partial`) і збір іде з нового порожнього стану: сервіс ніколи
   не передає застарілий стан у `collector.collect` (там це гучний `ValueError` — дефект викликача).
4. Існування токена (`get_account_info`) → `Rejection(token_not_found, …)` — **T-019** (точка
   розширення `_reject_missing_token`; поки що повертає `None`). Стоїть перед збором — і свіжим, і
   продовженням; чи пропускати її для продовження (стан уже пройшов крок 4), вирішує T-019.
5. Збір: `collector.collect`/`resume` — обидва самі створюють `Deadline(clock,
   config.time_budget_seconds)` на початку, тож бюджет покриває кожне звернення збору (FR-001-16).
6. `complete` → `cache.put_complete` (той сам виклик прибирає `partial`); `incomplete` → лише
   `cache.put_partial(state)`. Результат повертається в обох випадках із чесним статусом
   (FR-001-09/10): неповний ніколи не стає остаточним (FR-001-13), наступний виклик продовжує його.

ДО T-019 сервіс НЕ відфільтровує некоректні чи неіснуючі mint: такий рядок іде прямо в збір
(порожній рядок падає на валідації `RunMetadata`; неіснуючий токен дає порожній «повний» результат).
Не покладатися на `collect` для недовірених адрес, доки T-019 не закрито.

Рішення поза буквою контракту: `complete`-результат іншої `config_version`, ніж у сервісу (можливо
лише з переданим ззовні спільним кешем), не віддається — це промах кешу, збір іде заново, новий повний
результат замінює старий (принцип III: прогони різних версій не змішуються; те саме правило, що й для
`partial` у кроці 3).

Гарантії (контракт): винятки через дані з `collect` не виходять — збої джерела й вичерпаний бюджет
`collector` перетворює на неповноту. Дефекти програми (`CacheInvariantError`, `ConfigError`) не
ловляться і падають гучно. Сервіс не читає файлів/середовища, не пише на диск, у мережу — лише через
`source`. Один екземпляр = один кеш (`cache=None` → новий `ResultCache`); кеш живе з процесом.

Залежить від: `collector` (`CollectionState`, `collect`, `resume`), `cache` (`ResultCache`),
`budget` (`Clock`, `SystemClock`), `config`, `model`, `rpc.protocol`.
"""

from __future__ import annotations

from dataclasses import replace

from unmask.ingest.budget import Clock, SystemClock
from unmask.ingest.cache import ResultCache
from unmask.ingest.collector import CollectionState, collect, resume
from unmask.ingest.config import IngestConfig
from unmask.ingest.model import CompletenessStatus, IngestOutcome, IngestResult, Rejection
from unmask.ingest.rpc.protocol import RpcSource


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
        # 1. Адреса (T-019): до будь-якого звернення до кешу чи джерела.
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

        # 4. Існування токена (T-019): перед збором.
        rejection = self._reject_missing_token(mint)
        if rejection is not None:
            return rejection

        # 5. Збір під бюджетом часу (Deadline створює колектор).
        if state is None:
            state = CollectionState(mint=mint, config_version=self._config.version)
            result = collect(state, self._source, self._config, self._clock)
        else:
            result = resume(state, self._source, self._config, self._clock)

        # 6. Повний — у complete (partial чиститься); неповний — лише partial.
        self._store(state, result)
        return result

    def _store(self, state: CollectionState, result: IngestResult) -> None:
        if result.completeness.status is CompletenessStatus.COMPLETE:
            self._cache.put_complete(result)
        else:
            self._cache.put_partial(state)

    # --- Точки розширення T-019 (FR-001-11) ----------------------------------------------------

    def _reject_invalid_address(self, mint: str) -> Rejection | None:
        """Крок 1 (T-019): некоректна адреса → `Rejection(invalid_address)` без звернень до джерела.

        До T-019 — нічого не перевіряє (`None`): некоректний рядок іде далі (див. модуль)."""
        return None

    def _reject_missing_token(self, mint: str) -> Rejection | None:
        """Крок 4 (T-019): `get_account_info` → відсутній / не mint → `Rejection(token_not_found)`.

        Відмова не кешується; збій джерела тут — не відмова, а неповний результат. До T-019 — `None`."""
        return None
