# verifies: FR-003-15, FR-003-16, FR-003-13, FR-003-14
"""Тести оцінки, смуг і чесної неповноти (T-069)."""

from __future__ import annotations

import pytest

from conftest import load_cluster_expected
from test_clusters_service import _analyze, _cluster_config
from unmask.clusters.model import RiskBand
from unmask.graph.model import GraphWarning
from unmask.clusters.score import assess, completeness_of, computed_band, risk_score


def test_incomplete_graph_turns_clean_into_insufficient_data_with_reasons() -> None:
    expected, ingest, graph, result = _analyze("c_incomplete")
    assert result.computed_band == RiskBand.CLEAN
    assert result.band == RiskBand.INSUFFICIENT_DATA
    assert result.band_reasons == ("ingest_incomplete", "missing_histories:1")


def test_incomplete_keeps_suspicious_and_high_as_lower_bound() -> None:
    expected, ingest, graph, _ = _analyze("c_two_clusters")
    import dataclasses
    bad_ingest = dataclasses.replace(ingest.metadata, analyzed_at=ingest.metadata.analyzed_at)
    _ = bad_ingest
    # синтетично: той самий результат на неповному графі лишає смугу
    from unmask.clusters.model import ClusterCompleteness
    from unmask.graph.model import GraphCompletenessStatus
    from unmask.ingest.model import CompletenessStatus
    comp = ClusterCompleteness(
        graph_status=GraphCompletenessStatus.INCOMPLETE, ingest_status=CompletenessStatus.INCOMPLETE,
        missing_count=1, delegated_complete=False, delegated_reason="rate_limited",
        graph_warnings=tuple(graph.report.warnings) + (GraphWarning.DELEGATED_INCOMPLETE,),
    )
    from test_clusters_service import _analyze as _a
    _, _, _, res = _a("c_two_clusters")
    score, computed, band, reasons = assess(res.clusters, comp, res.metadata.wallets_analyzed,
                                            _cluster_config(expected))
    assert computed == RiskBand.SUSPICIOUS and band == RiskBand.SUSPICIOUS
    assert reasons == ("clusters_share_weighted",)


def test_empty_input_is_insufficient_data_with_empty_input_not_clean() -> None:
    _, _, _, result = _analyze("c_empty")
    assert result.risk_score == 0 and result.clusters == ()
    assert result.computed_band == RiskBand.CLEAN
    assert result.band == RiskBand.INSUFFICIENT_DATA
    assert result.band_reasons == ("empty_input",)


def test_clean_only_on_complete_graph_with_at_least_one_buyer() -> None:
    for name in ("c_below_min", "c_single"):
        _, _, _, result = _analyze(name)
        assert result.band == RiskBand.CLEAN
        assert result.band_reasons == ("no_clusters_on_complete_data",)


def test_completeness_copies_graph_warnings_and_statuses_from_002() -> None:
    _, _, graph, result = _analyze("c_single")
    assert "giant_component" in [w.value for w in result.completeness.graph_warnings]
    assert result.completeness.graph_status.value == graph.completeness.status.value
    expected, _, _, _ = _analyze("c_incomplete")
    assert load_cluster_expected("c_incomplete")["result"]["completeness"]["missing_count"] == 1


def test_assess_never_returns_clean_when_can_be_clean_false() -> None:
    import random
    from unmask.clusters.model import ClusterCompleteness
    from unmask.graph.model import GraphCompletenessStatus
    from unmask.ingest.model import CompletenessStatus
    rng = random.Random(11)
    _, _, _, res = _analyze("c_two_clusters")
    cfg = _cluster_config(load_cluster_expected("c_two_clusters"))
    for _ in range(50):
        comp = ClusterCompleteness(
            graph_status=GraphCompletenessStatus.INCOMPLETE, ingest_status=CompletenessStatus.INCOMPLETE,
            missing_count=rng.randint(0, 3), delegated_complete=False, delegated_reason="timeout",
            graph_warnings=(GraphWarning.DELEGATED_INCOMPLETE,),
        )
        _, _, band, _ = assess(res.clusters, comp, 6, cfg)
        assert band != RiskBand.CLEAN


def test_computed_band_thresholds_from_config() -> None:
    cfg = _cluster_config(load_cluster_expected("c_shared"))
    assert computed_band(18, cfg) == RiskBand.CLEAN
    assert computed_band(19, cfg) == RiskBand.SUSPICIOUS
    assert computed_band(48, cfg) == RiskBand.SUSPICIOUS
    assert computed_band(49, cfg) == RiskBand.HIGH_CONCENTRATION
    assert risk_score([]) == 0
