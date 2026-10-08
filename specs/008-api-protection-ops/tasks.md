# Tasks: Захист публічного API і операційна готовність (008)

**Input**: `plan.md`, `research.md` (R-1…R-11), `data-model.md`, `contracts/{overload-error.schema.json, overload.md}`, `quickstart.md`. Дата — 2026-10-08; подача — 11.10.

**Tests**: першими, червоні з очікуваної причини (поведінка/значення, не `ImportError` — заглушка сигнатури допустима). Без мережі й сокетів (гард `tests/conftest.py`), без реального часу: `ManualClock` (локальний клас у тесті: `now`, `advance(dt)`, `__call__`), конкурентність — лише `threading.Event`/`Barrier` з таймаутом, жодних `sleep`. Еталони — контракти 008 і явні числа в тесті; `expected`, отриманий з коду під тестом, ревʼюер відхиляє.

## Format: `- [ ] T-NNN [FR-008-NN, …] [USn?] [P?] critical?: опис — тест: …; мутація: …; файли: …`

- Нумерація з **T-161**; ця фіча — **T-161…T-173**.
- `critical:` → `implementer-senior` (sonnet): помилка тихо знімає захист або спотворює відповідь. Решта → `implementer` (haiku). Потрібен вибір семантики — `BLOCKED` до architect.
- Маркери — таблиця «Маркери трасування» `plan.md` (наявні рядки `impl:` доповнюються через кому, не замінюються). У рядках задач — лише FR-008-NN.
- Мутація: тест мусить червоніти на названій заміні; ревʼюер перевіряє вручну.

## Передумови

- Чинні тести 004 зелені до і після кожної задачі (`uv run pytest -q tests/test_delivery_*.py`); без нових аргументів поведінка 004 побайтово та сама.
- Задачі T-167 і T-169 правлять `src/unmask/delivery/main.py` лише ПІСЛЯ завершення задачі wiring пулу RPC з `specs/007-rpc-provider-pool/tasks.md` (черга — `plan.md`).

---

## Phase 1: Конфіг

- [x] T-161 [FR-008-08] Версіонований `config/protection.yaml` v1 + журнал + завантажувач. YAML (з коментарями до кожного ключа і шапкою `# impl: FR-008-08`): `version: 1`; `api: {requests_per_window: 30, window_seconds: 60}`; `heavy: {max_concurrent: 3, busy_retry_after_seconds: 60}`; `proxy: {trusted: ["127.0.0.1", "::1"]}`; `bot: {checks_per_window: 5, window_seconds: 600}` (обґрунтування — research R-7). `config/CHANGELOG.md`: розділ `# config/protection.yaml` ПЕРЕД `# config/ingest.yaml` (ingest лишається останнім — R-5), запис `## 1 — 2026-10-0X`, останній рядок `sha256: <content_digest>`; інших розділів не чіпати. `src/unmask/delivery/config.py`: `ProtectionConfig` (frozen; поля й межі — data-model) і `load_protection_config(path)` за зразком `load_delivery_config` (`_check_keys` на кожному рівні вкладеності, `ipaddress.ip_network(x, strict=False)` для `proxy.trusted`, помилка → `ConfigError` з назвою поля). `tests/test_hubs_config.py`: додати `"# config/protection.yaml"` у точний набір заголовків і збільшити очікувану кількість на 1 (черга з 007 — plan) — тест: `tests/test_protection_config.py::test_shipped_protection_yaml_passes_check_changelog`, `::test_entry_sha_equals_content_digest` (`changelog_entries(..., "protection.yaml") == {1: digest}`), `::test_protection_section_before_ingest_and_ingest_is_last`, `::test_shipped_values_load` (явні числа R-7), `::test_any_value_change_without_entry_is_detected[<кожен лист>]` (SC-004), `::test_unknown_or_missing_field_rejected[...]` (верхній і вкладений рівні), `::test_bad_values_rejected[...]` (0, `True`, `"10.0.0.300"`), `::test_delivery_yaml_digest_unchanged` (запис 1 `delivery.yaml` не зачеплено); мутація: прибрати `sha256:` у копії журналу → `ConfigError`; файли: `config/protection.yaml`, `config/CHANGELOG.md`, `src/unmask/delivery/config.py`, `tests/test_protection_config.py` (`# verifies: FR-008-08`), `tests/test_hubs_config.py`

