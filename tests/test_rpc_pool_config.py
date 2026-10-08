# verifies: FR-007-09
"""Версіонований `config/rpc_pool.yaml` v1, його журнал і завантажувач `load_rpc_pool_config` (T-137, critical).

Принцип III: перелік провайдерів, профілі, архівність і глибина історії змінюють, ЩО побачить аналіз (чи
дістане пул історію токена), тож живуть у YAML під git, і зміна без запису в `config/CHANGELOG.md` червонить
збірку. Контракт — specs/007-rpc-provider-pool/contracts/rpc-pool-config.md.

Що перевіряється (мережі немає; усе — на поставленому файлі й tmp-копіях):
- golden: поставлені значення (`GOLDEN`) і дайджест (`RPC_POOL_V1_SHA`) зафіксовано в тесті окремо від файла;
  дайджест незалежно обчислюється з `yaml.safe_load` → канонічний JSON → sha256 (правило research R-14 002);
- журнал: розділ `# config/rpc_pool.yaml` ПЕРЕД `# config/ingest.yaml`, ingest — останній; запис `## 1`;
- будь-яка зміна значення (кожен ключ верхнього рівня і кожен ключ кожного провайдера, додавання/видалення/
  перестановка провайдера) без запису → `ConfigError`; коментар файла дайджест не змінює;
- схема завантажувача: невідомий/відсутній ключ, типи, межі, дублікати, `archival ⇔ retention null`, покриття
  чотирьох методів; тексти помилок не відлунюють значень (сторожовий рядок).
"""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import math
import re
from pathlib import Path

import pytest
import yaml

import unmask.ingest.rpc.pool_config as pool_config_module
from unmask.hubs.config import ConfigError as HubsConfigError
from unmask.hubs.config import changelog_entries, check_changelog, content_digest
from unmask.ingest.config import ConfigError
from unmask.ingest.rpc.http import RPC_PROFILES
from unmask.ingest.rpc.pool_config import ProviderSpec, RpcPoolConfig, load_rpc_pool_config

ROOT = Path(__file__).resolve().parents[1]
CHANGELOG = ROOT / "config" / "CHANGELOG.md"
SHIPPED = ROOT / "config" / "rpc_pool.yaml"

# Зафіксований дайджест v1 (golden): змінити його можна лише новою версією файла й записом у журналі.
RPC_POOL_V1_SHA = "3f8c9d6b546f96ed2bbc4332c15fef598037f3e36eabf62e47e6305c7bc6202f"

METHODS = ("getAccountInfo", "getSignaturesForAddress", "getTransaction", "getTokenAccountsByOwner")

# Golden-значення v1 (дослівно з контракту; назви змінних — реальні імена з .env оператора).
GOLDEN = {
    "version": 1,
    "rate_limit_cooldown_seconds": 5,
    "age_cache_entries": 200000,
    "providers": [
        {"name": "rpcfast", "url_env": "UNMASK_RPC_URL", "profile": "rpcfast_start", "archival": False,
         "retention_seconds": 72000, "skip_methods": ["getTokenAccountsByOwner"]},
        {"name": "helius_1", "url_env": "UNMASK_RPC_URL_ALT", "profile": "helius_free", "archival": True,
         "retention_seconds": None, "skip_methods": []},
        {"name": "helius_2", "url_env": "UNMASK_RPC_URL_ALT1", "profile": "helius_free", "archival": True,
         "retention_seconds": None, "skip_methods": []},
        {"name": "helius_3", "url_env": "UNMASK_RPC_URL_ALT2", "profile": "helius_free", "archival": True,
         "retention_seconds": None, "skip_methods": []},
    ],
}
TOP_KEYS = ["version", "rate_limit_cooldown_seconds", "age_cache_entries", "providers"]
PROVIDER_KEYS = ["name", "url_env", "profile", "archival", "retention_seconds", "skip_methods"]
SENTINEL = "SENTINEL-SECRET-4f9c"


def _shipped_data() -> dict:
    return yaml.safe_load(SHIPPED.read_text(encoding="utf-8"))


def _independent_digest(data) -> str:
    """Дайджест за правилом R-14, окремо від `content_digest`."""
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


def _write(tmp_path: Path, data, name: str = "rpc_pool.yaml") -> Path:
    path = tmp_path / name
    path.write_text(yaml.dump(data, sort_keys=False), encoding="utf-8")
    return path


def _journal_copy(tmp_path: Path) -> Path:
    journal = tmp_path / "CHANGELOG.md"
    journal.write_text(CHANGELOG.read_text(encoding="utf-8"), encoding="utf-8")
    return journal


