# verifies: FR-004-06
"""Час холодного й повторного запиту на записах (T-098; годинник — під маркером `perf`)."""

from __future__ import annotations

import statistics
import time

import pytest

from test_delivery_service import RecordedIngest, _mint_of, _real_services
from unmask.delivery.cache import DeliveryCache
from unmask.delivery.service import DeliveryService

RUNS = 3
COLD_BUDGET_SECONDS = 60.0  # FR-004-06; детектор регресу з великим запасом, не гонка


def _service() -> tuple[DeliveryService, RecordedIngest, str]:
    from pathlib import Path
    real = Path(__file__).parent / "fixtures" / "real"
    mint, path = _mint_of("ins4")
    ingest = RecordedIngest({mint: path})
    graph, clusters = _real_services()
    return DeliveryService(ingest, graph, clusters, DeliveryCache()), ingest, mint


@pytest.mark.perf
def test_cold_request_under_60s_on_recorded_typical_token() -> None:
    service, _, mint = _service()
    times = []
    for _ in range(RUNS):
        service.cache._docs.clear()
        start = time.perf_counter()
        doc = service.analyze(mint)
        times.append(time.perf_counter() - start)
        assert doc["risk_score"] == 54
    assert statistics.median(times) < COLD_BUDGET_SECONDS


def test_repeat_request_needs_no_transport_calls() -> None:
    service, ingest, mint = _service()
    first = service.analyze(mint)
    calls_after_first = list(ingest.calls)
    second = service.analyze(mint)
    assert first == second
    assert ingest.calls == calls_after_first
