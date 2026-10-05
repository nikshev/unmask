# verifies: FR-002-08, FR-002-13, FR-002-14, FR-002-22
"""Версіонована конфігурація відсікання хабів (T-023): `config/hubs.yaml`, `config/hub_addresses.yaml`,
`hubs.config.load_hub_config`. Контракт — specs/002-funding-graph-hub-pruning/contracts/config-hubs.md.

Мережі немає. Дайджест-захист журналу (`content_digest`, `changelog_entries`) — T-024; тут лише перевірка, що
журнал має розділи й записи з коректним sha256 (локальний допоміжний дайджест за правилом R-14).

T-054 (FR-002-22, research R-22, `calibration.md`): `config/hubs.yaml` — версія 2 з `dust_amount_lamports`
(int ≥ 1) і `dust_min_fanout` (int ≥ 2). Golden-значення нижче (`BASE_THRESHOLDS`, перевірки поставних файлів і
журналу) свідомо оновлено з v1 на v2; запис журналу версії 1 — історичний, його sha256 незмінний.

T-058 (R-23, `calibration.md` «Перерахунок R-23»): `config/hubs.yaml` — версія 3, `one_off_min_senders` 10 → 50;
решта значень v2 без змін. Golden (`BASE_THRESHOLDS`, перевірки поставних файлів і журналу) свідомо оновлено з v2
на v3; записи 1 і 2 журналу — історичні: їхні заголовки, текст і sha256 незмінні (`HUBS_V1_SHA`, `HUBS_V2_SHA`), а
копії v3 з поверненими значеннями v1/v2 дають рівно їхні дайджести (решта значень не змінилась).
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
    "version": 3,
    "degree_threshold": 100,
    "one_off_senders_share": 0.8,
    "one_off_min_senders": 50,  # T-058: 10 → 50 (R-23, calibration.md «Перерахунок R-23»)
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
# sha256 запису 2 (T-054): канонічний вміст v2 дослівно з contracts/config-hubs.md. Історичний — T-058 не змінює;
# v2 = v3 з `one_off_min_senders: 10` і `version: 2`.
HUBS_V2_SHA = "fdf65bb5369e4e40e629ca4cd45f4952466ee21447ae8bbe79a4ee0035f45409"
# Значення, якими v3 відрізняється від v2 (T-058). Решта значень v2 у v3 без змін.
V2_DELTA = {"version": 2, "one_off_min_senders": 10}


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


def test_shipped_thresholds_load_with_version_3_and_documented_values():
    cfg = load_hub_config(THRESHOLDS, None)

    assert isinstance(cfg, HubConfig)
    t = cfg.thresholds
    assert isinstance(t, HubThresholds)
    assert t.version == 3
    assert t.degree_threshold == 100
    assert t.one_off_senders_share == 0.8
    assert t.one_off_min_senders == 50 and type(t.one_off_min_senders) is int  # T-058, R-23
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
    v1 = {k: v for k, v in BASE_THRESHOLDS.items() if k not in DUST_FIELDS} | {"version": 1, "one_off_min_senders": 10}
    path = _write(tmp_path, "hubs.yaml", v1)
    # копія v1 — це рівно історичний вміст запису 1 журналу: решта значень v1 у v2/v3 не змінились
    assert _digest(path) == HUBS_V1_SHA
    with pytest.raises(ConfigError, match=r"^dust_amount_lamports: missing required field$"):
        load_hub_config(path, None)
    only_amount = _write(tmp_path, "hubs.yaml", {**v1, "dust_amount_lamports": 1_000_000})
    with pytest.raises(ConfigError, match=r"^dust_min_fanout: missing required field$"):
        load_hub_config(only_amount, None)


def test_v3_with_v2_values_restored_is_exactly_the_historical_v2_entry(tmp_path):
    """T-058: v3 відрізняється від v2 рівно одним значенням (`one_off_min_senders` 10 → 50) і версією.

    Копія ПОСТАВЛЕНОГО файла з повернутими `one_off_min_senders: 10` і `version: 2` дає рівно дайджест запису 2;
    повернути лише одне з двох — не дає (обидві відмінності реальні й інших немає).
    """
    shipped = yaml.safe_load(THRESHOLDS.read_text(encoding="utf-8"))
    assert shipped == BASE_THRESHOLDS
    assert {k: shipped[k] for k in V2_DELTA} == {"version": 3, "one_off_min_senders": 50}
    assert _digest(_write(tmp_path, "hubs.yaml", shipped | V2_DELTA)) == HUBS_V2_SHA
    for key in V2_DELTA:
        assert _digest(_write(tmp_path, "hubs.yaml", shipped | {key: V2_DELTA[key]})) != HUBS_V2_SHA, key
    # v2-копія — валідний конфіг тієї самої схеми (дев'ять полів; v3 нових полів не додає)
    v2 = load_hub_config(_write(tmp_path, "hubs.yaml", shipped | V2_DELTA), None).thresholds
    assert (v2.version, v2.one_off_min_senders) == (2, 10)
    assert dataclasses.asdict(v2) == BASE_THRESHOLDS | V2_DELTA


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
    # T-058: причина значення 50 записана біля поля — калібрування R-23, кап підписів і переоцінка
    one_off_comment = text[text.index("one_off_min_senders:"):text.index("giant_component_warn_share:")]
    assert "ПЕРЕДУМОВА" in one_off_comment and ">= 50 (включно)" in one_off_comment
    for fragment in ("R-23", "calibration.md", "max_signatures_per_wallet", "ins1", "переоцінити"):
        assert fragment in one_off_comment, fragment


def test_shipped_changelog_has_hubs_sections_with_matching_sha256_and_keeps_ingest_entries():
    text = CHANGELOG.read_text(encoding="utf-8")
    lines = text.splitlines()
    headings = [line for line in lines if line.startswith("# ")]
    # Порядок розділів не фіксується: розділ ingest стоїть ОСТАННІМ, бо наївний розбір `## `-записів у
    # tests/test_tx_batch_size.py (001) бере «хвіст» запису 2 до кінця файла і вимагає, щоб його останнім
    # непорожнім рядком був `sha256:`; будь-який розділ після ingest зламав би цей тест.
    assert sorted(headings) == sorted(["# config/ingest.yaml", "# config/hubs.yaml", "# config/hub_addresses.yaml",
                                         "# config/clusters.yaml", "# config/delivery.yaml"])
    assert len(headings) == 5

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

    # hubs.yaml: ТРИ записи — 1 і 2 (історичні, sha незмінні) і 3 (поточний файл, T-058)
    entries = _hubs_entries(section("hubs.yaml"))
    assert [header.split(" — ")[0] for header, _ in entries] == ["## 1", "## 2", "## 3"]
    (_, body1), (_, body2), (_, body3) = entries
    assert body1[-1] == f"sha256: {HUBS_V1_SHA}"
    assert body2[-1] == f"sha256: {HUBS_V2_SHA}"
    assert body3[-1] == f"sha256: {_digest(THRESHOLDS)}"
    entry1 = "\n".join(body1)
    assert "не калібровано" in entry1
    for fragment in ("degree_threshold=100", "one_off_senders_share=0.8", "one_off_min_senders=10",
                     "giant_component_warn_share=0.5", "prune_off_curve=true", "prune_ingest_high_degree=true"):
        assert fragment in entry1
    entry2 = "\n".join(body2)
    for fragment in ("dust_amount_lamports=1000000", "dust_min_fanout=5", "calibration.md"):
        assert fragment in entry2
    entry3 = "\n".join(body3)
    for fragment in ("one_off_min_senders", "10 → 50", "calibration.md", "R-23", "T-058"):
        assert fragment in entry3, fragment
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


def test_shipped_changelog_entry_3_cites_r23_and_keeps_entries_1_and_2():
    entries = _hubs_entries(_changelog_section("hubs.yaml"))
    assert len(entries) == 3
    (head1, body1), (head2, body2), (head3, body3) = entries
    assert head1 == "## 1 — 2026-10-04"  # історичний запис T-023: заголовок, текст і sha не змінено
    assert body1 == [
        "Початкова версія, не калібровано на реальних токенах: degree_threshold=100, one_off_senders_share=0.8,",
        "one_off_min_senders=10, giant_component_warn_share=0.5, prune_off_curve=true, prune_ingest_high_degree=true.",
        "Правило порогу — строго «більше» (рівно поріг — не хаб), одне для всіх критеріїв; one_off_min_senders — передумова «>=».",
        "Обґрунтування — specs/002-funding-graph-hub-pruning/research.md R-12.",
        f"sha256: {HUBS_V1_SHA}",
    ]
    # історичний запис T-054: заголовок, текст і sha не змінено (T-058 лише дописує запис 3)
    assert head2 == "## 2 — 2026-10-04"
    assert body2 == [
        "Калібрування на 9 реальних токенах pump.fun (5 інсайдерських за MELT, 4 чисті; Helius, N=30, depth=2, кап 30;",
        "specs/002-funding-graph-hub-pruning/calibration.md). Додано критерій dust_fanout (FR-002-22, research R-22):",
        "dust_amount_lamports=1000000 (0,001 SOL; хаб, якщо медіана SOL-сум до різних покупців СТРОГО МЕНША),",
        "dust_min_fanout=5 (передумова, включно). Чому: пилові джерела (fan-out 5–23, медіана < 0,001 SOL) є в кожному",
        "токені й склеюють до 21/30 покупців; жоден критерій v1 їх не ловить. degree_threshold=100 лишено свідомо: на цих",
        "даних неактивний (макс. ступінь 45), зниження до ~30 відсікло б справжнього фінансиста ins1 (ступінь 45, медіана",
        "≥ 0,7 SOL). Решта значень v1 без змін. Пил/фінансист розділяє сума, не ступінь.",
        f"sha256: {HUBS_V2_SHA}",
    ]
    text2 = " ".join(body2)
    # що змінено, чому, на яких токенах (контракт config-hubs.md: «Кожен наступний запис…»)
    for fragment in ("9 реальних токенах", "calibration.md", "FR-002-22", "R-22", "dust_amount_lamports=1000000",
                     "СТРОГО МЕНША", "dust_min_fanout=5", "degree_threshold=100 лишено", "Решта значень v1 без змін"):
        assert fragment in text2, fragment
    # запис 3 (T-058): що змінено (старе → нове), чому (R-23, calibration.md, ins1), на яких токенах, обмеження
    assert head3 == "## 3 — 2026-10-05"
    text3 = " ".join(body3)
    for fragment in ("one_off_min_senders=10 → 50", "calibration.md", "R-23", "T-058", "9 реальних токенах",
                     "max_signatures_per_wallet", "ins1", "DKrPigau", "dust_fanout", "Решта значень v2 без змін",
                     "переоцінити"):
        assert fragment in text3, fragment
    assert body3[-1] == f"sha256: {_digest(THRESHOLDS)}"


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
    assert cfg.thresholds.version == BASE_THRESHOLDS["version"] == 3


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
