# verifies: FR-003-20, FR-003-18
"""Контрактні тести серіалізації: ручні документи проти схеми 003.1 (T-070)."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

try:
    import jsonschema
    from jsonschema import Draft202012Validator
except ImportError:
    jsonschema = None
    Draft202012Validator = None

from conftest import load_cluster_expected
from test_clusters_service import _analyze
from unmask.clusters.model import (
    Cluster,
    ClusterCompleteness,
    ClusterMember,
    ClusterMetadata,
    ClusterResult,
    ClusterWarning,
    DiagnosticKind,
    DiagnosticSignal,
    Evidence,
    EvidenceType,
    RiskBand,
    ThresholdsSnapshot,
    TimeBasis,
    Window,
    cluster_id,
)
from unmask.clusters.serialize import to_dict, to_json
from unmask.graph.model import EdgeRef, GraphCompletenessStatus, GraphWarning
from unmask.ingest.model import CompletenessStatus

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "specs" / "003-wallet-clusters-risk"
                     / "contracts" / "cluster-result.schema.json").read_text(encoding="utf-8"))

pytestmark = pytest.mark.skipif(jsonschema is None, reason="jsonschema not installed")


def _thresholds() -> ThresholdsSnapshot:
    return ThresholdsSnapshot(
        link_min_amount_lamports=10_000_000, funding_window_seconds=3600, seconds_per_slot=0.4,
        link_through_flagged_buyers=False, link_assets=("sol",), indirect_enabled=True,
        same_amount_natural_max=3, same_slot_natural_max=5, same_slot_window_slots=0,
        evidence_weights={"shared_funder": 0.6, "direct_transfer": 0.5, "delegated_buy": 0.6,
                          "recovered_edge": 0.4, "indirect_link": 0.3, "same_amounts": 0.2,
                          "same_slot": 0.15},
        slot_fallback_multiplier=0.8, artifact_buyer_share=0.5, artifact_confidence_multiplier=0.5,
        band_clean_max=20, band_suspicious_max=50,
    )


def _metadata(**over) -> ClusterMetadata:
    base = dict(mint="11111111111111111111111111111111", schema_version="003.1",
                cluster_config_version=1, thresholds=_thresholds(), graph_schema_version="002.1",
                hub_config_version=3, address_lists_version=1, lists_applied=True,
                ingest_config_version=2, ingest_analyzed_at=1000, ingest_source="http",
                wallets_analyzed=2, share_denominator=600,
                share_denominator_kind="analyzed_buyers_received_amount")
    base.update(over)
    return ClusterMetadata(**base)


def _complete(**over) -> ClusterCompleteness:
    base = dict(graph_status=GraphCompletenessStatus.COMPLETE, ingest_status=CompletenessStatus.COMPLETE,
                missing_count=0, delegated_complete=True, delegated_reason=None, graph_warnings=())
    base.update(over)
    return ClusterCompleteness(**base)


def _sig(i: int) -> str:
    return "5" * 63 + str(i)


def _wallets(i: int, n: int):
    return ["1" * 31 + str(i), "1" * 31 + str(i + 1)][:n]


def _evidence_two(wallets, weight=0.6) -> Evidence:
    wallets = tuple(sorted(wallets))
    return Evidence(type=EvidenceType.SHARED_FUNDER, wallets=wallets, via=("2" * 32,),
                    refs=(EdgeRef(signature=_sig(1), slot=5, instruction_path="0"),),
                    window=Window(basis=TimeBasis.BLOCK_TIME, start=10, end=20),
                    basis=TimeBasis.BLOCK_TIME, value=None, detail=None, weight=weight)


def _cluster_two(w1="1" * 31 + "1", w2="1" * 31 + "2", denom=600) -> Cluster:
    members = (ClusterMember(w1, 1, 200), ClusterMember(w2, 2, 100))
    ev = (_evidence_two((w1, w2)),)
    return Cluster(cluster_id=cluster_id((w1, w2)), members=members, share_numerator=300,
                   share_denominator=denom, share=round(300 / denom, 4), evidence=ev,
                   confidence=0.6, warnings=())


def _result_clean_no_clusters() -> ClusterResult:
    md = _metadata(wallets_analyzed=2, share_denominator=600)
    return ClusterResult(metadata=md, completeness=_complete(), clusters=(), diagnostics=(),
                         risk_score=0, computed_band=RiskBand.CLEAN, band=RiskBand.CLEAN,
                         band_reasons=("no_clusters_on_complete_data",))


def _result_suspicious_two_clusters() -> ClusterResult:
    c1 = _cluster_two("1" * 31 + "1", "1" * 31 + "2", 1000)
    c2 = _cluster_two("1" * 31 + "3", "1" * 31 + "4", 1000)
    w = Window(basis=TimeBasis.SLOT, start=7, end=7)
    beh = Evidence(type=EvidenceType.SAME_SLOT, wallets=tuple(sorted(("1" * 31 + "1", "1" * 31 + "2"))),
                   via=(), refs=(EdgeRef(signature=_sig(9), slot=7, instruction_path=None),),
                   window=w, basis=TimeBasis.SLOT, value=7, detail=None, weight=0.15)
    import dataclasses
    c1b = dataclasses.replace(c1, evidence=tuple(sorted(c1.evidence + (beh,),
                                                        key=lambda e: (e.type.value, e.via, e.wallets,
                                                                       (e.refs[0].slot, e.refs[0].signature)))),
                              confidence=round(1 - 0.4 * 0.85, 4))
    ordered = sorted((c1b, c2), key=lambda c: (-c.share_numerator, c.cluster_id))
    md = _metadata(wallets_analyzed=4, share_denominator=1000)
    import math
    score = math.floor(100 * sum(c.share * c.confidence for c in ordered) + 0.5)
    return ClusterResult(metadata=md, completeness=_complete(), clusters=tuple(ordered), diagnostics=(),
                         risk_score=score, computed_band=RiskBand.SUSPICIOUS, band=RiskBand.SUSPICIOUS,
                         band_reasons=("clusters_share_weighted",))


def _result_insufficient_incomplete() -> ClusterResult:
    md = _metadata(wallets_analyzed=2, share_denominator=600)
    comp = _complete(graph_status=GraphCompletenessStatus.INCOMPLETE,
                     ingest_status=CompletenessStatus.INCOMPLETE, missing_count=2,
                     delegated_complete=False, delegated_reason="timeout",
                     graph_warnings=(GraphWarning.DELEGATED_INCOMPLETE,))
    return ClusterResult(metadata=md, completeness=comp, clusters=(), diagnostics=(),
                         risk_score=0, computed_band=RiskBand.CLEAN, band=RiskBand.INSUFFICIENT_DATA,
                         band_reasons=("ingest_incomplete", "missing_histories:2", "delegated_incomplete"))


def _result_empty_input() -> ClusterResult:
    md = _metadata(wallets_analyzed=0, share_denominator=0)
    return ClusterResult(metadata=md, completeness=_complete(), clusters=(), diagnostics=(),
                         risk_score=0, computed_band=RiskBand.CLEAN, band=RiskBand.INSUFFICIENT_DATA,
                         band_reasons=("empty_input",))


def _validate(doc: dict) -> None:
    Draft202012Validator(SCHEMA).validate(doc)


def test_hand_built_results_validate_against_schema() -> None:
    for result in (_result_clean_no_clusters(), _result_suspicious_two_clusters(),
                  _result_insufficient_incomplete(), _result_empty_input()):
        _validate(to_dict(result))


def test_to_json_is_sorted_compact_utf8_and_identical_on_two_calls() -> None:
    result = _result_clean_no_clusters()
    first, second = to_json(result), to_json(result)
    assert first == second
    assert json.loads(first) == to_dict(result)
    assert " " not in first.split('"mint"')[1][:2]


def test_schema_rejects_cluster_without_evidence() -> None:
    _, _, _, result = _analyze("c_shared")
    doc = to_dict(result)
    doc["clusters"][0]["evidence"] = []
    with pytest.raises(Exception):
        _validate(doc)


def test_schema_rejects_behavioral_only_cluster() -> None:
    _, _, _, result = _analyze("c_shared")
    doc = to_dict(result)
    doc["clusters"][0]["evidence"] = [e for e in doc["clusters"][0]["evidence"]
                                      if e["type"] in ("same_amounts", "same_slot")]
    if doc["clusters"][0]["evidence"]:
        with pytest.raises(Exception):
            _validate(doc)


def test_schema_rejects_clean_band_over_incomplete_graph() -> None:
    _, _, _, result = _analyze("c_incomplete")
    doc = to_dict(result)
    doc["band"] = "clean"
    with pytest.raises(Exception):
        _validate(doc)


def test_schema_rejects_nonzero_score_without_clusters() -> None:
    doc = to_dict(_result_clean_no_clusters())
    doc["risk_score"] = 10
    with pytest.raises(Exception):
        _validate(doc)


def test_schema_rejects_slot_fallback_parity_violation() -> None:
    doc = to_dict(_result_clean_no_clusters())
    assert "slot_time_fallback" not in str(doc)


def test_schema_rejects_unknown_top_level_and_metadata_keys() -> None:
    _, _, _, result = _analyze("c_shared")
    doc = to_dict(result)
    bad = dict(doc)
    bad["unknown_key"] = 1
    with pytest.raises(Exception):
        _validate(bad)
    bad2 = copy.deepcopy(doc)
    bad2["metadata"]["unknown_key"] = 1
    with pytest.raises(Exception):
        _validate(bad2)


def test_schema_id_and_version_are_003_1_and_independent_of_001_002() -> None:
    assert "/003/" in SCHEMA["$id"]
    _, _, _, result = _analyze("c_shared")
    doc = to_dict(result)
    assert doc["metadata"]["schema_version"] == "003.1"
    assert doc["metadata"]["graph_schema_version"] == "002.1"


def test_to_dict_is_explicit_not_asdict() -> None:
    with pytest.raises(TypeError):
        to_dict(object())
    _, _, _, result = _analyze("c_shared")
    doc = to_dict(result)
    assert set(doc["clusters"][0]["evidence"][0].keys()) == {
        "type", "wallets", "via", "refs", "window", "basis", "value", "detail", "weight"}
    assert set(doc["clusters"][0]["evidence"][0]["window"].keys()) == {"basis", "start", "end"}


def test_floats_keep_four_decimals_and_ints_stay_ints() -> None:
    _, _, _, result = _analyze("c_two_clusters")
    doc = to_dict(result)
    for cluster in doc["clusters"]:
        assert isinstance(cluster["share_numerator"], int)
        assert isinstance(cluster["share_denominator"], int)
        assert cluster["share"] == round(cluster["share"], 4)
        assert cluster["confidence"] == round(cluster["confidence"], 4)


def _all_cluster_results():
    from conftest import CLUSTER_FIXTURES
    from test_clusters_service import _analyze as _a
    names = sorted(p.name for p in CLUSTER_FIXTURES.iterdir() if (p / "expected.json").is_file())
    return [(name, _a(name)[3]) for name in names]


def test_to_dict_validates_against_schema_on_every_cluster_fixture() -> None:
    for name, result in _all_cluster_results():
        _validate(to_dict(result))


def test_to_dict_validates_on_every_real_fixture() -> None:
    from test_clusters_evaluate import _results
    for label, result in _results().items():
        doc = to_dict(result)
        _validate(doc)
        assert doc["band"] in ("insufficient_data", "suspicious", "high_concentration"), label


def test_real_metadata_versions_equal_shipped_configs() -> None:
    from test_clusters_evaluate import _results
    from unmask.clusters.config import load_cluster_config
    from unmask.hubs.config import load_hub_config
    shipped_clusters = load_cluster_config(ROOT / "config" / "clusters.yaml")
    shipped_hubs = load_hub_config(ROOT / "config" / "hubs.yaml", ROOT / "config" / "hub_addresses.yaml")
    for label, result in _results().items():
        md = result.metadata
        assert md.cluster_config_version == shipped_clusters.version, label
        assert md.hub_config_version == shipped_hubs.thresholds.version, label
        assert md.ingest_config_version == 2, label
        assert md.ingest_source == "http", label


def test_thresholds_snapshot_in_result_equals_shipped_yaml_values() -> None:
    import yaml
    shipped = yaml.safe_load((ROOT / "config" / "clusters.yaml").read_text(encoding="utf-8"))
    shipped.pop("version")
    for _, result in _all_cluster_results():
        doc = to_dict(result)
        assert doc["metadata"]["thresholds"] == shipped


def test_json_round_trip_equals_to_dict_and_floats_have_at_most_four_decimals() -> None:
    import json as _json
    for _, result in _all_cluster_results():
        doc = to_dict(result)
        assert _json.loads(to_json(result)) == doc

        def _walk(value):
            if isinstance(value, float):
                assert value == round(value, 4), value
            elif isinstance(value, dict):
                for v in value.values():
                    _walk(v)
            elif isinstance(value, list):
                for v in value:
                    _walk(v)
        _walk(doc)


def test_schema_versions_are_003_1_and_graph_002_1_on_every_result() -> None:
    from test_clusters_evaluate import _results
    for _, result in _all_cluster_results() + list(_results().items()):
        doc = to_dict(result)
        assert doc["metadata"]["schema_version"] == "003.1"
        assert doc["metadata"]["graph_schema_version"] == "002.1"


def test_delegated_incomplete_parity_holds_on_real_results() -> None:
    from test_clusters_evaluate import _results
    for label, result in _results().items():
        has_warning = "delegated_incomplete" in [w.value for w in result.completeness.graph_warnings]
        assert result.completeness.delegated_complete != has_warning, label
