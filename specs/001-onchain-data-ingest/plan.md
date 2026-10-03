# Implementation Plan: Збір ончейн-даних по токену

**Branch**: `001-onchain-data-ingest` (робота в `main`, окрема гілка не створюється) | **Date**: 2026-10-03 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/001-onchain-data-ingest/spec.md`

## Summary

За адресою mint повернути перших N покупців (за часом першої купівлі, детерміновано) і всі вхідні перекази SOL/SPL, що фінансували кожного до його першої купівлі, на глибину до `funding_depth` стрибків назад, з чесним статусом повноти, явними відмовами для некоректної/неіснуючої адреси та кешем повних результатів у пам'яті процесу.

Підхід: один Python-пакет `unmask.ingest` з синхронним детермінованим ядром (перелічення покупців → BFS фінансування по рівнях → виведення повноти), доступом до Solana JSON-RPC лише через `RpcSource` (Protocol) із двома реалізаціями — фікстурною (тести, повтор) і HTTP (RPC Fast, остання фаза), конфігурацією у версіонованому `config/ingest.yaml` і дворівневим кешем (повні результати / партиційний стан для повторення лише недоотриманого). Рішення, що виходять за букву spec, зафіксовано в [research.md](research.md) R-1…R-4 і повторено в розділі «Відкриті питання».

## Technical Context

**Language/Version**: Python 3.12 (у системі `python3` 3.12.3; графові бібліотеки наступних фіч — networkx тощо)

**Primary Dependencies**: `pyyaml` (конфіг), `solders` (адреси, on-curve), `httpx` (живий адаптер, `MockTransport` у тестах); dev — `pytest`, `jsonschema`

**Storage**: N/A — структури в пам'яті процесу; кеш живе до перезапуску (spec, Assumptions)

**Testing**: `pytest` через `uv run pytest`; мережа заборонена гардом у `tests/conftest.py` (принцип II); джерело даних — записані JSON-фікстури у формі відповідей Solana JSON-RPC (`tests/fixtures/scenarios/*`)

**Target Platform**: Linux-сервер (той самий процес згодом обслуговує HTTP-API і Telegram-бот)

**Project Type**: бібліотека (пакет `src/unmask/ingest`), без CLI і без HTTP-ендпоінту в цій фічі

**Performance Goals**: холодний збір для N=300, depth=2 — повний або явно неповний результат ≤ 40 с (SC-003); повторний запит — з кешу, 0 звернень до джерела (SC-004)

**Constraints**: тести без мережі; усі результато-впливові константи — у `config/ingest.yaml` з версією; детермінований порядок при однакових даних; жоден збій даних не піднімає виняток із `collect` (повертається `Rejection` або `completeness`)

**Scale/Scope**: ≤ 500 покупців, ≤ 3 рівні, порядку 10³–10⁴ транзакцій на прогін; одна мережа (Solana); обсяг — 16 FR, 3 user stories

## Constitution Check

*GATE: перевірено до Phase 0 і повторно після Phase 1 — порушень немає.*

| Принцип | Як виконується в цьому плані | Статус |
|---|---|---|
| I. Трасування | кожна задача `tasks.md` несе `[FR-001-NN]`; розкладка маркерів `impl:`/`verifies:` по файлах — розділ «Маркери трасування»; `__init__.py`, `conftest.py`, генератор фікстур — `trace: ignore-file` | PASS |
| II. Test-First | у кожній задачі названо тест, який пишеться першим; фікстури й гард мережі — у фазах 1–2 до будь-якого коду фіч; `conftest.py` блокує `socket.connect` | PASS |
| III. Версіонування | N, глибина, пороги, бюджет, `collect_spl_inbound`, параметри RPC — у `config/ingest.yaml` з `version` і `config/CHANGELOG.md`; версія потрапляє у `metadata.config_version`; партиційний стан з іншою версією відкидається | PASS |
| IV. Межі модулів | мережа лише в `rpc/http.py`; ядро бачить `RpcSource`; кожен модуль має одну мету й окремий тест (розділ «Модулі») | PASS |
| V. Доказ і чесна неповнота | кожен переказ несе підпис, слот, час, відправника, отримувача, актив, суму; `Completeness.status` похідний і не може бути `complete` при непорожньому `missing`; `unexpanded[]` видимий у відповіді | PASS |
| VI. Хаби окремо | ця фіча хаби **не** відсікає — лише зупиняє розгортання за порогом і позначає вершину; списки адрес бірж і рішення про відсікання — наступна фіча | PASS |
| VII. Лише Solana, на запит | жодного моніторингу, іншої мережі, ML, авторизації, сховища графа; кеш у пам'яті | PASS |
| Ролі | задачі з `critical:` → `implementer-senior`; решта → `implementer` | PASS |

Re-check після Phase 1: data-model і контракти не додали нових зовнішніх залежностей, сховищ чи обходу інтерфейсу. Складність, що потребує обґрунтування: немає (Complexity Tracking порожній).

## Project Structure

### Documentation (this feature)

```text
specs/001-onchain-data-ingest/
├── spec.md                 # вимоги FR-001-01..16 (не змінюється)
├── plan.md                 # цей файл
├── research.md             # R-1…R-13: рішення з обґрунтуванням
├── data-model.md           # сутності, інваріанти, стан збору, кеш
├── quickstart.md           # як переконатися, що фіча працює
├── contracts/
│   ├── rpc-source.md               # RpcSource Protocol + форми JSON-RPC + помилки
│   ├── ingest-service.md           # IngestService.collect — публічний вхід
│   ├── config-ingest.md            # схема config/ingest.yaml і правила CHANGELOG
│   └── ingest-result.schema.json   # JSON Schema серіалізованого результату
├── checklists/requirements.md
└── tasks.md                # T-001…T-021 (speckit-tasks)
```

### Source Code (repository root)

```text
pyproject.toml                       # uv; [project] без build-system; pytest pythonpath=["src"]
config/
├── ingest.yaml                      # версіонована конфігурація (contracts/config-ingest.md)
└── CHANGELOG.md
src/unmask/
├── __init__.py                      # trace: ignore-file
└── ingest/
    ├── __init__.py                  # trace: ignore-file
    ├── config.py                    # load_config → IngestConfig, валідація, ConfigError
    ├── model.py                     # дата-класи, enum'и, Completeness.derive, ключі порядку
    ├── addresses.py                 # is_valid_address, address_type (on-curve)
    ├── budget.py                    # Clock / SystemClock / FakeClock, Deadline
    ├── parse.py                     # RawTransaction → ParsedTx: перекази SOL/SPL, дельти балансів, CorruptRecord
    ├── purchases.py                 # detect_purchases(tx, mint) — правило купівлі (R-2)
    ├── buyers.py                    # перелічення історії mint, перші N, курсор (R-6, R-7)
    ├── funding.py                   # BFS джерел фінансування: межа (R-1), дедуп (R-9), пороги (R-3), SPL (R-8)
    ├── collector.py                 # оркестрація, CollectionState, resume, Completeness
    ├── cache.py                     # ResultCache: complete / partial
    ├── service.py                   # IngestService.collect → IngestResult | Rejection
    ├── serialize.py                 # to_dict / to_json за схемою
    └── rpc/
        ├── __init__.py              # trace: ignore-file
        ├── protocol.py              # RpcSource Protocol, типи, RpcRateLimited/RpcTimeout/RpcUnavailable
        ├── fixture.py               # FixtureRpcSource: rpc.json, курсори, журнал викликів, ін'єкція збоїв
        └── http.py                  # HttpRpcSource: JSON-RPC 2.0 через httpx, batch, retries, мапування помилок

tests/
├── conftest.py                      # trace: ignore-file; autouse-гард мережі; фікстури config/clock/source
├── fixtures/
│   ├── build_fixtures.py            # trace: ignore-file; декларативний опис → scenarios/*/rpc.json + expected.json
│   └── scenarios/
│       ├── basic/      {rpc.json, expected.json}   # 5 покупців, глибина 1–3, пост-купівля, цикл, self, SPL, off_curve
│       ├── hub/        {rpc.json, expected.json}   # вершина з контрагентами > порога; історія > cap
│       ├── corrupt/    {rpc.json}                  # null-транзакція, запис без meta/slot
│       └── notfound/   {rpc.json}                  # mint відсутній; рахунок не-mint
├── test_no_network.py               # verifies: FR-001-15
├── test_config.py                   # verifies: FR-001-01, FR-001-05, FR-001-08, FR-001-14, FR-001-16
├── test_model_completeness.py       # verifies: FR-001-06, FR-001-09, FR-001-10
├── test_rpc_fixture.py              # verifies: FR-001-15
├── test_fixture_builder.py          # verifies: FR-001-15
├── test_addresses.py                # verifies: FR-001-11
├── test_parse_sol.py                # verifies: FR-001-03, FR-001-06
├── test_parse_spl.py                # verifies: FR-001-04, FR-001-06
├── test_parse_corrupt.py            # verifies: FR-001-06, FR-001-09
├── test_purchases.py                # verifies: FR-001-01, FR-001-02
├── test_buyers.py                   # verifies: FR-001-01, FR-001-02
├── test_funding.py                  # verifies: FR-001-03, FR-001-04, FR-001-05, FR-001-07
├── test_funding_limits.py           # verifies: FR-001-08
├── test_collector.py                # verifies: FR-001-09, FR-001-10
├── test_budget.py                   # verifies: FR-001-16
├── test_collector_failures.py       # verifies: FR-001-09, FR-001-10
├── test_resume.py                   # verifies: FR-001-13
├── test_cache.py                    # verifies: FR-001-12
├── test_service_rejections.py       # verifies: FR-001-11
├── test_serialize_contract.py       # verifies: FR-001-06, FR-001-14
└── test_rpc_http.py                 # verifies: FR-001-15
```

**Structure Decision**: один проєкт, src-layout. Пакет `unmask` — спільний корінь для наступних фіч (`unmask.graph`, `unmask.hubs`, …), `unmask.ingest` — цей шар. Поділ на модулі збігається з межами тестування: кожен файл у `src/unmask/ingest/` має рівно один тестовий файл-власник (плюс контрактні тести поверх).

## Модулі: що робить · як користуватись · від чого залежить

| Модуль | Що робить | Інтерфейс | Залежить від |
|---|---|---|---|
| `config` | читає й валідує YAML | `load_config(path) -> IngestConfig` | `pyyaml`, `model` |
| `model` | типи результату й інваріант повноти | дата-класи; `Completeness.derive(missing, buyers)` | — |
| `addresses` | валідність адреси, тип ключа | `is_valid_address(s) -> bool`, `address_type(s) -> AddressType` | `solders` |
| `budget` | час і дедлайн | `Clock`, `Deadline(clock, seconds)` | — |
| `parse` | з сирої транзакції — перекази, дельти, програми | `parse_transaction(raw) -> ParsedTx \| CorruptRecord` | `model` |
| `purchases` | правило купівлі R-2 | `detect_purchases(parsed, mint) -> list[Purchase]` | `parse`, `addresses` |
| `buyers` | перші N покупців з курсором | `enumerate_buyers(source, mint, state, config, deadline)` | `rpc.protocol`, `parse`, `purchases` |
| `funding` | BFS по рівнях з межею, дедупом, порогами | `expand_level(source, state, depth, config, deadline)` | `rpc.protocol`, `parse` |
| `collector` | оркестрація і resume | `collect(state, source, config, clock) -> IngestResult` | `buyers`, `funding`, `budget`, `model` |
| `cache` | два сховища | `get_complete / put_complete / get_partial / put_partial / drop_partial` | `model` |
| `service` | публічний вхід | `IngestService.collect(mint) -> IngestOutcome` | `addresses`, `cache`, `collector`, `rpc.protocol` |
| `serialize` | JSON за контрактом | `to_dict(outcome)`, `to_json(outcome)` | `model` |
| `rpc.protocol` | межа зовнішнього світу | `RpcSource`, винятки, типи | — |
| `rpc.fixture` | повтор записаних відповідей | `FixtureRpcSource(dir, failures, clock)` | `rpc.protocol` |
| `rpc.http` | живий провайдер | `HttpRpcSource(url, rpc_cfg, transport)` | `rpc.protocol`, `httpx` |

Правило залежностей: стрілки йдуть лише вниз таблицею до `model`/`protocol`; `rpc.http` ніким у ядрі не імпортується (підставляється викликачем).

## Маркери трасування

| Файл | Шапка |
|---|---|
| `src/unmask/__init__.py`, `src/unmask/ingest/__init__.py`, `src/unmask/ingest/rpc/__init__.py` | `# trace: ignore-file` |
| `config.py` | `# impl: FR-001-01, FR-001-05, FR-001-08, FR-001-14, FR-001-16` |
| `model.py` | `# impl: FR-001-06, FR-001-09, FR-001-10, FR-001-14` |
| `addresses.py` | `# impl: FR-001-11` |
| `budget.py` | `# impl: FR-001-16` |
| `parse.py` | `# impl: FR-001-03, FR-001-04, FR-001-06, FR-001-09` |
| `purchases.py`, `buyers.py` | `# impl: FR-001-01, FR-001-02` |
| `funding.py` | `# impl: FR-001-03, FR-001-04, FR-001-05, FR-001-07, FR-001-08` |
| `collector.py` | `# impl: FR-001-09, FR-001-10, FR-001-13, FR-001-16` |
| `cache.py` | `# impl: FR-001-12, FR-001-13` |
| `service.py` | `# impl: FR-001-11, FR-001-12, FR-001-13` |
| `serialize.py` | `# impl: FR-001-06, FR-001-14` |
| `rpc/protocol.py`, `rpc/fixture.py`, `rpc/http.py` | `# impl: FR-001-15` |
| `tests/conftest.py`, `tests/fixtures/build_fixtures.py` | `# trace: ignore-file` |
| кожен `tests/test_*.py` | `# verifies: FR-001-NN[, …]` — за таблицею в «Source Code» |

