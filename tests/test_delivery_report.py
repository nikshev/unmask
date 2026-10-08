# verifies: FR-004-01, FR-004-12
"""Тести документа відповіді: числа слово в слово з 003 + походження (T-089)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

try:
    import jsonschema
    from jsonschema import Draft202012Validator
except ImportError:
    jsonschema = None
    Draft202012Validator = None

from test_clusters_service import _analyze as _analyze_clusters
from unmask.delivery.report import build_report, to_json

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "specs" / "004-api-bot-delivery" / "contracts"
                     / "api-response.schema.json").read_text(encoding="utf-8"))

pytestmark = pytest.mark.skipif(jsonschema is None, reason="jsonschema not installed")


def _triple(name: str):
    from conftest import load_cluster_fixture
    from test_clusters_service import _cluster_config, _hub_config
    from unmask.clusters.service import ClusterService
    from unmask.graph.service import GraphService
    from conftest import load_cluster_expected
    expected = load_cluster_expected(name)
    ingest = load_cluster_fixture(name)
    graph = GraphService(_hub_config(expected)).analyze(ingest)
    result = ClusterService(_cluster_config(expected)).analyze(graph, ingest)
    return ingest, graph, result


def _validate(doc: dict) -> None:
    Draft202012Validator(SCHEMA).validate(doc)


def test_numbers_are_verbatim_from_003_result() -> None:
    for name, exp_risk, exp_band in (("c_two_clusters", 29, "suspicious"),
                                     ("c_incomplete", 10, "insufficient_data")):
        ingest, graph, result = _triple(name)
        doc = build_report(ingest, graph, result)
        assert doc["risk_score"] == exp_risk == result.risk_score
        assert doc["band"] == exp_band == result.band.value
        assert [c["confidence"] for c in doc["clusters"]] == [c.confidence for c in result.clusters]
        assert [c["supply_share"] for c in doc["clusters"]] == [c.share for c in result.clusters]
        assert [c["wallets"] for c in doc["clusters"]] == [
            [m.wallet for m in c.members] for c in result.clusters]


def test_to_dict_validates_against_api_response_schema() -> None:
    from test_clusters_service import _analyze as _a
    for name in ("c_two_clusters", "c_below_min", "c_incomplete", "c_empty"):
        _, _, _, result = _a(name)
        from conftest import load_cluster_fixture
        from test_clusters_service import _hub_config, _cluster_config
        from unmask.graph.service import GraphService
        from conftest import load_cluster_expected
        expected = load_cluster_expected(name)
        ingest = load_cluster_fixture(name)
        graph = GraphService(_hub_config(expected)).analyze(ingest)
        _validate(build_report(ingest, graph, result))


def test_insufficient_data_never_becomes_clean() -> None:
    ingest, graph, result = _triple("c_incomplete")
    doc = build_report(ingest, graph, result)
    assert doc["band"] == "insufficient_data"
    assert result.band.value == "insufficient_data"


def test_provenance_carries_all_four_versions_and_completeness() -> None:
    ingest, graph, result = _triple("c_two_clusters")
    prov = build_report(ingest, graph, result)["provenance"]
    assert prov["ingest_config_version"] == 2
    assert prov["hub_config_version"] == 3
    assert prov["cluster_config_version"] == 2
    assert prov["graph_status"] == "complete"
    assert prov["completeness_reasons"] == ["complete_data"]


def test_schema_rejects_cluster_without_evidence() -> None:
    ingest, graph, result = _triple("c_two_clusters")
    doc = build_report(ingest, graph, result)
    doc["clusters"][0]["evidence"] = []
    with pytest.raises(Exception):
        _validate(doc)


def test_to_json_is_sorted_compact_and_stable() -> None:
    ingest, graph, result = _triple("c_shared")
    first, second = to_json(build_report(ingest, graph, result)), None
    second = to_json(build_report(ingest, graph, result))
    assert first == second
    assert json.loads(first)["risk_score"] == 10
    with pytest.raises(TypeError):
        to_json(object())


def test_wrong_input_types_raise_type_error() -> None:
    ingest, graph, result = _triple("c_single")
    with pytest.raises(TypeError):
        build_report(object(), graph, result)
    with pytest.raises(TypeError):
        build_report(ingest, object(), result)
    with pytest.raises(TypeError):
        build_report(ingest, graph, object())


def test_coordination_category_follows_003_thresholds_not_hardcoded_bands() -> None:
    """Синхронізація з clusters.yaml: категорія йде за band_*_max зі знімка
    порогів результату 003, а не за хардкодом 50/20 (калібрування 005)."""
    import dataclasses

    from conftest import load_cluster_expected, load_cluster_fixture
    from test_clusters_service import _cluster_config, _hub_config
    from unmask.clusters.service import ClusterService
    from unmask.graph.service import GraphService

    expected = load_cluster_expected("c_two_clusters")
    ingest = load_cluster_fixture("c_two_clusters")
    graph = GraphService(_hub_config(expected)).analyze(ingest)

    def _category_with(clean_max: int, susp_max: int) -> str:
        cfg = dataclasses.replace(_cluster_config(expected),
                                  band_clean_max=clean_max,
                                  band_suspicious_max=susp_max)
        result = ClusterService(cfg).analyze(graph, ingest)
        assert result.risk_score == 29  # пороги не чіпають risk_score
        return build_report(ingest, graph, result)["coordination_category"]

    # risk=29: між каліброваними смугами (18, 48] → moderate
    assert _category_with(18, 48) == "moderate"
    # ті самі дані, ширша «чиста» смуга (30, 60]: 29 — не підозрілий → weak
    assert _category_with(30, 60) == "weak"
    # вужчі смуги (10, 20]: 29 — висока концентрація → strong
    assert _category_with(10, 20) == "strong"
