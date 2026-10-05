# verifies: FR-003-22, FR-003-23
"""Тести оцінювальної команди й контрольної точки (T-073, T-074)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from unmask.clusters.evaluate import evaluate, main
from unmask.hubs.config import content_digest

REAL = Path(__file__).parent / "fixtures" / "real"
ROOT = Path(__file__).resolve().parents[1]
CALIBRATION = ROOT / "specs" / "003-wallet-clusters-risk" / "calibration.md"


def _evaluate() -> str:
    return evaluate(REAL, clusters_config=ROOT / "config" / "clusters.yaml",
                    hubs_config=ROOT / "config" / "hubs.yaml",
                    lists_config=ROOT / "config" / "hub_addresses.yaml")


def _results() -> dict:
    from unmask.ingest.serialize import from_dict
    from unmask.graph.service import GraphService
    from unmask.hubs.config import load_hub_config
    from unmask.clusters.config import load_cluster_config
    from unmask.clusters.service import ClusterService
    hub = load_hub_config(ROOT / "config" / "hubs.yaml", ROOT / "config" / "hub_addresses.yaml")
    cc = load_cluster_config(ROOT / "config" / "clusters.yaml")
    manifest = yaml.safe_load((REAL / "manifest.yaml").read_text(encoding="utf-8"))
    out = {}
    for token in manifest["tokens"]:
        ingest = from_dict(json.loads((REAL / token["file"]).read_text(encoding="utf-8")))
        graph = GraphService(hub).analyze(ingest)
        out[token["label"]] = ClusterService(cc).analyze(graph, ingest)
    return out


def test_expected_table_matches_evaluate_output_byte_for_byte() -> None:
    assert (REAL / "expected_table.md").read_text(encoding="utf-8") == _evaluate()


def test_evaluate_check_returns_zero() -> None:
    assert main(["--fixtures", str(REAL), "--check"]) == 0


def test_expected_table_header_digest_equals_shipped_clusters_yaml_digest_and_hubs_v3() -> None:
    header = (REAL / "expected_table.md").read_text(encoding="utf-8").splitlines()[2]
    digest = content_digest(ROOT / "config" / "clusters.yaml")
    assert f"clusters.yaml v1 sha256 {digest}" in header
    assert digest == "c8a0636918c1d08f1af87af88da4117a55c4ecbc4760adabe57e621fa7426b05"
    assert "hubs.yaml v3" in header


def test_expected_table_differs_on_other_config(tmp_path: Path) -> None:
    import shutil
    alt = tmp_path / "clusters.yaml"
    shutil.copyfile(ROOT / "config" / "clusters.yaml", alt)
    text = alt.read_text(encoding="utf-8").replace("band_clean_max: 20", "band_clean_max: 30")
    alt.write_text(text, encoding="utf-8")
    other = evaluate(REAL, clusters_config=alt, hubs_config=ROOT / "config" / "hubs.yaml",
                     lists_config=ROOT / "config" / "hub_addresses.yaml")
    assert other.encode() != (REAL / "expected_table.md").read_bytes()


def test_checkpoint_ins4_has_cluster_funded_by_DhLPHfDo_with_at_least_ten_wallets_and_high_concentration() -> None:
    results = _results()
    (cluster,) = results["ins4"].clusters
    assert len(cluster.members) >= 10
    assert any(e.type.value == "shared_funder"
               and e.via == ("DhLPHfDofyck4MQ55hAwv18YmSpemtEpDkg9V1RRGX74",)
               for e in cluster.evidence)
    assert results["ins4"].risk_score > 50
    assert results["ins4"].band.value == "high_concentration"


def test_checkpoint_ins0_and_ins1_have_shared_funder_clusters() -> None:
    results = _results()
    c0 = results["ins0"].clusters[0]
    assert len(c0.members) >= 6
    assert any(e.type.value == "shared_funder"
               and e.via == ("DhLPHfDofyck4MQ55hAwv18YmSpemtEpDkg9V1RRGX74",) for e in c0.evidence)
    assert results["ins0"].risk_score > 20
    c1 = results["ins1"].clusters[0]
    assert len(c1.members) >= 10
    assert any(e.type.value == "shared_funder"
               and e.via == ("DKrPigauQDPbkBFdfkJAY2Pgzod3HxTwqjqxdbNuGscX",) for e in c1.evidence)
    assert results["ins1"].risk_score > 20


def test_checkpoint_cln1_has_no_clusters_and_risk_0() -> None:
    results = _results()
    assert results["cln1"].clusters == ()
    assert results["cln1"].risk_score == 0


def test_all_nine_are_incomplete_delegated_not_analyzed_and_none_shows_clean_band() -> None:
    results = _results()
    assert len(results) == 9
    for label, result in results.items():
        assert result.band.value != "clean", label
        assert result.completeness.delegated_complete is False, label
    assert "clean_band_shown 0/4" in (REAL / "expected_table.md").read_text(encoding="utf-8")


def test_calibration_md_journal_cites_current_digest_and_versions() -> None:
    text = CALIBRATION.read_text(encoding="utf-8")
    digest = content_digest(ROOT / "config" / "clusters.yaml")
    assert digest in text
    assert "hubs.yaml v3" in text
    assert "clusters.yaml v1" in text


def test_calibration_md_lists_every_r12_variant_with_nine_measured_scores() -> None:
    import re
    text = CALIBRATION.read_text(encoding="utf-8")
    rows = re.findall(r"^\| (?:v1|без проходу 2|funding_window_seconds 600|link_min_amount_lamports 100000000|"
                      r"link_through_flagged_buyers true|evidence_weights\.same_amounts 0\.1)"
                      r"((?: \| \d+){9})", text, re.MULTILINE)
    assert len(rows) == 6, rows
    for row in rows:
        assert len(re.findall(r"\| \d+", row)) == 9
