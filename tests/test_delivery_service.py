# verifies: FR-004-02, FR-004-07
"""Тести живого конвеєра доставки на записаних результатах (T-090).

Дубль стоїть на межі `IngestService.collect` і віддає записані `IngestResult`
(`tests/fixtures/real/*.json`) з лічильником викликів; граф і кластери рахують
справжні `GraphService`/`ClusterService` зі справжніми конфігами. Програвання
сирого RPC належить тестам 001 (`FixtureRpcSource`).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from unmask.clusters.config import load_cluster_config
from unmask.clusters.service import ClusterService
from unmask.delivery.cache import DeliveryCache
from unmask.delivery.service import DeliveryService
from unmask.graph.service import GraphService
from unmask.hubs.config import load_hub_config
from unmask.ingest.model import RejectKind, Rejection
from unmask.ingest.serialize import from_dict

ROOT = Path(__file__).resolve().parents[1]
REAL = ROOT / "tests" / "fixtures" / "real"


class RecordedIngest:
    """Дубль збору: `mint → IngestResult` із записаних файлів + лічильник."""

    def __init__(self, mapping: dict[str, Path]) -> None:
        self._mapping = mapping
        self.calls: list[str] = []

    def collect(self, mint: str):
        self.calls.append(mint)
        data = json.loads(self._mapping[mint].read_text(encoding="utf-8"))
        return from_dict(data)


def _real_services():
    hub = load_hub_config(ROOT / "config" / "hubs.yaml", ROOT / "config" / "hub_addresses.yaml")
    clusters = load_cluster_config(ROOT / "config" / "clusters.yaml")
    return GraphService(hub), ClusterService(clusters)


def _mint_of(label: str) -> tuple[str, Path]:
    import yaml
    manifest = yaml.safe_load((REAL / "manifest.yaml").read_text(encoding="utf-8"))
    token = next(t for t in manifest["tokens"] if t["label"] == label)
    return token["mint"], REAL / token["file"]


def _service(files: dict[str, Path]) -> tuple[DeliveryService, RecordedIngest]:
    ingest = RecordedIngest(files)
    graph, clusters = _real_services()
    return DeliveryService(ingest, graph, clusters, DeliveryCache()), ingest


def test_live_pipeline_uses_same_configs_and_versions() -> None:
    mint, path = _mint_of("ins4")
    service, _ = _service({mint: path})
    doc = service.analyze(mint)
    assert doc["mint"] == mint and doc["risk_score"] == 54
    assert doc["provenance"]["hub_config_version"] == 3
    assert doc["provenance"]["cluster_config_version"] == 1
    assert doc["provenance"]["ingest_config_version"] == 2


def test_rejection_maps_to_error_document_not_exception() -> None:
    class Rejecting:
        def collect(self, mint: str):
            return Rejection(kind=RejectKind.INVALID_ADDRESS, mint=mint, detail="bad base58")

    graph, clusters = _real_services()
    service = DeliveryService(Rejecting(), graph, clusters, DeliveryCache())
    doc = service.analyze("!!!")
    assert doc == {"mint": "!!!", "error": {"kind": "invalid_address", "detail": "bad base58"}}


def test_incomplete_collect_maps_to_insufficient_data_with_reasons() -> None:
    mint, path = _mint_of("cln1")
    service, _ = _service({mint: path})
    doc = service.analyze(mint)
    assert doc["band"] == "insufficient_data"
    assert "delegated_incomplete" in doc["band_reasons"]


def test_second_call_hits_cache_without_source_calls() -> None:
    mint, path = _mint_of("ins1")
    service, ingest = _service({mint: path})
    first = service.analyze(mint)
    second = service.analyze(mint)
    assert first == second
    assert ingest.calls == [mint]


def test_missing_env_key_fails_fast_with_var_name() -> None:
    class NoKey:
        def collect(self, mint: str):
            raise RuntimeError("missing UNMASK_RPC_URL")

    graph, clusters = _real_services()
    service = DeliveryService(NoKey(), graph, clusters, DeliveryCache())
    with pytest.raises(RuntimeError, match="UNMASK_RPC_URL"):
        service.analyze("11111111111111111111111111111111")