**Checkpoint**: `uv run pytest -q tests/test_protection_config.py tests/test_hubs_config.py tests/test_tx_batch_size.py tests/test_delivery_config.py` зелений.

---

## Phase 2: Примітиви обмеження (`delivery/limits.py`)

- [x] T-162 [FR-008-01, FR-008-14] [P] critical: `SlidingWindowLimiter` і `Decision` у новому `src/unmask/delivery/limits.py` (шапка — plan). Семантика — data-model: відхилені не записуються; `retry_after = clamp(ceil(oldest + window − now), 1, ceil(window))`; мітки з майбутнього затискаються до `now`; прибирання порожніх ключів не частіше разу на вікно; один `threading.Lock`; `clock` інжектований (умовчання `time.monotonic`). Оновити `tests/test_delivery_boundaries.py`: `limits` у списку модулів і `CORE`, `unmask.delivery.limits` в `ALLOWED_UNMASK` (R-11) — тест: `tests/test_delivery_limits.py::test_allows_exactly_limit_then_denies_with_retry_after` (30 за 60 с: 30 × allowed, 31-й — `retry_after` = явне число з `ManualClock`), `::test_retry_after_is_honest` (після `advance(retry_after)` — allowed; після `advance(retry_after − 1)` — ні), `::test_window_slides_not_resets` (мітки на t=0 і t=30: на t=61 звільняється рівно одна), `::test_denied_requests_do_not_extend_block`, `::test_keys_are_independent`, `::test_clock_going_back_never_blocks_longer_than_window`, `::test_stale_keys_are_swept` (1000 одноразових ключів → після вікна внутрішній словник порожній), `::test_concurrent_hits_never_exceed_limit` (50 потоків на `Barrier`, limit=10 → рівно 10 allowed); мутація: записувати відхилені → червоний `test_denied_requests_do_not_extend_block`; `<` замість `≤` при відкиданні старих міток → червоний `test_window_slides_not_resets`; файли: `src/unmask/delivery/limits.py`, `tests/test_delivery_limits.py` (`# verifies: FR-008-01, FR-008-02, FR-008-06, FR-008-07, FR-008-14`), `tests/test_delivery_boundaries.py`
- [ ] T-163 [FR-008-02, FR-008-06] critical: `HeavySlots` і `Lease` у `limits.py` (data-model): `try_acquire() -> Lease | None` без очікування; `release()` ідемпотентний; контекст-менеджер звільняє за будь-якого винятку, включно з `BaseException`; `in_use`, `retry_after_seconds` — тест: `tests/test_delivery_limits.py::test_capacity_then_none`, `::test_release_frees_slot_for_next`, `::test_double_release_does_not_underflow`, `::test_lease_released_on_exception_and_base_exception[ValueError, KeyboardInterrupt, TimeoutError]`, `::test_concurrent_acquire_respects_capacity` (40 потоків, capacity=3 → рівно 3 лізи); мутація: `release` без прапорця «вже звільнено» → червоний на подвійному звільненні; `try_acquire` з `<=` → червоний на ємності; файли: `src/unmask/delivery/limits.py`, `tests/test_delivery_limits.py`

---

## Phase 3: US1 — API (P1) 🎯 вертикальний зріз

**Independent Test**: `tests/test_delivery_http_protection.py::test_over_limit_gets_429_with_retry_after` і `::test_cold_over_capacity_gets_503_cache_hit_and_join_do_not`.

