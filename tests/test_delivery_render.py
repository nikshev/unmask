# verifies: FR-004-05
"""Тести PNG-рендера: розрізненість кластерів, легенда, заглушка, детермінізм (T-093)."""

from __future__ import annotations

import ast
import io
from pathlib import Path

import pytest
from PIL import Image

from test_delivery_service import _mint_of, _real_services
from test_delivery_service import RecordedIngest
from unmask.delivery.cache import DeliveryCache
from unmask.delivery.config import load_delivery_config
from unmask.delivery.render import GraphImage, render_png
from unmask.delivery.report import build_report
from unmask.delivery.service import DeliveryService
from unmask.clusters.service import ClusterService
from unmask.graph.service import GraphService
from unmask.ingest.serialize import from_dict
import json

ROOT = Path(__file__).resolve().parents[1]
REAL = ROOT / "tests" / "fixtures" / "real"


def _cfg():
    return load_delivery_config(ROOT / "config" / "delivery.yaml")


def _doc(label: str) -> dict:
    import yaml
    manifest = yaml.safe_load((REAL / "manifest.yaml").read_text(encoding="utf-8"))
    token = next(t for t in manifest["tokens"] if t["label"] == label)
    ingest = from_dict(json.loads((REAL / token["file"]).read_text(encoding="utf-8")))
    graph, clusters = _real_services()
    return build_report(ingest, graph.analyze(ingest), clusters.analyze(graph.analyze(ingest), ingest))


def _open(img: GraphImage) -> Image.Image:
    opened = Image.open(io.BytesIO(img.png))
    opened.load()
    return opened


def test_two_clusters_are_visually_distinct_with_legend() -> None:
    doc = _doc("cln4")  # 3 кластери
    assert len(doc["clusters"]) >= 2
    img = render_png(doc, _cfg())
    opened = _open(img)
    assert opened.size == (1200, 800)
    assert img.empty is False and img.clusters_shown == len(doc["clusters"])
    colors = {(r, g, b) for (r, g, b) in opened.get_flattened_data()
              if (r, g, b) != (255, 255, 255) and (r, g, b) != (0, 0, 0)}
    palette = {"#e74c3c", "#3498db", "#2ecc71"}
    found = sum(1 for c in palette
                if any(abs(r - int(c[1:3], 16)) < 8 and abs(g - int(c[3:5], 16)) < 8
                       and abs(b - int(c[5:7], 16)) < 8 for r, g, b in colors))
    assert found >= 2
    legend = opened.crop((0, 0, 1200, 110))
    assert any(px != (255, 255, 255) for px in legend.get_flattened_data())


def test_empty_result_gives_captioned_placeholder() -> None:
    doc = _doc("cln1")  # 0 кластерів
    assert doc["clusters"] == []
    img = render_png(doc, _cfg())
    assert img.empty is True and img.clusters_shown == 0
    opened = _open(img)
    assert opened.size == (1200, 800)
    assert any(px != (255, 255, 255) for px in opened.get_flattened_data())


def test_render_is_deterministic_for_same_document() -> None:
    doc = _doc("ins4")
    assert render_png(doc, _cfg()).png == render_png(doc, _cfg()).png


def test_no_result_affecting_constants_in_code() -> None:
    from dataclasses import replace
    doc = _doc("cln4")
    cfg = _cfg()
    base = render_png(doc, cfg).png
    other_palette = ("#111111",) * 8
    cfg2 = replace(cfg, cluster_palette=other_palette)
    assert render_png(doc, cfg2).png != base  # кольори — з конфігу, не зашиті
    cfg3 = replace(cfg, png_width=600, png_height=400)
    small = render_png(doc, cfg3)
    assert (small.width, small.height) == (600, 400)
    assert small.png != base  # розміри — з конфігу, не зашиті


def test_long_addresses_are_truncated_with_config_lengths() -> None:
    from unmask.delivery import render as render_module
    assert render_module._short("A" * 44, 6) == "AAAAAA..."
    assert render_module._short("ABC", 6) == "ABC"
    with pytest.raises(TypeError):
        render_png(object(), _cfg())


def _synthetic_doc(n_clusters: int, per_cluster: int = 3) -> dict:
    clusters = []
    for i in range(n_clusters):
        wallets = [f"{i + 1}" * 31 + str(j) for j in range(per_cluster)]
        clusters.append({
            "cluster_id": f"c-{i:016d}", "wallets": wallets, "supply_share": round(0.5 / (i + 1), 4),
            "supply_share_denominator": "analyzed_buyers_received_amount", "confidence": 0.6,
            "evidence": [{"type": "shared_funder", "source": sorted(set(wallets[:2] + [f"S{i}" + "2" * 31])),
                          "window": {"basis": "block_time", "start": 1, "end": 2}}],
        })
    return {"mint": "1" * 32, "analyzed_at": 1, "wallets_analyzed": n_clusters * per_cluster,
            "clusters": clusters, "risk_score": 10, "band": "suspicious",
            "band_reasons": ["clusters_share_weighted"],
            "provenance": {"ingest_config_version": 2, "hub_config_version": 3,
                           "address_lists_version": 1, "cluster_config_version": 1,
                           "analyzed_at": 1, "graph_status": "complete",
                           "completeness_reasons": ["complete_data"]},
            "error": None}


def test_empty_caption_carries_risk_and_band_without_ocr() -> None:
    from unmask.delivery.render import empty_caption
    doc = _doc("cln1")
    lines = empty_caption(doc)
    assert any("insufficient_data" in line for line in lines)
    assert any(str(doc["risk_score"]) in line for line in lines)
    assert not any(line.strip() == "clean" for line in lines)


def test_legend_wraps_and_nodes_report_overflow_honestly() -> None:
    from dataclasses import replace
    doc = _synthetic_doc(6, 2)
    img = render_png(doc, _cfg())
    assert img.clusters_shown == 6
    assert img.wallets_shown <= img.wallets_total
    tiny = replace(_cfg(), png_width=300, png_height=200)
    small = render_png(doc, tiny)
    assert small.wallets_shown < small.wallets_total
    assert small.wallets_shown >= 0


def test_ninth_cluster_color_differs_from_first() -> None:
    from unmask.delivery.render import _extended_palette
    palette = [(231, 76, 60), (52, 152, 219)]
    assert _extended_palette(palette, 0) == (231, 76, 60)
    assert _extended_palette(palette, 8) != _extended_palette(palette, 0)
    assert _extended_palette(palette, 8) == _extended_palette(palette, 8)


def test_funder_sources_come_from_evidence_source_not_via() -> None:
    doc = _synthetic_doc(1, 2)
    img = render_png(doc, _cfg())
    members = len(doc["clusters"][0]["wallets"])
    assert img.wallets_total == members + 1  # +1 funder from evidence source
