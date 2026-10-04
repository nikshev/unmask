# impl: FR-001-15, FR-001-16
"""Живий адаптер `RpcSource`: Solana JSON-RPC 2.0 через `httpx` (contracts/rpc-source.md).

Єдиний модуль, що імпортує `httpx` (принцип IV). Адаптер лише транспортує й мапить помилки: типи
результатів дослівно повторюють поле `result` методів Solana RPC. Запити — `encoding=jsonParsed`,
`maxSupportedTransactionVersion=max_tx_version` (за замовчуванням 1), `commitment` — з конфігу збору.

Рішення (поза буквою контракту, з причинами)
- `get_transactions` — **batch**, а не пул потоків: послідовні HTTP-запити з масивом JSON-RPC, кожен
  не більше `max_batch` підписів (T-049; живий провайдер: 40 проходить, 50 -> HTTP 429 / -32005). Це
  просто (жодних потоків, детермінізм, один дедлайн-цикл), і порядок відповіді тримається за JSON-RPC
  `id` = позиція підпису у ВСЬОМУ виклику (не в під-batch), а не за порядком, у якому його повернув
  сервер; результат не залежить від `max_batch`. `rpc.page_size` — лише розмір сторінки підписів
  (`get_signatures_for_address`) і розмір пачки ядра; batch транзакцій він НЕ визначає.
  `rpc.max_concurrency` цим адаптером не використовується.
- `max_tx_version`, `max_batch`, `rate_per_second`, `burst` — параметри адаптера (keyword-only), а не YAML: це
  константи протоколу/провайдера, вони не змінюють висновок (за невдалого значення збір чесно стає
  `incomplete`, а не іншим). Значення за замовчуванням беруться з ПРОФІЛЮ провайдера (`RPC_PROFILES`, T-053;
  `profile="rpcfast_start"` за замовчуванням); явно передані параметри перекривають профіль поодинці.
  Невідомий профіль (також не-рядок) -> `ValueError` зі списком відомих профілів, без значення і без URL.
  Перевірки — над ПІДСУМКОВИМИ значеннями: лише справжній `int` (`bool` ні), `max_tx_version >= 0`,
  `max_batch >= 1`; помилка — `ValueError` з назвою параметра, без значення і без URL. Мережа вже містить
  транзакції `version: 1`; запит із меншою версією дає -32015.
- Профілі (виміряні числа; не секрети, на результат не впливають — лише на швидкість і ризик 429):
  `rpcfast_start` — rate 12, burst 30, max_batch 25 (RPC Fast «Start», T-049: кошик ≈ 40, ≈ 15–16/с);
  `helius_free` — rate 4.5, burst 20, max_batch 10 (Helius free, known-issues §8: ОДИН кошик на ВСІ методи,
  кожен елемент batch = одиниця; виміряно: ємність ≈ 30, поповнення ≈ 5/с — batch 30 проходить, 35 ні; 10/12/16
  запитів/с протягом 6 с -> приймається 59–60 ≈ 30 + 5·6; сталі 5/с — без 429; «8/с без 429» виглядало так лише
  на коротких тестах, поки кошик не вичерпався. Профіль навмисно нижче: запас ≈ 10% за швидкістю, ≈ 33% за
  ємністю; живо перевірено власником процесу: 4.5/20/10 — 0×429 за 80 с).
- Під-batch — «все або нічого»: збій будь-якого під-batch (після повторів) або елемента піднімає виняток на
  весь виклик, наступні під-batch не відправляються, часткові результати не повертаються. Повтори — на рівні
  під-batch (повторюється лише той, що впав) і, як і раніше, лише для збоїв рівня HTTP/транспорту (`_send`);
  JSON-RPC `error` у тілі HTTP 200 (також -32005, -32015) не повторюється.
- Глобальний лімітер (кошик «одиниць запиту», T-049 -> T-053, рішення власника процесу). Ліміт живого
  провайдера — ШВИДКІСТЬ, а не розмір batch, і (Helius free, виміряно) ОДИН на всі методи. Тому кошик спільний
  для ВСІХ методів адаптера; ціна запиту в одиницях: `getAccountInfo` = 1, `getSignaturesForAddress` = 1,
  `getTokenAccountsByOwner` = 1 на КОЖНУ з двох програм (Token, Token-2022 — два запити, кожен окремо проходить
  кошик), batch `getTransaction` = кількість елементів під-batch. Без лімітера запити, що йдуть поспіль,
  майже гарантовано ловлять 429 (RPC Fast: HTTP 429 з тілом -32005 без Retry-After). Клієнтський кошик:
  `burst` токенів на старті, поповнення `rate_per_second` за інʼєктованим `clock.monotonic()`, не більше
  `burst`; значення — з профілю (дефолти профілів навмисно з запасом ≈ 20–25% від виміряного; 20/40 на RPC
  Fast давали 429 у кожному прогоні). Перед КОЖНОЮ спробою запиту на k одиниць (також повтором): поповнити; бракує —
  чекати `(k − tokens) / rate` інʼєктованим `sleep`, але лише якщо очікування МЕНШЕ за `deadline.remaining()`;
  інакше `RpcBudgetTimeout` (текст `budget`) без запиту, без сну і без зміни стану кошика (чесно: бюджет
  вичерпано; `expired()` тут ще хибне, тому ядро мапить саме цей підклас у `budget_exhausted` безумовно — T-051;
  resume продовжить). Після паузи — списати k і далі звичайний `_send_once`
  (перевірка `expired()`/`request_timeout()` — після паузи). HTTP 429 на БУДЬ-ЯКОМУ запиті -> локальні
  токени = 0 (кошик провайдера порожній), далі звичайний шлях повтору; повтор знову йде через лімітер. 5xx і
  мережеві збої кошик не обнуляють (це не ліміт швидкості); JSON-RPC помилка ліміту в тілі HTTP 200, як і
  раніше, не повторюється і кошик не обнуляє. Якщо після 429 бюджет не дозволяє дочекатись токенів,
  назовні виходить `RpcBudgetTimeout`, а не `RpcRateLimited` (контракт це дозволяє: винен бюджет).
  `rate_per_second = math.inf` вимикає лімітер; скінченна швидкість — не менше 0.1/с, тож одна пауза
  лімітера не перевищує `burst / 0.1` с (10·`burst`, навіть за безкінечного дедлайну) і завжди менша
  за `deadline.remaining()`. `max_batch <= burst` (інакше під-batch ніколи не вміститься). Старі імена
  T-049 (`tx_rate_per_second`, `tx_burst`) НЕ приймаються (TypeError), щоб перейменування не стало тихим
  аліасом зі зміненою семантикою (раніше — лише getTransaction). Адаптер однопотоковий, як і ядро: стан
  кошика не захищено блокуванням (потокобезпечність не потрібна).
- `-32015` (непідтримана версія транзакції, напр. майбутня v2) -> `RpcUnavailable` з фіксованою міткою
  `jsonrpc error code=-32015 (unsupported transaction version)` — ніколи `None` і не ліміт.
- Помилка одного елемента batch -> `RpcUnavailable` (або `RpcRateLimited`, якщо код ліміту) на ВЕСЬ
  виклик, а не `None` для елемента: `None` означає «транзакцію не знайдено» (`result: null`, обрізана
  історія), а JSON-RPC `error` — «не вдалось дізнатись». Підміна одного іншим видала б збій за відсутність
  даних (принцип V), і ще вилучила б повтор усього виклику. Те саме для невідповідності batch (немає
  id, чужий id, дубль, не масив): ніколи не вгадуємо.
- Повторюються лише `RpcRateLimited` і `RpcUnavailable` (`rpc.max_retries`, пауза
  `rpc.retry_backoff_seconds * 2**n`; для 429 — не менше `Retry-After`). `RpcTimeout` не повторюється:
  бюджет запиту вже витрачено. Пауза робиться лише якщо вона **менша за `deadline.remaining()`**; інакше
  повтор однаково не вмістився б, і піднімається остання справжня помилка (а не «бюджет»), без сну.
  Сон — через інʼєктований `sleep`, час — через інʼєктований `clock`.
- Перед КОЖНИМ запитом (також повтором і кожним під-batch): `deadline.expired()` -> `RpcBudgetTimeout`;
  `request_timeout() <= 0` (або NaN) — теж `RpcBudgetTimeout`, нуль запитів (на 0 httpx/urllib3 можуть
  кидати ValueError). `RpcBudgetTimeout` (T-051) — підклас `RpcTimeout` з фіксованим текстом `budget`: усі три
  місця, де адаптер сам вирішує «винен бюджет», піднімають саме його; таймаути транспорту (httpx, загальна
  тривалість запиту) лишаються звичайним `RpcTimeout`.
- Загальний таймаут запиту: `httpx.Timeout` з усіма чотирма полями = `deadline.request_timeout(cap)`, і
  додатково адаптер міряє загальну тривалість сам (від відправки до кінця тіла) — і при отриманні заголовків,
  і між шматками тіла. Перевищення -> `RpcTimeout`, навіть якщо відповідь прийшла. Лишковий ризик: заголовки,
  що «капають» по байту, обмежені лише per-read таймаутом httpx (без потоків це не зупинити).
- Мапування: HTTP 429 -> `RpcRateLimited(retry_after)` (лише `Retry-After` у секундах; HTTP-date, мінус,
  NaN/inf -> `None`); код JSON-RPC помилки 429 або -32005 -> теж `RpcRateLimited`; `httpx.TimeoutException`
  -> `RpcTimeout`; інші `httpx.TransportError`/`DecodingError`, не-2xx (також 3xx: редиректи НЕ йдемо, щоб не
  віддати ключ іншому хосту), невалідний JSON, JSON-RPC `error`, неочікувана форма -> `RpcUnavailable`.
  Усе інше (напр. `ValueError` із транспорту) — дефект і виходить назовні.
- `get_signatures_for_address(address, before=...)`: підпис `before` ПЕРЕДАЄТЬСЯ без перевірки, що він є в
  історії адреси. На живому RPC `before` працює за позицією (слотом) підпису, тож SPL-ребро від делегата
  (підпису немає в історії відправника, ревʼю T-012) коректно працює; перевірка наявності коштувала б
  зайвих запитів і хибно відмовляла б. Список перевіряється на контракт: ≤ `limit`, від найновішого.

Секрети: URL із ключем приймається лише ззовні (конструктор; `from_env()` читає змінну середовища `url_var`,
за замовчуванням `UNMASK_RPC_URL`, — лише тут і лише як зручність). URL ніколи не потрапляє в `repr`/`str`,
винятки, `detail` і логи. `url_var` має бути ім'ям змінної (`[A-Za-z_][A-Za-z0-9_]*`), інакше `ValueError` без
відлуння (захист від помилково переданого замість імені самого URL); у тексті `ConfigError` — лише ім'я.
- Політика `detail` (ескалація T-021, рішення власника процесу): тексти винятків адаптера НІКОЛИ не містять
  вільного тексту ззовні — ні `error.message`, ні `error.data`, ні тіла, ні заголовків, ні назви класу винятку
  транспорту. Лише категорія з ФІКСОВАНОЇ таблиці: за HTTP-статусом (`http 401 unauthorized (check API key)`,
  `http 5xx server error`, ...), за кодом JSON-RPC (`jsonrpc error code=-32001 (resource not found)`; невідомий
  код -> `jsonrpc error code=<int>` без тексту; код друкується лише якщо `type(code) is int` і він у межах
  int32, інакше `jsonrpc error (malformed code)`), за класом помилки транспорту (`network error: ConnectError`
  — мітка з таблиці через `isinstance`, а не `type(exc).__name__`), а також `invalid JSON in response` і
  `unexpected response shape: ...`. Причина: очищувати довільний текст провайдера від ключа принципово
  ненадійно (JSON-екранування `\\/`, подвійне percent-кодування, HTML-сутності, власна маска провайдера —
  кожне ревʼю знаходило нове кодування). Діагностика «хибний ключ» лишається категорією 401/403.
- Ланцюг винятків: винятки адаптера піднімаються ПОЗА блоками `except` (помилку повертає `_exchange`, а
  `_send_once` піднімає), тож `__cause__` і `__context__` порожні: httpx-виняток тримає `request.url` із ключем,
  `JSONDecodeError.doc` — неочищене тіло. `raise ... from None` усередині `except` цього не дає
  (лише ховає контекст у показі, об'єкт лишається досяжним).
- URL валідується повністю в конструкторі (`httpx.URL` + `urlsplit`: схема http/https, непорожній хост,
  порт 1..65535, обидва розбори згодні щодо порту). Будь-який виняток розбору (ValueError, `httpx.InvalidURL`
  тощо — їхні тексти містять частини URL, напр. «Invalid port: '<ключ>'») перетворюється на ОДНЕ
  `ValueError("invalid RPC URL")`, піднятий поза `except` (без `__context__`). `from_env` -> `ConfigError`
  без URL. Так ключ не витікає і при першому запиті: невалідний URL до запиту не доходить.
- Логи: УСІ записи логерів `httpx` і `httpcore*` (імена — як у встановлених пакетах) відкидаються фільтрами
  логерів, на будь-якому рівні. httpx на INFO пише «HTTP Request: POST <url>»; httpcore на DEBUG пише хост
  (ключ буває в піддомені) і СИРІ заголовки відповіді (`Location` при 3xx, `X-Debug` можуть нести ключ) —
  розпізнавати «безпечні» записи за текстом знову означало б очищати чужий текст. Ціна: транспортна
  діагностика httpx/httpcore недоступна навіть на DEBUG (прийнятно; помилки видно за категорією в `detail`).
  Фільтри ставляться при імпорті модуля й діють на логерах, а не на хендлерах; фільтр логера не діє на
  записи його дочірніх логерів, тому — на кожен логер поіменно.

Залежить від: `rpc.protocol`, `budget` (`Clock`, `SystemClock`), `config` (`RpcConfig`, `ConfigError`).
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit

import httpx

from unmask.ingest.budget import Clock, SystemClock
from unmask.ingest.config import ConfigError, RpcConfig
from unmask.ingest.rpc.protocol import (
    AccountInfo,
    Deadline,
    RawTransaction,
    RpcBudgetTimeout,
    RpcError,
    RpcRateLimited,
    RpcTimeout,
    RpcUnavailable,
    SignatureInfo,
    TokenAccountInfo,
)

ENV_URL = "UNMASK_RPC_URL"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

_COMMITMENTS = ("finalized", "confirmed")
# 429 — ліміт у тілі JSON-RPC (частина провайдерів не віддає HTTP 429); -32005 — «limit exceeded» у
# Alchemy/Infura-стилі. Хибне спрацювання (Solana -32005 = node unhealthy) нешкідливе: обидва повторюються.
_RATE_LIMIT_CODES = frozenset({429, -32005})
_RETRY_AFTER_RE = re.compile(r"[0-9]+(?:\.[0-9]+)?")
_SINGLE_ID = 1
_INVALID_URL = "invalid RPC URL"
_INT32 = range(-(2**31), 2**31)
_MIN_RATE = 0.1  # нижня межа скінченної швидкості: пауза лімітера ≤ burst / 0.1 с
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# Профілі провайдерів (T-053): виміряні числа, не секрети, на результат не впливають. Одиниця = один запит
# JSON-RPC або один елемент batch. Незмінні (MappingProxyType): таблиця — константа модуля.
RPC_PROFILES: Mapping[str, Mapping[str, float | int]] = MappingProxyType({
    # RPC Fast «Start» (known-issues §6, ревʼю T-049, 16 живих прогонів): кошик ≈ 40 (±3) елементів,
    # поповнення ≈ 15–16/с, 40 getTransaction у batch проходить, 50 -> 429; запас ≈ 20–25%.
    "rpcfast_start": MappingProxyType({"rate_per_second": 12.0, "burst": 30, "max_batch": 25, "max_tx_version": 1}),
    # Helius free (known-issues §8): один кошик на ВСІ методи, кожен елемент batch = одиниця; виміряно:
    # ємність ≈ 30 (batch 30 проходить, 35 ні), поповнення ≈ 5/с (10–16/с протягом 6 с -> 59–60 = 30 + 5·6).
    # Профіль навмисно нижче (≈ 10% за швидкістю, ≈ 33% за ємністю); 4.5/20/10 живо: 0×429 за 80 с.
    "helius_free": MappingProxyType({"rate_per_second": 4.5, "burst": 20, "max_batch": 10, "max_tx_version": 1}),
})
DEFAULT_PROFILE = "rpcfast_start"
# Текст — лише з констант модуля (імена профілів), ніколи з переданого значення.
_PROFILE_ERROR = "profile: unknown provider profile (expected one of: " + ", ".join(RPC_PROFILES) + ")"


class _FromProfile:
    """Маркер «параметр не передано — взяти з профілю». Не `None`: `None` лишається невалідним значенням."""

    def __repr__(self) -> str:
        return "<from profile>"


_FROM_PROFILE: Any = _FromProfile()

# Allow-list категорій `detail`. Кожен рядок — константа адаптера; ззовні береться лише int (статус, код).
_JSONRPC_LABELS: Mapping[int, str] = {
    -32700: "parse error",
    -32600: "invalid request",
    -32601: "method not found",
    -32602: "invalid params",
    -32603: "internal error",
    -32000: "server error",
    -32001: "resource not found",
    -32004: "block not available",
    -32005: "node unhealthy / rate limit",
    -32007: "slot skipped",
    -32009: "slot skipped (long-term storage)",
    -32010: "key excluded from secondary index",
    -32011: "transaction history not available",
    -32014: "block status not available yet",
    -32015: "unsupported transaction version",
    -32016: "minimum context slot not reached",
    429: "rate limited",
}
# Порядок важливий: специфічніші класи першими (ProxyError, UnsupportedProtocol — підкласи TransportError).
_NETWORK_LABELS: tuple[tuple[type[Exception], str], ...] = (
    (httpx.ConnectError, "network error: ConnectError"),
    (httpx.ReadError, "network error: ReadError"),
    (httpx.WriteError, "network error: WriteError"),
    (httpx.CloseError, "network error: CloseError"),
    (httpx.RemoteProtocolError, "network error: RemoteProtocolError"),
    (httpx.LocalProtocolError, "network error: LocalProtocolError"),
    (httpx.ProxyError, "network error: ProxyError"),
    (httpx.UnsupportedProtocol, "network error: UnsupportedProtocol"),
    (httpx.DecodingError, "network error: DecodingError"),
)
# Імена логерів у встановлених httpx/httpcore (тест звіряє цей список із `getLogger(...)` у їхньому коді).
_SILENCED_LOGGERS = (
    "httpx",
    "httpcore", "httpcore.connection", "httpcore.http11", "httpcore.http2", "httpcore.proxy", "httpcore.socks",
)


def _drop_all(record: logging.LogRecord) -> bool:
    """Фільтр логерів httpx/httpcore: відкинути запис (у ньому може бути URL, хост або заголовки з ключем)."""
    return False


for _name in _SILENCED_LOGGERS:
    logging.getLogger(_name).addFilter(_drop_all)


def _http_detail(status: int) -> str:
    if type(status) is not int or not 100 <= status <= 999:
        return "http status (malformed)"
    if status in (401, 403):
        return f"http {status} unauthorized (check API key)"
    if status == 404:
        return "http 404 not found"
    if 300 <= status < 400:
        return f"http {status} redirect (not followed)"
    if 500 <= status < 600:
        return f"http {status} server error"
    return f"http {status}"


def _jsonrpc_code(error: Any) -> int | None:
    """Код JSON-RPC помилки лише як справжній int у межах int32 (bool, float, рядок, величезне число -> None)."""
    code = error.get("code") if isinstance(error, dict) else None
    return code if type(code) is int and code in _INT32 else None


def _jsonrpc_detail(error: Any) -> str:
    if not isinstance(error, dict):
        return "jsonrpc error (malformed)"
    code = _jsonrpc_code(error)
    if code is None:
        return "jsonrpc error (malformed code)"
    label = _JSONRPC_LABELS.get(code)
    return f"jsonrpc error code={code}" + (f" ({label})" if label is not None else "")


def _network_detail(exc: Exception) -> str:
    for cls, label in _NETWORK_LABELS:
        if isinstance(exc, cls):
            return label
    return "network error"


def _valid_url(url: object) -> bool:
    """Чи придатний URL для запитів. Ніколи не піднімає: тексти винятків розбору містять частини URL."""
    if not isinstance(url, str) or any(c.isspace() or not c.isprintable() for c in url):
        return False  # httpx тихо percent-кодує пробіл у хості («rpc%20.example») — такий URL не наш
    try:
        parsed = httpx.URL(url)
        split = urlsplit(url)
        port = split.port  # ValueError для нечислового/поза межами порту
        return (
            parsed.scheme in ("http", "https")
            and split.scheme in ("http", "https")
            and bool(parsed.host)
            and bool(split.hostname)
            and parsed.port == port
            and (port is None or 1 <= port <= 65535)
        )
    except Exception:  # noqa: BLE001 — будь-яка відмова розбору = невалідний URL; текст не зберігаємо
        return False


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _parse_retry_after(raw: str | None) -> float | None:
    if raw is None:
        return None
    raw = raw.strip()
    if not _RETRY_AFTER_RE.fullmatch(raw):
        return None
    return float(raw)


class HttpRpcSource:
    def __init__(
        self,
        url: str,
        rpc_cfg: RpcConfig,
        transport: httpx.BaseTransport | None = None,
        *,
        commitment: str,
        clock: Clock | None = None,
        sleep: Callable[[float], None] | None = None,
        profile: str = DEFAULT_PROFILE,
        max_tx_version: int = _FROM_PROFILE,
        max_batch: int = _FROM_PROFILE,
        rate_per_second: float = _FROM_PROFILE,
        burst: int = _FROM_PROFILE,
    ) -> None:
        # Одне загальне повідомлення без url і його частин; піднімається поза `except` (див. `_valid_url`).
        if not _valid_url(url):
            raise ValueError(_INVALID_URL)
        if commitment not in _COMMITMENTS:
            raise ValueError(f"commitment: {commitment!r} not in {list(_COMMITMENTS)}")
        # `type(...) is str` до пошуку: не-рядок (у т.ч. нехешований список) — та сама відмова без відлуння.
        if type(profile) is not str or profile not in RPC_PROFILES:
            raise ValueError(_PROFILE_ERROR)
        defaults = RPC_PROFILES[profile]
        # Явно передане перекриває профіль (кожен параметр окремо).
        if max_tx_version is _FROM_PROFILE:
            max_tx_version = defaults["max_tx_version"]
        if max_batch is _FROM_PROFILE:
            max_batch = defaults["max_batch"]
        if rate_per_second is _FROM_PROFILE:
            rate_per_second = defaults["rate_per_second"]
        if burst is _FROM_PROFILE:
            burst = defaults["burst"]
        # Без значення в тексті: лише назва параметра й правило (значення — не наша справа логувати).
        if not _is_int(max_tx_version) or max_tx_version < 0:
            raise ValueError("max_tx_version: must be an int >= 0")
        if not _is_int(max_batch) or max_batch < 1:
            raise ValueError("max_batch: must be an int >= 1")
        if (
            not isinstance(rate_per_second, (int, float))
            or isinstance(rate_per_second, bool)
            or not rate_per_second >= _MIN_RATE  # також NaN
        ):
            raise ValueError("rate_per_second: must be a number >= 0.1 (finite, or math.inf to disable)")
        if not _is_int(burst) or burst < 1:
            raise ValueError("burst: must be an int >= 1")
        if max_batch > burst:
            raise ValueError("max_batch: must not exceed burst")
        self._rate = float(rate_per_second)
        self._burst = burst
        self._max_tx_version = max_tx_version
        self._max_batch = max_batch
        self._url = url
        self._cfg = rpc_cfg
        self._commitment = commitment
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._sleep: Callable[[float], None] = sleep if sleep is not None else time.sleep
        self._client = httpx.Client(transport=transport, follow_redirects=False)
        # Стан глобального лімітера (однопотоково, як і ядро): кошик повний на старті.
        self._tokens = float(burst)
        self._refilled_at = self._clock.monotonic()

    @classmethod
    def from_env(
        cls,
        rpc_cfg: RpcConfig,
        *,
        commitment: str,
        transport: httpx.BaseTransport | None = None,
        environ: Mapping[str, str] | None = None,
        clock: Clock | None = None,
        sleep: Callable[[float], None] | None = None,
        url_var: str = ENV_URL,
        profile: str = DEFAULT_PROFILE,
        max_tx_version: int = _FROM_PROFILE,
        max_batch: int = _FROM_PROFILE,
        rate_per_second: float = _FROM_PROFILE,
        burst: int = _FROM_PROFILE,
    ) -> HttpRpcSource:
        """Зручність: URL зі змінної `url_var` (за замовчуванням `UNMASK_RPC_URL`). Сервіс і ядро середовище не
        читають. Текст помилки — лише ім'я змінної (провалідоване як ім'я), ніколи її значення."""
        if not isinstance(url_var, str) or not _ENV_NAME_RE.fullmatch(url_var):
            raise ValueError("url_var: must be an environment variable name")
        env = os.environ if environ is None else environ
        url = env.get(url_var)
        if not url:
            raise ConfigError(url_var + ": not set")
        if not _valid_url(url):
            raise ConfigError(url_var + ": " + _INVALID_URL)
        return cls(url, rpc_cfg, transport, commitment=commitment, clock=clock, sleep=sleep, profile=profile,
                   max_tx_version=max_tx_version, max_batch=max_batch,
                   rate_per_second=rate_per_second, burst=burst)

    def __repr__(self) -> str:
        return f"HttpRpcSource(name={self.name!r})"

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HttpRpcSource:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def name(self) -> str:
        return "http"

    # ------------------------------------------------------------------ публічні методи

    def get_account_info(self, address: str, *, deadline: Deadline) -> AccountInfo | None:
        result = self._call(
            "getAccountInfo",
            [address, {"encoding": "jsonParsed", "commitment": self._commitment}],
            deadline,
        )
        if not isinstance(result, dict) or "value" not in result:
            raise RpcUnavailable("unexpected response shape: getAccountInfo result")
        value = result["value"]
        if value is not None and not isinstance(value, dict):
            raise RpcUnavailable("unexpected response shape: getAccountInfo value")
        return value

    def get_signatures_for_address(
        self,
        address: str,
        *,
        before: str | None,
        until: str | None,
        limit: int,
        deadline: Deadline,
    ) -> list[SignatureInfo]:
        """Підписи від найновішого. `before`/`until` лише передаються (див. шапку модуля)."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError(f"limit: {limit!r} must be an int in 1..1000")
        options: dict[str, Any] = {"limit": limit}
        if before is not None:
            options["before"] = before
        if until is not None:
            options["until"] = until
        options["commitment"] = self._commitment
        result = self._call("getSignaturesForAddress", [address, options], deadline)
        if not isinstance(result, list):
            raise RpcUnavailable("unexpected response shape: getSignaturesForAddress result")
        if len(result) > limit:
            raise RpcUnavailable("unexpected response shape: more signatures than limit")
        previous_slot: int | None = None
        for item in result:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("signature"), str)
                or isinstance(item.get("slot"), bool)
                or not isinstance(item.get("slot"), int)
            ):
                raise RpcUnavailable("unexpected response shape: signature entry")
            if previous_slot is not None and item["slot"] > previous_slot:
                raise RpcUnavailable("unexpected response shape: signatures not newest-first")
            previous_slot = item["slot"]
        return result

    def get_transactions(
        self, signatures: Sequence[str], *, deadline: Deadline
    ) -> list[RawTransaction | None]:
        """Послідовні під-batch ≤ `max_batch`; id = позиція підпису у всьому виклику; «все або нічого»."""
        out: list[RawTransaction | None] = []
        for start in range(0, len(signatures), self._max_batch):
            payload = [
                {
                    "jsonrpc": "2.0",
                    "id": position,
                    "method": "getTransaction",
                    "params": [
                        sig,
                        {
                            "encoding": "jsonParsed",
                            "maxSupportedTransactionVersion": self._max_tx_version,
                            "commitment": self._commitment,
                        },
                    ],
                }
                for position, sig in enumerate(signatures[start : start + self._max_batch], start)
            ]
            # Збій під-batch піднімається звідси ж: `out` (частковий) ніколи не повертається.
            out.extend(self._batch(payload, deadline))
        return out

    def get_token_accounts_by_owner(
        self, owner: str, *, deadline: Deadline
    ) -> list[TokenAccountInfo]:
        merged: list[TokenAccountInfo] = []
        # Два окремі запити — дві одиниці лімітера: кожен `_call` сам проходить кошик.
        for program in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
            result = self._call(
                "getTokenAccountsByOwner",
                [
                    owner,
                    {"programId": program},
                    {"encoding": "jsonParsed", "commitment": self._commitment},
                ],
                deadline,
            )
            value = result.get("value") if isinstance(result, dict) else None
            if not isinstance(value, list) or not all(
                isinstance(e, dict) and isinstance(e.get("pubkey"), str) for e in value
            ):
                raise RpcUnavailable("unexpected response shape: getTokenAccountsByOwner value")
            merged.extend(value)
        return merged

    # ------------------------------------------------------------------ JSON-RPC

    def _call(self, method: str, params: list[Any], deadline: Deadline) -> Any:
        payload = {"jsonrpc": "2.0", "id": _SINGLE_ID, "method": method, "params": params}
        body = self._send(payload, deadline, units=1)  # одиночний запит = 1 одиниця лімітера
        if not isinstance(body, dict):
            raise RpcUnavailable("unexpected response shape: not a JSON-RPC object")
        self._raise_if_error(body)
        if body.get("id") != _SINGLE_ID or "result" not in body:
            raise RpcUnavailable("unexpected response shape: id/result")
        return body["result"]

    def _batch(self, payload: list[dict[str, Any]], deadline: Deadline) -> list[RawTransaction | None]:
        body = self._send(payload, deadline, units=len(payload))  # кожен елемент batch = 1 одиниця
        if isinstance(body, dict):
            # Сервер відмовив на весь batch одним об'єктом помилки (або відповів не за контрактом).
            self._raise_if_error(body)
            raise RpcUnavailable("unexpected response shape: batch answered with an object")
        if not isinstance(body, list):
            raise RpcUnavailable("unexpected response shape: batch is not an array")
        by_id: dict[Any, dict[str, Any]] = {}
        for entry in body:
            if not isinstance(entry, dict) or isinstance(entry.get("id"), bool):
                raise RpcUnavailable("unexpected response shape: batch entry")
            key = entry.get("id")
            if key in by_id:
                raise RpcUnavailable("unexpected response shape: duplicate batch id")
            by_id[key] = entry
        expected = {item["id"] for item in payload}
        if by_id.keys() != expected:
            raise RpcUnavailable("unexpected response shape: batch ids do not match the request")
        # Помилки елементів: ліміт має пріоритет (повтор доречний), далі — перша за позицією.
        errors = [(item["id"], by_id[item["id"]]) for item in payload if "error" in by_id[item["id"]]]
        if any(_jsonrpc_code(entry["error"]) in _RATE_LIMIT_CODES for _, entry in errors):
            raise RpcRateLimited(None)
        if errors:
            index, entry = errors[0]
            raise RpcUnavailable(f"batch item {index}: {_jsonrpc_detail(entry['error'])}")
        out: list[RawTransaction | None] = []
        for item in payload:
            entry = by_id[item["id"]]
            if "result" not in entry:
                raise RpcUnavailable("unexpected response shape: batch entry without result")
            result = entry["result"]
            if result is not None and not isinstance(result, dict):
                raise RpcUnavailable("unexpected response shape: transaction result")
            out.append(result)
        return out

    @staticmethod
    def _raise_if_error(body: dict[str, Any]) -> None:
        if "error" not in body:
            return
        if _jsonrpc_code(body["error"]) in _RATE_LIMIT_CODES:
            raise RpcRateLimited(None)
        raise RpcUnavailable(_jsonrpc_detail(body["error"]))

    # ------------------------------------------------------------------ транспорт, повтори, дедлайн

    def _pace(self, k: int, deadline: Deadline) -> None:
        """Глобальний кошик одиниць запиту: дочекатись k токенів (у межах дедлайну) і списати їх до запиту."""
        if math.isinf(self._rate):
            return
        now = self._clock.monotonic()
        tokens = min(float(self._burst), self._tokens + max(now - self._refilled_at, 0.0) * self._rate)
        if tokens < k:
            wait = (k - tokens) / self._rate
            if wait >= deadline.remaining():
                raise RpcBudgetTimeout()  # дочекатись не дозволяє бюджет: ні сну, ні запиту, стан не змінено
            self._sleep(wait)
            after = self._clock.monotonic()
            tokens = min(float(self._burst), tokens + max(after - now, 0.0) * self._rate)
            now = after
        self._tokens = tokens - k
        self._refilled_at = now

    def _send(self, payload: Any, deadline: Deadline, *, units: int) -> Any:
        """`units` — ціна запиту в одиницях лімітера (1 — одиночний запит; k — batch на k елементів)."""
        attempt = 0
        while True:
            try:
                self._pace(units, deadline)  # кожна спроба, включно з повтором, спершу проходить кошик
                return self._send_once(payload, deadline)
            except (RpcRateLimited, RpcUnavailable) as exc:
                if isinstance(exc, RpcRateLimited):
                    # провайдер відмовив за швидкістю: його кошик порожній — наш теж
                    self._tokens = 0.0
                    self._refilled_at = self._clock.monotonic()
                if attempt >= self._cfg.max_retries:
                    raise
                pause = self._cfg.retry_backoff_seconds * (2**attempt)
                if isinstance(exc, RpcRateLimited) and exc.retry_after is not None:
                    pause = max(pause, exc.retry_after)
                if pause >= deadline.remaining():
                    raise  # повтор не вмістився б у бюджет: не спимо, віддаємо справжню причину
                if pause > 0:
                    self._sleep(pause)
                attempt += 1

    def _send_once(self, payload: Any, deadline: Deadline) -> Any:
        if deadline.expired():
            raise RpcBudgetTimeout()
        timeout = deadline.request_timeout(self._cfg.request_timeout_seconds)
        if not timeout > 0:  # <= 0 і NaN
            raise RpcBudgetTimeout()
        failure, raw = self._exchange(payload, timeout)
        if failure is not None:
            raise failure
        parsed: Any = None
        try:
            parsed = json.loads(raw)
        except (ValueError, RecursionError):  # RecursionError — глибоко вкладений JSON
            failure = RpcUnavailable("invalid JSON in response")
        if failure is not None:
            raise failure  # поза except: `__context__` порожній, тіло відповіді не чіпляється до винятку
        return parsed

    def _exchange(self, payload: Any, timeout: float) -> tuple[RpcError | None, bytes]:
        """Один HTTP-обмін. Помилку ПОВЕРТАЄ, а не піднімає всередині `except`: виняток, піднятий у
        `except`, тягне `__context__` на httpx-виняток, а той тримає `request.url` із ключем. Піднімає
        виклик-ер поза блоком, тож ні `__cause__`, ні `__context__` не лишаються."""
        started = self._clock.monotonic()

        def over_total() -> bool:
            return self._clock.monotonic() - started > timeout

        failure: RpcError | None = None
        chunks: list[bytes] = []
        try:
            with self._client.stream(
                "POST",
                self._url,
                json=payload,
                timeout=httpx.Timeout(connect=timeout, read=timeout, write=timeout, pool=timeout),
            ) as response:
                status = response.status_code
                if over_total():
                    failure = RpcTimeout("request exceeded total timeout")
                elif status == 429:
                    failure = RpcRateLimited(_parse_retry_after(response.headers.get("retry-after")))
                elif not 200 <= status < 300:
                    failure = RpcUnavailable(_http_detail(status))
                else:
                    for chunk in response.iter_bytes():
                        chunks.append(chunk)
                        if over_total():
                            failure = RpcTimeout("request exceeded total timeout")
                            break
        except httpx.TimeoutException:
            failure = RpcTimeout("request timed out")
        except (httpx.TransportError, httpx.DecodingError) as exc:
            # лише мітка з таблиці: текст винятку може містити URL, а назва класу — будь-що
            failure = RpcUnavailable(_network_detail(exc))
        return failure, b"".join(chunks)