- [ ] T-164 [FR-008-02, FR-008-06, FR-008-09] [US1] critical: Слот у `DeliveryService` (R-1, R-2). `__init__(..., slots=None)`; `_guarded_build(mint, progress)`: `slots is None` або `not is_valid_address(mint)` (імпорт `unmask.ingest.addresses`) → старий `_build`; інакше `try_acquire()`; `None` → `raise _ErrorDoc({"mint": mint, "error": {"kind": "busy", "detail": "too many analyses in progress, retry after N s", "retry_after_seconds": N}})` ДО будь-якого `progress`; інакше `with lease: return self._build(...)`. `needs_analysis(mint) -> bool` (валідний формат і немає в кеші). `DeliveryCache` НЕ змінюється. У `tests/test_delivery_boundaries.py` перенести `unmask.ingest.addresses` із `FORBIDDEN_SUBSTRINGS` в `ALLOWED_UNMASK` (R-2) — тест: `tests/test_delivery_service_slots.py` (дублі сервісів 001/002/003 з блокуванням на `Event`, лічильники викликів): `::test_cache_hit_served_while_all_slots_busy`, `::test_join_inflight_served_while_all_slots_busy` (власник тримає єдиний слот; другий запит того ж mint отримує той самий документ, `in_use` == 1), `::test_new_cold_mint_gets_busy_doc_without_ingest_call_or_progress`, `::test_busy_is_not_cached` (після звільнення той самий mint аналізується), `::test_invalid_address_never_takes_slot` (слоти зайняті → документ `invalid_address`, як без `slots`), `::test_slot_released_on_success_rejection_incomplete_and_exception` (SC-003: `in_use == 0` після кожного), `::test_busy_doc_validates_overload_schema_and_has_no_result_fields`, `::test_without_slots_behaviour_is_byte_identical` (ті самі фікстури, `to_json` рівні); мутація: брати слот у `analyze` до `single_flight` → червоний `test_join_inflight…`; прибрати `with` → червоний на винятку; файли: `src/unmask/delivery/service.py`, `tests/test_delivery_service_slots.py` (`# verifies: FR-008-02, FR-008-06, FR-008-09`), `tests/test_delivery_boundaries.py`
- [ ] T-165 [FR-008-05] [US1] [P] critical: `http.py::client_address(peer, forwarded_for, trusted) -> str` за R-4 (довіра лише при `peer` у `trusted`; розбір справа наліво; пропуск довірених ланок; сміття → `peer`; нормалізація `str(ip_address)`) — тест: `tests/test_delivery_client_ip.py::test_untrusted_peer_ignores_header` (підміна), `::test_trusted_peer_takes_rightmost_untrusted`, `::test_spoofed_left_entries_are_ignored` (`"6.6.6.6, 1.2.3.4"` від 127.0.0.1 → `1.2.3.4`), `::test_chain_of_trusted_proxies_skipped` (CIDR `10.0.0.0/8`), `::test_garbage_entry_falls_back_to_peer`, `::test_missing_or_empty_header_gives_peer`, `::test_ipv6_peer_and_entries`, `::test_empty_trusted_list_never_trusts`; мутація: брати крайній ЛІВИЙ → червоний на підміні; довіряти без перевірки `peer` → червоний; файли: `src/unmask/delivery/http.py`, `tests/test_delivery_client_ip.py` (`# verifies: FR-008-05`)
- [ ] T-166 [FR-008-01, FR-008-03, FR-008-04, FR-008-09, FR-008-10] [US1] critical: HTTP-шар (контракт `overload.md`). `_ERROR_STATUS += {"rate_limited": 429, "busy": 503}`; чиста `handle_get(path, peer, forwarded_for, service, protection=None) -> (status, doc, headers)`: `health` → як було (без ліміту); `token` → `client_address` → `protection.api.hit(client)` → відмова: `429` + документ `rate_limited` (сервіс не викликається); інакше `handle_token_request` без змін; `headers` = `[("Retry-After", str(N))]` лише для `rate_limited`/`busy`; `404`/`405` — без ліміту. `_Handler.do_GET` викликає `handle_get` (peer = `client_address[0]`, заголовки `get_all("X-Forwarded-For")` через кому) і додає `headers`; `serve(service, port, *, host="0.0.0.0", protection=None)` — тест: `tests/test_delivery_http_protection.py::test_over_limit_gets_429_with_retry_after` (SC-001, `ManualClock`), `::test_under_limit_client_never_refused`, `::test_invalid_mint_counts_toward_request_limit_but_returns_400`, `::test_cache_hits_count_toward_request_limit`, `::test_healthz_never_limited` (1000 поспіль), `::test_cold_over_capacity_gets_503_cache_hit_and_join_do_not`, `::test_overload_bodies_validate_against_008_schema_and_header_equals_body`, `::test_success_and_400_500_unchanged_vs_handle_token_request` (байти `to_json` і відсутність `Retry-After`), `::test_overload_never_looks_like_result` (немає `risk_score`/`band`/`clusters`), `::test_limits_key_only_on_address_no_auth` (AST: `http.py` не читає `Authorization`/`Cookie`; два клієнти з різними заголовками, однакова адреса → один лічильник), `::test_without_protection_identical_to_004`; мутація: ліміт після виклику сервісу → червоний (лічильник сервісу росте на 429); `Retry-After` з конфігу замість обчисленого → червоний; файли: `src/unmask/delivery/http.py`, `tests/test_delivery_http_protection.py` (`# verifies: FR-008-01, FR-008-03, FR-008-04, FR-008-09, FR-008-10`)
- [ ] T-167 [FR-008-08] [US1] critical: Один регулятор на процес. `limits.py::Protection` + `build_protection(cfg, clock=time.monotonic)`; `main.py` (ПІСЛЯ wiring 007): `load_protection_config(config_dir / "protection.yaml")` (помилка → exit 2 з назвою поля ДО відкриття порту); один `Protection` → `DeliveryService(..., slots=p.slots)` і `serve(service, port, host=args.host, protection=p)`; `--host` (умовчання `0.0.0.0`); бот отримує спільні слоти через той самий сервіс — тест: `tests/test_delivery_limits.py::test_build_protection_maps_every_config_field` (явні числа), `tests/test_delivery_main.py::test_one_protection_shared_by_service_and_http` (monkeypatch `serve`/`threading.Thread` у модулі `main`: `service._slots is protection.slots`, той самий `protection` у `serve`), `::test_missing_protection_yaml_fails_fast_before_serve`, `::test_host_flag_passed_to_serve`; мутація: два `build_protection` (окремі для HTTP і сервісу) → червоний; файли: `src/unmask/delivery/limits.py`, `src/unmask/delivery/main.py`, `tests/test_delivery_main.py` (дописати `FR-008-08` у `verifies:`)

