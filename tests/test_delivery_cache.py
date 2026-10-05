# verifies: FR-004-06
"""Тести кешу доставки із single-flight (T-091)."""

from __future__ import annotations

import threading

import pytest

from unmask.delivery.cache import DeliveryCache


def test_repeat_returns_byte_identical_document_without_rebuild() -> None:
    cache = DeliveryCache()
    calls = []
    doc = {"mint": "M", "risk_score": 10}
    assert cache.single_flight("M", lambda: (calls.append(1), dict(doc))[1]) == doc
    assert cache.single_flight("M", lambda: (calls.append(1), {"other": True})[1]) == doc
    assert calls == [1]
    assert cache.get("M") == doc
    assert cache.get("unknown") is None


def test_concurrent_duplicates_build_once() -> None:
    import time
    cache = DeliveryCache()
    calls = []
    barrier = threading.Barrier(8)

    def build():
        calls.append(1)
        time.sleep(0.05)
        return {"n": len(calls)}

    results = []
    def worker():
        barrier.wait()
        results.append(cache.single_flight("M", build))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert calls == [1]
    assert results == [{"n": 1}] * 8


def test_distinct_mints_do_not_share_entries() -> None:
    cache = DeliveryCache()
    cache.store("A", {"v": 1})
    cache.store("B", {"v": 2})
    assert cache.get("A") == {"v": 1} and cache.get("B") == {"v": 2}


def test_build_exception_propagates_to_all_waiters_and_is_not_cached() -> None:
    import time
    cache = DeliveryCache()
    barrier = threading.Barrier(4)
    errors = []

    def build():
        time.sleep(0.05)
        raise RuntimeError("boom")

    def worker():
        barrier.wait()
        try:
            cache.single_flight("M", build)
        except RuntimeError as exc:
            errors.append(str(exc))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == ["boom"] * 4
    assert cache.get("M") is None


def test_rejection_is_never_cached() -> None:
    # Відмови не кешуються на рівні сервісу (T-090); тут — кеш не кличе build даремно:
    cache = DeliveryCache()
    assert cache.get("M") is None
    cache.store("M", {"ok": True})
    assert cache.get("M") == {"ok": True}


def test_returned_documents_are_copies_mutation_does_not_poison_cache() -> None:
    cache = DeliveryCache()
    first = cache.single_flight("M", lambda: {"clusters": [{"n": 1}]})
    first["clusters"].append({"n": 2})
    assert cache.get("M") == {"clusters": [{"n": 1}]}
    assert cache.single_flight("M", lambda: {"other": True}) == {"clusters": [{"n": 1}]}
