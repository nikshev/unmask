# verifies: FR-002-08
"""Автоматичний захист журналу змін конфігурації відсікання (SC-008, research R-14; T-024).

Правило: кожна зміна `config/hubs.yaml` чи `config/hub_addresses.yaml` — підняти `version` і додати запис
`## <version> — <дата>` у розділ `# config/<файл>` журналу `config/CHANGELOG.md`, останній рядок якого —
`sha256: <hex>` канонічного вмісту файла (`yaml.safe_load` → `json.dumps(sort_keys=True,
separators=(",", ":"), ensure_ascii=False)` → sha256). Перевірка — `hubs.config.check_changelog`; цей тест
запускає її на поставлених файлах, тож зміна порогу чи списку без запису червонить збірку.

`load_hub_config` журнал НЕ читає (контракт graph-service §1: функція чиста відносно ФС і приймає лише два
шляхи; R-14 покладає перевірку на тест): вона лише обчислює дайджести, які несе `HubConfig`.

Мережі немає; усе — на поставлених файлах і їхніх tmp-копіях.
"""

import hashlib
import re
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

ROOT = Path(__file__).resolve().parent.parent
THRESHOLDS = ROOT / "config" / "hubs.yaml"
LISTS = ROOT / "config" / "hub_addresses.yaml"
CHANGELOG = ROOT / "config" / "CHANGELOG.md"

# Незалежний еталон: дайджести, записані в журнал T-023 за правилом R-14 (не обчислені кодом, що тестується).
SHIPPED_HUBS_SHA = "c410d677279d02687928b12fcf490e4685007c0bb499e12c2fbfb1ca6d25c63c"
SHIPPED_LISTS_SHA = "89c0a8be31ec2ffca32117ed38191e4caad0c840d9f4576048d0f312af80f2d5"

A = "a" * 64
B = "b" * 64


def _copy(tmp_path: Path, src: Path, text: str | None = None) -> Path:
    """tmp-копія під тим самим ім'ям файла (журнал адресує розділ за ім'ям)."""
    dst = tmp_path / src.name
    dst.write_text(src.read_text(encoding="utf-8") if text is None else text, encoding="utf-8")
    return dst