**Checkpoint (вертикальний зріз)**: `quickstart.md` §2 вручну — 429/503 з `Retry-After`, `/healthz` 200; `python3 scripts/trace.py --check` зелений.

---

## Phase 4: US2 — бот (P2)

- [ ] T-168 [FR-008-07] [US2] [P] critical: `UserGate` і `GateDecision` у `limits.py` (data-model): атомарні `try_enter(user, *, counts)` (порядок: `in_progress` → `rate_limited` → `ok`) і ідемпотентний `leave(user)`; вікно — `SlidingWindowLimiter` всередині; `build_protection` створює `users` з `bot.*` — тест: `tests/test_delivery_limits.py::test_second_enter_while_active_is_in_progress`, `::test_leave_allows_next`, `::test_window_counts_only_when_counts_true`, `::test_rate_limited_has_honest_retry_after`, `::test_in_progress_does_not_consume_window`, `::test_concurrent_enter_same_user_admits_one` (20 потоків → рівно один `ok`); мутація: перевіряти вікно до активності → червоний `test_in_progress_does_not_consume_window`; файли: `src/unmask/delivery/limits.py`, `tests/test_delivery_limits.py`
- [ ] T-169 [FR-008-07, FR-008-09] [US2] critical: Бот (R-9, тексти — `contracts/overload.md`). `run_polling(..., gate=None, spawn=None)`: без `gate` — поведінка 004 без змін; з `gate` для `/check <mint>` у потоці опитування: ключ `from.id` → `chat.id`; `gate.try_enter(user, counts=service.needs_analysis(mint))`; відмова → один `send_message` з текстом контракту; `ok` → `spawn(worker)`, де worker = `handle_check(...)` + `finally gate.leave(user)`; `spawn` за умовчанням — daemon-потік. Реєстр `requests` — під замком, `request_id` з `itertools.count`. `_format_error`: гілки `busy` і `rate_limited` з `retry_after_seconds`. `main.py` (ПІСЛЯ T-167): `run_polling(..., gate=protection.users)` — тест: `tests/test_delivery_bot_limits.py` (`JournalTransport`, `ManualClock`): `::test_user_over_window_gets_wait_text_and_no_analysis` (лічильник сервісу стоїть), `::test_second_check_while_running_gets_in_progress` (відкладений `spawn`: перший воркер ще не виконаний), `::test_global_busy_reply_without_progress_messages` (сервіс зі зайнятими слотами), `::test_cached_mint_does_not_consume_window`, `::test_gate_released_after_worker_exception`, `::test_users_are_independent`, `::test_evidence_callback_never_limited`, `::test_without_gate_journal_identical_to_004` (SC-006: той самий журнал викликів на фікстурах), `::test_main_passes_shared_users_gate` (monkeypatch як у T-167); мутація: `leave` поза `finally` → червоний на винятку; рішення в воркері замість потоку опитування → червоний `test_second_check_while_running…`; файли: `src/unmask/delivery/bot.py`, `src/unmask/delivery/main.py`, `tests/test_delivery_bot_limits.py` (`# verifies: FR-008-07, FR-008-09`)

