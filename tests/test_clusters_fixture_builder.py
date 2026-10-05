# verifies: FR-003-19
"""Тести генератора фікстур `c_*`: склад сценаріїв, детермінізм, digests (T-063, T-064)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from conftest import CLUSTER_FIXTURES

_G = Path(__file__).parent / "fixtures" / "build_cluster_fixtures.py"
_spec = importlib.util.spec_from_file_location("build_cluster_fixtures", _G)
mod = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(mod)


def test_scenario_list_is_exactly_the_14_after_t080() -> None:
    assert sorted(mod.SCENARIOS) == [
        "c_all_one", "c_behavior", "c_below_min", "c_delegated", "c_diamond",
        "c_direct_flagged", "c_empty", "c_giant", "c_incomplete", "c_no_time",
        "c_recovered", "c_shared", "c_single", "c_two_clusters",
    ]


def test_build_twice_is_byte_identical() -> None:
    assert mod.build_all() == mod.build_all()


def test_check_passes_on_committed_fixtures() -> None:
    assert mod.main(["--check"]) == 0


def test_cluster_config_digest_equals_shipped_clusters_yaml() -> None:
    from unmask.hubs.config import content_digest
    shipped = content_digest(Path(__file__).resolve().parents[1] / "config" / "clusters.yaml")
    assert mod._canonical_digest(mod.CLUSTER_CONFIG) == shipped == \
        "c8a0636918c1d08f1af87af88da4117a55c4ecbc4760adabe57e621fa7426b05"


def test_every_expected_has_config_hub_config_result_notes() -> None:
    for name in mod.SCENARIOS:
        expected = json.loads((CLUSTER_FIXTURES / name / "expected.json").read_text(encoding="utf-8"))
        assert set(expected) == {"config", "hub_config", "result", "notes"}
        assert expected["config"] == mod.CLUSTER_CONFIG
        assert expected["hub_config"]["version"] == 3
        assert set(expected["hub_config"]) == {"version", "thresholds", "lists"}
