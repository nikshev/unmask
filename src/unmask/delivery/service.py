# impl: FR-004-02, FR-004-06, FR-004-07
"""Оркестрація запиту: кеш → живий конвеєр 001→002→003 → звіт → кеш (FR-004-02).

Ті самі класи й конфіги, що й бібліотека: жодних окремих «демо-порогів».
`Rejection` мапиться в документ-помилку, а не у виняток; відмови не кешуються.
Дефекти середовища (немає ключа RPC) не ловляться — fail-fast наверх.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from unmask.delivery.cache import DeliveryCache
from unmask.delivery.report import build_report
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
                 cache: DeliveryCache | None = None) -> None:
        self._ingest = ingest_service
        self._graph = graph_service
        self._clusters = cluster_service
        self._cache = cache if cache is not None else DeliveryCache()

    @property
    def cache(self) -> DeliveryCache:
        return self._cache

    def analyze(self, mint: str, *, progress: Optional[Callable[[str], None]] = None) -> dict[str, Any]:
        """Документ відповіді 004.1 або документ-помилка `{mint, error:{kind, detail}}`."""
        if isinstance(mint, str):
            hit = self._cache.get(mint)
            if hit is not None:
                return hit
        else:
            return {"mint": "", "error": {"kind": "invalid_address", "detail": "mint must be a string"}}
        try:
            return self._cache.single_flight(mint, lambda: self._build(mint, progress=progress))
        except _ErrorDoc as err:
            return err.doc

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
