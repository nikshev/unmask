# verifies: FR-002-08, FR-002-13, FR-002-14, FR-002-22
"""Версіонована конфігурація відсікання хабів (T-023): `config/hubs.yaml`, `config/hub_addresses.yaml`,
`hubs.config.load_hub_config`. Контракт — specs/002-funding-graph-hub-pruning/contracts/config-hubs.md.

Мережі немає. Дайджест-захист журналу (`content_digest`, `changelog_entries`) — T-024; тут лише перевірка, що
журнал має розділи й записи з коректним sha256 (локальний допоміжний дайджест за правилом R-14).

T-054 (FR-002-22, research R-22, `calibration.md`): `config/hubs.yaml` — версія 2 з `dust_amount_lamports`
(int ≥ 1) і `dust_min_fanout` (int ≥ 2). Golden-значення нижче (`BASE_THRESHOLDS`, перевірки поставних файлів і
журналу) свідомо оновлено з v1 на v2; запис журналу версії 1 — історичний, його sha256 незмінний.
"""

import dataclasses
import hashlib
import json
import math
from pathlib import Path

import pytest
import yaml

from unmask.graph.model import ThresholdsSnapshot
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
    "version": 2,
    "degree_threshold": 100,
    "one_off_senders_share": 0.8,
    "one_off_min_senders": 10,
    "giant_component_warn_share": 0.5,
    "prune_off_curve": True,
    "prune_ingest_high_degree": True,
    "dust_amount_lamports": 1_000_000,  # 0,001 SOL — calibration.md, research R-22
    "dust_min_fanout": 5,
}
DUST_FIELDS = ("dust_amount_lamports", "dust_min_fanout")

# Незалежний еталон: sha256 запису 1 журналу (T-023), обчислений із канонічного вмісту v1. Запис історичний —
# T-054 його не змінює; v1 = v2 без `dust_*` і з `version: 1` (решта значень v1 без змін).
HUBS_V1_SHA = "c410d677279d02687928b12fcf490e4685007c0bb499e12c2fbfb1ca6d25c63c"


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


def test_shipped_thresholds_load_with_version_2_and_documented_values():
    cfg = load_hub_config(THRESHOLDS, None)

    assert isinstance(cfg, HubConfig)
    t = cfg.thresholds
    assert isinstance(t, HubThresholds)
    assert t.version == 2
    assert t.degree_threshold == 100
    assert t.one_off_senders_share == 0.8
    assert t.one_off_min_senders == 10
    assert t.giant_component_warn_share == 0.5
    assert t.prune_off_curve is True
    assert t.prune_ingest_high_degree is True
    assert t.dust_amount_lamports == 1_000_000 and type(t.dust_amount_lamports) is int
    assert t.dust_min_fanout == 5 and type(t.dust_min_fanout) is int
    assert dataclasses.asdict(t) == BASE_THRESHOLDS  # рівно дев'ять полів, жодного зайвого
    assert cfg.lists is None  # шлях до списків не передано


def test_hub_thresholds_match_thresholds_snapshot_fields():
    # Знімок у метаданих результату (T-055) — усі поля HubThresholds, крім version, у тому самому порядку.
    names = [f.name for f in dataclasses.fields(HubThresholds)]
    snapshot_names = [f.name for f in dataclasses.fields(ThresholdsSnapshot)]
    assert names == ["version", *snapshot_names]
    assert len(snapshot_names) == 8
    t = load_hub_config(THRESHOLDS, None).thresholds
    snap = ThresholdsSnapshot(**{k: v for k, v in dataclasses.asdict(t).items() if k != "version"})
    assert (snap.dust_amount_lamports, snap.dust_min_fanout) == (1_000_000, 5)


