# Tasks: Публічний API і Telegram-бот доставки аналізу (004)

**Input**: `plan.md`, `spec.md`, `research.md`, `data-model.md`, `contracts/{api-response.schema.json, bot-messages.md}`, `quickstart.md` з `/specs/004-api-bot-delivery/`. Дата декомпозиції — 2026-10-05; дедлайн подачі — 11 жовтня (гейт конституції).

**Tests**: обов'язкові й пишуться першими (принцип II). У кожній задачі названо тест(и), які мають бути червоними до коду — з очікуваної причини (неправильна поведінка/значення), а не через `ImportError`. Мережі й сокетів у тестах немає: RPC — записані `IngestResult`-документи (`tests/fixtures/real/*.json`), Telegram — несправжній транспорт із журналом викликів, HTTP — викликом функцій обробника (гард `tests/conftest.py`). Еталони — незалежні: `api-response.schema.json`, `bot-messages.md`, ручні очікування з явними числами. Тест, чий `expected` отримано з коду, що тестується, ревʼюер відхиляє.

**Organization**: фаза 1 — конфіг із журналом і залежність Pillow; фаза 2 — ядро (`report` + `service` + `cache` на записаних транспортах); фаза 3 — US1 (HTTP API) 🎯 MVP; фаза 4 — US3 (PNG, потрібен боту); фаза 5 — US2 (бот); фаза 6 — US4 (кеш/час), wiring, e2e, межі, гейти.

## Format: `- [ ] T-NNN [FR-004-NN, …] [USn?] [P?] critical?: опис — тест: …; мутація: …; файли: …`

- Нумерація `T-NNN` наскрізна по проєкту: фіча 003 закінчилась на **T-087**; ця фіча — **T-088…T-098**.
- `[FR-…]` — кожна задача несе щонайменше одну вимогу; `scripts/trace.py --check` перевіряє покриття всіх 12 (таблиця «Покриття вимог» нижче).
- `[P]` — можна виконувати паралельно з сусідніми `[P]` (справді різні файли; жодної залежності від незавершених задач).
- `critical:` — помилка тихо спотворює відповідь (числа, докази, смуга, кеш, картинка), а не ламає збірку → виконує `implementer-senior` (**sonnet**, конституція 1.1.1). Решта → `implementer` (**haiku**): задачі дрібні, самодостатні, з точними іменами файлів/функцій/тестів; де потрібне судження — підіймай до architect, не вигадуй.
- Маркери у шапках файлів — за таблицею «Маркери трасування» `plan.md`. `__init__.py`, `scripts/serve_unmask.py`, тестові дублі транспортів — `# trace: ignore-file`.
- Мутаційна стійкість: де вказано «мутація: …», тест мусить червоніти на названій заміні; ревʼюер перевіряє вручну.

## Path Conventions

Один проєкт: `src/unmask/delivery/…` (нове), `src/unmask/{ingest,graph,clusters}/…` (001/002/003, не змінюються), `tests/…`, `config/…`, `scripts/…` від кореня. Записані документи для тестів доставки — `tests/fixtures/real/*.json` (наявні; нові не потрібні, крім журналів викликів у самих тестах).

## Передумови

- `uv sync` ПІСЛЯ додавання Pillow у `pyproject.toml` (T-088; R-3 — єдина нова залежність фічі).
- `git config core.hooksPath .githooks`.
- Ядро 001/002/003 заморожене: жодна задача 004 не править `src/unmask/{ingest,graph,hubs,clusters}/`, `config/{ingest,hubs,hub_addresses,clusters}.yaml` і їхні тести. Потрібна зміна ядра → `BLOCKED`, не мовчазна правка.

---

## Phase 1: Setup (конфіг, журнал, залежність)

**Purpose**: жодна показова стала не з'являється в коді без YAML-запису; Pillow встановлено й зафіксовано.

