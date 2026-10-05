# Implementation Plan: Публічний API і Telegram-бот доставки аналізу

**Branch**: `004-api-bot-delivery` (робота в `main`, окрема гілка не створюється) | **Date**: 2026-10-05 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/004-api-bot-delivery/spec.md`

## Summary

Живий конвеєр 001→002→003 (`IngestService` → `GraphService` → `ClusterService` з тими самими
YAML-конфігами, без «демо-порогів») загортається в два тонкі інтерфейси: HTTP `GET /api/token/{mint}`
на stdlib `http.server` і Telegram-бота на сирих викликах Bot API через наявний `httpx`
(long polling, без бібліотеки-обгортки). Відповідь — документ за `api-response.schema.json`
(числа 003 слово в слово + блок походження), кеш у памʼяті за `mint` із single-flight,
PNG — серверний статичний рендер через Pillow (єдина нова залежність фічі, R-3).
Тести — записані транспорти й несправжній Bot-транспорт, жодних сокетів і мережі.

## Technical Context

**Language/Version**: Python 3.12 (як у 001/002/003)

**Primary Dependencies**: `httpx` (Bot API; уже є), `Pillow` (PNG-рендер; НОВЕ, R-3 — єдина нова
залежність фічі); stdlib `http.server` для HTTP; dev — `pytest`, `jsonschema`

**Storage**: N/A — кеш доставки в памʼяті процесу (R-5); персистентності немає

**Testing**: `pytest` через `uv run pytest`; гард мережі `tests/conftest.py` діє на всі тести;
сокети в тестах заборонені тим самим гардом — HTTP-логіка тестується викликом функцій обробника,
Telegram — несправжнім транспортом, RPC — записаними `IngestResult`-документами
(`tests/fixtures/real/*.json`)

**Target Platform**: Linux-сервер (один процес: HTTP-потік + polling-цикл; секрети з оточення)

**Project Type**: бібліотека `src/unmask/delivery` + точка входу `scripts/serve_unmask.py`
(тонка обгортка `delivery.main`)

**Performance Goals**: холодний запит типового токена < 60 с (SC-001/FR-004-06); повторний —
без звернень до RPC і побайтово той самий документ (SC-002)

**Constraints**: без авторизації (PRD); жодної нової константи, що змінює висновок, поза YAML
001/002/003 (FR-004-11); `insufficient_data` не стає `clean` у жодному шарі (FR-004-12);
повідомлення Telegram ≤ ~4000 символів (обрізання з повним текстом за callback, R-8)

**Scale/Scope**: 12 FR, 4 user stories; один ендпоінт, одна команда бота, один формат PNG;
орієнтовно 10–12 задач (нумерація з T-088 — наступний вільний номер проєкту після T-087)

## Constitution Check

*GATE: перевірено до Phase 0 і повторно після Phase 1 — порушень немає.*

| Принцип | Як виконується | Статус |
|---|---|---|
| I. Трасування | кожна задача нестиме `[FR-004-NN]`; маркери `impl:`/`verifies:` — розділ «Маркери трасування»; `__init__.py`, `scripts/serve_unmask.py` — `trace: ignore-file`; FR-ID формату `FR-004-NN` сумісні з `scripts/trace.py` без змін | PASS |
| II. Test-First | тести першими з червоним станом з очікуваної причини; записані RPC/бот-транспорти, жодних сокетів у тестах; perf-бюджет холодного запиту — записаний сценарій, не жива мережа | PASS |
| III. Версіонування | жодних нових чисел, що змінюють висновок: пороги/ваги — YAML 001/002/003; показові сталі PNG/бота документовані в `data-model.md`; відповідь несе версії, з яких пораховано; Pillow — залежність рендера, не висновку (R-3) | PASS |
| IV. Межі модулів | `delivery` бачить 001/002/003 лише як типи результатів і три сервіси; чиста логіка відділена від транспортів (HTTP/Bot/RPC — межі з дублюванням для тестів); AST-тест меж | PASS |
| V. Доказ і чесна неповнота | числа 003 проходять наскрізь без перерахунку; кожен кластер відповіді — той самий перелік доказів; кнопка «докази» розкриває повний текст (зі стисненням за R-8); `Rejection`/неповнота мапляться в явні помилки/статуси, ніколи в «чисто» | PASS |
| VI. Хаби окремо | 004 не чіпає відсікання: споживає `GraphResult` як є; у відповіді видно походження (версії), записи відсікання лишаються всередині графа 002 | PASS |
| VII. Лише Solana, на запит | тільки Solana; без моніторингу/ML/авторизації/сховища; Bot API і локальний PNG — у межах MVP за PRD, не «моніторинг» і не «сховище»; кеш — памʼять процесу (R-5) | PASS |
| Стоп-гейти | гейт контрольної точки (8.10) уже пройдено скриптом (calibration.md 003) — бот дозволено починати; гейт подачі (11.10) — ручний чекліст PRD, поза фічею | PASS |
| Ролі | `critical:` → `implementer-senior`; решта → `implementer` (визначить `tasks.md`) | PASS |

Re-check після Phase 1: контракти не додали залежностей (схема відповіді — дані, не код);
Pillow — єдина нова залежність, обґрунтована в R-3. Складність, що потребує обґрунтування:
single-flight кешу (простіше за чергу з TTL і достатньо для демо; див. Complexity Tracking —
порожній, порушень немає).

## Project Structure

### Documentation (this feature)

```text
specs/004-api-bot-delivery/
├── spec.md                           # FR-004-01..12 (не змінюється під час імплементації)
├── plan.md                           # цей файл
├── research.md                       # R-1…R-8
├── data-model.md                     # сутності відповіді, кешу, повідомлень, межі транспортів
├── quickstart.md                     # перевірка без мережі + живий дим вручну
├── contracts/
│   ├── api-response.schema.json      # схема 004.1 документа відповіді (походження всередині)
│   └── bot-messages.md               # формати /check, повідомлення, callback, помилок
├── checklists/requirements.md
└── tasks.md                          # /speckit-tasks (окремий крок)
```

### Source Code (repository root)

```text
config/
├── delivery.yaml                     # НОВЕ: version=1; лише показові й операційні сталі (порт умовчання,
│                                     #  межі тексту/картинки); ЖОДНИХ чисел, що змінюють висновок (FR-004-11)
└── CHANGELOG.md                      # + розділ "# config/delivery.yaml" ПЕРЕД "# config/ingest.yaml"
                                      #  (ingest лишається останнім — вимога 001/002)
src/unmask/delivery/
├── __init__.py                       # trace: ignore-file
├── report.py                         # документ відповіді з результатів 001/002/003 + to_json за схемою 004.1
├── render.py                         # PNG з документа (Pillow; показові сталі з delivery.yaml)
├── cache.py                          # кеш mint→документ + single-flight
├── service.py                        # DeliveryService: живий конвеєр 001→002→003, кеш, мапінг Rejection
├── http.py                           # stdlib-обробник GET /api/token/{mint}, /healthz; чисті функції маршруту
├── bot.py                            # polling-цикл, /check, callback «докази», форматування (BotTransport — межа)
└── main.py                           # точка входу: env→конфіги→сервіси→HTTP-потік+polling
scripts/
└── serve_unmask.py                   # тонка обгортка над delivery.main (trace: ignore-file)

tests/
├── fixtures/delivery/                # записані HTTP-запити/відповіді й журнали Bot-транспорту (за потреби)
├── test_delivery_report.py           # verifies: FR-004-01, FR-004-12
├── test_delivery_render.py           # verifies: FR-004-05
├── test_delivery_cache.py            # verifies: FR-004-06
├── test_delivery_service.py          # verifies: FR-004-02, FR-004-07
├── test_delivery_http.py            # verifies: FR-004-01, FR-004-07, FR-004-08
├── test_delivery_bot.py              # verifies: FR-004-03, FR-004-04
├── test_delivery_boundaries.py       # verifies: FR-004-11 (AST імпортів)
└── test_delivery_e2e.py              # verifies: FR-004-09 (наскрізь на записах, без мережі)
```

**Structure Decision**: src-layout, один новий пакет `unmask.delivery` у спільному корені
(як `unmask.clusters` у 003). Транспорти — межі з дублюванням: `BotTransport` (живий на `httpx`
vs журнальний у тестах), записаний `RpcSource` 001, HTTP-логіка без сокетів. Секрети — лише
`UNMASK_RPC_URL` (існуючий патерн `from_env`) і `UNMASK_BOT_TOKEN` (новий, той самий патерн).

## Модулі: що робить · як користуватись · від чого залежить

| Модуль | Що робить | Інтерфейс | Залежить від |
|---|---|---|---|
| `delivery.report` | документ відповіді з трійки результатів; серіалізація за схемою 004.1 | `build_report(ingest, graph, clusters) -> dict`; `to_json(doc) -> str` | `clusters.serialize.to_dict`, моделі 001/002/003 (типи) |
| `delivery.render` | PNG з документа | `render_png(doc) -> GraphImage` (байти + розміри + прапор порожнечі) | `report`-документ (словник), `Pillow`, `delivery.yaml` (показові) |
| `delivery.cache` | кеш і single-flight | `Cache.get/store`, `single_flight(mint, build)` | stdlib (`threading`) |
| `delivery.service` | оркестрація запиту: кеш → 001→002→003 → звіт → кеш; мапінг відмов | `DeliveryService(ingest, graph, clusters, cache).analyze(mint) -> ApiResponse \| DeliveryError` | сервіси 001/002/003, `report`, `cache` |
| `delivery.http` | маршрут і статуси; чисті функції | `route(method, path) -> handler`; `handle_token_request(mint, service) -> (status, body)` | `service` |
| `delivery.bot` | polling, `/check`, callback, форматування | `run_polling(transport, service)`; `handle_check(text, service)`; `handle_evidence(callback, service)` | `service`, `render`, `BotTransport` |
| `delivery.main` | wiring: env, YAML, сервіси, потоки | `main(argv) -> int` | усе вище + `HttpRpcSource.from_env` |

Правило залежностей (тест `test_delivery_boundaries.py`): з `unmask` ядро (`report`…`service`,
`cache`, `render`) імпортує лише моделі/сервіси/конфіги 001–003 і `unmask.delivery.*`;
`httpx` — лише в живому `BotTransport` (`bot.py`); `PIL` — лише в `render.py`; `yaml` — лише
в завантажувачі `delivery.yaml`; заборонено: `unmask.ingest.{rpc.collector,…внутрішності}`,
`unmask.hubs.*`, `unmask.graph.{build,…}`, сокети в тестах — гард `conftest.py`.

## FR → модуль / артефакт

| FR | Де виконується | Де перевіряється |
|---|---|---|
| FR-004-01 ендпоінт і поля відповіді | `http.py`, `report.py`, схема 004.1 | `test_delivery_http.py`, `test_delivery_report.py` |
| FR-004-02 живий конвеєр тими самими версіями | `service.py` (ті самі класи 001/002/003) | `test_delivery_service.py`, e2e |
| FR-004-03 повідомлення бота | `bot.py`, `bot-messages.md` | `test_delivery_bot.py` |
| FR-004-04 кнопка «докази» | `bot.py` (callback → повний текст) | `test_delivery_bot.py` |
| FR-004-05 PNG | `render.py` (Pillow) | `test_delivery_render.py` |
| FR-004-06 кеш і ≤ 60 с | `cache.py`, `service.py` | `test_delivery_cache.py` |
| FR-004-07 помилки без падіння | `service.py` (мапінг `Rejection`), `http.py`, `bot.py` | `test_delivery_service.py`, `test_delivery_http.py`, `test_delivery_bot.py` |
| FR-004-08 без авторизації | відсутність auth-логіки (перевірка негативна + ручний дим) | `test_delivery_http.py` (401/403 ніколи), quickstart §5 |
| FR-004-09 тести без мережі | записані транспорти, гард `conftest.py` | `test_delivery_e2e.py`, гард |
| FR-004-10 секрети з оточення | `main.py` (`from_env`-патерн), відсутність секретів у репо | `test_delivery_boundaries.py` (AST: жодних літералів ключів; grep-тест), ревʼю |
| FR-004-11 жодних нових чисел висновку | `config/delivery.yaml` (лише показові/операційні), AST-тест | `test_delivery_boundaries.py` |
| FR-004-12 походження, без remap | `report.py` (`provenance`), схема (`allOf`) | `test_delivery_report.py`, схема |

## Маркери трасування

| Файл | Шапка |
|---|---|
| `src/unmask/delivery/__init__.py`, `scripts/serve_unmask.py` | `# trace: ignore-file` |
| `delivery/report.py` | `# impl: FR-004-01, FR-004-12` |
| `delivery/render.py` | `# impl: FR-004-05` |
| `delivery/cache.py` | `# impl: FR-004-06` |
| `delivery/service.py` | `# impl: FR-004-02, FR-004-06, FR-004-07` |
| `delivery/http.py` | `# impl: FR-004-01, FR-004-07, FR-004-08` |
| `delivery/bot.py` | `# impl: FR-004-03, FR-004-04, FR-004-07, FR-004-09` |
| `delivery/main.py` | `# impl: FR-004-10` |
| `config/delivery.yaml` | (YAML під git; маркер не потрібен — `config/` не в `IMPL_ROOTS`) |
| кожен `tests/test_delivery_*.py` | `# verifies: FR-004-NN[, …]` — за таблицею в «Source Code» |

## Межі фічі й що відкладено

- **Входить**: FR-004-01…12; `config/delivery.yaml` v1 з журналом; пакет `unmask.delivery`;
  схема 004.1; контракт повідомлень; Pillow як залежність; кеш у памʼяті; polling-бот.
- **Не входить** (PRD «Поза межами» + constitution VII): інші мережі, моніторинг/сповіщення,
  ML, авторизація/тарифи, бектест, сховище графа, складний UI, саме розгортання, подача
  (Colosseum/Earn), демо-відео, README-розкриття AI (чекліст подачі — ручні кроки оператора).
- Жодна задача не тягне в «Поза межами» PRD; ядро 001/002/003 не змінюється жодною задачею
  (порушення — `BLOCKED`, не мовчазна правка).

## Порядок реалізації і ризики (дедлайн подачі — 11 жовтня; сьогодні 5 жовтня)

**Вертикальний зріз першим (ціль — вечір 7 жовтня, разом із фронтом демо):**

1. `config/delivery.yaml` v1 + розділ журналу + контракт схеми (без цього жодна показова стала
   не має права зʼявитись у коді).
2. `report.py` + схема-тести (документ слово в слово з 003 + походження).
3. `service.py` + `cache.py` на записаних транспортах (живий конвеєр без мережі в тестах).
4. `http.py` (чисті функції маршруту) → ручний `curl` проти локального сервера.
5. `render.py` (Pillow) → `bot.py` з несправжнім транспортом → наскрізний тест.
6. `main.py` + `scripts/serve_unmask.py` → живий дим з ключами оператора (поза типовим прогоном).

**Ризики:**

| Ризик | Що робимо |
|---|---|
| Pillow важко ставиться на сервері демо | Pillow — wheels під усі платформи; перевірка — `uv sync` на чистому оточенні як крок плану |
| Telegram-ліміти довжини ріжуть докази | стиснення з лічильником + повний текст другим повідомленням (R-8); контракт фіксує формат |
| Холодний живий запит > 60 с на великому токені | бюджет збору 001 уже обмежує час; вимір — записаний сценарій типового розміру, не жива мережа |
| Два одночасні запити подвоюють RPC | single-flight у `cache.py` з тестом на лічильник транспорту |
| Спокуса «підкрутити» текст відповіді під конкретний токен | відповідь — детермінована функція результату 003; golden-тести на записах це ловлять |
| Зміна ядра 001/002/003 «заодно» | заборонено розділом «Не входить»; знадобиться — окрема задача поза 004 з власним spec-циклом |

## Відкриті питання (не BLOCKED — архітектура однакова за будь-якої відповіді)

| # | Питання | Обрано в плані | Якщо відповідь інша (ціна) |
|---|---|---|---|
| Q1 | Персистентність кешу | памʼять процесу, без TTL (R-5) | диск/TTL — задача після хакатону; контракт відповіді не змінюється |
| Q2 | Polling vs вебхук | long polling в одному процесі (assumption spec) | вебхук — лише `bot.py` + `main.py`, семантика повідомлень та сама |
| Q3 | Текст доказів понад ліміт | стиснення + повний текст другим повідомленням (R-8) | файл-документ замість другого повідомлення — той самий callback, інший виклик транспорту |

## Complexity Tracking

Порушень Constitution Check немає — таблиця не заповнюється.
