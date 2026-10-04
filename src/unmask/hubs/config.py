# impl: FR-002-08, FR-002-12, FR-002-13, FR-002-14
"""Завантаження й валідація `config/hubs.yaml` і `config/hub_addresses.yaml` (принцип III;
specs/002-funding-graph-hub-pruning/contracts/config-hubs.md).

Тихих умовчань немає: відсутнє чи невідоме поле та значення поза межами дають `ConfigError` з назвою поля.

Правило порогу (FR-002-14, research R-9) — одне для всіх критеріїв: критерій спрацьовує, коли виміряне значення
СТРОГО БІЛЬШЕ (`>`) за поріг; рівно поріг — не хаб. Це стосується `degree_threshold`, `one_off_senders_share`
і `giant_component_warn_share`. `one_off_min_senders` — не поріг хаба, а передумова застосовності критерію
часток: застосовний при `unique_senders >= one_off_min_senders` (включно). Самі порівняння роблять
`hubs.criteria` і `hubs.report`; тут лише значення й межі їхньої коректності.

Список адрес (FR-002-12): файл відсутній або нечитабельний (`OSError`) чи шлях не заданий → `lists=None`
(`HubConfig.lists_applied == False`) — це умова виконання, не помилка. Файл присутній, але вміст некоректний
(не YAML, не base58, дубль, відсутня `version`) → `ConfigError`: це дефект репозиторію. Порожні категорії валідні.
Файл порогів недоступний чи некоректний — завжди `ConfigError`.

Розбір обмежений (`_read_yaml`, ревʼю T-023/T-024): лише прості мапи, списки й скаляри — анкери, аліаси, явні теги
й merge `<<` заборонені; ліміти розміру файла, глибини, кількості вузлів і довжини скаляра; будь-яка відмова розбору
→ `ConfigError` зі шляхом. Повідомлення про помилки не відлунюють значень (лише поле, клас причини, тип значення).

Захист журналу змін (FR-002-08, SC-008, research R-14; T-024). Кожна зміна файла — підняти його `version` і
додати запис `## <version> — <дата>` у розділ `# config/<ім'я файла>` журналу `config/CHANGELOG.md`; останній
непорожній рядок запису — `sha256: <64 hex>` канонічного вмісту (`content_digest`: `yaml.safe_load` →
`json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False)` → sha256; коментарі й форматування не
впливають, значення й типи — впливають). `changelog_entries` читає лише розділ свого файла (порядок розділів і
вміст чужих розділів, зокрема `# config/ingest.yaml` фічі 001 без sha256, не важать). `check_changelog` вимагає:
поточна `version` файла — останній (найбільший) запис його розділу, і sha256 цього запису == `content_digest`.

`load_hub_config` журнал НЕ читає й не перевіряє (контракт graph-service §1: два шляхи, чиста відносно ФС;
R-14 покладає перевірку на тест `tests/test_hubs_changelog_guard.py`): вона лише обчислює дайджести обох файлів
(`HubConfig.thresholds_digest`/`lists_digest`) з того самого розібраного вмісту, що й значення.
"""

from __future__ import annotations

import hashlib
import json
import errno
import math
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

from unmask.ingest.addresses import is_valid_address

__all__ = [
    "ADDRESS_CATEGORIES",
    "AddressLists",
    "ConfigError",
    "HubConfig",
    "HubLists",
    "HubThresholds",
    "changelog_entries",
    "check_changelog",
    "content_digest",
    "load_hub_config",
]

# Рівно ці сім ключів (contracts/config-hubs.md); порядок — порядок виводу категорій.
ADDRESS_CATEGORIES: tuple[str, ...] = (
    "system_programs",
    "token_programs",
    "dex_routers",
    "amm_programs",
    "launchpads",
    "exchanges",
    "market_makers",
)


class ConfigError(Exception):
    """Некоректна конфігурація відсікання; повідомлення містить назву поля."""


@dataclass(frozen=True)
class HubThresholds:
    version: int
    degree_threshold: int
    one_off_senders_share: float
    one_off_min_senders: int
    giant_component_warn_share: float
    prune_off_curve: bool
    prune_ingest_high_degree: bool