def _load_mutated(tmp_path: Path, mutate) -> RpcPoolConfig:
    data = copy.deepcopy(GOLDEN)
    mutate(data)
    return load_rpc_pool_config(_write(tmp_path, data))


# ---------------------------------------------------------------------------------------------- журнал і golden


def test_shipped_rpc_pool_yaml_passes_check_changelog() -> None:
    check_changelog(SHIPPED, CHANGELOG)


def test_entry_sha_equals_content_digest() -> None:
    entries = changelog_entries(CHANGELOG, "rpc_pool.yaml")
    assert entries == {1: content_digest(SHIPPED)}
    # незалежне обчислення і зафіксований golden збігаються з записом журналу
    assert content_digest(SHIPPED) == _independent_digest(_shipped_data()) == RPC_POOL_V1_SHA
    assert entries[1] == RPC_POOL_V1_SHA


def test_rpc_pool_section_before_ingest_and_ingest_is_last() -> None:
    # ingest — останній розділ, бо наївний розбір `## `-записів tests/test_tx_batch_size.py бере «хвіст» запису 2
    # до кінця файла і вимагає `sha256:` останнім рядком.
    lines = CHANGELOG.read_text(encoding="utf-8").splitlines()
    headings = [ln for ln in lines if ln.startswith("# ")]
    assert headings.count("# config/rpc_pool.yaml") == 1
    assert headings.index("# config/rpc_pool.yaml") < headings.index("# config/ingest.yaml")
    assert headings[-1] == "# config/ingest.yaml"
    start = lines.index("# config/rpc_pool.yaml")
    end = next(i for i in range(start + 1, len(lines)) if lines[i].startswith("# "))
    body = [ln for ln in lines[start + 1:end] if ln.strip()]
    assert body[0].startswith("## 1 — ")
    assert body[-1] == f"sha256: {RPC_POOL_V1_SHA}"


def test_shipped_values_load() -> None:
    cfg = load_rpc_pool_config(SHIPPED)
    assert isinstance(cfg, RpcPoolConfig)
    assert (cfg.version, cfg.rate_limit_cooldown_seconds, cfg.age_cache_entries) == (1, 5.0, 200000)
    assert [p.name for p in cfg.providers] == ["rpcfast", "helius_1", "helius_2", "helius_3"]
    assert [p.url_env for p in cfg.providers] == ["UNMASK_RPC_URL", "UNMASK_RPC_URL_ALT", "UNMASK_RPC_URL_ALT1",
                                                  "UNMASK_RPC_URL_ALT2"]
    assert [p.profile for p in cfg.providers] == ["rpcfast_start", "helius_free", "helius_free", "helius_free"]
    assert [p.archival for p in cfg.providers] == [False, True, True, True]
    assert [p.retention_seconds for p in cfg.providers] == [72000, None, None, None]
    assert [p.skip_methods for p in cfg.providers] == [("getTokenAccountsByOwner",), (), (), ()]
    assert all(isinstance(p, ProviderSpec) for p in cfg.providers)
    assert isinstance(cfg.providers, tuple)


def test_shipped_file_equals_golden_data() -> None:
    assert _shipped_data() == GOLDEN


def test_shipped_file_has_only_env_names_no_urls_or_keys() -> None:
    text = SHIPPED.read_text(encoding="utf-8")
    assert "://" not in text
    assert not re.search(r"https?:|wss?:|api[-_]?key|apikey|token\s*[:=]", text, re.IGNORECASE)
    for provider in _shipped_data()["providers"]:
        assert re.fullmatch(r"[A-Z][A-Z0-9_]*", provider["url_env"])
    for profile in {p["profile"] for p in _shipped_data()["providers"]}:
        assert profile in RPC_PROFILES


def test_comment_change_needs_no_entry(tmp_path: Path) -> None:
    changed = tmp_path / "rpc_pool.yaml"
    changed.write_text("# інший коментар\n" + SHIPPED.read_text(encoding="utf-8"), encoding="utf-8")
    assert content_digest(changed) == RPC_POOL_V1_SHA
    check_changelog(changed, CHANGELOG)


def _mutated(value):
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value + 1
    if isinstance(value, str):
        return value + "_x"
    if value is None:
        return 1
    if isinstance(value, list):
        return value[:-1] if value else ["getTransaction"]
    raise AssertionError(type(value))