- [x] T-088 [FR-004-11] critical: Версіонований `config/delivery.yaml` v1, запис у журналі, завантажувач, Pillow. `config/delivery.yaml` — `version: 1` + лише показові/операційні ключі (`http_port`, `png_width/height`, `cluster_palette`, `address_prefix_len`, `evidence_preview_limit`, `request_timeout_seconds`; ЖОДНИХ порогів/ваг/меж); критерій: `content_digest` стабільний, коментарі/порядок ключів на дайджест не впливають. `config/CHANGELOG.md`: новий розділ `# config/delivery.yaml` **ПЕРЕД** `# config/ingest.yaml` (ingest лишається останнім), один запис `## 1 — 2026-10-0X` з останнім рядком `sha256: <digest>`; інші розділи не чіпати. `pyproject.toml`: `+ Pillow` у `dependencies` (з записом у `uv.lock` через `uv sync`). `src/unmask/delivery/__init__.py` (`# trace: ignore-file`); `src/unmask/delivery/config.py` (поза таблицею модулів плану — лише завантажувач; frozen-датаклас, межі, `ConfigError(field)` з `hubs.config`; журнал НЕ читає). Захист журналу — ті самі тести, що T-060 003, але для `delivery.yaml` — тест: `tests/test_delivery_config.py::test_shipped_delivery_yaml_passes_check_changelog`, `::test_entry_sha_equals_content_digest`, `::test_delivery_section_sits_before_ingest_and_ingest_is_last`, `::test_any_value_change_without_entry_is_detected` (по всіх ключах), `::test_unknown_field_rejected`, `::test_pillow_importable_and_version_pinned_in_lockfile`; мутація: прибрати `sha256:` у копії журналу → `ConfigError`; файли: `config/delivery.yaml`, `config/CHANGELOG.md`, `pyproject.toml`, `uv.lock`, `src/unmask/delivery/__init__.py`, `src/unmask/delivery/config.py` (`# impl: FR-004-11`), `tests/test_delivery_config.py` (`# verifies: FR-004-11`)

**Checkpoint**: `uv sync` чистий; `python3 scripts/trace.py --check` зелений.

---

## Phase 2: Foundational — звіт, сервіс, кеш (блокують US1…US4)

**Purpose**: документ відповіді слово в слово з 003 + живий конвеєр на записаних транспортах + кеш із single-flight. Без HTTP і бота.

- [x] T-089 [FR-004-01, FR-004-12] [P] critical: Звіт відповіді (контракт §004.1). `src/unmask/delivery/report.py::build_report(ingest_result, graph_result, cluster_result) -> dict`, `to_json(doc) -> str` (сортовані ключі, компактні розділювачі, `allow_nan=False`): кластери/частки/впевненості/докази/`risk_score`/смуга — ті самі значення 003 (жодного перерахунку); `supply_share` + явний `supply_share_denominator`; `provenance` (4 версії, `analyzed_at`, повнота, `band_reasons`); `insufficient_data` копіюється як є. Невідомий тип входу → `TypeError` — тест: `tests/test_delivery_report.py::test_numbers_are_verbatim_from_003_result` (золотий документ з `c_two_clusters` + `c_incomplete`, поле за полем), `::test_to_dict_validates_against_api_response_schema[...]` (4 ручні документи: ok з кластерами / чисто без кластерів / insufficient_data / порожній вхід), `::test_insufficient_data_never_becomes_clean` (мутаційний вартовий: підміна смуги → схема+тест червоні), `::test_provenance_carries_all_four_versions_and_completeness`, `::test_schema_rejects_cluster_without_evidence`, `::test_to_json_is_sorted_compact_and_stable`; мутація: `supply_share` з округленням замість значення 003 → червоний на золотому; файли: `src/unmask/delivery/report.py` (`# impl: FR-004-01, FR-004-12`), `tests/test_delivery_report.py` (`# verifies: FR-004-01, FR-004-12`)
- [x] T-090 [FR-004-02, FR-004-07] critical: Живий конвеєр і мапінг відмов. `src/unmask/delivery/service.py::DeliveryService(ingest_service, graph_service, cluster_service, cache)`, `.analyze(mint) -> dict`: валідація mint → `Rejection`→документ-помилка `{error:{kind,detail}}` (не виняток); кеш-хіт → документ без звернень; інакше `IngestService.collect` → `GraphService.analyze` → `ClusterService.analyze` → `build_report` → покласти в кеш (повні й неповні — з позначкою; відмови НЕ кешуються); збій середовища (немає ключа) → виняток із назвою змінної наверх (fail-fast, не документ). Записаний `RpcSource`-дубль у тесті (віддає `tests/fixtures/real/*.json` за mint; лічильник викликів) — тест: `tests/test_delivery_service.py::test_live_pipeline_uses_same_configs_and_versions` (документ несе версії завантажених конфігів), `::test_rejection_maps_to_error_document_not_exception[...]` (invalid_address/token_not_found), `::test_incomplete_collect_maps_to_insufficient_data_with_reasons`, `::test_second_call_hits_cache_without_source_calls`, `::test_missing_env_key_fails_fast_with_var_name`; мутація: підмінити `hub_config_version` у відповіді → червоний; пропустити `GraphService` (звіт напряму зі збору) → червоний на походженні; файли: `src/unmask/delivery/service.py` (`# impl: FR-004-02, FR-004-06, FR-004-07`), `tests/test_delivery_service.py` (`# verifies: FR-004-02, FR-004-07`)
- [x] T-091 [FR-004-06] [P] critical: Кеш із single-flight. `src/unmask/delivery/cache.py::DeliveryCache`: `get(mint)`, `store(mint, doc)`, `single_flight(mint, build)` (одночасні запити одного нового токена чекають один `build`); ключ — точна адреса; інвалідації немає (R-5); потоко-безпечність (`threading`) — тест: `tests/test_delivery_cache.py::test_repeat_returns_byte_identical_document_without_rebuild` (лічильник `build` == 1), `::test_concurrent_duplicates_build_once` (N потоків → 1 `build`, N однакових документів), `::test_distinct_mints_do_not_share_entries`, `::test_rejection_is_never_cached` (через сервіс T-090: дві відмови → два звернення); мутація: прибрати блокування → червоний на конкурентному тесті (флейк допустимий 1/5 — ревʼюер ганяє 5 разів); файли: `src/unmask/delivery/cache.py` (`# impl: FR-004-06`), `tests/test_delivery_cache.py` (`# verifies: FR-004-06`)