@dataclass(frozen=True)
class AddressLists:
    """Вміст `config/hub_addresses.yaml`. `categories` і `index` — лише для читання."""

    version: int
    categories: Mapping[str, tuple[str, ...]]
    index: Mapping[str, str]  # адреса -> категорія (похідне)


HubLists = AddressLists  # псевдонім: так тип названо в постановці T-023; data-model/plan кажуть AddressLists


@dataclass(frozen=True)
class HubConfig:
    thresholds: HubThresholds
    lists: AddressLists | None  # None ⇔ списки недоступні (FR-002-12)
    thresholds_digest: str  # content_digest(config/hubs.yaml) — R-14
    lists_digest: str | None  # content_digest(config/hub_addresses.yaml); None ⇔ lists is None

    @property
    def lists_applied(self) -> bool:
        return self.lists is not None


# --- обмежений розбір недовіреного YAML (ревʼю T-023/T-024 №2) ----------------------------------------------
#
# Конфіги відсікання — прості мапи, списки й скаляри. Тому поверхню розбору звужено, а не «латано винятками»:
# анкери, аліаси (billion laughs), явні теги (`!!python/…`, `!!int` без значення тощо) і merge `<<` заборонені на
# рівні подій парсера — ДО конструювання значень; розмір файла перевіряється ДО розбору; глибина, кількість
# вузлів і довжина скаляра — під час композиції. Довжина скаляра обмежує й цілі (hex/oct/bin/sexagesimal), тож
# ліміт int→str у 4300 цифр недосяжний.

_MAX_FILE_BYTES = 256 * 1024  # розмір файла конфігу, перевіряється до розбору
_MAX_DEPTH = 20  # вкладеність колекцій (корінь-мапа — 1)
_MAX_NODES = 10_000  # усі вузли документа (скаляри + колекції)
_MAX_SCALAR = 1_000  # символів у скалярі (числа, ідентифікатори, рядки)
_MAX_ADDRESS = 64  # символів в адресі (base58 від 32 байтів — 32..44)
_MAX_VERSION = 999_999_999  # верхня межа версії в журналі (заголовок — до 9 цифр)
# Журнал змін: зараз ~3 КіБ на два розділи з трьома записами; запис — 0,3–1 КіБ, і з'являється він лише разом зі
# зміною порогу чи списку (рідко, вручну). 1 МіБ — це тисячі записів, тобто запас на десятиліття; більший файл —
# дефект або атака, і читати його повністю не варто.
_MAX_CHANGELOG_BYTES = 1024 * 1024
# Символи, допустимі в скалярі (printable-набір YAML 1.1 без NEL-обмежень): escape-послідовності `"\ud800"`,
# `"\x00"`, `"\uffff"` дають символи поза ним (одиночні сурогати не кодуються в UTF-8 — дайджест упав би).
_NON_PRINTABLE_RE = re.compile("[^\x09\x0a\x0d\x20-\x7e\x85\xa0-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")


class _Reject(Exception):
    """Порушення обмеженого формату; повідомлення — лише клас причини, без вмісту файла."""


def _key_name(key: Any) -> str:
    """Безпечне ім'я ключа для повідомлення: ідентифікатор як є, інше — лише тип."""
    if isinstance(key, str) and _IDENT_RE.fullmatch(key):
        return key
    return f"<{type(key).__name__} key>"