---

## Phase 5: US3 — операційні артефакти (P2; незалежні, можна паралельно зі зрізом)

Спільний файл тестів `tests/test_deploy_artifacts.py` (`# verifies: FR-008-11, FR-008-12, FR-008-13`); перший виконавець створює його з хелпером `SECRET_PATTERNS` (регекси: Telegram-токен `\d{8,10}:[A-Za-z0-9_-]{30,}`, `api[-_]?key=[A-Za-z0-9-]{16,}`, `sk-[A-Za-z0-9]{16,}`, UUID-ключ у URL), наступні — дописують.

- [x] T-170 [FR-008-11] [US3] [P] systemd-юніт за R-8 `deploy/unmask.service`: `[Unit] After=network-online.target Wants=network-online.target`; `[Service] WorkingDirectory=/opt/unmask`, `EnvironmentFile=/opt/unmask/.env`, `ExecStart=/opt/unmask/.venv/bin/python scripts/serve_unmask.py --host 127.0.0.1 --port 18080`, `Restart=always`, `RestartSec=5`, `NoNewPrivileges=yes`; `[Install] WantedBy=multi-user.target`; без `User=`/`DynamicUser` (коментар — чому). `deploy/smoke.sh` (шапка `# impl: FR-008-11, FR-008-12, FR-008-13`, `set -euo pipefail`, аргумент — домен): `systemctl show unmask -p Restart` == `Restart=always`; `curl` HTTPS `/healthz` → 200; HTTP → 301 — тест: `tests/test_deploy_artifacts.py::test_unit_restarts_on_failure`, `::test_unit_env_from_operator_file_not_inline` (жодного `Environment=` з `UNMASK_`), `::test_unit_execstart_binds_localhost_via_venv`, `::test_unit_has_install_section`, `::test_no_secrets_in_deploy_dir`, `::test_smoke_script_syntax_and_checks` (`bash -n`, містить три перевірки); мутація: `Restart=on-abnormal`→ червоний; файли: `deploy/unmask.service`, `deploy/smoke.sh`, `tests/test_deploy_artifacts.py`
- [x] T-171 [FR-008-12] [US3] [P] Приклад nginx за R-10 `deploy/nginx-unmask.conf.example`: `server { listen 80; server_name <DOMAIN>; return 301 https://$host$request_uri; }`; `server { listen 443 ssl; http2 on; server_name <DOMAIN>; ssl_certificate /etc/letsencrypt/live/<DOMAIN>/fullchain.pem; ssl_certificate_key …/privkey.pem; location / { proxy_pass http://127.0.0.1:18080; proxy_set_header Host $host; proxy_set_header X-Forwarded-For $remote_addr; proxy_read_timeout 660s; proxy_send_timeout 660s; } }` — тест: `tests/test_deploy_artifacts.py::test_nginx_tls_with_placeholders`, `::test_nginx_port80_only_redirects` (у блоці 80 немає `proxy_pass`), `::test_nginx_overwrites_xff_with_remote_addr` (і немає `$proxy_add_x_forwarded_for`), `::test_nginx_proxies_to_unit_port` (порт збігається з `ExecStart` юніта), `::test_nginx_read_timeout_exceeds_analysis_budget` (≥ `time_budget_seconds` з `config/ingest.yaml`); мутація: `proxy_pass` у блоці 80 → червоний; файли: `deploy/nginx-unmask.conf.example`, `tests/test_deploy_artifacts.py`
- [x] T-172 [FR-008-13] [US3] Розділ README `## Deploy` (після «Quick start», англійською): `uv sync --frozen --no-dev`; `.env` з `chmod 600` і порожніми плейсхолдерами `UNMASK_RPC_URL=` / `UNMASK_BOT_TOKEN=`; копіювання юніта, `systemctl daemon-reload && systemctl enable --now unmask`; nginx-приклад + сертифікат (certbot); `curl https://<DOMAIN>/healthz`; `deploy/smoke.sh <DOMAIN>`; ліміти — `config/protection.yaml` (змінювати лише версією із записом у `config/CHANGELOG.md`), `429`/`503` з `Retry-After`; лічильники скидаються при перезапуску; «Demo instance: `<DEMO_URL>` (added by the operator)» — тест: `tests/test_readme_deploy.py::test_readme_has_deploy_section_with_required_steps` (`systemctl`, `EnvironmentFile`/`.env`, `/healthz`, `protection.yaml`, `Retry-After`, `smoke.sh`), `::test_readme_deploy_has_no_secrets` (`SECRET_PATTERNS` по розділу), `::test_readme_references_existing_deploy_files`; мутація: вставити `UNMASK_BOT_TOKEN=1234567890:AA…` → червоний; файли: `README.md`, `tests/test_readme_deploy.py`