**Checkpoint**: `uv run pytest tests/test_delivery_report.py tests/test_delivery_service.py tests/test_delivery_cache.py tests/test_delivery_config.py -q` зелений; `python3 scripts/trace.py --check` зелений.

---

## Phase 3: User Story 1 — перевірка токена через API (Priority: P1) 🎯 MVP

**Goal**: `GET /api/token/{mint}` повертає документ за схемою 004.1; 4xx з поясненням на поганому вході; жодної авторизації.

**Independent Test**: `tests/test_delivery_http.py::test_token_endpoint_returns_schema_valid_document` — записаний транспорт, документ валідний проти схеми, числа збігаються з `expected_table.md` 003 для того самого токена.

- [x] T-092 [FR-004-01, FR-004-07, FR-004-08] [US1] Чисті функції маршруту без сокетів. `src/unmask/delivery/http.py::route(method, path)`, `handle_token_request(mint, service) -> (status, body)`, `handle_health() -> (200, …)`; `ThreadingHTTPServer`-обгортка лише запускає (`serve_forever` не тестується сокетами — гард); `401/403` не існує як гілка (FR-004-08 — негативна перевірка перебором маршрутів) — тест: `tests/test_delivery_http.py::test_token_endpoint_returns_schema_valid_document`, `::test_invalid_mint_returns_4xx_with_explanation_not_stacktrace`, `::test_unknown_token_returns_4xx_rejection_mapped`, `::test_healthz_ok`, `::test_no_auth_branch_exists` (AST: жодного `401`/`403`/`Authorization` у `http.py`), `::test_wrong_method_returns_405`; мутація: `200`→`201` на успіху → червоний; текст стека в тілі 4xx → червоний; файли: `src/unmask/delivery/http.py` (`# impl: FR-004-01, FR-004-07, FR-004-08`), `tests/test_delivery_http.py` (`# verifies: FR-004-01, FR-004-07, FR-004-08`)

**Checkpoint**: US1 незалежно працездатна (тести + ручний `curl` локального сервера з записаним джерелом); `quickstart.md` §1 проходить.

---

## Phase 4: User Story 3 — картинка графа (Priority: P2; потрібна US2)

**Goal**: статичне PNG з документа: кластери розрізнені, легенда з частками; заглушка на порожньому.

**Independent Test**: `tests/test_delivery_render.py::test_two_clusters_are_visually_distinct_with_legend` — PNG відкривається, кольори двох кластерів різні, легенда містить частки.

