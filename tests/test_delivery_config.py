# verifies: FR-004-11
"""Тести версіонованого `config/delivery.yaml` v1 і журналу (T-088)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from unmask.delivery.config import load_delivery_config
from unmask.hubs.config import ConfigError, changelog_entries, check_changelog, content_digest

REPO_ROOT = Path(__file__).resolve().parents[1]
CHANGELOG = REPO_ROOT / "config" / "CHANGELOG.md"
DELIVERY_YAML = REPO_ROOT / "config" / "delivery.yaml"

KEYS = ["http_port", "png_width", "png_height", "cluster_palette",
        "address_prefix_len", "evidence_preview_limit"]


def test_shipped_delivery_yaml_passes_check_changelog() -> None:
    check_changelog(DELIVERY_YAML, CHANGELOG)


def test_entry_sha_equals_content_digest() -> None:
    entries = changelog_entries(CHANGELOG, "delivery.yaml")
    assert entries == {1: content_digest(DELIVERY_YAML)}


def test_delivery_section_sits_before_ingest_and_ingest_is_last() -> None:
    lines = CHANGELOG.read_text(encoding="utf-8").splitlines()
    headings = [ln for ln in lines if ln.startswith("# ")]
    assert headings.index("# config/delivery.yaml") < headings.index("# config/ingest.yaml")
    assert headings[-1] == "# config/ingest.yaml"


def test_shipped_values_load() -> None:
    cfg = load_delivery_config(DELIVERY_YAML)
    assert (cfg.version, cfg.http_port, cfg.png_width, cfg.png_height) == (1, 8080, 1200, 800)
    assert len(cfg.cluster_palette) == 8 and cfg.address_prefix_len == 6
    assert cfg.evidence_preview_limit == 4000


@pytest.mark.parametrize("key", KEYS)
def test_any_value_change_without_entry_is_detected(tmp_path: Path, key: str) -> None:
    import shutil
    data = yaml.safe_load(DELIVERY_YAML.read_text(encoding="utf-8"))
    if isinstance(data[key], list):
        data[key] = data[key][:-1]
    elif isinstance(data[key], bool):
        pytest.skip("no bool keys")
    elif isinstance(data[key], int):
        data[key] += 1
    else:
        data[key] = "changed"
    changed = tmp_path / "delivery.yaml"
    changed.write_text(yaml.dump(data), encoding="utf-8")
    journal = tmp_path / "CHANGELOG.md"
    shutil.copyfile(CHANGELOG, journal)
    with pytest.raises(ConfigError, match=r"delivery\.yaml.*content digest does not match"):
        check_changelog(changed, journal)


def test_unknown_field_rejected_on_load(tmp_path: Path) -> None:
    data = yaml.safe_load(DELIVERY_YAML.read_text(encoding="utf-8"))
    data["no_such_key"] = 1
    path = tmp_path / "delivery.yaml"
    path.write_text(yaml.dump(data), encoding="utf-8")
    with pytest.raises(ConfigError, match="no_such_key"):
        load_delivery_config(path)


def test_bad_values_rejected() -> None:
    from unmask.delivery.config import DeliveryConfig
    good = dict(version=1, http_port=8080, png_width=1200, png_height=800,
                cluster_palette=("#ffffff",), address_prefix_len=6,
                evidence_preview_limit=4000, digest="fixture")
    for bad_key, bad_val in [("http_port", 0), ("http_port", 99999), ("png_width", 0),
                             ("cluster_palette", ()), ("address_prefix_len", 0),
                             ("evidence_preview_limit", True)]:
        bad = dict(good)
        bad[bad_key] = bad_val
        with pytest.raises(ConfigError, match=bad_key):
            DeliveryConfig(**bad)
    bad = dict(good, cluster_palette=("#gggggg",))
    with pytest.raises(ConfigError, match="cluster_palette"):
        DeliveryConfig(**bad)


def test_pillow_importable_and_version_pinned_in_lockfile() -> None:
    import PIL
    from PIL import Image
    assert PIL.__version__
    lock = (REPO_ROOT / "uv.lock").read_text(encoding="utf-8")
    assert re.search(r'name = "pillow"', lock)
    assert Image.new("RGB", (8, 8)) is not None