def _assert_detected(tmp_path: Path, data: dict, *, no_entry: bool = False) -> None:
    changed = _write(tmp_path, data)
    journal = _journal_copy(tmp_path)
    pattern = r"rpc_pool\.yaml.*has no entry" if no_entry else r"rpc_pool\.yaml.*content digest does not match"
    with pytest.raises(HubsConfigError, match=pattern):
        check_changelog(changed, journal)


@pytest.mark.parametrize("key", TOP_KEYS)
def test_any_value_change_without_entry_is_detected(tmp_path: Path, key: str) -> None:
    data = copy.deepcopy(GOLDEN)
    data[key] = _mutated(data[key]) if key != "providers" else data["providers"][:-1]
    # зміна `version` без запису — "немає запису для версії"; решта — розбіжність дайджесту
    _assert_detected(tmp_path, data, no_entry=(key == "version"))


@pytest.mark.parametrize("key", PROVIDER_KEYS)
@pytest.mark.parametrize("index", range(len(GOLDEN["providers"])))
def test_any_provider_value_change_without_entry_is_detected(tmp_path: Path, index: int, key: str) -> None:
    data = copy.deepcopy(GOLDEN)
    data["providers"][index][key] = _mutated(data["providers"][index][key])
    _assert_detected(tmp_path, data)


def test_provider_added_removed_or_reordered_without_entry_is_detected(tmp_path: Path) -> None:
    added = copy.deepcopy(GOLDEN)
    extra = copy.deepcopy(added["providers"][1])
    extra.update(name="helius_4", url_env="UNMASK_RPC_URL_ALT3")
    added["providers"].append(extra)
    _assert_detected(tmp_path, added)
    removed = copy.deepcopy(GOLDEN)
    del removed["providers"][2]
    _assert_detected(tmp_path, removed)
    # порядок провайдерів = порядок нічиїх у пулі: перестановка теж змінює результат
    reordered = copy.deepcopy(GOLDEN)
    reordered["providers"].reverse()
    _assert_detected(tmp_path, reordered)


def test_journal_without_sha_line_is_config_error(tmp_path: Path) -> None:
    # мутація: прибрати `sha256:` запису rpc_pool у копії журналу
    journal = _journal_copy(tmp_path)
    text = journal.read_text(encoding="utf-8")
    marker = f"sha256: {RPC_POOL_V1_SHA}\n"
    assert text.count(marker) == 1
    journal.write_text(text.replace(marker, ""), encoding="utf-8")
    with pytest.raises(HubsConfigError, match="rpc_pool.yaml"):
        check_changelog(SHIPPED, journal)


def test_journal_without_section_or_with_wrong_sha_is_config_error(tmp_path: Path) -> None:
    journal = _journal_copy(tmp_path)
    text = journal.read_text(encoding="utf-8")
    journal.write_text(text.replace("# config/rpc_pool.yaml", "# config/rpc_pool_renamed.yaml"), encoding="utf-8")
    with pytest.raises(HubsConfigError, match="section not found"):
        check_changelog(SHIPPED, journal)
    journal.write_text(text.replace(RPC_POOL_V1_SHA, "0" * 64), encoding="utf-8")
    with pytest.raises(HubsConfigError, match="content digest does not match"):
        check_changelog(SHIPPED, journal)


# --------------------------------------------------------------------------------------------- схема завантажувача


