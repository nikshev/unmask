# impl: FR-001-12, FR-001-13
"""Дворівневий кеш результатів у пам'яті процесу (data-model.md, «`ResultCache`»).

Що робить: за адресою mint тримає два незалежні сховища.
- `complete: dict[mint, IngestResult]` — лише результати зі `status == complete` (FR-001-12).
  `put_complete` з неповним результатом піднімає `CacheInvariantError` і кеш не змінює: це захист
  від регресії (дефект викликача), а не очікуваний шлях — неповний результат ніколи не стає
  остаточним (FR-001-13). Успішний `put_complete` атомарно прибирає `partial` того ж mint: після
  повного успіху продовжувати нічого, тож для mint існує щонайбільше один рівень із корисним вмістом.
- `partial: dict[mint, CollectionState]` — стан обірваного збору для `collector.resume`. Звідси
  ніколи не видається результат: `get_partial` повертає стан, а не `IngestResult`.

Як користуватись (порядок кроків — `contracts/ingest-service.md`, сервіс — T-018):
`get_complete(mint)` → якщо є, віддати; інакше `get_partial(mint)` → `resume(state, …)` або новий
`collect`; повний результат → `put_complete(result)`, неповний → `put_partial(state)`.

Ізоляція від викликача: `CollectionState` змінний (і `resume` його мутує), тому `put_partial`
зберігає копію, а `get_partial` повертає нову копію на кожен виклик — мутації збереженого чи
отриманого об'єкта кеш не зачіпають. Копія структурна (`_copy_state`), не `deepcopy`: копіюються лише
КОНТЕЙНЕРИ стану (dict/list/set верхнього рівня й вкладені словники `frontier_by_depth`), а значення
ділиться між копіями. Це коректно, бо значення незмінні — ІНВАРІАНТ, який перевіряє тест
(`tests/test_resume.py::test_collection_state_values_are_immutable`): `ParsedTx`, `Transfer`,
`Purchase`, `Buyer`, `MissingHistory`, `UnexpandedNode`, а також ключі й знімки `scan_memo`
(`funding.ScanKey`, `HistoryScan`, `TokenAccountsListing`, T-022) — frozen-дата-класи, чиї поля — кортежі,
рядки, числа, enum'и або такі самі frozen-об'єкти. Єдиний виняток — `err` у записах
`mint_signatures`: це сирий JSON від RPC (dict/list), тож він копіюється глибоко (лише коли не
`None`). Таблиця `_FIELD_COPY` перелічує КОЖНЕ поле `CollectionState`: нове поле без запису в ній —
гучний `KeyError` при першому `put_partial`, а не тихе спільне посилання (і окремий тест на повноту
таблиці). Глибока копія коштувала секунди на великому стані (30k транзакцій ≈1.2 с put / 1.3 с get)
і не входила в `Deadline`; структурна — ≈1 мс на тому самому стані.

`IngestResult` глибоко незмінний (frozen-дата-класи, кортежі, рядки), тож зберігається й повертається
як є — це незмінний знімок; копію з `served_from_cache=true` робить сервіс (T-018). Ключ —
`result.metadata.mint` / `state.mint`; різні mint не змішуються.

Потокобезпеки немає й не потрібно: ядро збору синхронне й однопотокове (research R-11); один
екземпляр сервісу = один кеш (contracts/ingest-service.md). Викликач, що ділить кеш між потоками,
синхронізує доступ сам. Кеш живе, поки живе процес (spec, Assumptions).

Залежить від: `model` (`IngestResult`, `CompletenessStatus`, `CacheInvariantError`),
`collector.CollectionState`.
"""

from __future__ import annotations

import copy
from dataclasses import fields
from typing import Any, Callable

from unmask.ingest.collector import CollectionState
from unmask.ingest.model import CacheInvariantError, CompletenessStatus, IngestResult


def _same(value: Any) -> Any:
    return value  # незмінне значення (str, int, bool, None, кортеж frozen-об'єктів)


def _mint_signatures(entries: list) -> list:
    # (signature, slot, block_time, err): err — сирий JSON від RPC, єдине мутабельне значення стану
    return [e if e[3] is None else (e[0], e[1], e[2], copy.deepcopy(e[3])) for e in entries]


# Як копіювати кожне поле CollectionState: лише контейнери; значення незмінні (див. шапку модуля).
_FIELD_COPY: dict[str, Callable[[Any], Any]] = {
    "mint": _same,
    "config_version": _same,
    "signature_cursor": _same,
    "mint_signatures": _mint_signatures,
    "mint_history_exhausted": _same,
    "purchases_by_wallet": dict,
    "buyers": _same,
    "frontier_by_depth": lambda by_depth: {depth: dict(level) for depth, level in by_depth.items()},
    "expanded": set,
    "transfers": dict,
    "unexpanded": list,
    "missing": dict,
    "tx_cache": dict,
    "scan_memo": dict,  # ключі й знімки — frozen-дата-класи з кортежами (T-022)
    "rpc_calls": _same,
    "transactions_scanned": _same,
}


def _copy_state(state: CollectionState) -> CollectionState:
    """Незалежна копія стану: нові контейнери, спільні незмінні значення."""
    return CollectionState(**{f.name: _FIELD_COPY[f.name](getattr(state, f.name)) for f in fields(state)})


class ResultCache:
    def __init__(self) -> None:
        self._complete: dict[str, IngestResult] = {}
        self._partial: dict[str, CollectionState] = {}

    # --- complete: лише повні результати (FR-001-12) ------------------------------------------

    def get_complete(self, mint: str) -> IngestResult | None:
        """Повний результат за mint або `None`. Повернений об'єкт незмінний."""
        return self._complete.get(mint)

    def put_complete(self, result: IngestResult) -> None:
        """Зберегти повний результат і прибрати партиційний стан того ж mint.

        Неповний результат → `CacheInvariantError`, кеш не змінюється (FR-001-13)."""
        if not isinstance(result, IngestResult):
            raise CacheInvariantError(f"put_complete: expected IngestResult, got {type(result).__name__}")
        status = result.completeness.status
        if status is not CompletenessStatus.COMPLETE:
            raise CacheInvariantError(
                f"put_complete: result for {result.metadata.mint} has status={status.value}; "
                "an incomplete result is never cached as final (FR-001-13)"
            )
        mint = result.metadata.mint
        self._complete[mint] = result
        self._partial.pop(mint, None)

    # --- partial: стан обірваного збору, ніколи не результат (FR-001-13) -----------------------

    def get_partial(self, mint: str) -> CollectionState | None:
        """Нова копія партиційного стану mint або `None`."""
        state = self._partial.get(mint)
        return None if state is None else _copy_state(state)

    def put_partial(self, state: CollectionState) -> None:
        """Зберегти копію стану (замінює попередній стан того ж mint)."""
        self._partial[state.mint] = _copy_state(state)

    def drop_partial(self, mint: str) -> None:
        """Прибрати партиційний стан mint; відсутній запис — не помилка."""
        self._partial.pop(mint, None)
