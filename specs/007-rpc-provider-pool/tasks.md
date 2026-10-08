# Tasks: Пул RPC-провайдерів із паралельним збором (007)

**Input**: `spec.md`, `plan.md`, `research.md`, `data-model.md`, `contracts/pooled-rpc-source.md`,
`contracts/rpc-pool-config.md`, `quickstart.md`.

## Format: `- [ ] T-NNN [FR-007-NN, …] [P?] critical?: опис — тест: …; мутація: …; файли: …`

- Нумерація продовжує 006: **T-131…T-145**.
- `critical:` — помилка тихо спотворює результат збору (порядок, «порожньо» замість неповноти, клас
  помилки, детермінізм, секрети, журнал конфігу) → `implementer-senior` (**sonnet**). Решта →
  `implementer` (**haiku**): імена й значення задано точно; де потрібне судження — назад до architect.
- `[GATE]` — ручний живий крок, не pytest. Тести — без мережі; годинникові — лише `@pytest.mark.perf`.
- Мутація: названа підміна мусить червонити названий тест; ревʼюер перевіряє вручну.
- Кожен новий тест-файл: `# verifies: <FR задачі>`; код — маркери з таблиці `plan.md`.

## Мінімальний вертикальний зріз (до контрольної точки)

T-131 → T-132 → T-133 → T-134 → T-135 → T-136 → T-137 → T-138 → T-139. Після T-139 сервіс доставки
збирає через пул із маршрутизацією за архівністю; відмова частки ще без перебирання (піднімається як у
одинокого адаптера — чесно). T-140…T-145 — після.

---

## Phase 1: Ядро пулу (US1, P1)

- [x] T-131 [FR-007-15] Тестові дублі провайдерів. `tests/rpc_pool_doubles.py` (`# impl: FR-007-15`):
  `ScriptedProvider(inner, *, horizon_block_time=None, failures=(), barrier=None)`, `ScriptedFailure(method,
  calls, exc)`, `SequentialRunner(order="forward"|"reverse"|перестановка)` — точно за контрактом
  `pooled-rpc-source.md` «Тестові дублі». `log` пише кожен виклик (також невдалий) під власним
  `threading.Lock`. — тест: `tests/test_rpc_pool_doubles.py::test_horizon_hides_old_transactions_as_none`,
  `::test_horizon_truncates_signature_pages`, `::test_failure_fires_on_listed_calls_only`,
  `::test_sequential_runner_returns_outcomes_in_task_order_for_reverse`,
  `::test_log_records_share_signatures` (усе на `tests/fixtures/scenarios/basic`); мутація: `<=` замість `<` у
  горизонті → червоний `test_horizon_hides_old_transactions_as_none`; файли: `tests/rpc_pool_doubles.py`,
  `tests/test_rpc_pool_doubles.py` (`# verifies: FR-007-15`).