def _changelog(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "CHANGELOG.md"
    path.write_text(text, encoding="utf-8")
    return path


def _shipped_with_changelog(tmp_path: Path, changelog_text: str) -> tuple[Path, Path, Path]:
    return _copy(tmp_path, THRESHOLDS), _copy(tmp_path, LISTS), _changelog(tmp_path, changelog_text)


# --- поставлені файли -------------------------------------------------------------------------------------


def test_shipped_hubs_yaml_digest_matches_changelog_entry_for_its_version():
    cfg = load_hub_config(THRESHOLDS, LISTS)
    entries = changelog_entries(CHANGELOG, "hubs.yaml")
    assert cfg.thresholds.version in entries
    assert entries[cfg.thresholds.version] == content_digest(THRESHOLDS) == cfg.thresholds_digest
    assert content_digest(THRESHOLDS) == SHIPPED_HUBS_SHA  # golden: правило дайджесту не дрейфує
    check_changelog(THRESHOLDS, CHANGELOG)


def test_shipped_address_lists_digest_matches_changelog_entry():
    cfg = load_hub_config(THRESHOLDS, LISTS)
    entries = changelog_entries(CHANGELOG, "hub_addresses.yaml")
    assert cfg.lists is not None
    assert entries[cfg.lists.version] == content_digest(LISTS) == cfg.lists_digest
    assert content_digest(LISTS) == SHIPPED_LISTS_SHA
    check_changelog(LISTS, CHANGELOG)


def test_parser_reads_only_its_own_section():
    # кожен розділ дає свій дайджест; хвіст до кінця файла (розділ ingest) чи сусідній розділ не підмішується
    assert changelog_entries(CHANGELOG, "hubs.yaml") == {1: SHIPPED_HUBS_SHA}
    assert changelog_entries(CHANGELOG, "hub_addresses.yaml") == {1: SHIPPED_LISTS_SHA}


def test_ingest_section_stays_last_for_001_changelog_test():
    # tests/test_tx_batch_size.py (001) бере хвіст останнього `## `-запису до кінця файла; розділ після ingest
    # зламав би його. Наш парсер від порядку розділів не залежить (див. test_section_order_is_irrelevant).
    headings = [ln for ln in CHANGELOG.read_text(encoding="utf-8").splitlines() if ln.startswith("# ")]
    assert headings[-1] == "# config/ingest.yaml"


# --- зміна без запису -------------------------------------------------------------------------------------


def test_changed_threshold_without_changelog_entry_is_detected(tmp_path):
    text = THRESHOLDS.read_text(encoding="utf-8")
    assert "degree_threshold: 100 " in text
    changed = _copy(tmp_path, THRESHOLDS, text.replace("degree_threshold: 100 ", "degree_threshold: 101 "))
    assert content_digest(changed) != changelog_entries(CHANGELOG, "hubs.yaml")[1]
    with pytest.raises(ConfigError, match=r"hubs\.yaml.*version 1.*digest"):
        check_changelog(changed, CHANGELOG)


@pytest.mark.parametrize(
    "old, new",
    [
        ("one_off_senders_share: 0.8 ", "one_off_senders_share: 0.81 "),
        ("prune_off_curve: true ", "prune_off_curve: false "),
        ("degree_threshold: 100 ", 'degree_threshold: "100" '),  # тип значення — теж зміна вмісту
    ],
)
def test_any_threshold_value_change_is_detected(tmp_path, old, new):
    text = THRESHOLDS.read_text(encoding="utf-8")
    assert old in text
    changed = _copy(tmp_path, THRESHOLDS, text.replace(old, new))
    with pytest.raises(ConfigError, match=r"hubs\.yaml"):
        check_changelog(changed, CHANGELOG)


@pytest.mark.parametrize(
    "old, new",
    [
        # нова адреса в порожній категорії
        ("  market_makers: []", '  market_makers:\n    - "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV5"'),
        # видалено адресу
        ('    - "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"   # Memo Program v2\n', ""),
    ],
)
def test_changed_address_list_without_changelog_entry_is_detected(tmp_path, old, new):
    text = LISTS.read_text(encoding="utf-8")
    assert old in text
    changed = _copy(tmp_path, LISTS, text.replace(old, new))
    assert content_digest(changed) != SHIPPED_LISTS_SHA
    with pytest.raises(ConfigError, match=r"hub_addresses\.yaml.*version 1.*digest"):
        check_changelog(changed, CHANGELOG)


def test_moving_address_between_categories_is_detected(tmp_path):
    # ті самі адреси, інша категорія — інший вміст
    text = LISTS.read_text(encoding="utf-8")
    jup = '    - "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"   # Jupiter Aggregator v6\n'
    assert jup in text
    moved = text.replace(jup, "    []\n").replace("  exchanges: []", "  exchanges:\n" + jup.rstrip("\n"))
    changed = _copy(tmp_path, LISTS, moved)
    load_hub_config(THRESHOLDS, changed)  # валідний файл
    with pytest.raises(ConfigError, match=r"hub_addresses\.yaml"):
        check_changelog(changed, CHANGELOG)


@pytest.mark.parametrize("src", [THRESHOLDS, LISTS], ids=["hubs", "hub_addresses"])
def test_version_bump_without_entry_is_detected(tmp_path, src):
    text = src.read_text(encoding="utf-8")
    assert "\nversion: 1\n" in text
    bumped = _copy(tmp_path, src, text.replace("\nversion: 1\n", "\nversion: 2\n"))
    with pytest.raises(ConfigError, match=rf"{re.escape(src.name)}.*version 2.*no entry"):
        check_changelog(bumped, CHANGELOG)


def test_new_entry_without_version_bump_is_detected(tmp_path):
    # змінено поріг, у журнал дописано запис 2 з правильним дайджестом, але version у файлі лишилась 1
    text = THRESHOLDS.read_text(encoding="utf-8")
    changed = _copy(tmp_path, THRESHOLDS, text.replace("degree_threshold: 100 ", "degree_threshold: 150 "))
    log = _changelog(
        tmp_path,
        f"# config/hubs.yaml\n\n## 1 — 2026-10-04\nv1\nsha256: {SHIPPED_HUBS_SHA}\n\n"
        f"## 2 — 2026-10-05\nv2\nsha256: {content_digest(changed)}\n",
    )
    with pytest.raises(ConfigError, match=r"hubs\.yaml.*version 1"):
        check_changelog(changed, log)


def test_journal_ahead_of_file_is_detected(tmp_path):
    # файл не змінено, але в журналі вже є новіша версія — файл не відповідає останньому запису
    log = _changelog(
        tmp_path,
        f"# config/hubs.yaml\n\n## 1 — 2026-10-04\nv1\nsha256: {SHIPPED_HUBS_SHA}\n\n"
        f"## 2 — 2026-10-05\nv2\nsha256: {A}\n",
    )
    with pytest.raises(ConfigError, match=r"hubs\.yaml.*version 1.*latest.*2"):
        check_changelog(THRESHOLDS, log)


def test_proper_bump_with_entry_passes(tmp_path):
    text = THRESHOLDS.read_text(encoding="utf-8")
    bumped = _copy(
        tmp_path,
        THRESHOLDS,
        text.replace("\nversion: 1\n", "\nversion: 2\n").replace("degree_threshold: 100 ", "degree_threshold: 150 "),
    )
    log = _changelog(
        tmp_path,
        f"# config/hubs.yaml\n\n## 1 — 2026-10-04\nv1\nsha256: {SHIPPED_HUBS_SHA}\n\n"
        f"## 2 — 2026-10-05\nпідняли поріг\nsha256: {content_digest(bumped)}\n",
    )
    check_changelog(bumped, log)
    assert load_hub_config(bumped).thresholds_digest == content_digest(bumped)


# --- канонічний дайджест -----------------------------------------------------------------------------------


def test_digest_ignores_comments_and_formatting(tmp_path):
    for src in (THRESHOLDS, LISTS):
        data = yaml.safe_load(src.read_text(encoding="utf-8"))
        block = "# зовсім інший коментар\n\n" + yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
        flow = yaml.safe_dump(data, sort_keys=True, default_flow_style=True)  # інший порядок ключів і стиль
        for i, text in enumerate((block, flow)):
            sub = tmp_path / f"{i}"
            sub.mkdir(exist_ok=True)
            reformatted = _copy(sub, src, text)
            assert reformatted.read_text(encoding="utf-8") != src.read_text(encoding="utf-8")
            assert content_digest(reformatted) == content_digest(src)
            check_changelog(reformatted, CHANGELOG)  # переформатування не вимагає запису


def test_digest_is_sha256_of_exact_canonical_json(tmp_path):
    # незалежний оракул — літерал канонічного рядка: ключі відсортовано, без пробілів, не-ASCII як є
    path = tmp_path / "x.yaml"
    path.write_text("b: 1\na: \"Біржа\"  # коментар\nc: [1.5, true, null]\n", encoding="utf-8")
    canonical = '{"a":"Біржа","b":1,"c":[1.5,true,null]}'
    assert content_digest(path) == hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_digest_of_invalid_yaml_is_config_error(tmp_path):
    path = tmp_path / "hubs.yaml"
    path.write_text("version: [1\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=r"hubs\.yaml"):
        content_digest(path)


# --- формат журналу ----------------------------------------------------------------------------------------


def test_entry_without_sha_line_is_rejected(tmp_path):
    shipped = CHANGELOG.read_text(encoding="utf-8")
    line = f"sha256: {SHIPPED_HUBS_SHA}\n"
    assert line in shipped
    log = _changelog(tmp_path, shipped.replace(line, ""))
    with pytest.raises(ConfigError, match=r"hubs\.yaml.*version 1.*sha256"):
        changelog_entries(log, "hubs.yaml")
    with pytest.raises(ConfigError, match=r"hubs\.yaml"):
        check_changelog(THRESHOLDS, log)
    # сусідній розділ не зачеплено
    assert changelog_entries(log, "hub_addresses.yaml") == {1: SHIPPED_LISTS_SHA}


@pytest.mark.parametrize(
    "body",
    [
        f"sha256: {A}\nа потім ще текст\n",  # sha256 — не останній рядок
        f"sha256: {A[:-1]}\n",  # 63 hex
        f"sha256: {A.upper()}\n",  # великі літери — не канонічний hex
        f"sha256:{A}\n",  # без пробілу
        f"SHA256: {A}\n",
    ],
)
def test_malformed_sha_line_is_rejected(tmp_path, body):
    log = _changelog(tmp_path, f"# config/hubs.yaml\n\n## 1 — 2026-10-04\nтекст\n{body}")
    with pytest.raises(ConfigError, match=r"hubs\.yaml"):
        changelog_entries(log, "hubs.yaml")


def test_trailing_blank_lines_after_sha_are_fine(tmp_path):
    log = _changelog(tmp_path, f"# config/hubs.yaml\n\n## 1 — 2026-10-04\nтекст\nsha256: {A}\n\n\n")
    assert changelog_entries(log, "hubs.yaml") == {1: A}


def test_entry_in_wrong_section_does_not_count(tmp_path):
    # запис hubs.yaml покладено в розділ hub_addresses.yaml (розділу hubs немає)
    log = _changelog(tmp_path, f"# config/hub_addresses.yaml\n\n## 1 — 2026-10-04\nhubs\nsha256: {SHIPPED_HUBS_SHA}\n")
    with pytest.raises(ConfigError, match=r"hubs\.yaml.*section"):
        check_changelog(THRESHOLDS, log)
    with pytest.raises(ConfigError, match=r"hub_addresses\.yaml.*digest"):
        check_changelog(LISTS, log)


def test_swapped_section_digests_are_detected(tmp_path):
    shipped = CHANGELOG.read_text(encoding="utf-8")
    swapped = (
        shipped.replace(SHIPPED_HUBS_SHA, "@@").replace(SHIPPED_LISTS_SHA, SHIPPED_HUBS_SHA).replace("@@", SHIPPED_LISTS_SHA)
    )
    _, _, log = _shipped_with_changelog(tmp_path, swapped)
    for src in (THRESHOLDS, LISTS):
        with pytest.raises(ConfigError, match=re.escape(src.name)):
            check_changelog(src, log)


def test_section_order_is_irrelevant(tmp_path):
    log = _changelog(
        tmp_path,
        f"# config/ingest.yaml\n\n## 1 — 2026-10-03\nбез sha\n\n"
        f"# config/hub_addresses.yaml\n\n## 1 — 2026-10-04\nсписки\nsha256: {SHIPPED_LISTS_SHA}\n\n"
        f"# config/hubs.yaml\n\n## 1 — 2026-10-04\nпороги\nsha256: {SHIPPED_HUBS_SHA}\n",
    )
    check_changelog(THRESHOLDS, log)
    check_changelog(LISTS, log)


def test_entry_ends_at_next_section_not_end_of_file(tmp_path):
    # у запису hubs немає sha256; наступний розділ має — він не має «прилипнути» до запису hubs
    log = _changelog(
        tmp_path,
        f"# config/hubs.yaml\n\n## 1 — 2026-10-04\nпороги без sha\n\n"
        f"# config/hub_addresses.yaml\n\n## 1 — 2026-10-04\nсписки\nsha256: {SHIPPED_HUBS_SHA}\n",
    )
    with pytest.raises(ConfigError, match=r"hubs\.yaml.*version 1.*sha256"):
        changelog_entries(log, "hubs.yaml")


def test_duplicate_versions_are_rejected(tmp_path):
    log = _changelog(
        tmp_path, f"# config/hubs.yaml\n\n## 1 — 2026-10-04\nx\nsha256: {A}\n\n## 1 — 2026-10-05\ny\nsha256: {B}\n"
    )
    with pytest.raises(ConfigError, match=r"hubs\.yaml.*version 1.*duplicate"):
        changelog_entries(log, "hubs.yaml")


def test_non_monotonic_versions_are_rejected(tmp_path):
    log = _changelog(
        tmp_path, f"# config/hubs.yaml\n\n## 2 — 2026-10-05\nx\nsha256: {A}\n\n## 1 — 2026-10-04\ny\nsha256: {B}\n"
    )
    with pytest.raises(ConfigError, match=r"hubs\.yaml.*increasing"):
        changelog_entries(log, "hubs.yaml")


def test_version_gaps_are_allowed_but_order_is_not(tmp_path):
    log = _changelog(
        tmp_path, f"# config/hubs.yaml\n\n## 1 — 2026-10-04\nx\nsha256: {A}\n\n## 3 — 2026-10-05\ny\nsha256: {B}\n"
    )
    assert changelog_entries(log, "hubs.yaml") == {1: A, 3: B}


@pytest.mark.parametrize(
    "header",
    ["## v1 — 2026-10-04", "## 1", "## 1 2026-10-04", "## 0 — 2026-10-04", "## — 2026-10-04"],
)
def test_malformed_entry_header_is_rejected(tmp_path, header):
    log = _changelog(tmp_path, f"# config/hubs.yaml\n\n{header}\nx\nsha256: {A}\n")
    with pytest.raises(ConfigError, match=r"hubs\.yaml.*header"):
        changelog_entries(log, "hubs.yaml")


def test_text_before_first_entry_in_section_is_rejected(tmp_path):
    log = _changelog(tmp_path, f"# config/hubs.yaml\nпреамбула\n\n## 1 — 2026-10-04\nx\nsha256: {A}\n")
    with pytest.raises(ConfigError, match=r"hubs\.yaml"):
        changelog_entries(log, "hubs.yaml")


def test_empty_changelog_is_rejected(tmp_path):
    log = _changelog(tmp_path, "")
    with pytest.raises(ConfigError, match=r"hubs\.yaml.*section"):
        changelog_entries(log, "hubs.yaml")


def test_changelog_without_section_of_file_is_rejected(tmp_path):
    log = _changelog(tmp_path, f"# config/ingest.yaml\n\n## 1 — 2026-10-03\nx\nsha256: {A}\n")
    with pytest.raises(ConfigError, match=r"hubs\.yaml.*section"):
        changelog_entries(log, "hubs.yaml")


def test_section_without_entries_is_rejected(tmp_path):
    log = _changelog(tmp_path, "# config/hubs.yaml\n\n# config/hub_addresses.yaml\n")
    with pytest.raises(ConfigError, match=r"hubs\.yaml.*no entries"):
        changelog_entries(log, "hubs.yaml")


def test_duplicate_section_is_rejected(tmp_path):
    log = _changelog(
        tmp_path,
        f"# config/hubs.yaml\n\n## 1 — 2026-10-04\nx\nsha256: {A}\n\n# config/hubs.yaml\n\n## 2 — 2026-10-05\ny\nsha256: {B}\n",
    )
    with pytest.raises(ConfigError, match=r"hubs\.yaml.*section.*twice"):
        changelog_entries(log, "hubs.yaml")


def test_section_name_must_match_exactly(tmp_path):
    # `# config/hubs.yaml.bak` чи `# config/xhubs.yaml` — не розділ hubs.yaml
    log = _changelog(
        tmp_path,
        f"# config/hubs.yaml.bak\n\n## 1 — 2026-10-04\nx\nsha256: {SHIPPED_HUBS_SHA}\n\n"
        f"# config/xhubs.yaml\n\n## 1 — 2026-10-04\nx\nsha256: {SHIPPED_HUBS_SHA}\n",
    )
    with pytest.raises(ConfigError, match=r"hubs\.yaml.*section"):
        check_changelog(THRESHOLDS, log)


def test_missing_changelog_file_is_config_error(tmp_path):
    with pytest.raises(ConfigError, match=r"CHANGELOG"):
        changelog_entries(tmp_path / "CHANGELOG.md", "hubs.yaml")


@pytest.mark.parametrize("version", ["0", "'1'", "true", "1.0"])
def test_check_rejects_file_with_invalid_version(tmp_path, version):
    text = THRESHOLDS.read_text(encoding="utf-8").replace("\nversion: 1\n", f"\nversion: {version}\n")
    # саме відмова за типом/межею версії, а не випадковий збіг (True == 1 у словнику записів)
    with pytest.raises(ConfigError, match=r"hubs\.yaml: version must be an int >= 1"):
        check_changelog(_copy(tmp_path, THRESHOLDS, text), CHANGELOG)


def test_error_messages_do_not_leak_config_values(tmp_path):
    text = THRESHOLDS.read_text(encoding="utf-8")
    changed = _copy(tmp_path, THRESHOLDS, text.replace("degree_threshold: 100 ", "degree_threshold: 98765 "))
    with pytest.raises(ConfigError) as exc:
        check_changelog(changed, CHANGELOG)
    assert "98765" not in str(exc.value) and "degree_threshold" not in str(exc.value)


# --- HubConfig несе дайджести; load_hub_config журнал не читає ----------------------------------------------


def test_hub_config_carries_digests_of_both_files(tmp_path):
    cfg = load_hub_config(THRESHOLDS, LISTS)
    assert (cfg.thresholds_digest, cfg.lists_digest) == (SHIPPED_HUBS_SHA, SHIPPED_LISTS_SHA)
    no_lists = load_hub_config(THRESHOLDS, None)
    assert no_lists.lists is None and no_lists.lists_digest is None
    assert load_hub_config(THRESHOLDS, tmp_path / "absent.yaml").lists_digest is None


def test_load_hub_config_does_not_consult_changelog(tmp_path):
    # рішення R-14 / контракт graph-service §1: журнал перевіряє тест (SC-008), не завантажувач
    text = THRESHOLDS.read_text(encoding="utf-8")
    changed = _copy(tmp_path, THRESHOLDS, text.replace("degree_threshold: 100 ", "degree_threshold: 101 "))
    cfg = load_hub_config(changed, LISTS)
    assert cfg.thresholds.degree_threshold == 101
    assert cfg.thresholds_digest == content_digest(changed) != SHIPPED_HUBS_SHA