class _StrictLoader(yaml.SafeLoader):
    """`SafeLoader` з обмеженим форматом: без анкерів, аліасів, явних тегів і merge; ліміти глибини, кількості
    вузлів і довжини скаляра; дубль ключа мапи — помилка (PyYAML мовчки взяв би останнє значення)."""

    def __init__(self, stream):
        super().__init__(stream)
        self._depth = 0
        self._nodes = 0

    def compose_node(self, parent, index):
        event = self.peek_event()
        if isinstance(event, yaml.AliasEvent):
            raise _Reject("aliases are not allowed")
        if event.anchor is not None:
            raise _Reject("anchors are not allowed")
        if event.tag is not None:
            raise _Reject("explicit tags are not allowed")
        self._nodes += 1
        if self._nodes > _MAX_NODES:
            raise _Reject(f"too many nodes (limit {_MAX_NODES})")
        if isinstance(event, yaml.ScalarEvent):
            if len(event.value) > _MAX_SCALAR:
                raise _Reject(f"scalar too long (limit {_MAX_SCALAR} characters)")
            if _NON_PRINTABLE_RE.search(event.value):
                raise _Reject("scalar contains non-printable or surrogate characters")
            if event.style is None and event.value == "<<":
                raise _Reject("merge keys are not allowed")
            return super().compose_node(parent, index)
        self._depth += 1
        if self._depth > _MAX_DEPTH:
            raise _Reject(f"nesting depth exceeds {_MAX_DEPTH}")
        try:
            return super().compose_node(parent, index)
        finally:
            self._depth -= 1

    def construct_mapping(self, node, deep=False):
        if isinstance(node, yaml.MappingNode):
            seen = set()
            for key_node, _ in node.value:
                key = self.construct_object(key_node, deep=deep)
                try:
                    duplicate = key in seen
                except TypeError:  # нехешований ключ — далі його відхилить сам PyYAML
                    continue
                if duplicate:
                    raise _Reject(f"duplicate key '{_key_name(key)}'")
                seen.add(key)
        return super().construct_mapping(node, deep=deep)


def _regular_size(st: os.stat_result) -> int:
    """Розмір регулярного файла за `fstat` (окремо — щоб тест міг змоделювати «розмір невідомий»)."""
    return st.st_size


def _read_at_most(fd: int, n: int) -> bytes:
    """Прочитати до `n` байтів із дескриптора (читання короткими порціями до EOF або `n`)."""
    chunks, left = [], n
    while left > 0:
        chunk = os.read(fd, min(left, 64 * 1024))
        if not chunk:
            break
        chunks.append(chunk)
        left -= len(chunk)
    return b"".join(chunks)


def _read_limited(path: Path, limit: int) -> bytes:
    """Прочитати регулярний файл не більше `limit` байтів; інакше `_Reject` з класом причини.

    `O_NONBLOCK` — щоб FIFO не заблокував `open`; не-регулярний файл (FIFO, пристрій, напр. симлінк на
    `/dev/zero`) відхиляється; каталог — `IsADirectoryError`, як було з `read_bytes`. Розмір перевіряється за `fstat` ДО читання, а саме читання все одно обмежене
    `limit + 1` байтом (st_size може бути 0 у /proc тощо). `OSError` (немає файла, немає прав) проходить нагору.
    """
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    try:
        st = os.fstat(fd)
        if stat.S_ISDIR(st.st_mode):  # як і раніше (read_bytes): каталог — OSError, тобто «файл недоступний»
            raise IsADirectoryError(errno.EISDIR, os.strerror(errno.EISDIR), str(path))
        if not stat.S_ISREG(st.st_mode):
            raise _Reject("not a regular file")
        if _regular_size(st) > limit:
            raise _Reject(f"file too large (limit {limit} bytes)")
        data = _read_at_most(fd, limit + 1)
    finally:
        os.close(fd)
    if len(data) > limit:  # файл виріс між fstat і read, або st_size бреше
        raise _Reject(f"file too large (limit {limit} bytes)")
    return data


def _read_yaml(path: Path) -> Any:
    """Прочитати конфіг обмеженим розбором.

    `OSError` (файл відсутній/нечитабельний) проходить нагору — рішення «None чи ConfigError» ухвалює
    викликач. Файл присутній, але некоректний → `ConfigError("<path>: …")` з класом причини, без вмісту файла:
    більший за `_MAX_FILE_BYTES` (до розбору), не UTF-8, порушення обмеженого формату (`_StrictLoader`) чи
    будь-яка інша відмова PyYAML. YAML-1.1 булеві (`yes`/`no`/`on`/`off`) PyYAML приймає як bool — відоме
    обмеження, не перевіряється.
    """
    path = Path(path)
    try:
        raw = _read_limited(path, _MAX_FILE_BYTES)
    except _Reject as exc:
        raise ConfigError(f"{path}: {exc}") from None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{path}: not valid UTF-8 (byte offset {exc.start})") from None
    loader = None
    try:
        loader = _StrictLoader(text)  # Reader перевіряє символи вже в конструкторі
        return loader.get_single_data()
    except _Reject as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from None
    except Exception as exc:  # noqa: BLE001 — межа розбору недовіреного тексту, див. нижче
        # Catch-all тут легітимний: це єдина межа, де довільний текст стає даними. PyYAML на зіпсованому вході
        # кидає не лише YAMLError (IndexError/KeyError/AttributeError/ValueError/OverflowError/RecursionError у
        # резолві й конструкторах неявних скалярів), а контракт модуля — лише ConfigError. Вміст не відлунюється:
        # лише клас винятку й позиція, якщо вона відома. НІКОЛИ не `str(exc)`: у YAMLError він містить сниппет
        # рядка файла (тест test_broken_yaml_message_does_not_echo_file_content).
        mark = getattr(exc, "problem_mark", None)
        where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark is not None else ""
        raise ConfigError(f"{path}: invalid YAML ({type(exc).__name__}{where})") from None
    finally:
        if loader is not None:
            loader.dispose()