def test_v1_file_without_dust_fields_is_rejected_naming_the_field(tmp_path):
    v1 = {k: v for k, v in BASE_THRESHOLDS.items() if k not in DUST_FIELDS} | {"version": 1}
    path = _write(tmp_path, "hubs.yaml", v1)
    # копія v1 — це рівно історичний вміст запису 1 журналу: решта значень v1 у v2 не змінились
    assert _digest(path) == HUBS_V1_SHA
    with pytest.raises(ConfigError, match=r"^dust_amount_lamports: missing required field$"):
        load_hub_config(path, None)
    only_amount = _write(tmp_path, "hubs.yaml", {**v1, "dust_amount_lamports": 1_000_000})
    with pytest.raises(ConfigError, match=r"^dust_min_fanout: missing required field$"):
        load_hub_config(only_amount, None)


def test_dust_amount_one_loads_as_valid_off_switch(tmp_path):
    # 1 — найменше допустиме значення; вимикає критерій (жодна сума ребра не < 1). Окремого перемикача немає.
    t = load_hub_config(_thresholds(tmp_path, dust_amount_lamports=1), None).thresholds
    assert t.dust_amount_lamports == 1
    assert not hasattr(t, "prune_dust_fanout")
    with pytest.raises(ConfigError, match=r"^dust_amount_lamports: must be >= 1$"):
        load_hub_config(_thresholds(tmp_path, dust_amount_lamports=0), None)


@pytest.mark.parametrize("value", [1, 0, -5])
def test_dust_min_fanout_below_two_is_config_error(tmp_path, value):
    with pytest.raises(ConfigError, match=r"^dust_min_fanout: must be >= 2$"):
        load_hub_config(_thresholds(tmp_path, dust_min_fanout=value), None)
    # межа включна: 2 — найменший осмислений fan-out (медіана щонайменше двох значень)
    assert load_hub_config(_thresholds(tmp_path, dust_min_fanout=2), None).thresholds.dust_min_fanout == 2


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
    # Напрямковий варіант правила (R-22): dust_amount_lamports — «СТРОГО МЕНШЕ», передумова dust_min_fanout — «>=».
    assert any("dust_amount_lamports" in ln and "СТРОГО МЕНШЕ" in ln for ln in text.splitlines())
    assert "СТРОГО МЕНШЕ" in (hubs_config.__doc__ or "")
    assert "dust_min_fanout" in (hubs_config.__doc__ or "")
    dust_comment = text[text.index("dust_amount_lamports:"):text.index("dust_min_fanout:")]
    assert "Рівно поріг — не хаб" in dust_comment
    assert "вимикає" in dust_comment  # значення 1 — вимикач, задокументований біля поля
    fanout_comment = text[text.index("dust_min_fanout:"):]
    assert "ПЕРЕДУМОВА" in fanout_comment and ">= 5 (включно)" in fanout_comment


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

    addresses_body = section("hub_addresses.yaml")
    assert [ln for ln in addresses_body.splitlines() if ln.startswith("## ")][0].startswith("## 1 — ")
    assert [ln for ln in addresses_body.splitlines() if ln.strip()][-1] == f"sha256: {_digest(LISTS)}"

    # hubs.yaml: ОБИДВА записи — 1 (історичний, sha незмінний) і 2 (поточний файл)
    entries = _hubs_entries(section("hubs.yaml"))
    assert [header.split(" — ")[0] for header, _ in entries] == ["## 1", "## 2"]
    (_, body1), (_, body2) = entries
    assert body1[-1] == f"sha256: {HUBS_V1_SHA}"
    assert body2[-1] == f"sha256: {_digest(THRESHOLDS)}"
    entry1 = "\n".join(body1)
    assert "не калібровано" in entry1
    for fragment in ("degree_threshold=100", "one_off_senders_share=0.8", "one_off_min_senders=10",
                     "giant_component_warn_share=0.5", "prune_off_curve=true", "prune_ingest_high_degree=true"):
        assert fragment in entry1
    entry2 = "\n".join(body2)
    for fragment in ("dust_amount_lamports=1000000", "dust_min_fanout=5", "calibration.md"):
        assert fragment in entry2
    assert "exchanges(0), market_makers(0)" in addresses_body