- [x] T-132 [FR-007-02, FR-007-03, FR-007-04] critical: Паралельний `get_transactions`. `src/unmask/ingest/rpc/pool.py`:
  `Provider`, `apportion`, `ThreadRunner`, `PooledRpcSource.__init__` (валідація конструктора з контракту),
  `get_transactions` — раунди без перебирання (будь-який `RpcError` частки піднімається як є, поза `except`),
  перевірка `deadline.expired()` перед раундом, локи провайдерів, порядок за позицією. Інші методи — поки
  `NotImplementedError`. — тест: `tests/test_rpc_pool.py::test_apportion_largest_remainder_and_ties`
  (`(25,[1,1,1])→[9,8,8]`, `(25,[12,4.5,4.5,4.5])→[12,5,4,4]`, `(0,…)→нулі`),
  `::test_order_preserved_across_two_providers` (50 підписів, кожен провайдер отримав непорожню частку),
  `::test_faster_provider_gets_proportionally_more` (ваги 12 і 4.5 → 36/14 із 50 за методом найбільших залишків; «37/13» арифметично неможливе),
  `::test_completion_order_does_not_change_result` (`SequentialRunner` forward/reverse/перестановка → однакові
  списки), `::test_providers_run_concurrently` (`ThreadRunner` + `Barrier(3, timeout=5)` — без вимірювання
  часу), `::test_virtual_makespan_three_equal_providers_at_most_half` (SC-001: для кожного виклику basic з n ≥ 2 підписів max частки == ceil(n/3) ≤ 0,5·n; сумарно по basic ≈ 0,53, бо підпис, що йде самотнім, неподільний; реплей реальних пакетів, не через IngestService до T-133), `::test_expired_deadline_raises_budget_timeout_with_zero_calls`,
  `::test_empty_signatures_zero_calls`; мутація: порівну замість за вагами → червоний
  `test_faster_provider_gets_proportionally_more`; збирати в порядку завершення → червоний
  `test_completion_order_does_not_change_result`; файли: `src/unmask/ingest/rpc/pool.py`
  (`# impl:` за `plan.md`), `tests/test_rpc_pool.py` (`# verifies: FR-007-02, FR-007-03, FR-007-04`).

