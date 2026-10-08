# verifies: FR-007-03, FR-007-09, FR-007-10, FR-007-11, FR-007-12
"""Версіонований `config/rpc_pool.yaml` v1, його журнал, завантажувач `load_rpc_pool_config` (T-137) і фабрика
`build_rpc_source` (T-138), обидва critical.

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
  чотирьох методів; тексти помилок не відлунюють значень (сторожовий рядок);
- фабрика (T-138, кроки 1–5 контракту): без файла — один провайдер `UNMASK_RPC_URL` (як `HttpRpcSource.from_env`);
  з файлом — провайдер на запис, відсутня змінна → пропуск із попередженням (лише імена), жоден не лишився →
  `ConfigError` з іменами змінних, невалідний URL → гучна помилка з іменем змінної; кожен провайдер зі СВОЇМ
  лімітером/профілем (віртуальний час, `httpx.MockTransport`, без мережі); секрети (URL, ключі) ніде: ні в логах,
  ні в текстах помилок, ні в `repr`, ні в `__cause__`/`__context__`;
- ім'я невідомого ключа в помилці завантажувача — лише якщо це ідентифікатор (як `hubs.config._key_name`).
"""

from __future__ import annotations

import ast
import copy
import dataclasses
import hashlib
import json
import logging
import math
import re
import traceback
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType

import httpx
import pytest
import yaml

import unmask.ingest.rpc.http as http_module
import unmask.ingest.rpc.pool_config as pool_config_module
from rpc_pool_doubles import SequentialRunner
from unmask.hubs.config import ConfigError as HubsConfigError
from unmask.hubs.config import changelog_entries, check_changelog, content_digest
from unmask.ingest.budget import Deadline, FakeClock
from unmask.ingest.config import ConfigError, load_config
from unmask.ingest.rpc.http import RPC_PROFILES, HttpRpcSource
from unmask.ingest.rpc.pool_config import ProviderSpec, RpcPoolConfig, load_rpc_pool_config
from unmask.ingest.rpc.protocol import RpcError

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


# ------------------------------------------------------------------ ім'я невідомого ключа (ревʼю T-137, FR-007-12)


def _unknown_key_message(tmp_path: Path, key, *, nested: bool) -> str:
    data = copy.deepcopy(GOLDEN)
    (data["providers"][1] if nested else data)[key] = 1
    with pytest.raises(ConfigError) as err:
        load_rpc_pool_config(_write(tmp_path, data))
    return str(err.value)


# Ключ-URL з ключем доступу, текст не за ідентифікатором, межі довжини, юнікод, службові символи.
SECRET_LOOKING_KEYS = [
    f"https://x.io/?api-key={SENTINEL}",
    f"api-key={SENTINEL}",
    f"{SENTINEL}",  # дефіси: не ідентифікатор
    "1" + "a" * 10,  # починається з цифри
    "a" * 65,  # на символ довший за межу
    "імʼя",
    "",
    "a b",
    "a\nb",
    "a.b",
]


@pytest.mark.parametrize("key", SECRET_LOOKING_KEYS)
@pytest.mark.parametrize("nested", [False, True])
def test_unknown_key_that_is_not_an_identifier_is_never_echoed(tmp_path: Path, key: str, nested: bool) -> None:
    message = _unknown_key_message(tmp_path, key, nested=nested)
    prefix = "providers[1]." if nested else ""
    assert message == f"{prefix}<str key>: unknown field"
    assert SENTINEL not in message and "x.io" not in message


@pytest.mark.parametrize("key", ["no_such_key", "_x", "A", "a" * 64, "Key9_z"])
@pytest.mark.parametrize("nested", [False, True])
def test_unknown_key_that_is_an_identifier_is_named(tmp_path: Path, key: str, nested: bool) -> None:
    prefix = "providers[1]." if nested else ""
    assert _unknown_key_message(tmp_path, key, nested=nested) == f"{prefix}{key}: unknown field"