def test_loader_does_not_import_hubs_and_reads_no_journal() -> None:
    tree = ast.parse(Path(pool_config_module.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert not any(name.startswith("unmask.hubs") for name in imported), imported
    docstring = ast.get_docstring(tree, clean=False)
    literals = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
                and n.value != docstring]
    assert not any("CHANGELOG" in text for text in literals)


def test_unknown_field_rejected(tmp_path: Path) -> None:
    top = copy.deepcopy(GOLDEN)
    top["no_such_key"] = 1
    with pytest.raises(ConfigError, match=r"^no_such_key: unknown field$"):
        load_rpc_pool_config(_write(tmp_path, top))
    nested = copy.deepcopy(GOLDEN)
    nested["providers"][2]["bogus"] = True
    with pytest.raises(ConfigError, match=r"^providers\[2\]\.bogus: unknown field$"):
        load_rpc_pool_config(_write(tmp_path, nested))


@pytest.mark.parametrize("key", TOP_KEYS)
def test_missing_top_key_rejected(tmp_path: Path, key: str) -> None:
    data = copy.deepcopy(GOLDEN)
    del data[key]
    with pytest.raises(ConfigError, match=rf"^{key}: missing required field$"):
        load_rpc_pool_config(_write(tmp_path, data))


@pytest.mark.parametrize("key", PROVIDER_KEYS)
def test_missing_provider_key_rejected(tmp_path: Path, key: str) -> None:
    data = copy.deepcopy(GOLDEN)
    del data["providers"][1][key]
    with pytest.raises(ConfigError, match=rf"^providers\[1\]\.{key}: missing required field$"):
        load_rpc_pool_config(_write(tmp_path, data))


@pytest.mark.parametrize("url", [
    f"https://mainnet.example.com/?api-key={SENTINEL}",
    f"http://{SENTINEL}.example.com",
    f"wss://{SENTINEL}@host",
    f"{SENTINEL}://x",
])
def test_url_in_url_env_rejected_without_echo(tmp_path: Path, url: str) -> None:
    data = copy.deepcopy(GOLDEN)
    data["providers"][1]["url_env"] = url
    with pytest.raises(ConfigError) as err:
        load_rpc_pool_config(_write(tmp_path, data))
    assert str(err.value) == "providers[1].url_env: must be an environment variable name"
    assert SENTINEL not in repr(err.value) and SENTINEL not in str(err.value)
    for chained in (err.value.__cause__, err.value.__context__):
        assert chained is None or SENTINEL not in repr(chained)


@pytest.mark.parametrize("bad", ["", "1ABC", "A-B", "A B", "A.B", "A\n", "ЗМІННА", 5, None, True, ["A"]])
def test_non_name_url_env_rejected(tmp_path: Path, bad) -> None:
    data = copy.deepcopy(GOLDEN)
    data["providers"][0]["url_env"] = bad
    with pytest.raises(ConfigError, match=r"^providers\[0\]\.url_env: "):
        load_rpc_pool_config(_write(tmp_path, data))


def test_duplicate_url_env_and_name_rejected(tmp_path: Path) -> None:
    dup_env = copy.deepcopy(GOLDEN)
    dup_env["providers"][2]["url_env"] = dup_env["providers"][1]["url_env"]
    with pytest.raises(ConfigError, match=r"^providers\[2\]\.url_env: duplicate$"):
        load_rpc_pool_config(_write(tmp_path, dup_env))
    dup_name = copy.deepcopy(GOLDEN)
    dup_name["providers"][3]["name"] = "helius_1"
    with pytest.raises(ConfigError, match=r"^providers\[3\]\.name: duplicate$"):
        load_rpc_pool_config(_write(tmp_path, dup_name))


@pytest.mark.parametrize("bad", ["", "Rpc", "a-b", "a b", "x" * 33, "a\n", "імʼя", 1, None, True])
def test_bad_provider_name_rejected(tmp_path: Path, bad) -> None:
    data = copy.deepcopy(GOLDEN)
    data["providers"][0]["name"] = bad
    with pytest.raises(ConfigError, match=r"^providers\[0\]\.name: "):
        load_rpc_pool_config(_write(tmp_path, data))


def test_name_length_boundary(tmp_path: Path) -> None:
    cfg = _load_mutated(tmp_path, lambda d: d["providers"][0].update(name="a" * 32))
    assert cfg.providers[0].name == "a" * 32
    cfg = _load_mutated(tmp_path, lambda d: d["providers"][0].update(name="a"))
    assert cfg.providers[0].name == "a"


@pytest.mark.parametrize("bad", ["unknown_profile", "", None, 1, ["helius_free"], f"{SENTINEL}"])
def test_unknown_profile_rejected_without_echo(tmp_path: Path, bad) -> None:
    data = copy.deepcopy(GOLDEN)
    data["providers"][0]["profile"] = bad
    with pytest.raises(ConfigError, match=r"^providers\[0\]\.profile: ") as err:
        load_rpc_pool_config(_write(tmp_path, data))
    assert SENTINEL not in str(err.value)


@pytest.mark.parametrize("archival, retention, ok", [
    (True, None, True),
    (False, 1, True),
    (False, 72000, True),
    (True, 72000, False),   # архівний не має горизонту
    (False, None, False),   # неархівний мусить мати горизонт
    (False, 0, False),
    (False, -5, False),
    (False, True, False),
    (False, 1.5, False),
    (False, "72000", False),
])
def test_archival_iff_retention_null(tmp_path: Path, archival, retention, ok) -> None:
    def mutate(d: dict) -> None:
        d["providers"][0].update(archival=archival, retention_seconds=retention)

    if ok:
        cfg = _load_mutated(tmp_path, mutate)
        assert cfg.providers[0].archival is archival and cfg.providers[0].retention_seconds == retention
    else:
        with pytest.raises(ConfigError, match=r"^providers\[0\]\.retention_seconds: "):
            _load_mutated(tmp_path, mutate)


@pytest.mark.parametrize("bad", [1, 0, "true", "yes", None, [True]])
def test_archival_must_be_real_bool(tmp_path: Path, bad) -> None:
    data = copy.deepcopy(GOLDEN)
    data["providers"][1]["archival"] = bad
    with pytest.raises(ConfigError, match=r"^providers\[1\]\.archival: "):
        load_rpc_pool_config(_write(tmp_path, data))


@pytest.mark.parametrize("bad", [0, -1, 0.0, -0.5, True, False, "5", None, [5], float("nan"), float("inf"),
                                 float("-inf")])
def test_bad_cooldown_rejected(tmp_path: Path, bad) -> None:
    data = copy.deepcopy(GOLDEN)
    data["rate_limit_cooldown_seconds"] = bad
    with pytest.raises(ConfigError, match=r"^rate_limit_cooldown_seconds: "):
        load_rpc_pool_config(_write(tmp_path, data))


@pytest.mark.parametrize("value", [0.001, 1, 5, 2.5, 3600])
def test_cooldown_accepts_positive_finite_numbers(tmp_path: Path, value) -> None:
    cfg = _load_mutated(tmp_path, lambda d: d.update(rate_limit_cooldown_seconds=value))
    assert cfg.rate_limit_cooldown_seconds == float(value) and math.isfinite(cfg.rate_limit_cooldown_seconds)
    assert isinstance(cfg.rate_limit_cooldown_seconds, float)


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, "10", None, [1]])
def test_bad_age_cache_entries_rejected(tmp_path: Path, bad) -> None:
    data = copy.deepcopy(GOLDEN)
    data["age_cache_entries"] = bad
    with pytest.raises(ConfigError, match=r"^age_cache_entries: "):
        load_rpc_pool_config(_write(tmp_path, data))


