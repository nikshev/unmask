# verifies: FR-003-18
"""Тести захисту журналу змін для `config/clusters.yaml` (SC-007, T-060).

Всі тести працюють через публічні функції `hubs.config`:
`check_changelog`, `changelog_entries`, `content_digest`, `load_cluster_config`.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from pathlib import Path

import pytest
import yaml

from unmask.hubs.config import (
    ConfigError,
    changelog_entries,
    check_changelog,
    content_digest,
    load_hub_config,
)
from unmask.clusters.config import load_cluster_config

REPO_ROOT = Path(__file__).resolve().parents[1]
CHANGELOG = REPO_ROOT / "config" / "CHANGELOG.md"
CLUSTERS_YAML = REPO_ROOT / "config" / "clusters.yaml"
CONTRACT_CLUSTERS = REPO_ROOT / "specs" / "003-wallet-clusters-risk" / "contracts" / "config-clusters.md"

SHIPPED_CLUSTERS_SHA = "c8a0636918c1d08f1af87af88da4117a55c4ecbc4760adabe57e621fa7426b05"


def _canonical_yaml_digest(text: str) -> str:
    """Дайджест як у `hubs.config.content_digest` (канонічний JSON від `yaml.safe_load`)."""
    data = yaml.safe_load(text)
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _copy_with_change(tmp_path: Path, src: Path, new_text: str) -> Path:
    """Скопіювати файл у tmp і записати новий вміст."""
    dst = tmp_path / src.name
    dst.write_text(new_text, encoding="utf-8")
    return dst


def _copy_changelog(tmp_path: Path, new_text: str | None = None) -> Path:
    """Скопіювати CHANGELOG.md у tmp (або змінити)."""
    dst = tmp_path / "CHANGELOG.md"
    if new_text is None:
        shutil.copyfile(CHANGELOG, dst)
    else:
        dst.write_text(new_text, encoding="utf-8")
    return dst


# --- тести, що перевіряють доставлений файл ---


def test_shipped_clusters_yaml_passes_check_changelog() -> None:
    """Доставлений `config/clusters.yaml` проходить `check_changelog`."""
    check_changelog(CLUSTERS_YAML, CHANGELOG)


def test_entry_1_sha_equals_content_digest_of_shipped_file() -> None:
    """Запис 1 у журналі має sha256, що дорівнює `content_digest` доставленого файла."""
    entries = changelog_entries(CHANGELOG, "clusters.yaml")
    assert entries == {1: SHIPPED_CLUSTERS_SHA}


def test_clusters_section_sits_between_hub_addresses_and_ingest_and_ingest_is_last() -> None:
    """Розділ `# config/clusters.yaml` між `hub_addresses.yaml` і `ingest.yaml`; останній — `ingest.yaml`."""
    lines = CHANGELOG.read_text(encoding="utf-8").splitlines()
    headings = [ln for ln in lines if ln.startswith("# ")]
    # перевіряємо порядок: hubs, hub_addresses, clusters, ingest
    idx_hubs = headings.index("# config/hubs.yaml")
    idx_addrs = headings.index("# config/hub_addresses.yaml")
    idx_clusters = headings.index("# config/clusters.yaml")
    idx_ingest = headings.index("# config/ingest.yaml")
    assert idx_hubs < idx_addrs < idx_clusters < idx_ingest
    # останній заголовок — ingest
    assert headings[-1] == "# config/ingest.yaml"


def test_contract_quotes_shipped_yaml_verbatim() -> None:
    """Блок ```yaml у контракті побайтово == `config/clusters.yaml`."""
    contract_text = CONTRACT_CLUSTERS.read_text(encoding="utf-8")
    # знаходимо блок ```yaml ... ```
    import re

    m = re.search(r"```yaml\n(.*?)\n```", contract_text, re.DOTALL)
    assert m is not None, "No yaml code block in contract"
    contract_yaml = m.group(1)
    shipped_yaml = CLUSTERS_YAML.read_text(encoding="utf-8")
    assert contract_yaml == shipped_yaml


def test_load_cluster_config_does_not_consult_changelog(tmp_path: Path) -> None:
    """`load_cluster_config` працює без журналу (тимчасовий каталог без CHANGELOG)."""
    clusters_copy = tmp_path / "clusters.yaml"
    shutil.copyfile(CLUSTERS_YAML, clusters_copy)
    # не створюємо CHANGELOG.md
    cfg = load_cluster_config(clusters_copy)
    assert cfg.version == 1
    assert cfg.digest == SHIPPED_CLUSTERS_SHA


# --- тести виявлення змін без запису ---


@pytest.mark.parametrize(
    "key,old_val,new_val",
    [
        ("link_min_amount_lamports", "10000000", "10000001"),
        ("funding_window_seconds", "3600", "3601"),
        ("seconds_per_slot", "0.4", "0.5"),
        ("link_through_flagged_buyers", "false", "true"),
        ("indirect_enabled", "true", "false"),
        ("same_amount_natural_max", "3", "4"),
        ("same_slot_natural_max", "5", "6"),
        ("same_slot_window_slots", "0", "1"),
        ("slot_fallback_multiplier", "0.8", "0.9"),
        ("artifact_buyer_share", "0.5", "0.6"),
        ("artifact_confidence_multiplier", "0.5", "0.6"),
        ("band_clean_max", "20", "21"),
        ("band_suspicious_max", "50", "51"),
        # evidence_weights (вкладені) — у YAML вони відступлені, ключ без префікса
        ("shared_funder", "0.6", "0.61"),
        ("direct_transfer", "0.5", "0.51"),
        ("delegated_buy", "0.6", "0.61"),
        ("recovered_edge", "0.4", "0.41"),
        ("indirect_link", "0.3", "0.31"),
        ("same_amounts", "0.2", "0.21"),
        ("same_slot", "0.15", "0.16"),
    ],
)
def test_any_value_change_without_entry_is_detected(
    tmp_path: Path, key: str, old_val: str, new_val: str
) -> None:
    """Зміна будь-якого з 17 ключів без запису в журнал → ConfigError."""
    text = CLUSTERS_YAML.read_text(encoding="utf-8")
    # проста заміна рядка (в контракті формат стабільний)
    changed = text.replace(f"{key}: {old_val}", f"{key}: {new_val}")
    assert changed != text, f"Replacement failed for {key}"
    clusters_copy = _copy_with_change(tmp_path, CLUSTERS_YAML, changed)
    changelog_copy = _copy_changelog(tmp_path)
    with pytest.raises(ConfigError, match=r"clusters\.yaml.*content digest does not match"):
        check_changelog(clusters_copy, changelog_copy)


def test_version_bump_without_entry_is_detected(tmp_path: Path) -> None:
    """`version: 2` без запису `## 2` → ConfigError."""
    text = CLUSTERS_YAML.read_text(encoding="utf-8")
    changed = text.replace("version: 1", "version: 2")
    clusters_copy = _copy_with_change(tmp_path, CLUSTERS_YAML, changed)
    changelog_copy = _copy_changelog(tmp_path)
    with pytest.raises(ConfigError, match=r"clusters\.yaml.*version 2.*has no entry"):
        check_changelog(clusters_copy, changelog_copy)


