# verifies: FR-002-08, FR-002-12
"""Недовірений текст конфігу й журналу: лише `ConfigError` (або коректний результат), НІКОЛИ інший виняток,
жодного виконання коду, жодного експоненційного розгортання, жодного відлуння значень (ревʼю T-023/T-024 №1, №2).

Підхід — звужена поверхня розбору (`hubs.config._read_yaml`): анкери, аліаси, явні теги й merge `<<` заборонені;
ліміти розміру файла, глибини, кількості вузлів і довжини скаляра перевіряються ДО конструювання значень; усе, що
PyYAML кидає на межі розбору, → `ConfigError("<path>: invalid YAML …")`. Файл списків, що присутній, але
некоректний, — `ConfigError`, а не `lists=None` (R-13, FR-002-12).

Детермінований перебір (фіксовані seed) по ≥ 3000 випадків на кожен із трьох файлів; патологічні входи
генеруються в пам'яті. Мережі немає.
"""

import random
import re
import time
from pathlib import Path

import pytest
import yaml

from unmask.hubs import config as hc
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
SEED = 20261004
FUZZ_ROUNDS = 3000

THRESHOLD_FIELDS = (
    "version",
    "degree_threshold",
    "one_off_senders_share",
    "one_off_min_senders",
    "giant_component_warn_share",
    "prune_off_curve",
    "prune_ingest_high_degree",
    "dust_amount_lamports",  # T-054: hubs.yaml v2 (v3, T-058, полів не додає — лише one_off_min_senders 10 → 50)
    "dust_min_fanout",
)
# Відтворення ревʼю №2, п.1: цілі, що PyYAML розбирає, а int→str (ліміт 4300 цифр) потім падає.
HUGE_NUMBERS = {
    "neg_hex": "-0x" + "f" * 5000,
    "hex": "0x" + "f" * 5000,
    "neg_bin": "-0b" + "1" * 20000,
    "oct": "0o" + "7" * 5000,
    "old_oct": "0" + "7" * 5000,
    "dec": "9" * 5000,
    "sexagesimal": "1" + ":59" * 2000,
    "float": "1." + "9" * 5000,
    "hex_in_list": "[0x" + "f" * 5000 + "]",
}


def _write(tmp_path: Path, name: str, data: bytes | str) -> Path:
    path = tmp_path / name
    path.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
    return path


def _with_field(src: Path, field: str, value: str) -> str:
    lines = src.read_text(encoding="utf-8").splitlines()
    out = [f"{field}: {value}" if ln.startswith(f"{field}:") else ln for ln in lines]
    assert out != lines, field
    return "\n".join(out) + "\n"


def _all_reject(path: Path, *, lists: bool = False) -> list[str]:
    """Кожна публічна точка входу на цьому файлі дає саме ConfigError; повертає повідомлення."""
    calls = [lambda: content_digest(path), lambda: check_changelog(path, CHANGELOG)]
    calls.append((lambda: load_hub_config(THRESHOLDS, path)) if lists else (lambda: load_hub_config(path, LISTS)))
    messages = []
    for call in calls:
        with pytest.raises(ConfigError) as exc:
            call()
        assert path.name in str(exc.value)
        messages.append(str(exc.value).replace(str(path), path.name))  # без випадкових цифр tmp-шляху
    return messages


# --- п.1: величезні числа в кожному полі ---------------------------------------------------------------------


@pytest.mark.parametrize("form", sorted(HUGE_NUMBERS))
@pytest.mark.parametrize("field", THRESHOLD_FIELDS)
def test_huge_number_in_any_threshold_field_is_config_error(tmp_path, field, form):
    path = _write(tmp_path, "hubs.yaml", _with_field(THRESHOLDS, field, HUGE_NUMBERS[form]))
    for message in _all_reject(path):
        assert "fff" not in message and "999" not in message and "777" not in message


@pytest.mark.parametrize("form", sorted(HUGE_NUMBERS))
def test_huge_number_in_lists_is_config_error(tmp_path, form):
    text = LISTS.read_text(encoding="utf-8")
    for variant in (
        text.replace("\nversion: 1\n", f"\nversion: {HUGE_NUMBERS[form]}\n"),
        text.replace("  exchanges: []", f"  exchanges: [{HUGE_NUMBERS[form]}]"),
    ):
        assert variant != text
        _all_reject(_write(tmp_path, "hub_addresses.yaml", variant), lists=True)


def test_changelog_header_with_5000_digits_is_config_error(tmp_path):
    log = _write(tmp_path, "CHANGELOG.md", f"# config/hubs.yaml\n\n## 1{'0' * 5000} — 2026-10-04\nx\nsha256: {'a' * 64}\n")
    t0 = time.perf_counter()
    with pytest.raises(ConfigError, match=r"hubs\.yaml.*header") as exc:
        changelog_entries(log, "hubs.yaml")
    assert time.perf_counter() - t0 < 1
    assert "00000" not in str(exc.value)


def test_changelog_version_digit_limit(tmp_path):
    ok = _write(tmp_path, "CHANGELOG.md", f"# config/hubs.yaml\n\n## 999999999 — d\nx\nsha256: {'a' * 64}\n")
    assert changelog_entries(ok, "hubs.yaml") == {999999999: "a" * 64}
    bad = _write(tmp_path, "CHANGELOG.md", f"# config/hubs.yaml\n\n## 1000000000 — d\nx\nsha256: {'a' * 64}\n")
    with pytest.raises(ConfigError, match="header"):
        changelog_entries(bad, "hubs.yaml")