---

## Phase 6: Наскрізна перевірка критеріїв

- [ ] T-173 [FR-008-02, FR-008-09, FR-008-14] Наскрізно без мережі й реального часу (зібрати з готового): `tests/test_delivery_protection_e2e.py`: справжні `DeliveryService` (записані фікстури `tests/fixtures/real/*.json`) + `handle_get` + `run_polling` на `JournalTransport`, один `Protection` з `ManualClock` — `::test_n_plus_one_cold_exactly_one_503` (SC-002: N=3, 4 різні mint, блокувальний дубль збору на `Barrier`; кеш-хіт і `/healthz` під час насичення — 200), `::test_no_orphan_slots_after_mixed_outcomes` (SC-003: успіх/відмова/виняток/неповний → `in_use == 0`), `::test_http_busy_is_shared_with_bot` (HTTP займає всі слоти → бот отримує busy-текст), `::test_success_bytes_identical_with_and_without_protection` (SC-006: API `to_json` і журнал бота для кожної фікстури), `::test_no_wall_clock_used` (monkeypatch `time.monotonic`/`time.time` на виняток — прогін проходить); мутація: `slots=None` у сервісі e2e → червоний SC-002; файли: `tests/test_delivery_protection_e2e.py` (`# verifies: FR-008-02, FR-008-09, FR-008-14`)

**Checkpoint (перед мержем)**: `uv run pytest -q` зелений; `python3 scripts/trace.py` (матриця перегенерована) зелений; `quickstart.md` §1–2; ревʼю фічі `reviewer` проти FR-008-01…14 і SC-001…006; встановлення на сервер — оператор (§3).

---

## Dependencies