- [x] T-133 [FR-007-01, FR-007-04] critical: Одиночні методи, ім'я, потокобезпечність. `pool.py`:
  `get_account_info`, `get_signatures_for_address`, `get_token_accounts_by_owner` через SWRR (поки без правил
  архівності й без перебирання), `name` (спільне ім'я або `"pool"`), `__repr__` (лише імена), `_state_lock`.
  — тест: `tests/test_rpc_pool.py::test_single_calls_rotate_by_weight` (ваги 2:1 → послідовність
  `a,b,a,a,b,a` на 6 викликах), `::test_name_is_common_provider_name_else_pool`,
  `::test_repr_contains_only_provider_names`, `::test_concurrent_calls_never_overlap_on_one_provider`
  (дубль фіксує одночасний вхід лічильником під локом; 4 потоки × 20 викликів → максимум 1);
  мутація: прибрати лок провайдера → червоний `test_concurrent_calls_never_overlap_on_one_provider`;
  файли: `pool.py`, `tests/test_rpc_pool.py`.

- [x] T-134 [FR-007-01, FR-007-04, FR-007-05] critical: Еквівалентність пулу одиночному джерелу.
  Лише тести (код — T-132/T-133). `IngestService(cfg, source, FakeClock(...))`: одиночний `FixtureRpcSource`
  vs пул із трьох `ScriptedProvider` (власні `FixtureRpcSource`, усі архівні, ваги 12/4.5/4.5,
  `SequentialRunner("reverse")`) на сценаріях `basic`, `hub`, `corrupt`, `swapsend` → `to_json` результату
  побайтово рівний; resume: збій `FailFor` на всіх провайдерах першого прогону → `resume` → дорівнює свіжому
  (порівняння як у `tests/test_resume.py::_golden_view`). — тест:
  `tests/test_rpc_pool_equivalence.py::test_pool_equals_single[basic|hub|corrupt|swapsend]`,
  `::test_pool_result_independent_of_runner_order` (SC-003), `::test_resume_through_pool_equals_fresh`,
  `::test_core_rpc_calls_unchanged_by_pool`; мутація: пул повертає частки в порядку провайдерів, а не
  позицій → червоний `test_pool_equals_single`; файли: `tests/test_rpc_pool_equivalence.py`
  (`# verifies: FR-007-01, FR-007-04, FR-007-05`).

## Phase 2: Маршрутизація за архівністю (US2, P1)

- [x] T-135 [FR-007-06, FR-007-07] critical: Вік і підтвердження «не знайдено» для транзакцій. `pool.py`:
  кеш віку з відповідей `get_signatures_for_address` (FIFO ≤ `age_cache_entries`, лише int `blockTime`);
  придатність провайдера за віком і `skip_methods`; групування позицій за множиною придатних; `None` від
  неархівного → раунд «лише архівні», `not_found_escalations`; без придатного архівного →
  `RpcUnavailable("history beyond provider retention")` до звернень. — тест:
  `tests/test_rpc_pool_routing.py::test_known_old_transaction_never_sent_to_nonarchival` (US2-1, журнал дубля),
  `::test_unknown_age_none_from_nonarchival_confirmed_by_archival` (US2-2),
  `::test_no_archival_unknown_age_none_raises_unavailable` (US2-3),
  `::test_fresh_transaction_served_by_nonarchival` (US2-4: неархівний отримав непорожню частку),
  `::test_data_answer_preferred_over_none` (FR-007-07 розбіжність),
  `::test_age_cache_bounded_fifo`; мутація: прийняти `None` від неархівного як остаточне → червоний
  `test_unknown_age_none_from_nonarchival_confirmed_by_archival`; `>` замість `>=`/`≤` у межі віку — тест на
  межі `retention_seconds` червоніє; файли: `pool.py`, `tests/test_rpc_pool_routing.py`
  (`# verifies: FR-007-06, FR-007-07`).

- [ ] T-136 [FR-007-06, FR-007-07, FR-007-04] critical: Підписи — архівним; коротка сторінка неархівного не
  остаточна. `pool.py`: `get_signatures_for_address` — лише архівні, поки є придатний архівний; неархівний —
  запасний, коротка/порожня сторінка → `RpcUnavailable("history beyond provider retention")`; `skip_methods`
  в одиночних методах. — тест: `tests/test_rpc_pool_routing.py::test_signatures_go_to_archival_when_present`,
  `::test_nonarchival_short_page_raises_unavailable`, `::test_nonarchival_full_page_accepted`,
  `::test_skip_methods_respected`; `tests/test_rpc_pool_equivalence.py::test_nonarchival_hidden_history_equals_single[basic|hub]`
  (пул: неархівний дубль із горизонтом посередині історії + архівний → результат == одиночному; SC-004),
  `::test_only_nonarchival_pool_is_incomplete_not_clean` (статус `incomplete`, причина `unavailable`);
  мутація: прийняти коротку сторінку неархівного → червоний `test_only_nonarchival_pool_is_incomplete_not_clean`;
  файли: `pool.py`, `tests/test_rpc_pool_routing.py`, `tests/test_rpc_pool_equivalence.py`.

## Phase 3: Конфіг і підключення (US4, P2 — у зрізі)

- [x] T-137 [FR-007-09] critical: `config/rpc_pool.yaml` v1, журнал, завантажувач. Файл — дослівно з
  `contracts/rpc-pool-config.md`; `config/CHANGELOG.md`: розділ `# config/rpc_pool.yaml` **ПЕРЕД**
  `# config/ingest.yaml`, запис `## 1 — <дата>` з останнім рядком `sha256: <content_digest>`, інші розділи
  не чіпати. `src/unmask/ingest/rpc/pool_config.py`: `ProviderSpec`, `RpcPoolConfig`,
  `load_rpc_pool_config` (схема й тексти помилок — з контракту). — тест:
  `tests/test_rpc_pool_config.py::test_shipped_rpc_pool_yaml_passes_check_changelog`,
  `::test_entry_sha_equals_content_digest`, `::test_rpc_pool_section_before_ingest_and_ingest_is_last`,
  `::test_any_value_change_without_entry_is_detected` (по кожному ключу верхнього рівня й провайдера),
  `::test_unknown_field_rejected`, `::test_url_in_url_env_rejected_without_echo`,
  `::test_archival_iff_retention_null`, `::test_shipped_values_load`; мутація: прибрати `sha256:` у копії
  журналу → `ConfigError`. **Спільний файл з 008** `tests/test_hubs_config.py::test_shipped_changelog_has_hubs_sections_with_matching_sha256_and_keeps_ingest_entries`
  фіксує точний набір заголовків журналу: додати `"# config/rpc_pool.yaml"` до очікуваного переліку і
  `len(headings) == 5` → `6` (більше нічого в тесті не змінювати; 008 потім додає свій заголовок і `7`);
  ingest лишається ОСТАННІМ (цього вимагає наївний розбір `tests/test_tx_batch_size.py`) — тест має бути
  червоним до вставки розділу й зеленим після; файли: `config/rpc_pool.yaml`, `config/CHANGELOG.md`,
  `src/unmask/ingest/rpc/pool_config.py` (`# impl:` за `plan.md`), `tests/test_rpc_pool_config.py`
  (`# verifies: FR-007-09`), `tests/test_hubs_config.py` (один рядок переліку + довжина).

- [x] T-138 [FR-007-03, FR-007-10, FR-007-11, FR-007-12] critical: Фабрика `build_rpc_source`. `pool_config.py`
  — кроки 1–5 контракту. — тест: `tests/test_rpc_pool_config.py::test_absent_config_builds_single_default_provider`
  (ім'я `http`, архівний, профіль з аргументу), `::test_absent_config_missing_env_same_error_as_from_env`,
  `::test_four_providers_built_when_all_env_set` (US4-1), `::test_missing_env_skipped_with_name_only_warning`
  (US4-2, `caplog`), `::test_no_provider_left_fails_with_all_var_names`, `::test_invalid_url_fails_loud_with_var_name`,
  `::test_each_provider_paces_by_own_profile` (`transport_factory` → `httpx.MockTransport`, що пише
  `clock.monotonic()` запитів; `FakeClock` + `sleep`, що просуває його; `SequentialRunner`; для кожного
  провайдера одиниць у будь-якому вікні ≤ burst + rate × вікно), `::test_secret_sentinel_never_in_logs_errors_repr`
  (URL зі сторожовим ключем; `caplog`, `repr`, тексти й `__context__` винятків); мутація: спільний лімітер на
  всіх → червоний `test_each_provider_paces_by_own_profile`; логувати значення змінної → червоний сторож;
  файли: `pool_config.py`, `tests/test_rpc_pool_config.py` (`# verifies: FR-007-03, FR-007-09, FR-007-10, FR-007-11, FR-007-12`).

- [ ] T-139 [FR-007-14] Сервіс доставки через пул (**спільний файл з 008 — 007 першою**).
  `src/unmask/delivery/main.py`: замінити `HttpRpcSource.from_env(...)` на
  `build_rpc_source(config_dir / "rpc_pool.yaml", ingest_cfg, dict(env))` з `unmask.ingest.rpc.pool_config`;
  `ConfigError` → `serve: <текст>`, код 2 (як зараз); до рядка `# impl:` додати `FR-007-14`.
  `tests/test_delivery_boundaries.py`: додати `"unmask.ingest.rpc.pool_config"` в `ALLOWED_UNMASK` (більше
  нічого). — тест: наявні `tests/test_delivery_main.py` зелені без змін; новий
  `tests/test_delivery_main_pool.py::test_main_builds_pool_from_config_dir` (`--config-dir` у tmp з копіями
  чотирьох YAML + `rpc_pool.yaml` на 2 провайдери; `monkeypatch` `serve` → без сокета; джерело —
  `PooledRpcSource` з 2 провайдерами), `::test_main_without_rpc_pool_yaml_uses_unmask_rpc_url`; файли:
  `src/unmask/delivery/main.py`, `tests/test_delivery_boundaries.py`, `tests/test_delivery_main_pool.py`
  (`# verifies: FR-007-14`).

## Phase 4: Відмовостійкість і експлуатація (US3, P2)

- [ ] T-140 [FR-007-08, FR-007-05, FR-007-12] critical: Перебирання й класи помилок. `pool.py`: виключення
  провайдера до кінця виклику, `last_error` за позицією, паузи після `RpcRateLimited`
  (`max(cooldown, retry_after)`; усі на паузі → пауза ігнорується), не-`RpcError` → дефект назовні, усе
  `raise` — поза `except`. Те саме в одиночних методах. — тест:
  `tests/test_rpc_pool_failover.py::test_rate_limited_share_taken_by_others` (US3-1, результат повний),
  `::test_rate_limited_provider_cooled_down_for_next_calls`, `::test_all_timeout_raises_timeout` (US3-2,
  через `IngestService` → `incomplete` з причиною `timeout`), `::test_unavailable_class_same_as_single` (US3-3),
  `::test_n_minus_one_failures_same_result[basic|hub]` (SC-005), `::test_budget_timeout_class_preserved`,
  `::test_defect_exception_propagates_not_masked`, `::test_raised_error_has_no_context_chain`;
  мутація: піднімати перший, а не останній виняток позиції → червоний `test_all_timeout_raises_timeout`
  (сценарій: 429, потім timeout); ковтати не-`RpcError` → червоний
  `test_defect_exception_propagates_not_masked`; файли: `pool.py`, `tests/test_rpc_pool_failover.py`
  (`# verifies: FR-007-05, FR-007-08, FR-007-12`).

- [ ] T-141 [FR-007-13, FR-007-12] Лічильники. `pool.py`: `ProviderStats`, `stats()`, `stats_line()` (формат
  контракту дослівно), лог у `close()` (логер `unmask.ingest.rpc.pool`, INFO), класи `failures`
  (`budget` перевіряється до `timeout`). — тест: `tests/test_rpc_pool_stats.py::test_stats_count_requests_and_units_per_provider`,
  `::test_failures_counted_by_class`, `::test_escalations_counted`, `::test_stats_line_exact_format`,
  `::test_close_logs_stats_line_once_without_urls`; файли: `pool.py`, `tests/test_rpc_pool_stats.py`
  (`# verifies: FR-007-13, FR-007-12`).

- [ ] T-142 [FR-007-01] [P] AST-тест меж `pool.py`: імпорти лише stdlib, `unmask.ingest.rpc.protocol`,
  `unmask.ingest.budget`; заборонені `httpx`, `yaml`, `os`, `unmask.ingest.rpc.http`, `unmask.ingest.rpc.fixture`,
  `unmask.graph*`, `unmask.hubs*`, `unmask.clusters*`, `unmask.delivery*`; `pool_config.py` не імпортує
  `unmask.hubs*`. — тест: `tests/test_rpc_pool_boundaries.py::test_pool_imports_only_protocol_and_budget`,
  `::test_pool_config_does_not_import_hubs`; файли: `tests/test_rpc_pool_boundaries.py`
  (`# verifies: FR-007-01`).

- [ ] T-143 [FR-007-14] [P] Воркер експерименту через пул. `scripts/screen_worker.py` (у git попри шапку):
  винести `build_source(environ)` → `build_rpc_source(ROOT / "config" / "rpc_pool.yaml", cfg, environ,
  fallback_profile="helius_free")`; у `main()` використати його й після кожного токена
  `print(src.stats_line(), flush=True)`; шапку виправити («committed; source via rpc pool»), додати `FR-007-14`
  до `# impl:`. `.env.example`: + `UNMASK_RPC_URL_ALT=`, `_2=`, `_3=` з коментарем (без значень). — тест:
  `tests/test_screen_worker_source.py::test_worker_builds_pool_from_config` (імпорт через
  `importlib.util.spec_from_file_location`, env з двома змінними → `PooledRpcSource`, 2 провайдери);
  файли: `scripts/screen_worker.py`, `.env.example`, `tests/test_screen_worker_source.py` (`# verifies: FR-007-14`).

- [ ] T-144 [FR-007-15, FR-007-02] [P] Perf-тест реального паралелізму (`@pytest.mark.perf`, поза
  типовим прогоном). Три `ScriptedProvider` з `time.sleep(0.02)` на виклик `get_transactions` + `ThreadRunner`
  vs одиночний; `collect` на `hub` — стінний час пулу ≤ 0,5 × одиночного. — тест:
  `tests/test_rpc_pool_perf.py::test_pool_wall_time_at_most_half_of_single` (маркер `perf`); файли:
  `tests/test_rpc_pool_perf.py` (`# verifies: FR-007-02, FR-007-15`).

- [ ] T-145 [FR-007-04, FR-007-13, FR-007-14] [GATE] Живий замір SC-002 за `quickstart.md`: один Helius vs
  пул із чотирьох; побайтовий збіг `buyers`/`transfers`/`completeness.status`, час, рядок `rpc_pool` (у
  `rpcfast` `requests > 0`). Нотатка з числами — `specs/007-rpc-provider-pool/live-sc002.md`, без
  секретів. Ручна перевірка; за < 2× — ВП-2 плану.

---

## Покриття вимог

| FR | Задачі |
|---|---|
| FR-007-01 | T-133, T-134, T-142 |
| FR-007-02 | T-132, T-144 |
| FR-007-03 | T-132, T-138 |
| FR-007-04 | T-132, T-133, T-134, T-136, T-145 |
| FR-007-05 | T-134, T-140 |
| FR-007-06 | T-135, T-136 |
| FR-007-07 | T-135, T-136 |
| FR-007-08 | T-140 |
| FR-007-09 | T-137 |
| FR-007-10 | T-138 |
| FR-007-11 | T-138 |
| FR-007-12 | T-138, T-140, T-141 |
| FR-007-13 | T-141, T-145 |
| FR-007-14 | T-139, T-143, T-145 |
| FR-007-15 | T-131, T-144 |

## Залежності

T-131 → T-132 → T-133 → T-134; T-133 → T-135 → T-136; T-137 → T-138 → T-139 (T-137 можна паралельно з
T-132…T-136); T-136 + T-139 → кінець зрізу; T-140, T-141 після T-136; T-142, T-143, T-144 — після T-139
і паралельно; T-145 — останнім.

## Нотатки ревʼю (2026-10-08)

- **T-138 (вимога з ревʼю T-137):** завантажувач `pool_config.py` (~рядок 111) повертає для невідомого ключа ім'я ключа до 64 символів (ключ `https://x.io/?api-key=…` попав би в текст помилки). Узгодити з `hubs.config._key_name`: показувати ім'я лише якщо воно ідентифікатор `[A-Za-z_][A-Za-z0-9_]{0,63}`, інакше `<str key>`; додати тест-сторож (FR-007-12).
- **Спільний файл (зроблено в 007):** запас розміру журналу в `tests/test_hubs_config_robustness.py:757` змінено з ×100 на ×25 (журнал ≈ 10 КБ, ліміт 1 МіБ). 008/T-161 цього не повторює.
- **T-132:** `None` від будь-якого провайдера поки остаточний (усі провайдери в T-132/T-134 архівні); підтвердження «не знайдено» архівним — T-135/T-136. Заглушки `NotImplementedError` у `name` і одиночних методах заміняє T-133.
- **T-136 (вимога з виконання T-134):** мутація «частки в порядку провайдерів, а не позицій» ЕКВІВАЛЕНТНА на поточній реалізації (плани суцільні: у раунді одна група придатних провайдерів). Вона стає розрізнюваною лише після T-135/T-136, коли підтверджувальний раунд має групи з різними множинами придатних провайдерів і плани НЕ суцільні. T-136 ОБОВʼЯЗКОВО додає тест, де позиції підтверджувального раунду чергуються з рештою (горизонт історії `ScriptedProvider` на середніх позиціях), і мутацію «збирання в порядку провайдерів» робить червоною; без цього T-134-мутація лишається непокритою.