def test_check_changelog_rejects_version_beyond_journal_range(tmp_path):
    path = _write(tmp_path, "hubs.yaml", _with_field(THRESHOLDS, "version", "1000000000"))
    with pytest.raises(ConfigError, match=r"hubs\.yaml: version must be an int >= 1 and <= 999999999"):
        check_changelog(path, CHANGELOG)


# --- п.2: явні теги, виконання коду --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["!!int", "!!float", "!!int _", "!!timestamp x", "!!bool x", "!!str 1", "!!int 100", "! 1", "!custom 1",
     "!!binary aGVsbG8=", "!!set {a}", "!!omap [a: 1]", "!<tag:yaml.org,2002:int> 1"],
)
def test_explicit_tags_are_config_error(tmp_path, value):
    _all_reject(_write(tmp_path, "hubs.yaml", _with_field(THRESHOLDS, "degree_threshold", value)))


def test_explicit_tag_on_collection_is_config_error(tmp_path):
    text = LISTS.read_text(encoding="utf-8").replace("  exchanges: []", "  exchanges: !!seq []")
    _all_reject(_write(tmp_path, "hub_addresses.yaml", text), lists=True)


@pytest.mark.parametrize(
    "template",
    [
        'degree_threshold: !!python/object/apply:os.system ["touch {m}"]',
        'degree_threshold: !!python/object/apply:subprocess.call [["touch", "{m}"]]',
        "degree_threshold: !!python/name:os.system",
        'degree_threshold: !!python/object/new:os.system ["touch {m}"]',
    ],
)
def test_python_tags_never_execute(tmp_path, template):
    marker = tmp_path / "marker"
    line = template.format(m=marker)
    text = "\n".join(line if ln.startswith("degree_threshold:") else ln
                     for ln in THRESHOLDS.read_text(encoding="utf-8").splitlines())
    _all_reject(_write(tmp_path, "hubs.yaml", text))
    assert not marker.exists()


# --- п.3: анкери, аліаси, billion laughs, merge --------------------------------------------------------------


def _laughs(levels: int) -> str:
    rows = ["x0: &x0 [" + ", ".join(["a"] * 10) + "]"]
    rows += [f"x{i}: &x{i} [" + ", ".join([f"*x{i - 1}"] * 10) + "]" for i in range(1, levels)]
    return "\n".join(rows) + "\n"


@pytest.mark.parametrize("levels", [2, 8, 12])
def test_billion_laughs_is_rejected_fast(tmp_path, levels):
    path = _write(tmp_path, "hubs.yaml", _laughs(levels))
    t0 = time.perf_counter()
    for message in _all_reject(path):
        assert "anchor" in message or "alias" in message
    assert time.perf_counter() - t0 < 1.0


@pytest.mark.parametrize(
    "text, reason",
    [("a: &x 1\nb: *x\n", "anchors"), ("a: &x 1\n", "anchors"), ("a: *x\n", "aliases"),
     ("a: &x [1]\nb: 2\n", "anchors"), ("&top {a: 1}\n", "anchors")],
    ids=["anchor+alias", "anchor_only", "alias_only", "anchor_on_seq", "anchor_on_root"],
)
def test_any_anchor_or_alias_is_config_error(tmp_path, text, reason):
    path = _write(tmp_path, "hubs.yaml", text)
    with pytest.raises(ConfigError, match=rf"hubs\.yaml: invalid YAML: {reason} are not allowed"):
        content_digest(path)


@pytest.mark.parametrize("text", ["a: 1\n<<: {b: 2}\n", "base: &b {x: 1}\nother:\n  <<: *b\n", "a: <<\n"])
def test_merge_key_is_config_error(tmp_path, text):
    # Свідома зміна (ревʼю №2): прості конфіги merge не потребують; раніше inline-merge приймався.
    path = _write(tmp_path, "hubs.yaml", text)
    with pytest.raises(ConfigError, match=r"hubs\.yaml: invalid YAML: (merge keys|anchors) are not allowed"):
        content_digest(path)
    if "&" not in text:
        with pytest.raises(ConfigError, match="merge keys are not allowed"):
            content_digest(path)


def test_quoted_merge_like_key_is_a_plain_string(tmp_path):
    path = _write(tmp_path, "x.yaml", "'<<': 1\n")
    content_digest(path)


# --- ліміти ---------------------------------------------------------------------------------------------------


def test_nesting_depth_limit(tmp_path):
    ok = _write(tmp_path, "a.yaml", "[" * hc._MAX_DEPTH + "]" * hc._MAX_DEPTH)
    content_digest(ok)
    deep = _write(tmp_path, "b.yaml", "[" * (hc._MAX_DEPTH + 1) + "]" * (hc._MAX_DEPTH + 1))
    with pytest.raises(ConfigError, match=r"b\.yaml.*depth"):
        content_digest(deep)


@pytest.mark.parametrize("text", ["[" * 100000, "{a: " * 50000, "a:\n" + "".join(" " * i + "- \n" for i in range(1, 3000))])
def test_pathological_nesting_is_rejected_fast(tmp_path, text):
    path = _write(tmp_path, "hubs.yaml", text)
    t0 = time.perf_counter()
    _all_reject(path)
    assert time.perf_counter() - t0 < 5.0


