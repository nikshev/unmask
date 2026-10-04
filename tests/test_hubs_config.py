# verifies: FR-002-08, FR-002-13, FR-002-14
"""Версіонована конфігурація відсікання хабів (T-023): `config/hubs.yaml`, `config/hub_addresses.yaml`,
`hubs.config.load_hub_config`. Контракт — specs/002-funding-graph-hub-pruning/contracts/config-hubs.md.

Мережі немає. Дайджест-захист журналу (`content_digest`, `changelog_entries`) — T-024; тут лише перевірка, що
журнал має розділи й запис версії 1 з коректним sha256 (локальний допоміжний дайджест за правилом R-14).
"""

import dataclasses
import hashlib
import json
import math
from pathlib import Path

import pytest
import yaml

from unmask.hubs import config as hubs_config
from unmask.hubs.config import (
    AddressLists,
    ConfigError,
    HubConfig,
    HubThresholds,
    load_hub_config,
)

ROOT = Path(__file__).resolve().parent.parent
THRESHOLDS = ROOT / "config" / "hubs.yaml"
LISTS = ROOT / "config" / "hub_addresses.yaml"
CHANGELOG = ROOT / "config" / "CHANGELOG.md"

CATEGORIES = (
    "system_programs",
    "token_programs",
    "dex_routers",
    "amm_programs",
    "launchpads",
    "exchanges",
    "market_makers",
)

# Реальні адреси (валідні base58, 32 байти) — з config/hub_addresses.yaml v1.
SYSTEM = "11111111111111111111111111111111"
TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
JUPITER = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"

BASE_THRESHOLDS = {
    "version": 1,
    "degree_threshold": 100,
    "one_off_senders_share": 0.8,
    "one_off_min_senders": 10,
    "giant_component_warn_share": 0.5,
    "prune_off_curve": True,
    "prune_ingest_high_degree": True,
}


def _base_lists() -> dict:
    return {"version": 1, "categories": {name: [] for name in CATEGORIES}}


def _write(tmp_path: Path, name: str, data) -> Path:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def _thresholds(tmp_path: Path, **changes) -> Path:
    return _write(tmp_path, "hubs.yaml", {**BASE_THRESHOLDS, **changes})


def _thresholds_without(tmp_path: Path, key: str) -> Path:
    data = dict(BASE_THRESHOLDS)
    del data[key]
    return _write(tmp_path, "hubs.yaml", data)


def _lists(tmp_path: Path, categories: dict | None = None, **top) -> Path:
    data = _base_lists()
    if categories is not None:
        data["categories"] = categories
    data.update(top)
    return _write(tmp_path, "hub_addresses.yaml", data)


