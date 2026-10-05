# verifies: FR-003-18, FR-003-04, FR-003-10, FR-003-13
"""Тести завантажувача `config/clusters.yaml` v1 (T-059)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from unmask.clusters.config import (
    ClusterConfig,
    EvidenceWeights,
    load_cluster_config,
)
from unmask.hubs.config import ConfigError, content_digest

REPO_ROOT = Path(__file__).resolve().parents[1]
CLUSTERS_YAML = REPO_ROOT / "config" / "clusters.yaml"

SHIPPED_SHA = "c8a0636918c1d08f1af87af88da4117a55c4ecbc4760adabe57e621fa7426b05"


def _shipped_kwargs(**overrides):
    data = yaml.safe_load(CLUSTERS_YAML.read_text(encoding="utf-8"))
    data.pop("version", None)
    data["link_assets"] = tuple(data["link_assets"])
    data["evidence_weights"] = EvidenceWeights(**data["evidence_weights"])
    data["version"] = 1
    data["digest"] = SHIPPED_SHA
    data.update(overrides)
    return data


def test_shipped_config_loads_with_v1_values_and_digest() -> None:
    cfg = load_cluster_config(CLUSTERS_YAML)
    assert cfg.version == 1
    assert cfg.digest == SHIPPED_SHA == content_digest(CLUSTERS_YAML)
    assert cfg.link_min_amount_lamports == 10_000_000
    assert cfg.funding_window_seconds == 3600
    assert cfg.seconds_per_slot == 0.4
    assert cfg.link_through_flagged_buyers is False
    assert cfg.link_assets == ("sol",)
    assert cfg.indirect_enabled is True
    assert cfg.same_amount_natural_max == 3
    assert cfg.same_slot_natural_max == 5
    assert cfg.same_slot_window_slots == 0
    assert cfg.evidence_weights.shared_funder == 0.6
    assert cfg.evidence_weights.indirect_link == 0.3
    assert cfg.slot_fallback_multiplier == 0.8
    assert cfg.artifact_buyer_share == 0.5
    assert cfg.artifact_confidence_multiplier == 0.5
    assert cfg.band_clean_max == 20
    assert cfg.band_suspicious_max == 50


def test_indirect_link_must_be_below_shared_funder() -> None:
    w = dict(shared_funder=0.3, direct_transfer=0.5, delegated_buy=0.6, recovered_edge=0.4,
             indirect_link=0.3, same_amounts=0.2, same_slot=0.15)
    with pytest.raises(ConfigError, match="indirect_link"):
        ClusterConfig(**_shipped_kwargs(evidence_weights=EvidenceWeights(**w)))


def test_band_order_is_enforced() -> None:
    with pytest.raises(ConfigError, match="band_clean_max"):
        ClusterConfig(**_shipped_kwargs(band_clean_max=50, band_suspicious_max=50))


@pytest.mark.parametrize("field", [
    "link_min_amount_lamports", "funding_window_seconds",
    "same_amount_natural_max", "same_slot_natural_max",
    "band_clean_max", "band_suspicious_max", "version",
])
def test_bool_is_not_int(field) -> None:
    with pytest.raises(ConfigError, match=field):
        ClusterConfig(**_shipped_kwargs(**{field: True}))


def test_seconds_per_slot_must_be_positive_number_not_nan() -> None:
    import math
    with pytest.raises(ConfigError, match="seconds_per_slot"):
        ClusterConfig(**_shipped_kwargs(seconds_per_slot=0.0))
    with pytest.raises(ConfigError, match="seconds_per_slot"):
        ClusterConfig(**_shipped_kwargs(seconds_per_slot=math.nan))


@pytest.mark.parametrize("weights_field", [
    "shared_funder", "direct_transfer", "delegated_buy", "recovered_edge",
    "indirect_link", "same_amounts", "same_slot",
])
def test_evidence_weights_in_open_unit_interval(weights_field) -> None:
    base = dict(shared_funder=0.6, direct_transfer=0.5, delegated_buy=0.6, recovered_edge=0.4,
                indirect_link=0.3, same_amounts=0.2, same_slot=0.15)
    for bad in (0.0, 1.0, -0.1, 1.5):
        bad_w = dict(base)
        bad_w[weights_field] = bad
        # keep indirect < shared for this check unless testing those two
        if weights_field not in ("shared_funder", "indirect_link"):
            pass
        else:
            bad_w["shared_funder"] = 0.6
            bad_w["indirect_link"] = 0.3
            if weights_field == "shared_funder":
                bad_w["shared_funder"] = bad
                bad_w["indirect_link"] = 0.1 if bad != 0.1 else 0.05
            else:
                bad_w["indirect_link"] = bad
                bad_w["shared_funder"] = 0.9 if bad != 0.9 else 0.95
        with pytest.raises(ConfigError, match="evidence_weights"):
            ClusterConfig(**_shipped_kwargs(evidence_weights=EvidenceWeights(**bad_w)))


def test_link_assets_rules() -> None:
    with pytest.raises(ConfigError, match="link_assets"):
        ClusterConfig(**_shipped_kwargs(link_assets=()))
    with pytest.raises(ConfigError, match="link_assets"):
        ClusterConfig(**_shipped_kwargs(link_assets=("sol", "sol")))
    with pytest.raises(ConfigError, match="link_assets"):
        ClusterConfig(**_shipped_kwargs(link_assets=("eth",)))


def test_missing_and_unknown_fields_rejected_on_load(tmp_path: Path) -> None:
    data = yaml.safe_load(CLUSTERS_YAML.read_text(encoding="utf-8"))
    missing = dict(data)
    del missing["funding_window_seconds"]
    p1 = tmp_path / "c1.yaml"
    p1.write_text(yaml.dump(missing), encoding="utf-8")
    with pytest.raises(ConfigError, match="funding_window_seconds"):
        load_cluster_config(p1)
    extra = dict(data)
    extra["no_such_key"] = 1
    p2 = tmp_path / "c2.yaml"
    p2.write_text(yaml.dump(extra), encoding="utf-8")
    with pytest.raises(ConfigError, match="no_such_key"):
        load_cluster_config(p2)