def _hubs_entries(body: str) -> list[tuple[str, list[str]]]:
    """Записи розділу: (заголовок `## …`, непорожні рядки тіла)."""
    entries: list[tuple[str, list[str]]] = []
    for line in body.splitlines():
        if line.startswith("## "):
            entries.append((line, []))
        elif line.strip():
            assert entries, "текст до першого запису"
            entries[-1][1].append(line)
    return entries


def _changelog_section(name: str) -> str:
    lines = CHANGELOG.read_text(encoding="utf-8").splitlines()
    start = lines.index(f"# config/{name}")
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("# ")), len(lines))
    return "\n".join(lines[start + 1 : end])


def test_shipped_changelog_entry_2_cites_calibration_and_keeps_entry_1_digest():
    entries = _hubs_entries(_changelog_section("hubs.yaml"))
    assert len(entries) == 2
    (head1, body1), (head2, body2) = entries
    assert head1 == "## 1 — 2026-10-04"  # історичний запис T-023: заголовок, текст і sha не змінено
    assert body1 == [
        "Початкова версія, не калібровано на реальних токенах: degree_threshold=100, one_off_senders_share=0.8,",
        "one_off_min_senders=10, giant_component_warn_share=0.5, prune_off_curve=true, prune_ingest_high_degree=true.",
        "Правило порогу — строго «більше» (рівно поріг — не хаб), одне для всіх критеріїв; one_off_min_senders — передумова «>=».",
        "Обґрунтування — specs/002-funding-graph-hub-pruning/research.md R-12.",
        f"sha256: {HUBS_V1_SHA}",
    ]
    assert head2.startswith("## 2 — 2026-")
    text2 = " ".join(body2)
    # що змінено, чому, на яких токенах (контракт config-hubs.md: «Кожен наступний запис…»)
    for fragment in ("9 реальних токенах", "calibration.md", "FR-002-22", "R-22", "dust_amount_lamports=1000000",
                     "СТРОГО МЕНША", "dust_min_fanout=5", "degree_threshold=100 лишено", "Решта значень v1 без змін"):
        assert fragment in text2, fragment
    assert body2[-1] == f"sha256: {_digest(THRESHOLDS)}"


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
        ("dust_amount_lamports", 250_000),
        ("dust_min_fanout", 7),
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
        ("dust_amount_lamports", 1),
        ("dust_min_fanout", 2),
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
    # dust_* (T-054, R-22): ціле, не bool, межі `>= 1` і `>= 2`, обов'язкові
    pytest.param({"dust_amount_lamports": 0}, "dust_amount_lamports", id="dust_amount=0"),
    pytest.param({"dust_amount_lamports": -1}, "dust_amount_lamports", id="dust_amount=-1"),
    pytest.param({"dust_amount_lamports": True}, "dust_amount_lamports", id="dust_amount=true"),
    pytest.param({"dust_amount_lamports": 1000000.0}, "dust_amount_lamports", id="dust_amount-float"),
    pytest.param({"dust_amount_lamports": "1000000"}, "dust_amount_lamports", id="dust_amount-str"),
    pytest.param({"dust_min_fanout": 1}, "dust_min_fanout", id="dust_min_fanout=1"),
    pytest.param({"dust_min_fanout": True}, "dust_min_fanout", id="dust_min_fanout=true"),
    pytest.param({"dust_min_fanout": 5.0}, "dust_min_fanout", id="dust_min_fanout-float"),
    pytest.param("MISSING:dust_amount_lamports", "dust_amount_lamports", id="missing-dust_amount"),
    pytest.param("MISSING:dust_min_fanout", "dust_min_fanout", id="missing-dust_min_fanout"),
    pytest.param({"prune_dust_fanout": True}, "prune_dust_fanout", id="unknown-dust-switch"),
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
    assert cfg.thresholds.version == 2


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