- [x] T-093 [FR-004-05] [US3] critical: Серверний PNG-рендер (Pillow). `src/unmask/delivery/render.py::render_png(doc) -> GraphImage` (байти + `width`/`height` + `empty`): детермінована розкладка за ключами з даних (сортування гаманців/кластерів — ті самі ключі 003); колір кластера — за індексом у впорядкованому списку; адреси — скорочено (`prefix…suffix` з `delivery.yaml`); показові сталі — лише з `delivery.yaml` (жодних чисел у коді, FR-004-11); порожній документ → заглушка з підписом — тест: `tests/test_delivery_render.py::test_two_clusters_are_visually_distinct_with_legend` (декодування PNG у тесті, вибірка пікселів областей кластерів), `::test_empty_result_gives_captioned_placeholder`, `::test_render_is_deterministic_for_same_document` (двічі → ті самі байти), `::test_no_result_affecting_constants_in_code` (AST/grep: у `render.py` немає чисел, що змінюють склад/частки — лише розміри/кольори з конфігу), `::test_long_addresses_are_truncated_with_config_lengths`; мутація: поміняти кольори двох кластерів місцями → червоний на тесті легенди; прибрати кластер з картинки → червоний; файли: `src/unmask/delivery/render.py` (`# impl: FR-004-05`), `tests/test_delivery_render.py` (`# verifies: FR-004-05`)

**Checkpoint**: `quickstart.md` §3 проходить.

---

## Phase 5: User Story 2 — бот (Priority: P1; після US3)

**Goal**: `/check <mint>` → одне повідомлення (ризик, частки, PNG, кнопка «докази»); callback → повний текст доказів; помилки — текстом, процес живий.

**Independent Test**: `tests/test_delivery_bot.py::test_check_returns_photo_message_with_evidence_button` — несправжній транспорт, журнал викликів: рівно один `send_photo` з caption за контрактом і кнопкою; callback → `send_message` з доказами.

- [x] T-094 [FR-004-03, FR-004-04, FR-004-07] [US2] critical: Polling-бот на сирому Bot API. `src/unmask/delivery/bot.py::BotTransport` (живий на `httpx`: `send_message/send_photo/answer_callback/edit_message`; несправжній — журнальний, у тестах), `handle_check(text, service, transport, chat_id)`, `handle_evidence(callback, service, transport)`, `run_polling(transport, service)` (цикл — інтеграційно, з несправжнім транспортом і прапорцем зупинки); форматування — дослівно за `contracts/bot-messages.md` (рядок смуги, топ-3, походження; докази — тип/джерело/вікно; стиснення з лічильником за R-8) — тест: `tests/test_delivery_bot.py::test_check_returns_photo_message_with_evidence_button`, `::test_evidence_callback_sends_full_proof_text` (кожен доказ кластера присутній; число без доказів неможливе), `::test_evidence_overflow_is_truncated_with_remainder_count` (синтетично довгий перелік → стиснення + `…і ще K`), `::test_no_clusters_message_says_so_without_evidence_button`, `::test_invalid_mint_replies_error_and_stays_alive` (цикл переживає виняток обробника), `::test_unknown_command_is_ignored_silently`, `::test_stale_callback_answers_expired_rerun_check`; мутація: пропустити один доказ у тексті → червоний; переплутати частки двох кластерів → червоний; файли: `src/unmask/delivery/bot.py` (`# impl: FR-004-03, FR-004-04, FR-004-07`), `tests/test_delivery_bot.py` (`# verifies: FR-004-03, FR-004-04`)

**Checkpoint**: US2 незалежно працездатна; `quickstart.md` §2 проходить; демо-повідомлення показується власнику.

---

## Phase 6: User Story 4, wiring, наскрізні й межі (Priority: P2 + гейти)

**Goal**: кеш/час доведено виміром; точка входу з env; e2e без мережі; AST-межі; усі гейти зелені.