def _count_nodes(data: Any) -> None:
    """Захист `_canonical_digest` (ревʼю №2, В): ліміт вузлів і глибини ДО `json.dumps`, ітеративно.

    Спільні посилання рахуються стільки разів, скільки їх розгорне `json.dumps`, тож експоненційна структура
    відхиляється на перших `_MAX_NODES` вузлах.
    """
    stack = [(data, 1)]
    count = 0
    while stack:
        item, depth = stack.pop()
        count += 1
        if count > _MAX_NODES:
            raise _Reject(f"too many nodes (limit {_MAX_NODES})")
        if isinstance(item, dict):
            children = [*item.keys(), *item.values()]
        elif isinstance(item, (list, tuple)):
            children = list(item)
        else:
            continue
        if depth > _MAX_DEPTH:
            raise _Reject(f"nesting depth exceeds {_MAX_DEPTH}")
        stack.extend((child, depth + 1) for child in children)


def _canonical_digest(data: Any, where: Path | str) -> str:
    """sha256 канонічного JSON розібраного YAML (research R-14).

    Вміст без канонічного JSON (дата без лапок, змішані типи ключів, ціле поза лімітом int→str) чи понад ліміт
    вузлів/глибини → `ConfigError(where)`. Catch-all — з тієї ж причини, що й у `_read_yaml`: `data` походить із
    недовіреного тексту, а контракт — лише `ConfigError`.
    """
    try:
        _count_nodes(data)
        canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        encoded = canonical.encode("utf-8")  # одиночний сурогат → UnicodeEncodeError — теж усередині межі
    except _Reject as exc:
        raise ConfigError(f"{where}: content has no canonical JSON form: {exc}") from None
    except Exception as exc:  # noqa: BLE001 — межа: дані з недовіреного тексту
        raise ConfigError(f"{where}: content has no canonical JSON form ({type(exc).__name__})") from None
    return hashlib.sha256(encoded).hexdigest()


def content_digest(path: Path) -> str:
    """sha256 канонічного вмісту YAML-файла: незалежний від коментарів, форматування й порядку ключів.

    Нечитабельний файл → `OSError`; присутній, але некоректний (не UTF-8, не YAML, дубль ключа, без
    канонічного JSON) → `ConfigError` з шляхом.
    """
    return _canonical_digest(_read_yaml(path), path)


_SECTION_RE = re.compile(r"^# config/(\S+)\s*$")
_ENTRY_RE = re.compile(r"^## ([1-9][0-9]{0,8}) — \S.*$")  # ≤ 9 цифр: int() завжди дешевий
_SHA_RE = re.compile(r"^sha256: ([0-9a-f]{64})$")