@pytest.mark.parametrize("key, label", [(5, "int"), (1.5, "float"), (None, "NoneType"), (True, "bool")])
def test_unknown_non_string_key_is_named_by_type_only(tmp_path: Path, key, label: str) -> None:
    assert _unknown_key_message(tmp_path, key, nested=False) == f"<{label} key>: unknown field"
    assert _unknown_key_message(tmp_path, key, nested=True) == f"providers[1].<{label} key>: unknown field"


# ------------------------------------------------------------------------------------------- фабрика build_rpc_source

INGEST = load_config(ROOT / "config" / "ingest.yaml")
ENV_VARS = {"rpcfast": "UNMASK_RPC_URL", "helius_1": "UNMASK_RPC_URL_ALT", "helius_2": "UNMASK_RPC_URL_ALT1",
            "helius_3": "UNMASK_RPC_URL_ALT2"}
NAMES = list(ENV_VARS)
PROFILE_OF = {"rpcfast": "rpcfast_start", "helius_1": "helius_free", "helius_2": "helius_free",
              "helius_3": "helius_free"}
TOKEN_PROGRAMS = 2  # getTokenAccountsByOwner = два запити (Token, Token-2022)


def build_rpc_source(*args, **kwargs):
    return pool_config_module.build_rpc_source(*args, **kwargs)


def _url(name: str) -> str:
    """URL провайдера зі сторожовим «ключем»: хост ідентифікує провайдера, ключ — секрет."""
    return f"https://{name.replace('_', '-')}.example/rpc/?api-key={SENTINEL}"


def _env(*names: str) -> dict[str, str]:
    return {ENV_VARS[name]: _url(name) for name in (names or NAMES)}


def _providers(pool):
    # Єдине місце зв'язку з внутрішністю пулу (T-132): публічного переліку провайдерів у контракті немає.
    return pool._providers


@dataclasses.dataclass(frozen=True)
class Req:
    name: str
    at: float
    units: int
    method: str
    host: str


class _SpyTransport(httpx.MockTransport):
    def __init__(self, handler, on_close) -> None:
        super().__init__(handler)
        self._on_close = on_close

    def close(self) -> None:
        self._on_close()
        super().close()