- [x] T-095 [FR-004-10] [P] Точка входу й секрети. `src/unmask/delivery/main.py::main(argv) -> int` (`--port`, `--no-bot` для диму без токена; `UNMASK_RPC_URL` через `HttpRpcSource.from_env`-патерн, `UNMASK_BOT_TOKEN` новим тим самим патерном; відсутня змінна → fail-fast із назвою змінної ДО відкриття портів); `scripts/serve_unmask.py` (`# trace: ignore-file`) — тонка обгортка — тест: `tests/test_delivery_main.py::test_missing_rpc_key_fails_fast_with_var_name` (без мережі: падає до будь-якого звернення), `::test_missing_bot_token_with_bot_enabled_fails_fast`, `::test_no_bot_flag_runs_http_only`, `::test_no_secret_literals_in_repo` (grep по `src/unmask/delivery/` і `scripts/`: жодних `123:`, `xox`, `sk-`, `helius`-ключів — список літералів у тесті); файли: `src/unmask/delivery/main.py` (`# impl: FR-004-10`), `scripts/serve_unmask.py`, `tests/test_delivery_main.py` (`# verifies: FR-004-10`)
- [x] T-096 [FR-004-09] [P] Наскрізний тест без мережі. `tests/test_delivery_e2e.py`: записаний транспорт (3 інсайдерські + 3 чисті з `tests/fixtures/real/`) → `DeliveryService` → HTTP-обробник І бот-обробник → документи валідні проти схеми 004.1; інсайдерські показують кластери з доказами, чисті — ні (SC-005, відповідно до еталона 003 `expected_table.md`) — тест: `::test_three_insider_three_clean_end_to_end_matches_003_baseline` (числа `risk_score`/смуги збігаються з еталоном 003), `::test_bot_and_http_agree_byte_for_byte_on_same_mint` (бот бере текст із того самого документа, що й API); файли: `tests/test_delivery_e2e.py` (`# verifies: FR-004-09`)
- [x] T-097 [FR-004-11] [P] AST-тест меж (plan «Правило залежностей»; хелпери `_imports`/`_under` скопіювати з `tests/test_clusters_boundaries.py`, не імпортувати тестовий модуль): `tests/test_delivery_boundaries.py::test_package_has_exactly_the_documented_modules` (`__init__, config, report, render, cache, service, http, bot, main`), `::test_core_imports_only_allowed_modules[...]` (з `unmask` — моделі/сервіси/конфіги 001–003 і `unmask.delivery.*`; `httpx` лише в `bot.py`; `PIL` лише в `render.py`; `yaml` лише в `config.py`), `::test_no_forbidden_substrings[...]` (внутрішності збору/побудови графа/відсікання, сокети в `src`, `os.environ` поза `main.py`), `::test_no_result_affecting_constants_outside_yaml` (у `report/service/cache/http/bot` немає числових літералів, що змінюють склад/частки/ризик — білий список: HTTP-статуси, ліміт 4000 символів, порт умовчання); файли: `tests/test_delivery_boundaries.py` (`# verifies: FR-004-11`)
- [x] T-098 [FR-004-06] [P] Час холодного й повторного запиту (SC-001/002; записаний сценарій типового розміру, не жива мережа; годинниковий тест — під маркером `perf`, як T-086 003): `tests/test_delivery_perf.py` — `@pytest.mark.perf::test_cold_request_under_60s_on_recorded_typical_token` (медіана 3 прогонів повного `analyze` на записі ~300 покупців < 60 с — з великим запасом, це детектор регресу, не гонка), `::test_repeat_request_needs_no_transport_calls` (без маркера: лічильник записаного джерела стоїть); файли: `tests/test_delivery_perf.py` (`# verifies: FR-004-06`)

**Checkpoint**: `uv run pytest -q` зелений (типовий набір); `uv run pytest -m perf tests/test_delivery_perf.py -q` зелений; `python3 scripts/trace.py --check` зелений; `evaluate --check` 003 лишається 0 (ядро не чіпали); `quickstart.md` §1–4 вручну; ручний дим §5 з ключами оператора.

---

## Dependencies & Execution Order

### Phase Dependencies

| Фаза | Залежить від | Блокує |
|---|---|---|
| 1 Setup (T-088) | `uv sync` (Pillow) | усе |
| 2 Ядро (T-089…T-091) | T-088 (показові сталі й дайджест) | фази 3–6 |
| 3 US1 API (T-092) | фаза 2 | гейт A; нічого не блокує далі (незалежний зріз) |
| 4 US3 PNG (T-093) | фаза 2; Pillow з T-088 | фазу 5 (бот прикріплює PNG) |
| 5 US2 бот (T-094) | T-093 (рендер), фаза 2 | фазу 6 |
| 6 Wiring/e2e/межі/час (T-095…T-098) | фази 3–5 | гейт C |

### Внутрішні залежності

| Задача | Після | Примітка |
|---|---|---|
| T-088 | — | перша; розділ CHANGELOG між `hub_addresses` і `ingest` |
| T-089 | T-088 | схема 004.1 уже є (план); показові сталі з YAML |
| T-090 | T-089 | звіт як вихід конвеєра |
| T-091 | — (після T-088) | власний файл; паралельно з T-089/T-090 |
| T-092 | T-089, T-090 | обробник викликає сервіс |
| T-093 | T-089 | рендер бере документ звіту; паралельно з T-092 |
| T-094 | T-090, T-093 | бот викликає сервіс і рендер |
| T-095 | T-092, T-094 | wiring останнім |
| T-096 | T-092, T-094 | e2e збирає готове |
| T-097 | T-095 | сканує всі модулі пакета |
| T-098 | T-090 | вимір на готовому сервісі |