def test_node_count_limit(tmp_path):
    n = hc._MAX_NODES - 1  # + сама послідовність
    ok = _write(tmp_path, "a.yaml", "[" + ",".join(["1"] * n) + "]")
    content_digest(ok)
    big = _write(tmp_path, "b.yaml", "[" + ",".join(["1"] * (n + 1)) + "]")
    with pytest.raises(ConfigError, match=r"b\.yaml: invalid YAML: too many nodes"):  # уже на розборі
        content_digest(big)


@pytest.mark.parametrize("quote", ["", '"', "'"])
def test_scalar_length_limit(tmp_path, quote):
    ok = _write(tmp_path, "a.yaml", f"a: {quote}{'x' * hc._MAX_SCALAR}{quote}\n")
    content_digest(ok)
    long = _write(tmp_path, "b.yaml", f"a: {quote}{'x' * (hc._MAX_SCALAR + 1)}{quote}\n")
    with pytest.raises(ConfigError, match=r"b\.yaml.*scalar") as exc:
        content_digest(long)
    assert "xxx" not in str(exc.value)


def test_long_key_is_limited_too(tmp_path):
    path = _write(tmp_path, "a.yaml", f"{'k' * (hc._MAX_SCALAR + 1)}: 1\n")
    with pytest.raises(ConfigError, match="scalar"):
        content_digest(path)


def test_file_size_limit_checked_before_parsing(tmp_path):
    body = THRESHOLDS.read_bytes()
    pad = hc._MAX_FILE_BYTES - len(body)
    ok = _write(tmp_path, "hubs.yaml", body + b"#" + b"x" * (pad - 2) + b"\n")
    assert ok.stat().st_size == hc._MAX_FILE_BYTES
    assert content_digest(ok) == content_digest(THRESHOLDS)
    big = _write(tmp_path, "hubs.yaml", body + b"#" + b"x" * (pad - 1) + b"\n")
    _all_reject(big)
    # навіть невалідний YAML понад ліміт відхиляється за розміром, а не розбором
    junk = _write(tmp_path, "hubs.yaml", b"[" * (hc._MAX_FILE_BYTES + 1))
    with pytest.raises(ConfigError, match="too large"):
        content_digest(junk)


def test_oversized_file_is_rejected_without_reading_it(tmp_path, monkeypatch):
    big = _write(tmp_path, "hubs.yaml", b"#" * (hc._MAX_FILE_BYTES + 1))

    def _no_read(fd, n):
        raise AssertionError("oversized file must be rejected by size before reading")

    monkeypatch.setattr(hc, "_read_at_most", _no_read)
    with pytest.raises(ConfigError, match=r"hubs\.yaml: file too large"):
        content_digest(big)
    log = _write(tmp_path, "CHANGELOG.md", b"#" * (hc._MAX_CHANGELOG_BYTES + 1))
    with pytest.raises(ConfigError, match=r"CHANGELOG\.md.*file too large"):
        changelog_entries(log, "hubs.yaml")


def test_strict_loader_constructor_layer_is_safe_even_without_tag_ban():
    # другий рубіж: навіть якщо тег якось пройде композицію, конструктор — SafeLoader і python/* не знає
    assert issubclass(hc._StrictLoader, yaml.SafeLoader)
    loader = hc._StrictLoader("")
    try:
        node = yaml.ScalarNode("tag:yaml.org,2002:python/name:os.system", "")
        with pytest.raises(yaml.constructor.ConstructorError):
            loader.construct_document(node)
    finally:
        loader.dispose()


def test_canonical_digest_catches_any_dump_failure():
    # ціле понад ліміт int→str (ValueError у json.dumps) — теж ConfigError, не лише TypeError
    with pytest.raises(ConfigError, match=r"mem\.yaml"):
        hc._canonical_digest({"x": 10**5000}, "mem.yaml")
    with pytest.raises(ConfigError, match=r"mem\.yaml"):
        hc._canonical_digest({"x": {1, 2}}, "mem.yaml")


def test_address_longer_than_64_is_not_valid_address(tmp_path):
    text = LISTS.read_text(encoding="utf-8").replace("  exchanges: []", f'  exchanges: ["{"1" * 65}"]')
    path = _write(tmp_path, "hub_addresses.yaml", text)
    with pytest.raises(ConfigError, match="exchanges: not a valid address") as exc:
        load_hub_config(THRESHOLDS, path)
    assert "1" * 65 not in str(exc.value)


# --- без відлуння значень -----------------------------------------------------------------------------------


@pytest.mark.parametrize("field", THRESHOLD_FIELDS)
def test_wrong_type_message_names_field_and_type_but_not_value(tmp_path, field):
    path = _write(tmp_path, "hubs.yaml", _with_field(THRESHOLDS, field, '"SECRETVALUE"'))
    with pytest.raises(ConfigError, match=field) as exc:
        load_hub_config(path)
    assert "SECRETVALUE" not in str(exc.value) and "str" in str(exc.value)