def changelog_entries(changelog_path: Path, file_name: str) -> dict[int, str]:
    """`{version: sha256}` записів розділу `# config/<file_name>` журналу змін.

    Розділ — від свого заголовка рівня 1 до наступного рядка `# ` (або кінця файла); інші розділи не читаються.
    Будь-яка вада розділу → `ConfigError` з назвою файла: розділу немає чи він двічі, розділ без записів, текст
    до першого запису, заголовок запису не `## <ціле ≥ 1> — <дата>`, останній непорожній рядок запису не
    `sha256: <64 hex у нижньому регістрі>`, повтор версії, версії не строго зростають.
    """
    where = f"{Path(changelog_path).name}: section '# config/{file_name}'"
    try:
        raw = _read_limited(Path(changelog_path), _MAX_CHANGELOG_BYTES)
    except _Reject as exc:
        raise ConfigError(f"{where}: changelog {exc}") from None
    except OSError as exc:
        raise ConfigError(f"{where}: cannot read changelog: {exc}") from exc
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{where}: changelog is not valid UTF-8 (byte offset {exc.start})") from None

    sections = [i for i, line in enumerate(lines) if (m := _SECTION_RE.match(line)) and m.group(1) == file_name]
    if not sections:
        raise ConfigError(f"{where}: section not found")
    if len(sections) > 1:
        raise ConfigError(f"{where}: section appears twice")
    start = sections[0] + 1
    end = next((i for i in range(start, len(lines)) if lines[i].startswith("# ")), len(lines))

    blocks: list[tuple[int, list[str]]] = []  # (версія, непорожні рядки тіла запису)
    for lineno, line in enumerate(lines[start:end], start=start + 1):
        if line.startswith("## "):
            m = _ENTRY_RE.match(line)
            if m is None:
                raise ConfigError(
                    f"{where}: malformed entry header at line {lineno} (expected '## <version 1..{_MAX_VERSION}> — <date>')"
                )
            blocks.append((int(m.group(1)), []))
        elif line.strip():
            if not blocks:
                raise ConfigError(f"{where}: text before the first entry")
            blocks[-1][1].append(line)
    if not blocks:
        raise ConfigError(f"{where}: no entries")

    entries: dict[int, str] = {}
    last = 0  # найбільша версія досі: версії ≥ 1, тож 0 — «записів ще немає»; лінійний прохід, без max() у циклі
    for version, body in blocks:
        if version in entries:
            raise ConfigError(f"{where}: version {version}: duplicate entry")
        if version <= last:
            raise ConfigError(f"{where}: version {version}: versions must be strictly increasing")
        last = version
        m = _SHA_RE.match(body[-1]) if body else None
        if m is None:
            raise ConfigError(f"{where}: version {version}: last line of the entry must be 'sha256: <64 hex>'")
        entries[version] = m.group(1)
    return entries


def check_changelog(config_path: Path, changelog_path: Path) -> None:
    """Перевірити, що вміст файла конфігу записано в журнал під його поточною версією (SC-008).

    Вимоги: `version` файла — ціле ≥ 1 і найбільша версія розділу `# config/<ім'я файла>`; sha256 її запису ==
    `content_digest(config_path)`. Інакше `ConfigError` з назвою файла й причиною (значення конфігу в
    повідомленні не наводяться).
    """
    path = Path(config_path)
    name = path.name
    try:
        data = _read_yaml(path)
    except OSError as exc:
        raise ConfigError(f"{name}: cannot read: {exc}") from exc
    version = data.get("version") if isinstance(data, dict) else None
    if isinstance(version, bool) or not isinstance(version, int) or not 1 <= version <= _MAX_VERSION:
        raise ConfigError(f"{name}: version must be an int >= 1 and <= {_MAX_VERSION}")
    entries = changelog_entries(changelog_path, name)
    if version not in entries:
        raise ConfigError(
            f"{name}: version {version} has no entry in section '# config/{name}' of {Path(changelog_path).name}"
            " (bump version together with a changelog entry)"
        )
    latest = max(entries)
    if version != latest:
        raise ConfigError(f"{name}: version {version} is not the latest changelog entry ({latest})")
    if entries[version] != _canonical_digest(data, name):
        raise ConfigError(
            f"{name}: version {version}: content digest does not match its changelog entry"
            " (file changed without a new version and changelog entry)"
        )


def _check_keys(data: Any, expected: tuple[str, ...], where: str) -> dict:
    prefix = f"{where}." if where else ""
    if not isinstance(data, dict):
        raise ConfigError(f"{where or 'config'}: expected a mapping")
    for key in expected:
        if key not in data:
            raise ConfigError(f"{prefix}{key}: missing required field")
    for key in data:
        if not (isinstance(key, str) and key in expected):
            raise ConfigError(f"{prefix}{_key_name(key)}: unknown field")
    return data


def _int(data: dict, key: str, lo: int) -> int:
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{key}: expected int, got {type(value).__name__}")
    if value < lo:
        raise ConfigError(f"{key}: must be >= {lo}")
    return value