### Спільні файли (черга)

| Файл | Задачі (у порядку) | Правило |
|---|---|---|
| `config/CHANGELOG.md` | T-088 | розділ `delivery` перед `ingest`; `ingest` останній |
| `config/delivery.yaml` | T-088 | далі — лише версією із записом (T-088 задає прецедент) |
| `pyproject.toml` / `uv.lock` | T-088 | лише `+ Pillow`; інших залежностей фіча не додає |

### Parallel Opportunities

- Фаза 2: T-089 ∥ T-091 (різні файли); T-090 після T-089.
- Фази 3–4: T-092 ∥ T-093 (HTTP проти PNG; спільне лише `report.py`, уже готовий).
- Фаза 6: T-095 ∥ T-096 ∥ T-097 ∥ T-098 (різні файли; T-097/T-098 читають готовий код).

## Parallel Example: старт ядра

```bash
# Після T-088:
Task: "T-089 delivery/report.py + tests/test_delivery_report.py"   # senior
Task: "T-091 delivery/cache.py + tests/test_delivery_cache.py"     # implementer
# Потім:
Task: "T-090 delivery/service.py + tests/test_delivery_service.py" # senior
```

## Implementation Strategy

### MVP First (фази 1–3, US1)

1. T-088: YAML з журналом + Pillow.
2. T-089 → T-090 → T-091: звіт, конвеєр, кеш на записаних транспортах.
3. T-092: HTTP. **STOP and VALIDATE — гейт A**: ендпоінт віддає schema-valid документ, числа збігаються з еталоном 003.

### Incremental Delivery

- + фаза 4 (PNG) → фаза 5 (бот): **гейт B** — демо-повідомлення власнику на записах.
- + фаза 6: wiring, e2e, межі, час → **гейт C** (повний `pytest`, `trace.py`, `evaluate --check` 003, ручний дим з ключами) → подача.

### Розподіл за ролями (конституція 1.1.1)

- `implementer-senior` (**sonnet**): усі рядки з `critical:` — **T-088, T-089, T-090, T-091, T-093, T-094** (6): конфіг і журнал, числа звіту слово в слово, конвеєр і мапінг відмов, коректність кешу, відповідність картинки даним, тексти бота з доказами.
- `implementer` (**haiku**): **T-092, T-095, T-096, T-097, T-098** (5): тонкий HTTP-роутер, wiring й секрети, збірка e2e з готових частин, AST-правило, perf-вимір. Якщо виконавець мусить обирати семантику — `BLOCKED` до architect, не вгадувати.
- `reviewer` (opus) після кожної задачі; перед `[x]` — `python3 scripts/trace.py --check` зелений і мутаційна перевірка там, де задача її називає. Ескалація — за правилами `CLAUDE.md`/`AGENTS.md`.

## Покриття вимог

| FR | Задачі |
|---|---|
| FR-004-01 | T-089, T-092 |
| FR-004-02 | T-090 |
| FR-004-03 | T-094 |
| FR-004-04 | T-094 |
| FR-004-05 | T-093 |
| FR-004-06 | T-090, T-091, T-098 |
| FR-004-07 | T-090, T-092, T-094 |
| FR-004-08 | T-092 |
| FR-004-09 | T-096 |
| FR-004-10 | T-095 |
| FR-004-11 | T-088, T-093, T-097 |
| FR-004-12 | T-089, T-096 |

## Гейти фаз (не задачі; виконує оркестратор з ревʼюером)

- **Гейт A — після фази 3 (вертикальний зріз API)**: `uv run pytest tests/test_delivery_*.py -q` (готові файли) зелений; документ для збереженого токена побайтово збігається з перерахунком з ядра 003; `python3 scripts/trace.py --check` зелений.
- **Гейт B — демо-повідомлення**: несправжній транспорт показує власнику повідомлення за контрактом `bot-messages.md` на записах (без мережі); рішення людини — йдемо в живу демонстрацію.
- **Гейт C — перед мержем фічі**: (1) `uv run pytest -q` зелений; (2) `python3 scripts/trace.py --check` зелений і `docs/traceability.md` перегенеровано; (3) `evaluate --check` фічі 003 → 0 (ядро не зачеплено); (4) `quickstart.md` §1–4 вручну + §5 з ключами оператора; (5) ревʼю фічі `reviewer` проти всіх FR-004-01…12 і SC-001…005; (6) `known-issues.md` 004 створюється після ревʼю з його зауважень.
