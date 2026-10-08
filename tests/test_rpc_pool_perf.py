# verifies: FR-007-02, FR-007-15
"""Реальний паралелізм пулу на hub (T-144, FR-007-02, SC-001): стінний час пулу ≤ 0,5 × одиночного джерела.

Маркер `perf` — поза типовим прогоном (`addopts = -m 'not perf'`). Запуск:
`pytest -m perf tests/test_rpc_pool_perf.py`; годинник чутливий до навантаження машини.

МОДЕЛЬ ЗАТРИМКИ — ліміт швидкості провайдера, а не латентність виклику. Провайдер з ставкою `r` одиниць/с
витрачає на кожен підпис у `getTransaction` час `UNIT_SECONDS_PER_ITEM / r`: вузьке місце — витрата одиниць
rate-ліміту на запити за підписами, тому пакет з n підписів коштує n/r, а не фіксовані 20 ms незалежно від
розміру. Саме така модель робить розподіл пакета між провайдерами корисним. Одиночні методи (`getSignatures…`)
без штучної затримки. Підклас `_RateLimitedProvider` локальний для цього файлу; `rpc_pool_doubles.py` не
змінюється.

Поріг `RATIO_LIMIT = 0.5` — це SC-001 («≥ 2× прискорення з трьома рівними провайдерами»). Нерівні ваги SC-001
не покриває, і тут поріг не тестується: виміряно, що 2:1:1 з рівними за вагою ставками (ставка кожного = вазі)
дає ratio ≈ 0,57–0,61 проти найсильнішого одиночного (ставка 2). Теоретична межа цього порівняння — 0,5 при
ідеальному балансі, тож поріг недосяжний за конструкцією; ваги, що не відповідають реальним ставкам, дають ≈ 0,55.
Це вимога до конфігу/постановки, а не баг пулу; рішення про поріг для нерівних ваг — за людиною.

Результат обох варіантів має бути побайтово однаковий (`to_json`): виграш часу не куплений зміною висновку.
"""

from __future__ import annotations

import dataclasses
import json
import time
from pathlib import Path

import pytest

from rpc_pool_doubles import ScriptedProvider
from unmask.ingest.budget import FakeClock
from unmask.ingest.config import load_config
from unmask.ingest.rpc.fixture import FixtureRpcSource
from unmask.ingest.rpc.pool import PooledRpcSource, Provider
from unmask.ingest.serialize import to_json
from unmask.ingest.service import IngestService

pytestmark = pytest.mark.perf

ROOT = Path(__file__).resolve().parent.parent
SCENARIOS = ROOT / "tests" / "fixtures" / "scenarios"
SHIPPED = ROOT / "config" / "ingest.yaml"
BASIC_EXPECTED = json.loads((SCENARIOS / "basic" / "expected.json").read_text(encoding="utf-8"))
HUB = SCENARIOS / "hub"
HUB_EXPECTED = json.loads((HUB / "expected.json").read_text(encoding="utf-8"))

# Витрата часу на один підпис для провайдера ставкою 1 одиниця/с. Вибрано так, щоб затримка домінувала над
# накладними (CPU фікстури, одиночні методи): одиночний збір ≈ 2 с. При ≈1 с ratio був ≈0,44 — надто близько до порогу.
UNIT_SECONDS_PER_ITEM = 0.0016
RATIO_LIMIT = 0.5  # SC-001: пул ≤ 0,5 × одиночного (≥ 2× прискорення)
RUNS = 2  # найкращий з двох прогонів на варіант: зменшує шум від навантаження машини


class _RateLimitedProvider(ScriptedProvider):
    """Дубль з лімітом швидкості: `getTransaction` витрачає `per_item / rate` с на підпис (модель витрати одиниць)."""

    def __init__(self, inner, rate: float) -> None:
        super().__init__(inner)
        self._per_item = UNIT_SECONDS_PER_ITEM / rate

    def get_transactions(self, signatures, *, deadline):
        time.sleep(self._per_item * len(signatures))
        return super().get_transactions(signatures, deadline=deadline)


def _cfg():
    values = {**BASIC_EXPECTED["config"], **HUB_EXPECTED["cases"][0]["config"]}
    return dataclasses.replace(load_config(SHIPPED), **values)


def _collect_timed(source, cfg, mint: str):
    start = time.perf_counter()
    result = IngestService(cfg, source, FakeClock(), None).collect(mint)
    return result, time.perf_counter() - start


def _pool_vs_single(weights: tuple[float, ...], rates: tuple[float, ...], single_rate: float):
    """Найкращі з RUNS прогонів: (single_best, pool_best, json_single, json_pool, served_providers)."""
    cfg = _cfg()
    mint = HUB_EXPECTED["mint"]

    single_times, single_json = [], None
    for _ in range(RUNS):
        result, elapsed = _collect_timed(_RateLimitedProvider(FixtureRpcSource(HUB), single_rate), cfg, mint)
        single_times.append(elapsed)
        single_json = to_json(result)

    pool_times, pool_json, doubles = [], None, []
    for _ in range(RUNS):
        doubles = [_RateLimitedProvider(FixtureRpcSource(HUB), rate) for rate in rates]
        providers = [
            Provider(name=f"p{i}", source=double, weight=weight, archival=True, retention_seconds=None)
            for i, (double, weight) in enumerate(zip(doubles, weights))
        ]
        # runner не передається: вимірюється шлях за замовчуванням (ThreadRunner), як у продакшені
        pool = PooledRpcSource(
            providers,
            clock=FakeClock(),
            rate_limit_cooldown_seconds=30.0,
            age_cache_entries=1000,
        )
        result, elapsed = _collect_timed(pool, cfg, mint)
        pool_times.append(elapsed)
        pool_json = to_json(result)

    served = sum(1 for d in doubles if any(method == "getTransaction" for method, _ in d.log))
    return min(single_times), min(pool_times), single_json, pool_json, served, single_times, pool_times


def _assert_at_most_half(single_best, pool_best, single_times, pool_times, label: str) -> None:
    assert pool_best <= RATIO_LIMIT * single_best, (
        f"{label}: пул {pool_best:.3f}s > {RATIO_LIMIT} × одиночного {single_best:.3f}s "
        f"(пул: {[round(t, 3) for t in pool_times]}, одиночний: {[round(t, 3) for t in single_times]})"
    )


def test_pool_wall_time_at_most_half_of_single() -> None:
    # SC-001: три рівні провайдери (ставка 1 кожен) проти одного такого ж
    single_best, pool_best, single_json, pool_json, served, s_times, p_times = _pool_vs_single(
        weights=(1.0, 1.0, 1.0), rates=(1.0, 1.0, 1.0), single_rate=1.0
    )
    assert '"buyers"' in single_json  # збір не порожній
    assert served >= 2, "пул не розподілив getTransaction між провайдерами"
    assert pool_json == single_json  # результат ідентичний одиночному
    _assert_at_most_half(single_best, pool_best, s_times, p_times, "рівні провайдери")

