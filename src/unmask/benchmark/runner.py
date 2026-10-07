# impl: FR-005-01, FR-005-05
"""Benchmark runner: прогін конвеєра 001→002→003 на ground truth токенах."""

from __future__ import annotations

import dataclasses
import json
import time
from pathlib import Path
from typing import Any

from unmask.benchmark.ground_truth import GroundTruthRecord
from unmask.delivery.service import DeliveryService
from unmask.graph.service import GraphService
from unmask.clusters.service import ClusterService
from unmask.clusters.config import load_cluster_config
from unmask.hubs.config import load_hub_config
from unmask.ingest.config import load_config as load_ingest_config
from unmask.ingest.rpc.http import HttpRpcSource
from unmask.ingest.serialize import to_dict, from_dict
from unmask.ingest.model import IngestResult
from unmask.delivery.cache import DeliveryCache

__all__ = ["Prediction", "run_benchmark"]


@dataclasses.dataclass(frozen=True)
class Prediction:
    """Результат прогнозу для одного токена."""
    mint: str
    pred_has_cluster: bool
    risk_score: int
    band: str
    clusters_count: int
    max_share: float
    coordination_category: str
    elapsed_s: float
    rpc_calls: int
    error: str | None = None


class _RecordedIngest:
    """Фікстурний IngestService для офлайн бенчмарку."""

    def __init__(self, fixtures_dir: Path) -> None:
        self._fixtures_dir = fixtures_dir
        import yaml
        manifest = yaml.safe_load((fixtures_dir / "manifest.yaml").read_text(encoding="utf-8"))
        self._files = {}
        for token in manifest["tokens"]:
            self._files[token["mint"]] = fixtures_dir / token["file"]

    def collect(self, mint: str) -> IngestResult:
        path = self._files.get(mint)
        if path is None:
            raise ValueError(f"no fixture for mint {mint}")
        raw = json.loads(path.read_text(encoding="utf-8"))
        return from_dict(raw)


def _build_services(
    fixtures_dir: Path,
    *,
    live: bool,
    rpc_source: Any = None,
) -> tuple[Any, Any, Any]:
    """Створити сервіси для бенчмарку."""
    root = Path(__file__).resolve().parents[3]
    ingest_cfg = load_ingest_config(root / "config" / "ingest.yaml")
    hub_cfg = load_hub_config(root / "config" / "hubs.yaml", root / "config" / "hub_addresses.yaml")
    cluster_cfg = load_cluster_config(root / "config" / "clusters.yaml")

    graph_svc = GraphService(hub_cfg)
    cluster_svc = ClusterService(cluster_cfg)

    if live:
        if rpc_source is None:
            raise ValueError("live=True requires rpc_source")
        from unmask.ingest.service import IngestService
        ingest_svc = IngestService(ingest_cfg, rpc_source)
    else:
        ingest_svc = _RecordedIngest(fixtures_dir)

    delivery_svc = DeliveryService(
        ingest_svc,
        graph_svc,
        cluster_svc,
        DeliveryCache(),
    )
    return delivery_svc, graph_svc, cluster_svc


def run_benchmark(
    ground_truth: list,
    fixtures_dir: Path,
    *,
    live: bool = False,
    rpc_source: Any = None,
) -> list:
    """Прогнати бенчмарк на ground truth токенах.

    Args:
        ground_truth: список GroundTruthRecord або dict.
        fixtures_dir: директорія з фікстурами (для offline).
        live: якщо True — живий збір через RPC.
        rpc_source: HttpRpcSource для live режиму.

    Returns:
        Список Prediction для кожного токена.
    """
    delivery_svc, graph_svc, cluster_svc = _build_services(
        fixtures_dir, live=live, rpc_source=rpc_source
    )

    predictions: list = []

    for record in ground_truth:
        if hasattr(record, "mint"):
            mint = record.mint
            gt_class = record.class_
        else:
            mint = record["mint"]
            gt_class = record["class_"]

        start = time.time()
        error: str | None = None
        try:
            doc = delivery_svc.analyze(mint)
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"
            doc = {"error": error}

        elapsed = time.time() - start

        if error or doc.get("error"):
            pred = Prediction(
                mint=mint,
                pred_has_cluster=False,
                risk_score=0,
                band="insufficient_data",
                clusters_count=0,
                max_share=0.0,
                coordination_category="none",
                elapsed_s=round(elapsed, 2),
                rpc_calls=0,
                error=error or doc.get("error"),
            )
        else:
            clusters = doc.get("clusters", [])
            pred = Prediction(
                mint=mint,
                pred_has_cluster=len(clusters) > 0,
                risk_score=doc.get("risk_score", 0),
                band=doc.get("band", "insufficient_data"),
                clusters_count=len(clusters),
                max_share=round(clusters[0]["supply_share"], 4) if clusters else 0.0,
                coordination_category=doc.get("coordination_category", "none"),
                elapsed_s=round(elapsed, 2),
                rpc_calls=0,  # TODO: extract from provenance/ingest metadata
            )
        predictions.append(pred)

    return predictions