`.json`-фікстури й `config/ingest.yaml` маркерів не потребують (не під `IMPL_ROOTS`, JSON не сканується).

## Межі фічі й що відкладено

- **Входить**: усе з FR-001-01..16, фікстурне джерело, живий HTTP-адаптер (фаза 6, deferrable — обґрунтування R-4).
- **Не входить**: відсікання хабів (принцип VI, окрема фіча), граф, кластеризація, HTTP-API, бот, CLI, персистентний кеш, запис реальних фікстур з мережі (ручний крок пізніше), мультичейн.
- Жодна задача не тягне в список «Поза межами» PRD.

## Відкриті питання (не BLOCKED — архітектура однакова за будь-якої відповіді; змінюється одна задача)

| # | Питання | Обрано в плані | Якщо відповідь інша |
|---|---|---|---|
| Q1 | Межа часу на глибині ≥ 2: причинна (раніше за ребро, R-1) чи буквальна (раніше за першу купівлю покупця)? | причинна | змінити `cutoff_for()` у `funding.py` і фікстуру `basic` (T-012) |
| Q2 | Правило купівлі без списку DEX (R-2) прийнятне? | балансові дельти з поправкою на fee/rent | додати `dex_programs` у YAML і фільтр у `purchases.py` (T-010) |
| Q3 | Додаткові YAML-поля поза spec: `max_signatures_per_wallet`, `collect_spl_inbound` (R-3, R-8) | додано, версіоновано | прибрати поля і відповідні гілки у `funding.py` (T-002, T-013) |
| Q4 | Порядок купівель в одному слоті — за підписом (R-6), не за позицією в блоку | за підписом | `getBlock` у `RpcSource` і ключ порядку (T-011) |
| Q5 | Жива реалізація RPC у цій фічі (R-4) | так, фаза 6, можна відкласти | вилучити T-021; решта не змінюється |

## Complexity Tracking

Порушень Constitution Check немає — таблиця не заповнюється.