@pytest.mark.parametrize(
    "field, value",
    [("degree_threshold", "-77777"), ("one_off_min_senders", "1"), ("one_off_senders_share", "7.7777"),
     ("giant_component_warn_share", "-0.7777"), ("version", "0")],
)
def test_out_of_range_message_does_not_echo_value(tmp_path, field, value):
    path = _write(tmp_path, "hubs.yaml", _with_field(THRESHOLDS, field, value))
    with pytest.raises(ConfigError, match=field) as exc:
        load_hub_config(path)
    assert value not in str(exc.value).split(":", 1)[1]


def test_invalid_address_and_non_list_messages_do_not_echo(tmp_path):
    text = LISTS.read_text(encoding="utf-8")
    bad_addr = _write(tmp_path, "hub_addresses.yaml", text.replace("  exchanges: []", '  exchanges: ["SECRET0"]'))
    with pytest.raises(ConfigError, match="exchanges: not a valid address") as exc:
        load_hub_config(THRESHOLDS, bad_addr)
    assert "SECRET0" not in str(exc.value)
    not_list = _write(tmp_path, "hub_addresses.yaml", text.replace("  exchanges: []", "  exchanges: SECRET1"))
    with pytest.raises(ConfigError, match="exchanges") as exc:
        load_hub_config(THRESHOLDS, not_list)
    assert "SECRET1" not in str(exc.value)


def test_unknown_odd_key_is_not_echoed(tmp_path):
    text = THRESHOLDS.read_text(encoding="utf-8") + '\n"SECRET KEY with spaces": 1\n'
    with pytest.raises(ConfigError, match="unknown field") as exc:
        load_hub_config(_write(tmp_path, "hubs.yaml", text))
    assert "SECRET" not in str(exc.value)


def test_duplicate_odd_key_is_not_echoed(tmp_path):
    path = _write(tmp_path, "a.yaml", '"SECRET KEY": 1\n"SECRET KEY": 2\n')
    with pytest.raises(ConfigError, match="duplicate key") as exc:
        content_digest(path)
    assert "SECRET" not in str(exc.value)


def test_malformed_changelog_header_is_not_echoed(tmp_path):
    log = _write(tmp_path, "CHANGELOG.md", f"# config/hubs.yaml\n\n## SECRET — x\nsha256: {'a' * 64}\n")
    with pytest.raises(ConfigError, match="header") as exc:
        changelog_entries(log, "hubs.yaml")
    assert "SECRET" not in str(exc.value)


# --- раніші відтворення (ревʼю №1) -------------------------------------------------------------------------


@pytest.mark.parametrize("src", [THRESHOLDS, LISTS], ids=["hubs", "hub_addresses"])
def test_non_utf8_byte_is_config_error_with_path_not_lists_none(tmp_path, src):
    bad = _write(tmp_path, src.name, src.read_bytes() + b"\n# \xff\n")
    for message in _all_reject(bad, lists=src is LISTS):
        assert "UTF-8" in message and "\\xff" not in message


@pytest.mark.parametrize("encoding", ["cp1251", "utf-16", "utf-16-le"])
@pytest.mark.parametrize("src", [THRESHOLDS, LISTS, CHANGELOG], ids=["hubs", "hub_addresses", "changelog"])
def test_file_saved_in_other_encoding_is_config_error(tmp_path, src, encoding):
    bad = _write(tmp_path, src.name, src.read_text(encoding="utf-8").encode(encoding, errors="replace"))
    with pytest.raises(ConfigError, match=src.name):
        if src is CHANGELOG:
            changelog_entries(bad, "hubs.yaml")
        else:
            load_hub_config(*((bad, LISTS) if src is THRESHOLDS else (THRESHOLDS, bad)))


def test_changelog_non_utf8_is_config_error_in_check(tmp_path):
    bad = _write(tmp_path, "CHANGELOG.md", CHANGELOG.read_bytes() + b"\n\xff\n")
    with pytest.raises(ConfigError, match=r"CHANGELOG\.md.*UTF-8"):
        check_changelog(THRESHOLDS, bad)


def test_utf8_bom_is_accepted_as_same_content(tmp_path):
    for src in (THRESHOLDS, LISTS):
        bom = _write(tmp_path, src.name, b"\xef\xbb\xbf" + src.read_bytes())
        assert content_digest(bom) == content_digest(src)
    load_hub_config(tmp_path / THRESHOLDS.name, tmp_path / LISTS.name)


@pytest.mark.parametrize("key", ["one_off_senders_share", "giant_component_warn_share"])
@pytest.mark.parametrize("sign", ["", "-"])
def test_huge_decimal_share_within_scalar_limit_is_config_error_with_field(tmp_path, key, sign):
    # 400 цифр — у межах довжини скаляра, але поза float: відмова за межами частки, не OverflowError
    path = _write(tmp_path, "hubs.yaml", _with_field(THRESHOLDS, key, f"{sign}1{'0' * 400}"))
    with pytest.raises(ConfigError, match=key):
        load_hub_config(path)


def test_duplicate_key_in_thresholds_is_config_error_with_key(tmp_path):
    bad = _write(tmp_path, "hubs.yaml", THRESHOLDS.read_text(encoding="utf-8") + "\ndegree_threshold: 5\n")
    for message in _all_reject(bad):
        assert "duplicate key 'degree_threshold'" in message


def test_duplicate_key_in_lists_is_config_error_with_key(tmp_path):
    bad = _write(tmp_path, "hub_addresses.yaml", LISTS.read_text(encoding="utf-8") + "\n  exchanges: []\n")
    for message in _all_reject(bad, lists=True):
        assert "duplicate key 'exchanges'" in message