def test_age_cache_entries_lower_bound(tmp_path: Path) -> None:
    assert _load_mutated(tmp_path, lambda d: d.update(age_cache_entries=1)).age_cache_entries == 1


@pytest.mark.parametrize("bad", [0, -1, True, False, 1.0, "1", None, [1]])
def test_bad_version_rejected(tmp_path: Path, bad) -> None:
    data = copy.deepcopy(GOLDEN)
    data["version"] = bad
    with pytest.raises(ConfigError, match=r"^version: "):
        load_rpc_pool_config(_write(tmp_path, data))


@pytest.mark.parametrize("bad", [[], None, {}, "rpcfast", 5, [1], ["rpcfast"]])
def test_bad_providers_rejected(tmp_path: Path, bad) -> None:
    data = copy.deepcopy(GOLDEN)
    data["providers"] = bad
    with pytest.raises(ConfigError, match=r"^providers"):
        load_rpc_pool_config(_write(tmp_path, data))


def test_skip_methods_validation(tmp_path: Path) -> None:
    for bad in (["getTransaction", "getTransaction"], ["getBlock"], "getTransaction", None, [1], [["getTransaction"]],
                [f"{SENTINEL}"]):
        data = copy.deepcopy(GOLDEN)
        data["providers"][1]["skip_methods"] = bad
        with pytest.raises(ConfigError, match=r"^providers\[1\]\.skip_methods") as err:
            load_rpc_pool_config(_write(tmp_path, data))
        assert SENTINEL not in str(err.value)
    cfg = _load_mutated(tmp_path, lambda d: d["providers"][1].update(skip_methods=["getTransaction", "getAccountInfo"]))
    assert cfg.providers[1].skip_methods == ("getTransaction", "getAccountInfo")  # порядок файла збережено


@pytest.mark.parametrize("method", METHODS)
def test_every_method_must_be_served_by_some_provider(tmp_path: Path, method: str) -> None:
    def mutate(d: dict) -> None:
        for provider in d["providers"]:
            provider["skip_methods"] = [method]

    with pytest.raises(ConfigError) as err:
        _load_mutated(tmp_path, mutate)
    assert method in str(err.value) and "serves" in str(err.value)


def test_method_served_by_exactly_one_provider_is_enough(tmp_path: Path) -> None:
    def mutate(d: dict) -> None:
        d["providers"] = d["providers"][:2]
        d["providers"][0]["skip_methods"] = ["getSignaturesForAddress", "getTokenAccountsByOwner"]
        d["providers"][1]["skip_methods"] = ["getAccountInfo", "getTransaction"]

    cfg = _load_mutated(tmp_path, mutate)
    assert len(cfg.providers) == 2