def _digest(path: Path) -> str:
    """sha256 канонічного вмісту (research R-14); локально, до T-024."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --- поставні файли ----------------------------------------------------------------------------------


def test_shipped_thresholds_load_with_version_1_and_documented_values():
    cfg = load_hub_config(THRESHOLDS, None)

    assert isinstance(cfg, HubConfig)
    t = cfg.thresholds
    assert isinstance(t, HubThresholds)
    assert t.version == 1
    assert t.degree_threshold == 100
    assert t.one_off_senders_share == 0.8
    assert t.one_off_min_senders == 10
    assert t.giant_component_warn_share == 0.5
    assert t.prune_off_curve is True
    assert t.prune_ingest_high_degree is True
    assert cfg.lists is None  # шлях до списків не передано


def test_shipped_address_lists_load_with_seven_categories_and_version_1():
    cfg = load_hub_config(THRESHOLDS, LISTS)

    lists = cfg.lists
    assert isinstance(lists, AddressLists)
    assert lists.version == 1
    assert tuple(lists.categories) == CATEGORIES  # рівно сім ключів, у порядку контракту
    assert {name: len(addrs) for name, addrs in lists.categories.items()} == {
        "system_programs": 3,
        "token_programs": 3,
        "dex_routers": 1,
        "amm_programs": 2,
        "launchpads": 1,
        "exchanges": 0,  # не вигадуємо адрес бірж і ММ
        "market_makers": 0,
    }
    assert lists.categories["system_programs"] == (
        SYSTEM,
        "ComputeBudget111111111111111111111111111111",
        "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr",
    )
    assert lists.categories["token_programs"] == (
        TOKEN,
        "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
        "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
    )
    assert lists.categories["dex_routers"] == (JUPITER,)
    assert lists.categories["amm_programs"] == (
        "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",
        "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc",
    )
    assert lists.categories["launchpads"] == ("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",)
    # похідний індекс адреса -> категорія
    assert lists.index[JUPITER] == "dex_routers"
    assert lists.index[SYSTEM] == "system_programs"
    assert len(lists.index) == 10


def test_shipped_files_state_strictly_greater_rule_and_min_senders_precondition():
    # Правило порогу (FR-002-14) зафіксоване в самому конфігу й у докстрінгу завантажувача.
    text = THRESHOLDS.read_text(encoding="utf-8")
    assert "СТРОГО БІЛЬШЕ" in text
    assert "Рівно поріг — не хаб" in text
    assert "включно" in text  # one_off_min_senders — передумова «>=», не поріг
    assert "СТРОГО БІЛЬШЕ" in (hubs_config.__doc__ or "")


def test_shipped_changelog_has_hubs_sections_with_matching_sha256_and_keeps_ingest_entries():
    text = CHANGELOG.read_text(encoding="utf-8")
    lines = text.splitlines()
    headings = [line for line in lines if line.startswith("# ")]
    # Порядок розділів не фіксується: розділ ingest стоїть ОСТАННІМ, бо наївний розбір `## `-записів у
    # tests/test_tx_batch_size.py (001) бере «хвіст» запису 2 до кінця файла і вимагає, щоб його останнім
    # непорожнім рядком був `sha256:`; будь-який розділ після ingest зламав би цей тест.
    assert sorted(headings) == sorted(["# config/ingest.yaml", "# config/hubs.yaml", "# config/hub_addresses.yaml"])
    assert len(headings) == 3

    def section(name: str) -> str:
        start = lines.index(f"# config/{name}")
        end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("# ")), len(lines))
        return "\n".join(lines[start + 1 : end])

    # записи 001 лишились (текст не змінено) — їх захищає tests/test_tx_batch_size.py
    ingest = section("ingest.yaml")
    assert "## 1 — 2026-10-03" in ingest and "## 2 — 2026-10-04" in ingest

    for name, path in (("hubs.yaml", THRESHOLDS), ("hub_addresses.yaml", LISTS)):
        body = section(name)
        assert [ln for ln in body.splitlines() if ln.startswith("## ")][0].startswith("## 1 — ")
        last = [ln for ln in body.splitlines() if ln.strip()][-1]
        assert last == f"sha256: {_digest(path)}"
    hubs_body = section("hubs.yaml")
    assert "не калібровано" in hubs_body
    for fragment in ("degree_threshold=100", "one_off_senders_share=0.8", "one_off_min_senders=10",
                     "giant_component_warn_share=0.5", "prune_off_curve=true", "prune_ingest_high_degree=true"):
        assert fragment in hubs_body
    addresses_body = section("hub_addresses.yaml")
    assert "exchanges(0), market_makers(0)" in addresses_body


# --- кожне значення читається з YAML (тихих умовчань у коді немає) ------------------------------------


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", 7),
        ("degree_threshold", 3),
        ("one_off_senders_share", 0.25),
        ("one_off_min_senders", 4),
        ("giant_component_warn_share", 0.9),
        ("prune_off_curve", False),
        ("prune_ingest_high_degree", False),
    ],
)
def test_each_threshold_round_trips_from_yaml(tmp_path, field, value):
    cfg = load_hub_config(_thresholds(tmp_path, **{field: value}), None)

    assert getattr(cfg.thresholds, field) == value
    others = {k: v for k, v in BASE_THRESHOLDS.items() if k != field}
    for key, expected in others.items():
        assert getattr(cfg.thresholds, key) == expected  # решта не зачеплена


@pytest.mark.parametrize(
    "field,value",
    [
        ("degree_threshold", 1),
        ("one_off_senders_share", 0),
        ("one_off_senders_share", 1),
        ("one_off_min_senders", 2),
        ("giant_component_warn_share", 1),
        ("giant_component_warn_share", 0.0001),
        ("version", 1),
    ],
)
def test_threshold_boundary_values_are_accepted(tmp_path, field, value):
    cfg = load_hub_config(_thresholds(tmp_path, **{field: value}), None)
    assert getattr(cfg.thresholds, field) == value


def test_numeric_types_are_normalized(tmp_path):
    cfg = load_hub_config(_thresholds(tmp_path, one_off_senders_share=1, giant_component_warn_share=1), None)
    assert isinstance(cfg.thresholds.one_off_senders_share, float)
    assert isinstance(cfg.thresholds.giant_component_warn_share, float)
    assert isinstance(cfg.thresholds.degree_threshold, int)


# --- валідація порогів --------------------------------------------------------------------------------

INVALID_THRESHOLDS = [
    pytest.param({"degree_threshold": 0}, "degree_threshold", id="degree=0"),
    pytest.param({"degree_threshold": -5}, "degree_threshold", id="degree=-5"),
    pytest.param({"one_off_senders_share": 1.5}, "one_off_senders_share", id="share=1.5"),
    pytest.param({"one_off_senders_share": -0.1}, "one_off_senders_share", id="share=-0.1"),
    pytest.param({"one_off_min_senders": 1}, "one_off_min_senders", id="min_senders=1"),
    pytest.param({"giant_component_warn_share": 0}, "giant_component_warn_share", id="warn=0"),
    pytest.param({"giant_component_warn_share": 1.01}, "giant_component_warn_share", id="warn=1.01"),
    pytest.param({"version": 0}, "version", id="version=0"),
    pytest.param({"unknown_field": 1}, "unknown_field", id="unknown-field"),
    pytest.param("MISSING:version", "version", id="missing-version"),
    pytest.param("MISSING:degree_threshold", "degree_threshold", id="missing-degree"),
    pytest.param("MISSING:prune_off_curve", "prune_off_curve", id="missing-bool"),
    pytest.param({"prune_off_curve": "true"}, "prune_off_curve", id="not-bool-str"),
    pytest.param({"prune_ingest_high_degree": 1}, "prune_ingest_high_degree", id="not-bool-int"),
    pytest.param({"degree_threshold": True}, "degree_threshold", id="int-field-bool"),
    pytest.param({"degree_threshold": 100.0}, "degree_threshold", id="int-field-float"),
    pytest.param({"degree_threshold": "100"}, "degree_threshold", id="int-field-str"),
    pytest.param({"one_off_senders_share": "0.8"}, "one_off_senders_share", id="share-str"),
    pytest.param({"one_off_senders_share": True}, "one_off_senders_share", id="share-bool"),
    pytest.param({"one_off_senders_share": math.nan}, "one_off_senders_share", id="share-nan"),
    pytest.param({"giant_component_warn_share": math.nan}, "giant_component_warn_share", id="warn-nan"),
]


@pytest.mark.parametrize("change,field", INVALID_THRESHOLDS)
def test_out_of_range_or_unknown_field_raises_config_error(tmp_path, change, field):
    if isinstance(change, str):
        path = _thresholds_without(tmp_path, change.removeprefix("MISSING:"))
    else:
        path = _thresholds(tmp_path, **change)

    with pytest.raises(ConfigError, match=field):
        load_hub_config(path, None)


@pytest.mark.parametrize("content", ["", "[1, 2]", "just text", "version: [1"])
def test_thresholds_that_are_not_a_mapping_or_not_yaml_raise_config_error(tmp_path, content):
    path = tmp_path / "hubs.yaml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_hub_config(path, None)


def test_missing_thresholds_file_raises_config_error(tmp_path):
    with pytest.raises(ConfigError, match="hubs.yaml"):
        load_hub_config(tmp_path / "hubs.yaml", None)


def test_invalid_thresholds_fail_even_when_lists_are_fine(tmp_path):
    with pytest.raises(ConfigError, match="degree_threshold"):
        load_hub_config(_thresholds(tmp_path, degree_threshold=0), _lists(tmp_path))


# --- списки адрес -------------------------------------------------------------------------------------


def test_address_in_two_categories_raises_config_error(tmp_path):
    cats = {name: [] for name in CATEGORIES}
    cats["dex_routers"] = [JUPITER]
    cats["amm_programs"] = [JUPITER]
    with pytest.raises(ConfigError, match=JUPITER):
        load_hub_config(_thresholds(tmp_path), _lists(tmp_path, cats))


def test_duplicate_address_inside_one_category_raises_config_error(tmp_path):
    cats = {name: [] for name in CATEGORIES}
    cats["dex_routers"] = [JUPITER, JUPITER]
    with pytest.raises(ConfigError, match=JUPITER):
        load_hub_config(_thresholds(tmp_path), _lists(tmp_path, cats))


@pytest.mark.parametrize(
    "bad",
    [
        "not-a-base58-address-0OIl",  # символи поза алфавітом base58
        "abc",  # валідний base58, але не 32 байти
        "",
        "1" * 33,  # 33 байти
        12345,  # не рядок
        None,
    ],
)
def test_invalid_address_raises_config_error(tmp_path, bad):
    cats = {name: [] for name in CATEGORIES}
    cats["launchpads"] = [bad]
    with pytest.raises(ConfigError, match="launchpads"):
        load_hub_config(_thresholds(tmp_path), _lists(tmp_path, cats))


def test_missing_lists_file_yields_lists_none_not_error(tmp_path):
    cfg = load_hub_config(_thresholds(tmp_path), tmp_path / "no_such_hub_addresses.yaml")

    assert cfg.lists is None
    assert cfg.lists_applied is False  # FR-002-12: недоступний список явно позначений
    assert cfg.thresholds.version == 1


def test_lists_path_none_and_unreadable_file_yield_lists_none(tmp_path):
    assert load_hub_config(_thresholds(tmp_path), None).lists is None
    directory = tmp_path / "is_a_directory.yaml"  # читання каталогу — OSError, тобто «нечитабельний»
    directory.mkdir()
    assert load_hub_config(_thresholds(tmp_path), directory).lists is None


def test_available_lists_are_marked_applied_even_when_all_categories_empty(tmp_path):
    cfg = load_hub_config(_thresholds(tmp_path), _lists(tmp_path))

    assert cfg.lists is not None and cfg.lists_applied is True
    assert all(addrs == () for addrs in cfg.lists.categories.values())
    assert cfg.lists.index == {}


def test_unknown_category_raises_config_error(tmp_path):
    cats = {name: [] for name in CATEGORIES}
    cats["whales"] = []
    with pytest.raises(ConfigError, match="whales"):
        load_hub_config(_thresholds(tmp_path), _lists(tmp_path, cats))


@pytest.mark.parametrize("category", CATEGORIES)
def test_missing_category_raises_config_error(tmp_path, category):
    cats = {name: [] for name in CATEGORIES if name != category}
    with pytest.raises(ConfigError, match=category):
        load_hub_config(_thresholds(tmp_path), _lists(tmp_path, cats))


@pytest.mark.parametrize(
    "build,field",
    [
        (lambda: {"categories": {n: [] for n in CATEGORIES}}, "version"),
        (lambda: {"version": 0, "categories": {n: [] for n in CATEGORIES}}, "version"),
        (lambda: {"version": True, "categories": {n: [] for n in CATEGORIES}}, "version"),
        (lambda: {"version": 1}, "categories"),
        (lambda: {"version": 1, "categories": [], "extra": 1}, "extra"),
        (lambda: {"version": 1, "categories": ["a"]}, "categories"),
        (lambda: {"version": 1, "categories": {**{n: [] for n in CATEGORIES}, "launchpads": "x"}}, "launchpads"),
    ],
)
def test_invalid_lists_structure_raises_config_error_with_field_name(tmp_path, build, field):
    path = _write(tmp_path, "hub_addresses.yaml", build())
    with pytest.raises(ConfigError, match=field):
        load_hub_config(_thresholds(tmp_path), path)


def test_lists_that_are_not_yaml_raise_config_error(tmp_path):
    path = tmp_path / "hub_addresses.yaml"
    path.write_text("version: [1", encoding="utf-8")  # присутній, але зіпсований — дефект репозиторію
    with pytest.raises(ConfigError):
        load_hub_config(_thresholds(tmp_path), path)


def test_empty_lists_file_raises_config_error_not_none(tmp_path):
    path = tmp_path / "hub_addresses.yaml"
    path.write_text("", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_hub_config(_thresholds(tmp_path), path)


def test_lists_version_is_read_independently_from_thresholds_version(tmp_path):
    cfg = load_hub_config(_thresholds(tmp_path, version=3), _lists(tmp_path, version=5))
    assert (cfg.thresholds.version, cfg.lists.version) == (3, 5)


# --- незмінність --------------------------------------------------------------------------------------


def test_config_types_are_frozen(tmp_path):
    cfg = load_hub_config(THRESHOLDS, LISTS)
    for obj, attr in ((cfg, "lists"), (cfg.thresholds, "degree_threshold"), (cfg.lists, "version")):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(obj, attr, None)
    assert isinstance(cfg.lists.categories["launchpads"], tuple)  # кортежі, не списки: вміст не змінити
    with pytest.raises(TypeError):
        cfg.lists.categories["launchpads"] = ()
    with pytest.raises(TypeError):
        cfg.lists.index["x"] = "y"