@pytest.mark.parametrize(
    "text",
    ["version: 1\nwhen: 2026-10-04\n", "version: 1\n1: a\nb: c\n", "version: 1\nwhen: 2026-10-04 12:00:00\n",
     "version: 1\nwhen: 2026-13-45\n", "version: 1\nx: .nan\n", "version: 1\nx: [.inf, -.inf]\n"],
)
def test_non_canonicalizable_or_odd_content_is_config_error_or_digest(tmp_path, text):
    path = _write(tmp_path, "hubs.yaml", text)
    try:
        content_digest(path)
    except ConfigError as exc:
        assert "hubs.yaml" in str(exc)
    with pytest.raises(ConfigError, match=r"hubs\.yaml"):
        check_changelog(path, CHANGELOG)


def test_nul_byte_is_config_error(tmp_path):
    # версії поставлених файлів різні (hubs.yaml v3 з T-058, hub_addresses.yaml v1)
    for src, line in ((THRESHOLDS, b"version: 3"), (LISTS, b"version: 1")):
        data = src.read_bytes()
        assert data.count(b"\n" + line + b"\n") == 1  # інакше заміна нижче мовчки нічого б не зробила
        bad = _write(tmp_path, src.name, data.replace(line, line + b"\x00", 1))
        _all_reject(bad, lists=src is LISTS)


def test_canonical_digest_refuses_oversized_structure_before_dumping():
    # захист (В): навіть якщо структура прийшла не з _read_yaml, ліміт вузлів перевіряється до json.dumps
    shared = ["a"] * 10
    for _ in range(8):
        shared = [shared] * 10  # 10^9 листків через спільні посилання
    t0 = time.perf_counter()
    with pytest.raises(ConfigError, match="nodes"):
        hc._canonical_digest({"x": shared}, "mem.yaml")
    assert time.perf_counter() - t0 < 1.0


# --- детермінований перебір --------------------------------------------------------------------------------

_JUNK = [
    b"[", b"{", b": ", b"- ", b"&a ", b"*a", b"<<: ", b"? ", b"|", b"\t", b"%YAML 1.1\n", b"---\n", b"...\n",
    b"!!python/object/apply:os.system [\"true\"] ", b"!!int ", b"!!float ", b"!!timestamp x ", b"!!bool x ", b"! ",
    b"0x" + b"f" * 5000, b"-0b" + b"1" * 6000, b"0o" + b"7" * 5000, b"9" * 5000, b"1" + b":59" * 2000,
    b"2026-10-04", b"2026-13-45", b"null", b"~", b".nan", b"-.inf", b"1e999", b"yes", b"\x00", b"\xff", b"\xef\xbb\xbf",
    # escape-послідовності в лапках (ревʼю №3): одиночні сурогати, неписьмові, межі діапазону
    b'"\\ud800"', b'"\\udfff"', b'"\\U0010ffff"', b'"\\U0000d800"', b'"\\x00"', b'"\\x85"', b'"\\uffff"',
    b'"\\ufffe"', b'"\\u"', b'"\\U"', b'"\\x"', b'"a\\ud800b"', b'"\\ud83d\\ude00"', b"\xed\xa0\x80",
]