| Задача | Після | Примітка |
|---|---|---|
| T-161 | — | черга `CHANGELOG.md`/`test_hubs_config.py` з 007 |
| T-162 | — (новий файл) | ∥ T-161, T-165 |
| T-163 | T-162 | той самий файл |
| T-164 | T-163 | |
| T-165 | — | ∥ T-162…T-164 (інша функція `http.py`, до T-166) |
| T-166 | T-162, T-164, T-165 | |
| T-167 | T-161, T-166, **задача wiring пулу RPC з `specs/007-rpc-provider-pool/tasks.md` (за plan 007 — T-139)** | перша правка `main.py` фічею 008 |
| T-168 | T-163 | ∥ T-164…T-166 (лише `limits.py` — після T-167, якщо та вже правила `limits.py`: послідовно) |
| T-169 | T-164, T-167, T-168 | друга правка `main.py` |
| T-170, T-171, T-172 | — | будь-коли; T-171 читає порт із юніта T-170, T-172 посилається на обидва |
| T-173 | T-166, T-167, T-169 | останнім |

Один файл — одна задача за раз: `limits.py` (T-162 → T-163 → T-167/T-168 послідовно), `http.py` (T-165 → T-166), `tests/test_delivery_limits.py` (так само, як `limits.py`), `tests/test_deploy_artifacts.py` (T-170 → T-171 → T-172), `tests/test_delivery_boundaries.py` (007 T-139 → T-162 → T-164).

## Розподіл за ролями

- `implementer-senior` (sonnet), `critical:` — **T-162, T-163, T-164, T-165, T-166, T-167, T-168, T-169** (8): ковзне вікно, слоти зі звільненням у всіх завершеннях, взаємодія з in-flight-кешем, довіра до проксі-заголовка, чесні 429/503, спільний регулятор, ліміти бота й паралельний воркер.
- `implementer` (haiku) — **T-161, T-170, T-171, T-172, T-173** (5): конфіг за готовим зразком, статичні артефакти, збірка e2e.
- `reviewer` (opus) після кожної; перед `[x]` — `python3 scripts/trace.py --check` і названа мутація.

## Покриття вимог

| FR | Задачі |
|---|---|
| FR-008-01 | T-162, T-166 |
| FR-008-02 | T-163, T-164, T-173 |
| FR-008-03 | T-166 |
| FR-008-04 | T-166 |
| FR-008-05 | T-165 |
| FR-008-06 | T-163, T-164 |
| FR-008-07 | T-168, T-169 |
| FR-008-08 | T-161, T-167 |
| FR-008-09 | T-164, T-166, T-169, T-173 |
| FR-008-10 | T-166 |
| FR-008-11 | T-170 |
| FR-008-12 | T-171 |
| FR-008-13 | T-172 |
| FR-008-14 | T-162, T-173 |

## Нотатки ревʼю (2026-10-08)

- **T-167 (вимога з ревʼю T-170/T-171):** прапорець `--host` додає T-167 (у `main.py`/`scripts/serve_unmask.py`). Разом з ним T-167 ОБОВʼЯЗКОВО оновлює `deploy/unmask.service` (`--host 127.0.0.1` в ExecStart), тест `tests/test_deploy_artifacts.py` (`test_unit_execstart_binds_localhost_via_venv`) і коментар у `deploy/nginx-unmask.conf.example`. Поки прапорця немає, юніт БЕЗ `--host` (інакше argparse → код 2 і цикл перезапусків), порт 18080 слухає 0.0.0.0 і має бути закритий фаєрволом.
- **T-172 (README):** згадати, що `.env` читають і systemd (`EnvironmentFile`), і bash (`set -a; . ./.env`): значення з лапками, `&`, пробілами, `$`, `\` інтерпретуються ними по-різному — URL RPC краще без лапок і без `&`.
- **Трасування:** `.service` і `.conf.example` трасувальник не сканує; носієм `# impl: FR-008-11, FR-008-12, FR-008-13` є `deploy/smoke.sh`.
- **nginx на Ubuntu 24.04 (1.24):** директива `http2 on;` з'явилась у 1.25.1 — приклад використовує `listen 443 ssl http2;` (працює на 1.24, на ≥ 1.25.1 лише попередження).

