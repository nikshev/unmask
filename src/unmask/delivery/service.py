# impl: FR-004-02, FR-004-06, FR-004-07, FR-008-02, FR-008-06, FR-008-09
"""Оркестрація запиту: кеш → живий конвеєр 001→002→003 → звіт → кеш (FR-004-02).

Ті самі класи й конфіги, що й бібліотека: жодних окремих «демо-порогів».
`Rejection` мапиться в документ-помилку, а не у виняток; відмови не кешуються.
Дефекти середовища (немає ключа RPC) не ловляться — fail-fast наверх.

Слот важкого аналізу (фіча 008, R-1/R-2): береться лише власником НОВОГО аналізу валідної адреси,
всередині `build`-замикання `single_flight`. Кеш-хіт повертається до `single_flight`, а приєднаний
до in-flight чекає на подію й `build` не викликає, тож слота не споживають (FR-008-02). Відмова
(`busy`) — документ-помилка через `_ErrorDoc`: не кешується й не лишає запису in-flight; слот
звільняється за будь-якого завершення, включно з `BaseException` (FR-008-06).
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from unmask.delivery.cache import DeliveryCache
from unmask.delivery.limits import HeavySlots
from unmask.delivery.report import build_report
from unmask.ingest.addresses import is_valid_address
from unmask.ingest.model import Rejection

__all__ = ["DeliveryService"]


class _ErrorDoc(Exception):
    """Документ-помилка всередині single-flight: досягає всіх чекаючих, не кешується."""

    def __init__(self, doc: dict[str, Any]) -> None:
        super().__init__(doc.get("error", {}).get("detail", "rejection"))
        self.doc = doc


class DeliveryService:
    """Живий конвеєр доставки з кешем за `mint`."""

    def __init__(self, ingest_service, graph_service, cluster_service,
                 cache: DeliveryCache | None = None, *, slots: HeavySlots | None = None) -> None:
        self._ingest = ingest_service
        self._graph = graph_service
        self._clusters = cluster_service
        self._cache = cache if cache is not None else DeliveryCache()
        self._slots = slots

    @property
    def cache(self) -> DeliveryCache:
        return self._cache

    def needs_analysis(self, mint: str) -> bool:
        """`True` ⇔ формат адреси валідний і документа в кеші немає (слот не береться, збір не йде)."""
        if not isinstance(mint, str) or not is_valid_address(mint):
            return False
        return self._cache.get(mint) is None

    def analyze(self, mint: str, *, progress: Optional[Callable[[str], None]] = None) -> dict[str, Any]:
        """Документ відповіді 004.1 або документ-помилка `{mint, error:{kind, detail}}`."""
        if isinstance(mint, str):
            hit = self._cache.get(mint)
            if hit is not None:
                return hit
        else:
            return {"mint": "", "error": {"kind": "invalid_address", "detail": "mint must be a string"}}
        try:
            return self._cache.single_flight(mint, lambda: self._guarded_build(mint, progress))
        except _ErrorDoc as err:
            return err.doc

    def _guarded_build(self, mint: str, progress: Optional[Callable[[str], None]]) -> dict[str, Any]:
        """Власник нового аналізу: слот → `_build`; немає слота — документ `busy` (без `progress`)."""
        slots = self._slots
        if slots is None or not is_valid_address(mint):
            return self._build(mint, progress=progress)
        lease = slots.try_acquire()
        if lease is None:
            n = slots.retry_after_seconds
            raise _ErrorDoc({
                "mint": mint,
                "error": {
                    "kind": "busy",
                    "detail": f"too many analyses in progress, retry after {n} s",
                    "retry_after_seconds": n,
                },
            })
        with lease:
            return self._build(mint, progress=progress)

    def _build(self, mint: str, *, progress: Optional[Callable[[str], None]] = None) -> dict[str, Any]:
        if progress:
            progress("🔄 Collecting data...")
        outcome = self._ingest.collect(mint)
        if isinstance(outcome, Rejection):
            raise _ErrorDoc({
                "mint": outcome.mint or mint,
                "error": {"kind": outcome.kind.value, "detail": outcome.detail},
            })
        if progress:
            progress("🔄 Building funding graph...")
        graph_result = self._graph.analyze(outcome)
        if progress:
            progress("🔄 Finding clusters...")
        cluster_result = self._clusters.analyze(graph_result, outcome)
        if progress:
            progress("🔄 Building report...")
        return build_report(outcome, graph_result, cluster_result)