def _share(data: dict, key: str, *, lo_inclusive: bool) -> float:
    """Частка: `0 ≤ x ≤ 1` (`lo_inclusive`) або `0 < x ≤ 1`. NaN відхиляється."""
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{key}: expected number, got {type(value).__name__}")
    bounds = "0 <= x <= 1" if lo_inclusive else "0 < x <= 1"
    try:
        value = float(value)
    except OverflowError:  # ціле поза діапазоном float — заздалегідь поза межами частки
        raise ConfigError(f"{key}: integer out of range, must satisfy {bounds}") from None
    if math.isnan(value) or value > 1 or value < 0 or (value == 0 and not lo_inclusive):
        raise ConfigError(f"{key}: must satisfy {bounds}")
    return value


def _bool(data: dict, key: str) -> bool:
    value = data[key]
    if not isinstance(value, bool):
        raise ConfigError(f"{key}: expected bool, got {type(value).__name__}")
    return value


def _parse_thresholds(raw: Any) -> HubThresholds:
    data = _check_keys(raw, tuple(HubThresholds.__dataclass_fields__), "")
    return HubThresholds(
        version=_int(data, "version", 1),
        degree_threshold=_int(data, "degree_threshold", 1),
        one_off_senders_share=_share(data, "one_off_senders_share", lo_inclusive=True),
        one_off_min_senders=_int(data, "one_off_min_senders", 2),
        giant_component_warn_share=_share(data, "giant_component_warn_share", lo_inclusive=False),
        prune_off_curve=_bool(data, "prune_off_curve"),
        prune_ingest_high_degree=_bool(data, "prune_ingest_high_degree"),
    )


def _parse_lists(raw: Any) -> AddressLists:
    data = _check_keys(raw, ("version", "categories"), "")
    version = _int(data, "version", 1)
    categories = _check_keys(data["categories"], ADDRESS_CATEGORIES, "categories")

    parsed: dict[str, tuple[str, ...]] = {}
    index: dict[str, str] = {}
    for name in ADDRESS_CATEGORIES:
        entries = categories[name]
        if not isinstance(entries, list):
            raise ConfigError(f"{name}: expected a list of addresses, got {type(entries).__name__}")
        for position, address in enumerate(entries):
            if not isinstance(address, str) or len(address) > _MAX_ADDRESS or not is_valid_address(address):
                raise ConfigError(f"{name}: not a valid address (entry {position}; expected base58, 32 bytes)")
            # далі адреса вже валідна (base58, 32 байти) — її можна назвати в повідомленні про дубль
            if address in index:
                where = "twice in " if index[address] == name else f"in both {index[address]} and "
                raise ConfigError(f"{name}: address {address} appears {where}{name}")
            index[address] = name
        parsed[name] = tuple(entries)
    return AddressLists(
        version=version,
        categories=MappingProxyType(parsed),
        index=MappingProxyType(index),
    )


def load_hub_config(thresholds_path: Path, lists_path: Path | None = None) -> HubConfig:
    """Прочитати пороги й (якщо доступні) списки адрес; некоректне → `ConfigError`.

    `lists_path=None`, відсутній чи нечитабельний файл списків → `HubConfig.lists is None` (FR-002-12).
    Журнал змін не читається (див. докстрінг модуля); дайджести — з того самого розібраного вмісту.
    """
    try:
        raw_thresholds = _read_yaml(thresholds_path)
    except OSError as exc:
        raise ConfigError(f"{thresholds_path}: cannot read thresholds: {exc}") from exc
    thresholds = _parse_thresholds(raw_thresholds)
    thresholds_digest = _canonical_digest(raw_thresholds, thresholds_path)

    no_lists = HubConfig(thresholds=thresholds, lists=None, thresholds_digest=thresholds_digest, lists_digest=None)
    if lists_path is None:
        return no_lists
    try:
        raw = _read_yaml(lists_path)
    except OSError:  # відсутній чи нечитабельний файл — умова виконання, не помилка (FR-002-12)
        return no_lists
    return HubConfig(
        thresholds=thresholds,
        lists=_parse_lists(raw),
        thresholds_digest=thresholds_digest,
        lists_digest=_canonical_digest(raw, lists_path),
    )