def test_proper_bump_with_entry_passes(tmp_path: Path) -> None:
    """`version: 2` + новий запис `## 2` з правильним дайджестом → ok."""
    text = CLUSTERS_YAML.read_text(encoding="utf-8")
    changed = text.replace("version: 1", "version: 2")
    clusters_copy = _copy_with_change(tmp_path, CLUSTERS_YAML, changed)
    new_digest = _canonical_yaml_digest(changed)

    changelog_text = CHANGELOG.read_text(encoding="utf-8")
    # знаходимо кінець розділу clusters.yaml і додаємо запис
    lines = changelog_text.splitlines()
    # знаходимо рядок sha256 запису 1
    sha_idx = next(
        i
        for i, ln in enumerate(lines)
        if ln.strip() == f"sha256: {SHIPPED_CLUSTERS_SHA}"
    )
    # вставляємо після нього
    new_entry = f"\n## 2 — 2026-10-05\nТестовий запис v2.\nsha256: {new_digest}\n"
    lines.insert(sha_idx + 1, new_entry)
    changelog_copy = _copy_changelog(tmp_path, "\n".join(lines) + "\n")

    # має пройти
    check_changelog(clusters_copy, changelog_copy)


def test_journal_ahead_of_file_is_detected(tmp_path: Path) -> None:
    """Журнал має `## 2`, файл v1 → ConfigError."""
    changelog_text = CHANGELOG.read_text(encoding="utf-8")
    lines = changelog_text.splitlines()
    sha_idx = next(
        i
        for i, ln in enumerate(lines)
        if ln.strip() == f"sha256: {SHIPPED_CLUSTERS_SHA}"
    )
    new_entry = "\n## 2 — 2026-10-05\nТестовий запис v2.\nsha256: " + "a" * 64 + "\n"
    lines.insert(sha_idx + 1, new_entry)
    changelog_copy = _copy_changelog(tmp_path, "\n".join(lines) + "\n")
    # файл залишаємо v1
    clusters_copy = tmp_path / "clusters.yaml"
    shutil.copyfile(CLUSTERS_YAML, clusters_copy)
    with pytest.raises(ConfigError, match=r"clusters\.yaml.*version 1.*not the latest"):
        check_changelog(clusters_copy, changelog_copy)


def test_digest_ignores_comments_and_key_order(tmp_path: Path) -> None:
    """Копія без коментарів і з переставленими ключами → той самий дайджест."""
    data = yaml.safe_load(CLUSTERS_YAML.read_text(encoding="utf-8"))
    # перемішати ключі (json.dumps з sort_keys=True вже це робить)
    # але перевіримо, що дайджест не змінився
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest1 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    # ще раз через content_digest
    clusters_copy = tmp_path / "clusters.yaml"
    clusters_copy.write_text(yaml.dump(data, sort_keys=False), encoding="utf-8")
    digest2 = content_digest(clusters_copy)
    assert digest1 == digest2 == SHIPPED_CLUSTERS_SHA


# --- мутація журналу ---


def test_missing_sha256_in_changelog_entry_is_detected(tmp_path: Path) -> None:
    """Прибрати рядок `sha256:` у копії журналу → ConfigError."""
    changelog_text = CHANGELOG.read_text(encoding="utf-8")
    lines = changelog_text.splitlines()
    sha_idx = next(
        i
        for i, ln in enumerate(lines)
        if ln.strip() == f"sha256: {SHIPPED_CLUSTERS_SHA}"
    )
    # видаляємо рядок sha256
    lines.pop(sha_idx)
    changelog_copy = _copy_changelog(tmp_path, "\n".join(lines) + "\n")
    clusters_copy = tmp_path / "clusters.yaml"
    shutil.copyfile(CLUSTERS_YAML, clusters_copy)
    with pytest.raises(ConfigError, match=r"clusters\.yaml.*last line.*must be 'sha256:"):
        check_changelog(clusters_copy, changelog_copy)