def test_provider_cannot_skip_every_method(tmp_path: Path) -> None:
    def mutate(d: dict) -> None:
        d["providers"][0]["skip_methods"] = list(METHODS)

    with pytest.raises(ConfigError, match=r"^providers\[0\]\.skip_methods: "):
        _load_mutated(tmp_path, mutate)


def test_single_provider_file_is_valid(tmp_path: Path) -> None:
    cfg = _load_mutated(tmp_path, lambda d: d.update(providers=[d["providers"][1]]))
    assert [p.name for p in cfg.providers] == ["helius_1"]


def test_loader_never_echoes_values_in_errors(tmp_path: Path) -> None:
    # сторожовий рядок у кожному текстовому полі: жодне повідомлення про помилку його не містить
    for path in (("name",), ("url_env",), ("profile",)):
        data = copy.deepcopy(GOLDEN)
        data["providers"][0][path[0]] = f"{SENTINEL} bad value"
        with pytest.raises(ConfigError) as err:
            load_rpc_pool_config(_write(tmp_path, data))
        assert SENTINEL not in str(err.value) and SENTINEL not in repr(err.value)
    data = copy.deepcopy(GOLDEN)
    for key in ("version", "rate_limit_cooldown_seconds", "age_cache_entries"):
        data[key] = SENTINEL
        with pytest.raises(ConfigError) as err:
            load_rpc_pool_config(_write(tmp_path, data))
        assert SENTINEL not in str(err.value)
        data = copy.deepcopy(GOLDEN)


def test_unreadable_or_malformed_files_are_config_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read"):
        load_rpc_pool_config(tmp_path / "absent.yaml")
    with pytest.raises(ConfigError, match="cannot read"):
        load_rpc_pool_config(tmp_path)  # каталог
    for name, content in {
        "not_yaml.yaml": f"version: [{SENTINEL}\n  : :",
        "scalar.yaml": "42\n",
        "list.yaml": "- a\n- b\n",
        "empty.yaml": "",
        "tag.yaml": "version: !!python/object/apply:os.system ['true']\n",
    }.items():
        path = tmp_path / name
        path.write_text(content, encoding="utf-8")
        with pytest.raises(ConfigError) as err:
            load_rpc_pool_config(path)
        assert SENTINEL not in str(err.value)
    binary = tmp_path / "binary.yaml"
    binary.write_bytes(b"\xff\xfe\x00version: 1\n")
    with pytest.raises(ConfigError):
        load_rpc_pool_config(binary)


def test_duplicate_keys_anchors_and_oversize_are_rejected(tmp_path: Path) -> None:
    base = SHIPPED.read_text(encoding="utf-8")
    dup = tmp_path / "dup.yaml"
    dup.write_text(base + "version: 2\n", encoding="utf-8")  # друге `version` мовчки перебило б перше
    with pytest.raises(ConfigError):
        load_rpc_pool_config(dup)
    alias = tmp_path / "alias.yaml"
    # аліас сам по собі дав би валідний конфіг (skip_methods: [] двічі) — відхиляється саме як аліас
    aliased = base.replace("skip_methods: []", "skip_methods: &s []", 1).replace("skip_methods: []", "skip_methods: *s", 1)
    assert "*s" in aliased and "&s" in aliased
    alias.write_text(aliased, encoding="utf-8")
    with pytest.raises(ConfigError, match="anchors and aliases"):
        load_rpc_pool_config(alias)
    big = tmp_path / "big.yaml"
    big.write_text(base + "#" + "x" * (1024 * 1024) + "\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_rpc_pool_config(big)


def test_loader_result_is_immutable(tmp_path: Path) -> None:
    cfg = load_rpc_pool_config(SHIPPED)
    with pytest.raises(Exception):
        cfg.version = 2  # type: ignore[misc]
    with pytest.raises(Exception):
        cfg.providers[0].name = "x"  # type: ignore[misc]


def test_loader_does_not_read_the_journal(tmp_path: Path) -> None:
    # файл без запису в журналі завантажується (журнал — справа тесту, не рантайму)
    data = copy.deepcopy(GOLDEN)
    data["age_cache_entries"] = 7
    cfg = load_rpc_pool_config(_write(tmp_path, data))
    assert cfg.age_cache_entries == 7