def _mutate(rng: random.Random, data: bytes) -> bytes:
    kind = rng.randrange(10)
    if kind == 0:  # перевернути байти
        buf = bytearray(data)
        for _ in range(rng.randint(1, 8)):
            buf[rng.randrange(len(buf))] = rng.randrange(256)
        return bytes(buf)
    if kind == 1:  # усічення
        return data[: rng.randrange(len(data) + 1)]
    if kind == 2:  # вставка випадкових байтів
        pos = rng.randrange(len(data) + 1)
        return data[:pos] + bytes(rng.randrange(256) for _ in range(rng.randint(1, 32))) + data[pos:]
    if kind == 3:  # інше кодування
        return data.decode("utf-8").encode(rng.choice(["cp1251", "utf-16", "utf-16-be", "latin-1"]), errors="replace")
    if kind == 4:  # BOM
        return rng.choice([b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff"]) + data
    if kind == 5:  # переставити/продублювати рядки
        lines = data.split(b"\n")
        i, j = rng.randrange(len(lines)), rng.randrange(len(lines))
        if rng.random() < 0.5:
            lines[i], lines[j] = lines[j], lines[i]
        else:
            lines.insert(j, lines[i])
        return b"\n".join(lines)
    if kind == 6:  # замінити значення рядка «ключ: значення» на щось із відтворень ревʼю
        lines = data.split(b"\n")
        idx = [i for i, ln in enumerate(lines) if b": " in ln]
        if idx:
            i = rng.choice(idx)
            lines[i] = lines[i].split(b": ", 1)[0] + b": " + rng.choice(_JUNK)
        return b"\n".join(lines)
    if kind == 7:  # billion laughs / анкери
        return data + b"\n" + _laughs(rng.randint(1, 6)).encode()
    if kind == 8:  # глибока вкладеність
        n = rng.randint(1, 60)
        # будь-яка версія (hubs.yaml v3, hub_addresses.yaml v1): із літералом `version: 1` заміна на v2 мовчки
        # перестала б щось робити й перебір глибини для hubs.yaml вироджувався б
        return re.sub(rb"version: (\d+)", lambda m: b"version: " + b"[" * n + m.group(1) + b"]" * n, data, count=1)
    pos = rng.randrange(len(data) + 1)  # вставка YAML-сміття
    return data[:pos] + rng.choice(_JUNK) * rng.randint(1, 3) + data[pos:]


def _swallow(call) -> bool:
    try:
        call()
    except ConfigError:
        return False
    return True


def _compact(src: Path) -> bytes:
    """Той самий вміст без коментарів (той самий дайджест): основа більшості раундів — у рази швидше за
    розбір повного файла з коментарями; повний файл — кожен 10-й раунд (коментарі покриті й окремими тестами)."""
    return yaml.safe_dump(yaml.safe_load(src.read_text(encoding="utf-8")), sort_keys=False).encode("utf-8")


def _run_fuzz(tmp_path: Path, src: Path, round_call) -> tuple[int, int]:
    rng = random.Random(f"{SEED}:{src.name}")
    full = src.read_bytes()
    compact = full if src is CHANGELOG else _compact(src)
    path = tmp_path / "fuzz" / src.name
    path.parent.mkdir()
    ok = 0
    for i in range(FUZZ_ROUNDS):
        data = _mutate(rng, full if i % 10 == 1 else compact)
        path.write_bytes(data)
        try:
            ok += round_call(path, i)
        except Exception as exc:  # noqa: BLE001 — саме це й перевіряємо: жодного іншого винятку
            pytest.fail(f"round {i}: {type(exc).__name__}: {exc!r:.200} on input {data[:120]!r}")
    return ok, FUZZ_ROUNDS - ok


@pytest.fixture
def compact_pair(tmp_path):
    """Компактні копії поставлених файлів під їхніми іменами (дайджест той самий — журнал їх приймає)."""
    base = tmp_path / "base"
    base.mkdir()
    pair = {}
    for src in (THRESHOLDS, LISTS):
        (base / src.name).write_bytes(_compact(src))
        pair[src.name] = base / src.name
    check_changelog(pair["hubs.yaml"], CHANGELOG)
    check_changelog(pair["hub_addresses.yaml"], CHANGELOG)
    return pair


def test_fuzz_thresholds_only_config_error_or_valid_result(tmp_path, compact_pair):
    calls = (lambda p: load_hub_config(p), lambda p: check_changelog(p, CHANGELOG), content_digest)

    def round_call(path, i):  # точки входу по колу: кожна отримує ≥ 1000 випадків, спільна межа — _read_yaml
        return _swallow(lambda: calls[i % 3](path))

    ok, rejected = _run_fuzz(tmp_path, THRESHOLDS, round_call)
    assert ok > 0 and rejected > 0  # перебір і ламає, і лишає частину входів валідними


def test_fuzz_address_lists_only_config_error_or_valid_result(tmp_path, compact_pair):
    thresholds = compact_pair["hubs.yaml"]

    calls = (lambda p: load_hub_config(thresholds, p), lambda p: check_changelog(p, CHANGELOG), content_digest)

    def round_call(path, i):
        return _swallow(lambda: calls[i % 3](path))

    ok, rejected = _run_fuzz(tmp_path, LISTS, round_call)
    assert ok > 0 and rejected > 0


def test_fuzz_changelog_only_config_error_or_valid_result(tmp_path, compact_pair):
    lists = compact_pair["hub_addresses.yaml"]

    calls = (
        lambda p: changelog_entries(p, "hubs.yaml"),
        lambda p: changelog_entries(p, "hub_addresses.yaml"),
        lambda p: check_changelog(lists, p),
    )

    def round_call(path, i):
        return _swallow(lambda: calls[i % 3](path))

    ok, rejected = _run_fuzz(tmp_path, CHANGELOG, round_call)
    assert ok > 0 and rejected > 0



# --- ревʼю №3, п.1: сурогати й неписьмові символи з escape-послідовностей -----------------------------------------

SURROGATE_ESCAPES = ['"\\ud800"', '"\\udfff"', '"\\U0000d800"', '"a\\ud800b"', '"\\ud83d\\ude00"', '"\\uffff"',
                     '"\\ufffe"', '"\\x00"', '"\\x07"']


@pytest.mark.parametrize("escape", SURROGATE_ESCAPES)
def test_surrogate_or_nonprintable_escape_in_thresholds_is_config_error(tmp_path, escape):
    text = THRESHOLDS.read_text(encoding="utf-8") + f"\nnote: {escape}\n"
    path = _write(tmp_path, "hubs.yaml", text)
    for message in _all_reject(path):
        assert "non-printable" in message


@pytest.mark.parametrize("escape", SURROGATE_ESCAPES)
def test_surrogate_escape_as_address_or_key_is_config_error(tmp_path, escape):
    text = LISTS.read_text(encoding="utf-8")
    _all_reject(_write(tmp_path, "hub_addresses.yaml", text.replace("  exchanges: []", f"  exchanges: [{escape}]")), lists=True)
    _all_reject(_write(tmp_path, "hubs.yaml", THRESHOLDS.read_text(encoding="utf-8") + f"\n{escape}: 1\n"))


@pytest.mark.parametrize("escape", ['"\\U0010ffff"', '"\\x85"', '"\\u00e9"', '"\\t"'])
def test_printable_escapes_are_accepted(tmp_path, escape):
    path = _write(tmp_path, "x.yaml", f"note: {escape}\n")
    content_digest(path)


def test_canonical_digest_wraps_encode_failure():
    # другий рубіж: навіть якщо сурогат дійде до дайджесту оминаючи розбір — ConfigError, не UnicodeEncodeError
    with pytest.raises(ConfigError, match=r"mem\.yaml"):
        hc._canonical_digest({"note": "\ud800"}, "mem.yaml")


def test_encoded_surrogate_bytes_in_changelog_is_config_error(tmp_path):
    text = CHANGELOG.read_bytes() + b"\n\xed\xa0\x80\n"  # UTF-8-кодування U+D800 — невалідний UTF-8
    log = _write(tmp_path, "CHANGELOG.md", text)
    with pytest.raises(ConfigError, match=r"CHANGELOG\.md.*UTF-8"):
        changelog_entries(log, "hubs.yaml")
    with pytest.raises(ConfigError, match=r"CHANGELOG\.md.*UTF-8"):
        check_changelog(THRESHOLDS, log)


def test_escape_text_in_changelog_is_just_text(tmp_path):
    shipped = CHANGELOG.read_text(encoding="utf-8")
    log = _write(tmp_path, "CHANGELOG.md", shipped.replace("Початкова версія, не", "Початкова \\ud800 версія, не", 1))
    assert changelog_entries(log, "hubs.yaml") == changelog_entries(CHANGELOG, "hubs.yaml")
    check_changelog(THRESHOLDS, log)


# --- ревʼю №3, п.2: синтаксично зламані файли — повідомлення без вмісту файла -----------------------------------

MARK = "SECRETVALUE"
BROKEN_YAML = {
    "unclosed_bracket": f"degree_threshold: [{MARK}, 1\n",
    "unclosed_brace": f"degree_threshold: {{{MARK}: 1\n",
    "tab_indent": f"degree_threshold:\n\t{MARK}: 1\n",
    "broken_double_quote": f'degree_threshold: "{MARK}\n',
    "broken_single_quote": f"degree_threshold: '{MARK}\n",
    "bad_indent": f"degree_threshold:\n  a: 1\n {MARK}: 2\n",
    "mapping_in_plain": f"degree_threshold: {MARK}: [\n",
    "bad_escape": f'degree_threshold: "{MARK}\\q"\n',
    "stray_colon_block": f"- {MARK}\ndegree_threshold: 1\n",
    "undefined_tag_handle": f"degree_threshold: !x!{MARK} 1\n",
    "undefined_tag_handle_named": f"degree_threshold: !{MARK}!x 1\n",  # problem PyYAML містить хендл
    "bad_anchor_char": f"degree_threshold: &{MARK}\x01 1\n",
    "bad_directive": f"%{MARK} 1\n---\ndegree_threshold: 1\n",
}


@pytest.mark.parametrize("case", sorted(BROKEN_YAML))
@pytest.mark.parametrize("src", [THRESHOLDS, LISTS], ids=["hubs", "hub_addresses"])
def test_broken_yaml_message_does_not_echo_file_content(tmp_path, src, case):
    text = src.read_text(encoding="utf-8") + "\n" + BROKEN_YAML[case]
    path = _write(tmp_path, src.name, text)
    for message in _all_reject(path, lists=src is LISTS):
        assert MARK not in message, message
        for line in text.splitlines():  # і жодного іншого непорожнього рядка файла
            if len(line.strip()) > 12:
                assert line.strip() not in message, (line, message)


@pytest.mark.parametrize(
    "text",
    [
        f"# config/hubs.yaml\n\n## {MARK} — 2026-10-04\nx\nsha256: {'a' * 64}\n",
        f"# config/hubs.yaml\n{MARK}\n\n## 1 — 2026-10-04\nx\nsha256: {'a' * 64}\n",
        f"# config/hubs.yaml\n\n## 1 — 2026-10-04\nx\nsha256: {MARK}\n",
        f"# config/hubs.yaml\n\n## 1 — 2026-10-04\n{MARK}\n",
        f"# config/hubs.yaml\n\n## 1 {MARK}\nx\nsha256: {'a' * 64}\n",
        f"# config/{MARK}.yaml\n",
    ],
    ids=["header", "preamble", "sha", "no_sha", "header_no_dash", "no_section"],
)
def test_broken_changelog_message_does_not_echo_content(tmp_path, text):
    log = _write(tmp_path, "CHANGELOG.md", text)
    for call in (lambda: changelog_entries(log, "hubs.yaml"), lambda: check_changelog(THRESHOLDS, log)):
        with pytest.raises(ConfigError) as exc:
            call()
        assert MARK not in str(exc.value), str(exc.value)


# --- ревʼю №3, п.3: журнал — лінійний час, ліміт розміру, обмежене читання ------------------------------------------


def _long_changelog(n: int) -> bytes:
    rows = ["# config/hubs.yaml", ""]
    for v in range(1, n + 1):
        rows += [f"## {v} — 2026-10-04", f"sha256: {'a' * 64}", ""]
    return "\n".join(rows).encode()


def test_changelog_with_20000_entries_is_parsed_in_linear_time(tmp_path, monkeypatch):
    # 20 000 записів (~1,9 МБ) більші за ліміт розміру — тут міряємо складність розбору, тож ліміт піднято
    data = _long_changelog(20_000)
    monkeypatch.setattr(hc, "_MAX_CHANGELOG_BYTES", 4 * 1024 * 1024)
    log = _write(tmp_path, "CHANGELOG.md", data)
    t0 = time.perf_counter()
    entries = changelog_entries(log, "hubs.yaml")
    assert time.perf_counter() - t0 < 1.0
    assert len(entries) == 20_000 and max(entries) == 20_000


def test_changelog_size_limit_boundary(tmp_path):
    shipped = CHANGELOG.read_bytes()
    pad = hc._MAX_CHANGELOG_BYTES - len(shipped)
    ok = _write(tmp_path, "CHANGELOG.md", b"x" * (pad - 1) + b"\n" + shipped)  # преамбула поза розділами
    assert ok.stat().st_size == hc._MAX_CHANGELOG_BYTES
    assert changelog_entries(ok, "hubs.yaml") == changelog_entries(CHANGELOG, "hubs.yaml")
    big = _write(tmp_path, "CHANGELOG.md", b"x" * pad + b"\n" + shipped)
    with pytest.raises(ConfigError, match=r"CHANGELOG\.md.*file too large"):
        changelog_entries(big, "hubs.yaml")
    with pytest.raises(ConfigError, match=r"CHANGELOG\.md.*file too large"):
        check_changelog(THRESHOLDS, big)


def test_changelog_at_size_limit_with_max_entries_is_fast(tmp_path):
    # стільки записів, скільки вміщає ліміт (~11 000), — у межах секунди
    n = hc._MAX_CHANGELOG_BYTES // len(_long_changelog(1))
    data = _long_changelog(n)
    while len(data) > hc._MAX_CHANGELOG_BYTES:
        n -= 200
        data = _long_changelog(n)
    log = _write(tmp_path, "CHANGELOG.md", data)
    t0 = time.perf_counter()
    assert len(changelog_entries(log, "hubs.yaml")) == n
    assert time.perf_counter() - t0 < 1.0


def test_directory_is_still_unavailable_not_config_error(tmp_path):
    d = tmp_path / "hub_addresses.yaml"
    d.mkdir()
    assert load_hub_config(THRESHOLDS, d).lists is None
    with pytest.raises(IsADirectoryError):
        content_digest(d)


def test_changelog_limit_is_well_above_real_needs():
    assert len(CHANGELOG.read_bytes()) * 100 < hc._MAX_CHANGELOG_BYTES


@pytest.mark.parametrize("name", ["hubs.yaml", "CHANGELOG.md"])
def test_sparse_huge_file_is_rejected_fast(tmp_path, name):
    path = tmp_path / name
    with open(path, "wb") as fh:
        fh.truncate(1 << 30)  # 1 ГіБ, розріджений — читати не можна
    t0 = time.perf_counter()
    with pytest.raises(ConfigError, match="file too large"):
        content_digest(path) if name.endswith(".yaml") else changelog_entries(path, "hubs.yaml")
    assert time.perf_counter() - t0 < 1.0


def test_read_is_bounded_even_when_size_is_unknown(tmp_path, monkeypatch):
    # st_size може брехати (/proc, пристрої): читання все одно обмежене limit + 1 байтом
    path = _write(tmp_path, "hubs.yaml", b"#" * (hc._MAX_FILE_BYTES + 10))
    requested = []
    real = hc._read_at_most

    def spy(fd, n):
        requested.append(n)
        return real(fd, n)

    monkeypatch.setattr(hc, "_read_at_most", spy)
    monkeypatch.setattr(hc, "_regular_size", lambda st: 0)  # «розмір невідомий»
    with pytest.raises(ConfigError, match="file too large"):
        content_digest(path)
    assert requested == [hc._MAX_FILE_BYTES + 1]


@pytest.mark.skipif(not hasattr(__import__("os"), "mkfifo"), reason="FIFO недоступні на цій платформі")
@pytest.mark.parametrize("name", ["hubs.yaml", "hub_addresses.yaml", "CHANGELOG.md"])
def test_fifo_is_rejected_without_blocking(tmp_path, name):
    import os

    import signal

    path = tmp_path / name
    os.mkfifo(path)

    def _hung(signum, frame):
        raise AssertionError("відкриття FIFO заблокувалось (потрібен O_NONBLOCK)")

    # без O_NONBLOCK os.open на FIFO без письменника висить назавжди — SIGALRM
    # перетворює зависання на звичайне падіння тесту
    old = signal.signal(signal.SIGALRM, _hung)
    signal.alarm(5)
    t0 = time.perf_counter()
    try:
        with pytest.raises(ConfigError, match="not a regular file"):
            if name == "CHANGELOG.md":
                changelog_entries(path, "hubs.yaml")
            elif name == "hubs.yaml":
                load_hub_config(path)
            else:
                load_hub_config(THRESHOLDS, path)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)
    assert time.perf_counter() - t0 < 1.0


def test_char_device_is_rejected(tmp_path):
    dev = Path("/dev/zero")
    if not dev.exists():
        pytest.skip("/dev/zero недоступний")
    link = tmp_path / "hubs.yaml"
    link.symlink_to(dev)
    with pytest.raises(ConfigError, match="not a regular file"):
        content_digest(link)