class Net:
    """Підставна «мережа» всіх провайдерів: журнал запитів за іменем провайдера, віртуальний час, режими збоїв."""

    def __init__(self, clock: FakeClock | None = None, mode: str = "ok") -> None:
        self.clock = clock if clock is not None else FakeClock()
        self.mode = mode
        self.requests: list[Req] = []
        self.built: list[str] = []
        self.closed: list[str] = []

    def factory(self, name: str) -> httpx.BaseTransport:
        self.built.append(name)
        return _SpyTransport(lambda request: self._handle(name, request), lambda: self.closed.append(name))

    def of(self, name: str) -> list[Req]:
        return [r for r in self.requests if r.name == name]

    def _handle(self, name: str, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        batch = isinstance(body, list)
        method = "getTransaction" if batch else body["method"]
        self.requests.append(Req(name, self.clock.monotonic(), len(body) if batch else 1, method, request.url.host))
        if self.mode != "ok":
            return self._fail(request)
        if batch:
            return httpx.Response(200, json=[{"jsonrpc": "2.0", "id": e["id"], "result": None} for e in body])
        result = {"getAccountInfo": {"context": {"slot": 1}, "value": None}, "getSignaturesForAddress": [],
                  "getTokenAccountsByOwner": {"value": []}}[method]
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    def _fail(self, request: httpx.Request) -> httpx.Response:
        mode = self.mode
        if mode == "http_503":
            return httpx.Response(503, text=f"upstream {SENTINEL} {request.url}")
        if mode == "http_429":
            return httpx.Response(429, headers={"retry-after": "1"}, text=f"{SENTINEL} {request.url}")
        if mode == "http_401":
            return httpx.Response(401, text=f"bad key {SENTINEL}")
        if mode == "connect_error":
            raise httpx.ConnectError(f"cannot reach {request.url}", request=request)
        if mode == "read_timeout":
            raise httpx.ReadTimeout(str(request.url), request=request)
        if mode == "bad_json":
            return httpx.Response(200, text=f"not json {SENTINEL} {request.url}")
        if mode == "jsonrpc_error":
            error = {"code": -32000, "message": f"leak {SENTINEL}", "data": {"url": str(request.url)}}
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": error})
        raise AssertionError(mode)


def _build(net: Net, environ, path: Path = SHIPPED, **kwargs):
    kwargs.setdefault("runner", SequentialRunner())
    return build_rpc_source(path, INGEST, environ, clock=net.clock, sleep=net.clock.advance,
                            transport_factory=net.factory, **kwargs)


def _deadline(net: Net) -> Deadline:
    return Deadline(net.clock, 100_000)


def _chain(exc: BaseException) -> list[BaseException]:
    seen: list[BaseException] = []
    stack: list[BaseException | None] = [exc]
    while stack:
        current = stack.pop()
        if current is None or any(current is s for s in seen):
            continue
        seen.append(current)
        stack += [current.__cause__, current.__context__]
    return seen


def _assert_no_secret(exc: BaseException) -> None:
    for link in _chain(exc):
        assert SENTINEL not in repr(link) and SENTINEL not in str(link)
    assert SENTINEL not in "".join(traceback.format_exception(exc))


def _assert_no_chain(exc: BaseException) -> None:
    assert exc.__cause__ is None and exc.__context__ is None
    _assert_no_secret(exc)


# ----------------------------------------------------------------------------------------------- крок 1: без файла


@pytest.mark.parametrize("profile, batches", [("helius_free", [10, 10, 5]), ("rpcfast_start", [25])])
def test_absent_config_builds_single_default_provider(tmp_path: Path, profile: str, batches: list[int]) -> None:
    net = Net()
    pool = _build(net, {"UNMASK_RPC_URL": _url("rpcfast")}, tmp_path / "absent.yaml", fallback_profile=profile)
    (provider,) = _providers(pool)
    assert pool.name == "http"  # метадані результату — як сьогодні (R-6)
    assert provider.name == "default" and net.built == ["default"]
    assert provider.archival is True and provider.retention_seconds is None  # довіра, як до цієї фічі
    assert provider.skip_methods == frozenset()
    assert provider.weight == RPC_PROFILES[profile]["rate_per_second"]
    # профіль — з аргументу: його max_batch видно в розмірі під-batch
    assert pool.get_transactions([f"s{i}" for i in range(25)], deadline=_deadline(net)) == [None] * 25
    assert [r.units for r in net.requests] == batches
    assert {r.host for r in net.requests} == {"rpcfast.example"}


def test_absent_config_default_profile_is_the_module_default(tmp_path: Path) -> None:
    pool = _build(Net(), {"UNMASK_RPC_URL": _url("rpcfast")}, tmp_path / "absent.yaml")
    assert _providers(pool)[0].weight == RPC_PROFILES[http_module.DEFAULT_PROFILE]["rate_per_second"]


def test_absent_config_ignores_the_other_provider_variables(tmp_path: Path) -> None:
    net = Net()
    pool = _build(net, _env(), tmp_path / "absent.yaml")  # всі чотири змінні задано, файла немає
    assert [p.name for p in _providers(pool)] == ["default"] and net.built == ["default"]


@pytest.mark.parametrize("environ", [
    {},
    {"UNMASK_RPC_URL": ""},
    {"UNMASK_RPC_URL_ALT": "https://other.example/?api-key=" + SENTINEL},  # чужа змінна не підміняє UNMASK_RPC_URL
    {"UNMASK_RPC_URL": " "},
    {"UNMASK_RPC_URL": f"not a url {SENTINEL}"},
    {"UNMASK_RPC_URL": f"ftp://h.example/?api-key={SENTINEL}"},
    {"UNMASK_RPC_URL": f"https://h.example:99999/?api-key={SENTINEL}"},
])
def test_absent_config_missing_env_same_error_as_from_env(tmp_path: Path, environ: dict) -> None:
    with pytest.raises(ConfigError) as expected:
        HttpRpcSource.from_env(INGEST.rpc, commitment=INGEST.commitment, environ=environ)
    net = Net()
    with pytest.raises(ConfigError) as actual:
        _build(net, environ, tmp_path / "absent.yaml")
    assert type(actual.value) is type(expected.value) and str(actual.value) == str(expected.value)
    _assert_no_chain(actual.value)
    assert sorted(net.closed) == sorted(net.built)  # нічого не лишилось відкритим


def test_factory_reads_only_the_given_environ_never_os_environ(tmp_path: Path, monkeypatch) -> None:
    for var in ENV_VARS.values():
        monkeypatch.setenv(var, _url("rpcfast"))
    with pytest.raises(ConfigError, match=r"^UNMASK_RPC_URL: not set$"):
        _build(Net(), {}, tmp_path / "absent.yaml")
    with pytest.raises(ConfigError, match=r"^rpc_pool.yaml: no RPC provider available"):
        _build(Net(), {})


def test_absent_config_pool_settings_are_module_constants(tmp_path: Path) -> None:
    pool = _build(Net(), {"UNMASK_RPC_URL": _url("rpcfast")}, tmp_path / "absent.yaml")
    assert (pool._cooldown_seconds, pool._age_cache_entries) == (5.0, 1)


# ------------------------------------------------------------------------------------------------ кроки 2–5: файл є


def test_four_providers_built_when_all_env_set() -> None:
    net = Net()
    pool = _build(net, _env())
    providers = _providers(pool)
    assert [p.name for p in providers] == NAMES and net.built == NAMES  # порядок файла
    assert repr(pool) == "PooledRpcSource(providers=('rpcfast', 'helius_1', 'helius_2', 'helius_3'))"
    assert pool.name == "http"
    assert [p.weight for p in providers] == [12.0, 4.5, 4.5, 4.5]
    assert [p.weight for p in providers] == [RPC_PROFILES[PROFILE_OF[n]]["rate_per_second"] for n in NAMES]
    assert [p.archival for p in providers] == [False, True, True, True]
    assert [p.retention_seconds for p in providers] == [72000, None, None, None]
    assert [p.skip_methods for p in providers] == [frozenset({"getTokenAccountsByOwner"})] + [frozenset()] * 3
    # кожна змінна доходить до СВОГО провайдера: запити кожного транспорту йдуть на його хост
    assert pool.get_transactions([f"s{i}" for i in range(60)], deadline=_deadline(net)) == [None] * 60
    for name in NAMES:
        assert net.of(name), name
        assert {r.host for r in net.of(name)} == {f"{name.replace('_', '-')}.example"}


def test_pool_settings_come_from_the_file(tmp_path: Path) -> None:
    data = copy.deepcopy(GOLDEN)
    data.update(rate_limit_cooldown_seconds=7.5, age_cache_entries=33)
    pool = _build(Net(), _env(), _write(tmp_path, data))
    assert (pool._cooldown_seconds, pool._age_cache_entries) == (7.5, 33)


def test_provider_order_follows_the_file_not_the_environment_order() -> None:
    environ = dict(reversed(list(_env().items())))
    assert list(environ)[0] == "UNMASK_RPC_URL_ALT2"
    assert [p.name for p in _providers(_build(Net(), environ))] == NAMES


def test_extra_environment_variables_are_ignored() -> None:
    pool = _build(Net(), {**_env(), "PATH": "/bin", "UNMASK_RPC_URL_ALT3": _url("helius_3")})
    assert [p.name for p in _providers(pool)] == NAMES


def test_build_without_transport_factory_makes_no_network_call() -> None:
    pool = build_rpc_source(SHIPPED, INGEST, _env(), clock=FakeClock())
    try:
        assert [p.name for p in _providers(pool)] == NAMES
    finally:
        for provider in _providers(pool):
            provider.source.close()


def test_build_logs_pool_summary_without_urls(caplog) -> None:
    caplog.set_level(logging.DEBUG)
    _build(Net(), _env())
    infos = [r for r in caplog.records if r.name == "unmask.ingest.rpc.pool_config" and r.levelno == logging.INFO]
    assert [r.getMessage() for r in infos] == [
        "rpc pool v1: rpcfast(rpcfast_start, non-archival), helius_1(helius_free, archival), "
        "helius_2(helius_free, archival), helius_3(helius_free, archival)"
    ]
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert SENTINEL not in caplog.text and ".example" not in caplog.text


@pytest.mark.parametrize("empty", [False, True], ids=["unset", "empty"])
@pytest.mark.parametrize("missing", [("helius_2",), ("rpcfast",), ("helius_1", "helius_3"), ("rpcfast", "helius_2")])
def test_missing_env_skipped_with_name_only_warning(caplog, missing: tuple[str, ...], empty: bool) -> None:
    environ = _env()
    for name in missing:
        if empty:
            environ[ENV_VARS[name]] = ""
        else:
            del environ[ENV_VARS[name]]
    caplog.set_level(logging.DEBUG)
    net = Net()
    pool = _build(net, environ)
    kept = [n for n in NAMES if n not in missing]
    assert [p.name for p in _providers(pool)] == kept and net.built == kept  # для пропущених нічого не створено
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert [r.getMessage() for r in warnings] == [
        f"rpc pool: provider {name} skipped: {ENV_VARS[name]} not set" for name in missing
    ]
    assert {r.name for r in warnings} == {"unmask.ingest.rpc.pool_config"}
    assert SENTINEL not in caplog.text and ".example" not in caplog.text
    # решта працює: усі методи обслуговує хтось із лишених
    assert pool.get_account_info("A", deadline=_deadline(net)) is None


@pytest.mark.parametrize("environ", [
    {},
    {"UNRELATED": "x"},
    {var: "" for var in ENV_VARS.values()},
], ids=["empty-env", "unrelated-env", "all-empty"])
def test_no_provider_left_fails_with_all_var_names(caplog, environ: dict) -> None:
    caplog.set_level(logging.DEBUG)
    net = Net()
    with pytest.raises(ConfigError) as err:
        _build(net, environ)
    assert str(err.value) == ("rpc_pool.yaml: no RPC provider available (set one of: UNMASK_RPC_URL, "
                              "UNMASK_RPC_URL_ALT, UNMASK_RPC_URL_ALT1, UNMASK_RPC_URL_ALT2)")
    _assert_no_chain(err.value)
    assert net.built == []
    assert [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING] == [
        f"rpc pool: provider {name} skipped: {var} not set" for name, var in ENV_VARS.items()
    ]


def test_method_nobody_left_serves_is_config_error_and_closes_what_was_built() -> None:
    net = Net()
    with pytest.raises(ConfigError) as err:
        _build(net, _env("rpcfast"))  # лишився лише RPC Fast, а він пропускає getTokenAccountsByOwner
    assert str(err.value) == "rpc_pool.yaml: no available provider serves getTokenAccountsByOwner"
    _assert_no_chain(err.value)
    assert net.built == ["rpcfast"] and net.closed == ["rpcfast"]  # клієнт не лишився відкритим


def test_single_remaining_provider_serving_every_method_is_enough() -> None:
    pool = _build(Net(), _env("helius_2"))
    assert [p.name for p in _providers(pool)] == ["helius_2"]


BAD_URLS = [
    f"not a url {SENTINEL}",
    f"ftp://h.example/?api-key={SENTINEL}",
    f"https://h.example:99999/?api-key={SENTINEL}",
    f"https://?api-key={SENTINEL}",
    f"https://h.example/ {SENTINEL}",
    SENTINEL,
    " \t",
]


@pytest.mark.parametrize("bad", BAD_URLS)
@pytest.mark.parametrize("name", NAMES)
def test_invalid_url_fails_loud_with_var_name(caplog, name: str, bad: str) -> None:
    environ = _env()
    environ[ENV_VARS[name]] = bad
    caplog.set_level(logging.DEBUG)
    net = Net()
    with pytest.raises(ConfigError) as err:
        _build(net, environ)
    assert str(err.value) == f"{ENV_VARS[name]}: invalid RPC URL"  # лише ім'я змінної
    _assert_no_chain(err.value)
    assert SENTINEL not in caplog.text
    # не пропущено мовчки й не збудовано «наполовину»: усе вже створене (також транспорт відхиленого) закрито
    index = NAMES.index(name)
    assert net.built == NAMES[: index + 1]
    assert sorted(net.closed) == sorted(NAMES[: index + 1])


def test_invalid_url_is_not_treated_like_a_missing_variable(caplog) -> None:
    caplog.set_level(logging.DEBUG)
    with pytest.raises(ConfigError, match="invalid RPC URL"):
        _build(Net(), {**_env(), "UNMASK_RPC_URL_ALT1": "not a url"})
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_malformed_config_file_is_config_error_and_builds_nothing(tmp_path: Path) -> None:
    bad = tmp_path / "rpc_pool.yaml"
    bad.write_text(f"version: 1\nproviders: [{SENTINEL}\n", encoding="utf-8")
    net = Net()
    with pytest.raises(ConfigError) as err:
        _build(net, _env(), bad)
    _assert_no_chain(err.value)  # розбірник YAML тримає уривок файла: у ланцюг він не потрапляє
    assert net.built == []


@pytest.mark.parametrize("content", [
    f"version: [{SENTINEL}\n  : :",
    f"version: 1\nproviders: [{SENTINEL}\n",
    f"!!python/object/apply:os.system ['{SENTINEL}']\n",
    f"a: &x 1\nb: *x  # {SENTINEL}\n",
    f"version: 1\nversion: 2  # {SENTINEL}\n",
], ids=["parse", "unclosed", "tag", "alias", "duplicate"])
def test_loader_errors_carry_no_exception_chain(tmp_path: Path, content: str) -> None:
    path = tmp_path / "rpc_pool.yaml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigError) as err:
        load_rpc_pool_config(path)
    _assert_no_chain(err.value)


def test_loader_io_and_encoding_errors_carry_no_exception_chain(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as err:
        load_rpc_pool_config(tmp_path / "absent.yaml")
    _assert_no_chain(err.value)
    binary = tmp_path / "binary.yaml"
    binary.write_bytes(b"\xff\xfe\x00version: 1\n")
    with pytest.raises(ConfigError) as err:
        load_rpc_pool_config(binary)
    _assert_no_chain(err.value)


def test_provider_with_infinite_rate_profile_gets_a_finite_weight(monkeypatch, tmp_path: Path) -> None:
    unlimited = MappingProxyType({"rate_per_second": math.inf, "burst": 30, "max_batch": 25, "max_tx_version": 1})
    profiles = MappingProxyType({**RPC_PROFILES, "unlimited": unlimited})
    monkeypatch.setattr(http_module, "RPC_PROFILES", profiles)
    monkeypatch.setattr(pool_config_module, "RPC_PROFILES", profiles)
    pool = _build(Net(), {"UNMASK_RPC_URL": _url("rpcfast")}, tmp_path / "absent.yaml", fallback_profile="unlimited")
    weight = _providers(pool)[0].weight
    assert math.isfinite(weight) and weight > 0  # data-model: inf -> 1e6


# ------------------------------------------------------------------- кожен провайдер зі своїм профілем і лімітером


def _assert_paced(reqs: list[Req], rate: float, burst: int, label: str) -> None:
    """Токен-кошик: одиниць у будь-якому вікні [t_i, t_j] не більше burst + rate × (t_j − t_i)."""
    for i in range(len(reqs)):
        total = 0
        for j in range(i, len(reqs)):
            total += reqs[j].units
            allowed = burst + rate * (reqs[j].at - reqs[i].at)
            assert total <= allowed + 1e-6, f"{label}: {total} units in window {reqs[i].at:.3f}..{reqs[j].at:.3f}"


def test_each_provider_paces_by_own_profile() -> None:
    net = Net()
    pool = _build(net, _env(), runner=SequentialRunner())
    sigs = [f"s{i}" for i in range(300)]
    deadline = _deadline(net)
    for _ in range(2):  # другий виклик стартує з кошиків, які лишив перший
        assert pool.get_transactions(sigs, deadline=deadline) == [None] * 300
    assert net.clock.monotonic() > 0  # лімітери справді спали (віртуальний час)
    for name in NAMES:
        profile = RPC_PROFILES[PROFILE_OF[name]]
        mine = net.of(name)
        assert sum(r.units for r in mine) > 2 * profile["burst"], name  # навантаження виходить за кошик
        assert max(r.units for r in mine) <= profile["max_batch"], name
        _assert_paced(mine, profile["rate_per_second"], profile["burst"], name)


def test_each_provider_paces_independently_of_the_others_traffic() -> None:
    # Швидкий провайдер не «витрачає» чужий кошик: повільний Helius тримає свій ліміт, навіть коли RPC Fast
    # у цей час вільний, а єдине джерело часу — спільний годинник.
    net = Net()
    pool = _build(net, _env("rpcfast", "helius_1"), runner=SequentialRunner("reverse"))
    for _ in range(3):
        pool.get_transactions([f"s{i}" for i in range(200)], deadline=_deadline(net))
    for name in ("rpcfast", "helius_1"):
        profile = RPC_PROFILES[PROFILE_OF[name]]
        _assert_paced(net.of(name), profile["rate_per_second"], profile["burst"], name)
    assert max(r.units for r in net.of("helius_1")) <= RPC_PROFILES["helius_free"]["max_batch"]


# ---------------------------------------------------------------------------------------------------------- секрети


def _all_calls(pool, net: Net):
    """Усі методи пулу: кожен у `RpcError` (режими збоїв) або в нормальну відповідь."""
    deadline = _deadline(net)
    calls = [
        lambda: pool.get_transactions([f"s{i}" for i in range(12)], deadline=deadline),
        lambda: pool.get_account_info("A", deadline=deadline),
        lambda: pool.get_signatures_for_address("A", before=None, until=None, limit=10, deadline=deadline),
        lambda: pool.get_token_accounts_by_owner("A", deadline=deadline),
    ]
    return calls


@pytest.mark.parametrize("mode", ["ok", "http_503", "http_429", "http_401", "connect_error", "read_timeout",
                                  "bad_json", "jsonrpc_error"])
@pytest.mark.parametrize("skip_one", [False, True], ids=["all-set", "one-skipped"])
def test_secret_sentinel_never_in_logs_errors_repr(caplog, mode: str, skip_one: bool) -> None:
    caplog.set_level(logging.DEBUG)
    net = Net(mode=mode)
    pool = _build(net, _env(*(NAMES[:2] + NAMES[3:] if skip_one else NAMES)))  # пропуск теж пише в лог
    failures = 0
    for call in _all_calls(pool, net):
        try:
            call()
        except RpcError as exc:
            failures += 1
            _assert_no_chain(exc)
    assert (failures == 0) == (mode == "ok")
    assert failures == (0 if mode == "ok" else 4)
    # repr/str пулу, провайдерів і їхніх джерел — лише імена
    shown = [repr(pool), str(pool)] + [repr(p) + str(p) + repr(p.source) + str(p.source) for p in _providers(pool)]
    assert not any(SENTINEL in text or ".example" in text for text in shown)
    assert SENTINEL not in caplog.text and ".example" not in caplog.text
    assert all(SENTINEL not in repr(r.__dict__) for r in caplog.records)
    assert net.requests, "тест нічого не перевірив: жодного запиту"


def test_secret_sentinel_in_absent_config_mode(caplog, tmp_path: Path) -> None:
    caplog.set_level(logging.DEBUG)
    net = Net(mode="http_503")
    pool = _build(net, {"UNMASK_RPC_URL": _url("rpcfast")}, tmp_path / "absent.yaml")
    with pytest.raises(RpcError) as err:
        pool.get_transactions(["a", "b"], deadline=_deadline(net))
    _assert_no_chain(err.value)
    assert SENTINEL not in repr(pool) and SENTINEL not in caplog.text


def test_provider_repr_never_contains_the_source_url() -> None:
    pool = _build(Net(), _env())
    for provider in _providers(pool):
        assert repr(provider) == f"Provider(name={provider.name!r})"
        assert SENTINEL not in repr(provider.source) and "example" not in repr(provider.source)
