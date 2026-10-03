# Tasks: Збір ончейн-даних по токену (001)

**Input**: `plan.md`, `spec.md`, `research.md`, `data-model.md`, `contracts/`, `quickstart.md` з `/specs/001-onchain-data-ingest/`

**Tests**: обов'язкові й пишуться першими (принцип II). У кожній задачі названо тест, який має бути червоним до коду — з очікуваної причини (відсутня поведінка), а не через ImportError. Якщо тест не можна зробити червоним без коду задачі — задача завелика; повернись до architect.

**Organization**: фази 1–2 — інфраструктура й чисті функції розбору; фази 3–5 — user stories за пріоритетом; фаза 6 — живий RPC-адаптер (deferrable, research R-4).

## Format: `- [ ] T-NNN [FR-001-NN, …] [P?] [USn?] critical?: опис — тест: …; файли: …`

- Нумерація `T-NNN` наскрізна по проєкту (ця фіча починає з T-001).
- `[FR-…]` — кожна задача несе щонайменше одну вимогу; `scripts/trace.py` перевіряє покриття всіх 16.
- `[P]` — можна виконувати паралельно з сусідніми `[P]` (різні файли, без залежності від незавершених задач).
- `critical:` — помилка тихо спотворює результат, а не ламає збірку → виконує `implementer-senior`.
- Маркери у шапках файлів — за таблицею «Маркери трасування» у `plan.md`; у рядках нижче вони повторені там, де файл створюється вперше.

## Path Conventions

Один проєкт: `src/unmask/ingest/…`, `tests/…`, `config/…` від кореня репозиторію. Фікстури — `tests/fixtures/scenarios/<name>/{rpc.json, expected.json}`.

---

## Phase 1: Setup (каркас, конфіг, типи)

**Purpose**: проєкт, у якому тест можна написати й побачити червоним; константи вже у YAML; інваріант повноти зафіксований у типах до будь-якої логіки збору.

- [x] T-001 [FR-001-15] Каркас проєкту під uv + гард мережі: `pyproject.toml` (`[project]` name=unmask, requires-python>=3.12, deps pyyaml/solders/httpx, dev pytest/jsonschema, без build-system, `[tool.pytest.ini_options] pythonpath=["src"] testpaths=["tests"]`), порожні пакети з `# trace: ignore-file`, `tests/conftest.py` з autouse-фікстурою, що підміняє `socket.socket.connect` на виняток `NetworkForbidden` для всього прогону — тест: `tests/test_no_network.py::test_socket_connect_is_forbidden` (verifies FR-001-15; спроба `socket.create_connection(("127.0.0.1", 9))` піднімає `NetworkForbidden`) і `::test_unmask_package_importable`; файли: `pyproject.toml`, `src/unmask/__init__.py`, `src/unmask/ingest/__init__.py`, `src/unmask/ingest/rpc/__init__.py`, `tests/conftest.py`, `tests/test_no_network.py`
- [x] T-002 [FR-001-01, FR-001-05, FR-001-08, FR-001-14, FR-001-16] Версіонована конфігурація: `config/ingest.yaml` (version=1 і типові значення за `contracts/config-ingest.md`), `config/CHANGELOG.md` із записом «1», `config.py::load_config(path) -> IngestConfig` з валідацією обмежень «1 ≤ first_buyers_n ≤ 500», «1 ≤ funding_depth ≤ 3», «counterparty_threshold ≥ 1», «max_signatures_per_wallet ≥ 1», «time_budget_seconds > 0», «commitment ∈ {finalized, confirmed}», «1 ≤ rpc.page_size ≤ 1000», невідоме/відсутнє поле → `ConfigError` з назвою поля — тест: `tests/test_config.py::test_shipped_config_loads_with_version_1`, `::test_each_field_round_trips_from_yaml`, `::test_out_of_range_raises_config_error[...]` (параметризовано: N=0, N=501, depth=4, відсутній version, невідоме поле); файли: `config/ingest.yaml`, `config/CHANGELOG.md`, `src/unmask/ingest/config.py` (`# impl: FR-001-01, FR-001-05, FR-001-08, FR-001-14, FR-001-16`), `tests/test_config.py` (`# verifies: FR-001-01, FR-001-05, FR-001-08, FR-001-14, FR-001-16`)
- [x] T-003 [FR-001-06, FR-001-09, FR-001-10, FR-001-14] [P] critical: Модель результату з похідним статусом повноти: frozen-дата-класи й enum'и з `data-model.md` (`Asset`, `AddressType`, `CompletenessStatus`, `MissingReason`, `UnexpandedReason`, `RejectKind`, `Buyer`, `Spend`, `Transfer`, `UnexpandedNode`, `MissingHistory`, `BuyersCompleteness`, `Completeness`, `RunMetadata`, `IngestResult`, `Rejection`, `CacheInvariantError`); `Completeness.derive(missing, buyers)` — єдиний спосіб побудови: `status == complete` тоді й лише тоді, коли `missing` порожній **і** `buyers.complete`; `Transfer` вимагає всі 7 полів доказу (signature, slot, block_time, sender, receiver, asset, amount) без умовчань; ключі порядку `buyer_sort_key`, `transfer_sort_key` — тест: `tests/test_model_completeness.py::test_derive_incomplete_when_missing_nonempty`, `::test_derive_incomplete_when_buyers_truncated`, `::test_derive_complete_only_when_both_clean`, `::test_direct_construction_of_contradictory_completeness_rejected`, `::test_transfer_requires_all_evidence_fields`, `::test_sort_keys_are_data_only`; файли: `src/unmask/ingest/model.py` (`# impl: FR-001-06, FR-001-09, FR-001-10, FR-001-14`), `tests/test_model_completeness.py` (`# verifies: FR-001-06, FR-001-09, FR-001-10`)

---

## Phase 2: Foundational (межа зовнішнього світу, фікстури, розбір транзакцій)

**Purpose**: усе, без чого не можна написати жоден тест user story: інтерфейс джерела, записані сценарії, чисті функції розбору. Жодного звернення до мережі.

**⚠️ Blocking**: фази 3–5 не починаються, доки не завершено T-004…T-010.

- [ ] T-004 [FR-001-15] Інтерфейс джерела й фікстурна реалізація: `rpc/protocol.py` (`RpcSource` Protocol з 4 методами й `name`, `TypedDict`-типи форм JSON-RPC, винятки `RpcRateLimited(retry_after)`, `RpcTimeout`, `RpcUnavailable(detail)`), `rpc/fixture.py::FixtureRpcSource(scenario_dir, *, failures=None, clock=None)`: читає `rpc.json`, емулює `before`/`until`/`limit` поверх повного списку від найновішого, веде журнал `calls`, адреса відсутня у файлі → порожня історія, політики `FailAfter(n, exc)` / `FailFor(key, exc, times)`, просування `FakeClock` на кожен виклик; `budget.py::Clock/SystemClock/FakeClock` (мінімум для фікстури) — тест: `tests/test_rpc_fixture.py::test_before_returns_strictly_older_entries`, `::test_until_returns_strictly_newer_entries`, `::test_limit_and_empty_list_at_end_of_history`, `::test_get_transactions_preserves_argument_order_and_none_for_unknown`, `::test_unknown_address_yields_empty_history_not_error`, `::test_fail_after_raises_configured_exception_on_nth_call`, `::test_calls_log_records_method_and_params` (на малому рукописному `rpc.json` у `tests/fixtures/scenarios/minimal/`); файли: `src/unmask/ingest/rpc/protocol.py` (`# impl: FR-001-15`), `src/unmask/ingest/rpc/fixture.py` (`# impl: FR-001-15`), `src/unmask/ingest/budget.py` (`# impl: FR-001-16`), `tests/fixtures/scenarios/minimal/rpc.json`, `tests/test_rpc_fixture.py` (`# verifies: FR-001-15`)
- [ ] T-005 [FR-001-15] Генератор синтетичних сценаріїв у формі реальних відповідей Solana JSON-RPC: `tests/fixtures/build_fixtures.py` (`# trace: ignore-file`; не імпортує `unmask`) з декларативного опису будує `rpc.json` (getAccountInfo mint jsonParsed; getSignaturesForAddress від найновішого з `blockTime/slot/err`; getTransaction jsonParsed v0 з `accountKeys`, `pre/postBalances`, `pre/postTokenBalances`, `innerInstructions`, `fee`; getTokenAccountsByOwner) і незалежний `expected.json`; сценарії: `basic` (mint M; 5 покупців P1…P5 у різних слотах, P2 і P3 в одному слоті; A→P1 депозит SOL і A→P2 — спільний відправник; B→A (глибина 2) і C→B (глибина 3); X→P1 після першої купівлі; D→A і A→D цикл; P3→P3 self-transfer; P4 без вхідних; P5 — off-curve власник, що отримує M при продажу користувача; USDC-подібний SPL-переказ на існуючий токен-рахунок P2; одна транзакція з `err`; одна транзакція з двома однаковими переказами на різних `instruction_path`), `hub` (H фінансує покупця й має 6 унікальних відправників і 1200 підписів), `corrupt` (getTransaction → null для одного підпису; інша транзакція без `meta`), `notfound` (getAccountInfo null; рахунок, що належить System Program) — тест: `tests/test_fixture_builder.py::test_build_is_deterministic_and_matches_committed_files`, `::test_every_listed_signature_has_a_transaction_or_explicit_null`, `::test_transactions_have_jsonrpc_shape` (обов'язкові ключі `slot, blockTime, transaction.message.accountKeys, meta.fee, meta.preBalances, meta.postBalances, meta.preTokenBalances, meta.postTokenBalances, meta.innerInstructions`), `::test_basic_scenario_loads_in_fixture_source`; файли: `tests/fixtures/build_fixtures.py`, `tests/fixtures/scenarios/{basic,hub,corrupt,notfound}/rpc.json`, `tests/fixtures/scenarios/{basic,hub}/expected.json`, `tests/test_fixture_builder.py` (`# verifies: FR-001-15`)
- [ ] T-006 [FR-001-11] [P] Валідність і тип адреси без мережі: `addresses.py::is_valid_address(s) -> bool` (base58, рівно 32 байти, непорожній) і `address_type(s) -> AddressType` (`off_curve`, якщо `solders.Pubkey.is_on_curve()` хибне) — тест: `tests/test_addresses.py::test_invalid_strings_rejected[...]` (порожній, не-base58 символи `0OIl`, 31 байт, 33 байти), `::test_system_program_and_fixture_wallets_are_on_curve`, `::test_find_program_address_result_is_off_curve` (PDA, виведений із фіксованих seeds через `solders.Pubkey.find_program_address`); файли: `src/unmask/ingest/addresses.py` (`# impl: FR-001-11`), `tests/test_addresses.py` (`# verifies: FR-001-11`)
- [ ] T-007 [FR-001-03, FR-001-06] critical: Розбір вхідних SOL-переказів: `parse.py::parse_transaction(raw) -> ParsedTx` витягує з інструкцій верхнього рівня й `innerInstructions` System `transfer`, `transferWithSeed`, `createAccount`, `createAccountWithSeed` → `Transfer(asset="sol", decimals=None)` з `instruction_path` `"i"`/`"i.j"`, `signature`, `slot`, `block_time`, `sender`, `receiver`, `amount` (lamports, int); `meta.err != null` → `ParsedTx.failed=True` без переказів; також `programs` (programId верхнього рівня, у порядку), `sol_delta(owner)`, `fee_payer`, `fee`, `created_accounts_lamports` — тест: `tests/test_parse_sol.py::test_top_level_and_inner_sol_transfers_extracted_with_all_fields`, `::test_create_account_counts_as_sol_transfer`, `::test_failed_transaction_yields_no_transfers`, `::test_two_identical_transfers_get_distinct_instruction_paths`, `::test_non_transfer_instructions_ignored_but_program_recorded`; файли: `src/unmask/ingest/parse.py` (`# impl: FR-001-03, FR-001-04, FR-001-06, FR-001-09`), `tests/test_parse_sol.py` (`# verifies: FR-001-03, FR-001-06`)
- [ ] T-008 [FR-001-04, FR-001-06] critical: Розбір вхідних SPL-переказів у `parse.py`: spl-token і spl-token-2022 `transfer`/`transferChecked` (верхній рівень і inner) → `Transfer(asset="spl:<mint>")`, `sender`/`receiver` — **власники** токен-рахунків через `pre/postTokenBalances[accountIndex].owner` (індекс за `accountKeys`), `amount` з `tokenAmount.amount`/`amount` як int, `decimals` з `uiTokenAmount.decimals`; WSOL лишається `spl:So111…112`; `token_delta(owner, mint)` для правила купівлі — тест: `tests/test_parse_spl.py::test_transfer_checked_resolves_owners_from_token_balances`, `::test_legacy_transfer_without_mint_field_uses_token_balance_mint`, `::test_token_2022_program_recognized`, `::test_wsol_is_spl_not_sol`, `::test_token_delta_per_owner_and_mint`; файли: `src/unmask/ingest/parse.py`, `tests/test_parse_spl.py` (`# verifies: FR-001-04, FR-001-06`)
- [ ] T-009 [FR-001-06, FR-001-09] critical: Пошкоджені записи не маскуються: `parse_transaction` повертає `CorruptRecord(signature, reason)` замість тихого пропуску, якщо немає `meta`, `slot`, `transaction.message.accountKeys`, або токен-рахунок переказу не знайдено у `pre/postTokenBalances`, або `amount` не парситься як int; `None` від `get_transactions` трактується викликачем як `unavailable` (не тут) — тест: `tests/test_parse_corrupt.py::test_missing_meta_is_corrupt_record`, `::test_missing_slot_is_corrupt_record`, `::test_unresolvable_token_account_owner_is_corrupt_record`, `::test_non_integer_amount_is_corrupt_record`, `::test_valid_transaction_is_not_flagged`; файли: `src/unmask/ingest/parse.py`, `tests/test_parse_corrupt.py` (`# verifies: FR-001-06, FR-001-09`)
- [ ] T-010 [FR-001-01, FR-001-02] critical: Правило купівлі (research R-2): `purchases.py::detect_purchases(parsed, mint) -> list[Purchase]`: власник O — покупець, якщо `token_delta(O, mint) > 0` **і** (`spent_sol(O) > 0` **або** ∃ m≠mint: `token_delta(O, m) < 0`), де `spent_sol = −Δsol(O) − fee·[O=fee_payer] − created_accounts_lamports·[O=fee_payer]`; `Purchase` несе `wallet, signature, slot, block_time, received_amount, spent[], programs, address_type`; транзакції з `failed=True` дають порожній список — тест: `tests/test_purchases.py::test_swap_sol_for_token_is_purchase_with_spent_sol`, `::test_swap_usdc_for_token_is_purchase_with_spent_spl`, `::test_plain_token_transfer_recipient_is_not_purchase`, `::test_airdrop_recipient_paying_own_ata_rent_is_not_purchase`, `::test_mint_to_creator_paying_rent_is_not_purchase`, `::test_pool_pda_receiving_tokens_on_user_sell_is_purchase_off_curve`, `::test_sponsored_swap_non_fee_payer_is_purchase`, `::test_failed_tx_yields_nothing`; файли: `src/unmask/ingest/purchases.py` (`# impl: FR-001-01, FR-001-02`), `tests/test_purchases.py` (`# verifies: FR-001-01, FR-001-02`)

**Checkpoint**: інтерфейс, сценарії й чисті функції готові — user stories можна писати тест-першими.

---

## Phase 3: User Story 1 — Вибірка ранніх покупців і історія фінансування (Priority: P1) 🎯 MVP

**Goal**: для mint повернути рівно перших N покупців у детермінованому порядку й усі вхідні перекази до `funding_depth`, без третього стрибка, без переказів після купівлі, без дублікатів.

**Independent Test**: сценарій `basic`, `first_buyers_n=3`, `funding_depth=2` → результат збігається з `expected.json` без розбіжностей (`tests/test_collector.py::test_basic_scenario_matches_expected_golden`).

- [ ] T-011 [FR-001-01, FR-001-02] [US1] critical: Перші N покупців із курсором (research R-6, R-7): `buyers.py::enumerate_buyers(source, mint, state, config, deadline)` перегортає `get_signatures_for_address(mint)` від найновішого через `before` сторінками `rpc.page_size`, зберігає `(signature, slot, block_time, err)` і `signature_cursor`; після вичерпання історії бере транзакції **від найстаріших** пакетами, пропускає `err != null`, застосовує `detect_purchases`, накопичує перше входження кожного `wallet`, **добиває поточний слот до кінця** після досягнення N, сортує за `(first_buy_slot, first_buy_signature, wallet)`, бере рівно N і проставляє `rank`; менше за N → усі з `buyers.complete=true`; `transactions_scanned` рахується — тест: `tests/test_buyers.py::test_exactly_n_buyers_in_first_buy_order_no_later_buyer_included`, `::test_same_slot_tie_broken_by_signature_then_wallet_and_stable_across_runs`, `::test_nth_buyer_boundary_inside_slot_processes_whole_slot_before_cut`, `::test_fewer_buyers_than_n_returns_all_and_marks_complete`, `::test_repeat_purchase_by_same_wallet_counts_once_at_first`, `::test_pagination_cursor_advances_through_multiple_pages` (page_size=2 на `basic`); файли: `src/unmask/ingest/buyers.py` (`# impl: FR-001-01, FR-001-02`), `tests/test_buyers.py` (`# verifies: FR-001-01, FR-001-02`)
- [ ] T-012 [FR-001-03, FR-001-04, FR-001-05, FR-001-07] [US1] critical: BFS джерел фінансування по рівнях (research R-1, R-8, R-9): `funding.py::expand_level(source, state, depth, config, deadline)`: для кожної вершини рівня `cutoff_for(node)` = підпис першої купівлі (глибина 0) або найпізніше ребро до вершини попереднього рівня; `get_signatures_for_address(wallet, before=cutoff)`; якщо `collect_spl_inbound` — `get_token_accounts_by_owner` і історії токен-рахунків від найновішого з фільтром `slot ≤ slot(cutoff)`; транзакції — через `state.tx_cache` (один `get_transactions` на підпис за прогін); лишаються лише перекази з `receiver == wallet`, `sender != receiver`; ключ дедупу `(signature, instruction_path)`, `depth` мінімальний; відправники стають вершинами рівня `depth+1`, але рівень `> funding_depth` не розгортається; `expanded` поповнюється — тест: `tests/test_funding.py::test_depth1_and_depth2_edges_present_with_all_fields` (B→A і A→P1), `::test_third_hop_absent_at_depth_2` (C→B), `::test_transfer_after_first_buy_excluded` (X→P1), `::test_depth2_transfer_after_funding_edge_excluded_causal_cutoff`, `::test_cycle_a_d_a_terminates_without_duplicates`, `::test_self_transfer_ignored`, `::test_shared_funder_expanded_once_transfers_not_duplicated` (A для P1 і P2), `::test_spl_inbound_to_existing_token_account_collected` (USDC→P2), `::test_collect_spl_inbound_false_skips_token_account_calls` (журнал викликів), `::test_buyer_without_history_has_empty_funding_and_no_error` (P4); файли: `src/unmask/ingest/funding.py` (`# impl: FR-001-03, FR-001-04, FR-001-05, FR-001-07, FR-001-08`), `tests/test_funding.py` (`# verifies: FR-001-03, FR-001-04, FR-001-05, FR-001-07`)
- [ ] T-013 [FR-001-08] [US1] critical: Межі розгортання (research R-3): у `funding.py` лічити унікальних відправників інкрементально (від найновішого до межі); перевищення `counterparty_threshold` → `UnexpandedNode(reason=high_degree, counterparties_seen, signatures_seen, signatures_truncated)`, зібрані до порога перекази **зберігаються**, відправники цієї вершини **не** стають вершинами наступного рівня; історія довша за `max_signatures_per_wallet` (гаманець + токен-рахунки разом) → переглядаються лише найновіші до межі `max_signatures_per_wallet`, `UnexpandedNode(reason=signature_cap)`, знайдені відправники розгортаються нормально; `high_degree` має пріоритет над `signature_cap`; `unexpanded[]` не впливає на `Completeness` — тест: `tests/test_funding_limits.py::test_hub_over_threshold_marked_high_degree_with_counts`, `::test_hub_collected_transfers_kept_and_senders_not_expanded`, `::test_long_history_marked_signature_cap_and_scans_only_cap_newest`, `::test_high_degree_wins_over_signature_cap`, `::test_unexpanded_does_not_make_result_incomplete`, `::test_below_threshold_node_not_marked` (сценарій `hub`, `counterparty_threshold=3`, `max_signatures_per_wallet=50`); файли: `src/unmask/ingest/funding.py`, `tests/test_funding_limits.py` (`# verifies: FR-001-08`)
- [ ] T-014 [FR-001-09, FR-001-10] [US1] critical: Оркестрація збору й виведення повноти: `collector.py::CollectionState` (поля з `data-model.md`) і `collect(state, source, config, clock) -> IngestResult`: перелічення покупців → рівні 1…`funding_depth` → `Completeness.derive` → `RunMetadata` (усі поля FR-001-14: `wallets_analyzed`, `config_version`, використані N/глибина/пороги, `rpc_calls`, `transactions_scanned`, `source=source.name`, `elapsed_seconds` з `clock`); `CorruptRecord` і `None`-транзакції → `MissingHistory(reason=corrupt_data | unavailable, detail=signature)` для гаманця, чия історія розбиралась, решта записів гаманця зберігається; упорядкування результату ключами з `model.py` — тест: `tests/test_collector.py::test_basic_scenario_matches_expected_golden` (SC-001: `to_dict(result)` без volatile-полів metadata == `expected.json`), `::test_complete_status_with_empty_missing_when_all_fetched`, `::test_corrupt_scenario_marks_incomplete_with_corrupt_data_reason_and_keeps_other_transfers`, `::test_null_transaction_marks_unavailable_not_silently_dropped`, `::test_metadata_reports_used_config_values_and_version`, `::test_output_order_independent_of_rpc_response_order` (перемішане `rpc.json`); файли: `src/unmask/ingest/collector.py` (`# impl: FR-001-09, FR-001-10, FR-001-13, FR-001-16`), `tests/test_collector.py` (`# verifies: FR-001-09, FR-001-10`)

**Checkpoint**: US1 працює на фікстурі end-to-end і збігається з еталоном.

---

## Phase 4: User Story 2 — Чесна неповнота (Priority: P1)

**Goal**: будь-який збій джерела чи вичерпаний бюджет дає `incomplete` з переліком і причинами; повторний запит домагається лише відсутнього.

**Independent Test**: `FailAfter(K, RpcRateLimited)` на `basic` → `status=incomplete`, `missing[]` з гаманцями й `rate_limited`; після зняття збою повторний `collect` звертається лише за цими гаманцями (журнал викликів) і дає `complete`.

- [ ] T-015 [FR-001-16] [US2] critical: Бюджет часу: `budget.py::Deadline(clock, seconds)` (`remaining`, `expired`, `request_timeout(cap)`) передається в кожен виклик джерела з `collector`, `buyers`, `funding`; перед кожним кроком перевіряється `expired()`; на вичерпанні збір зупиняється негайно: нерозгорнуті вершини поточного й наступних рівнів → `MissingHistory(reason=budget_exhausted)`, незавершене перелічення → `buyers.complete=false, reason=budget_exhausted`; `RpcTimeout`, спричинений дедлайном, мапиться в `budget_exhausted`, звичайний — у `timeout` — тест: `tests/test_budget.py::test_deadline_remaining_and_expired_with_fake_clock`, `::test_request_timeout_is_min_of_remaining_and_cap`, `::test_budget_exhausted_mid_funding_returns_partial_incomplete_with_reason` (`FakeClock(advance_per_call=…)`, бюджет вичерпується на рівні 2), `::test_budget_exhausted_during_buyer_enumeration_marks_buyers_incomplete`, `::test_no_rpc_call_made_after_deadline_expired` (журнал), `::test_within_budget_result_complete`; файли: `src/unmask/ingest/budget.py`, `src/unmask/ingest/collector.py`, `src/unmask/ingest/buyers.py`, `src/unmask/ingest/funding.py`, `tests/test_budget.py` (`# verifies: FR-001-16`)
- [ ] T-016 [FR-001-09, FR-001-10] [US2] critical: Збої джерела посеред збору: `RpcRateLimited`/`RpcTimeout`/`RpcUnavailable` з будь-якого виклику → `MissingHistory` для гаманця (або `buyers.complete=false`) з відповідною причиною й `detail`; збір **продовжується** для решти вершин у межах бюджету; жоден виняток джерела не виходить із `collect`; часткові дані зберігаються у результаті — тест: `tests/test_collector_failures.py::test_rate_limit_after_k_calls_yields_incomplete_with_rate_limited_reason_per_wallet`, `::test_unavailable_for_one_wallet_keeps_other_wallets_complete`, `::test_timeout_reason_distinct_from_budget_exhausted`, `::test_failure_during_buyer_enumeration_marks_buyers_incomplete_with_reason`, `::test_partial_transfers_retained_alongside_missing`, `::test_never_complete_when_any_missing[...]` (параметризовано по трьох винятках); файли: `src/unmask/ingest/collector.py`, `tests/test_collector_failures.py` (`# verifies: FR-001-09, FR-001-10`)
- [ ] T-017 [FR-001-13] [US2] critical: Партиційний стан і повторення лише недоотриманого: `cache.py::ResultCache.get_partial/put_partial/drop_partial`, `collector.py::resume(state, …)`: продовжує перелічення з `signature_cursor`, якщо `mint_history_exhausted=false`; повторює гаманці з `missing` (успіх видаляє запис) і нерозгорнуті рівні; гаманці з `expanded` **не** запитуються; `metadata.resumed=true`; стан із `config_version ≠ config.version` відкидається, збір починається заново; після повного успіху `partial` чиститься, результат кладеться у `complete` — тест: `tests/test_resume.py::test_partial_never_returned_as_final_result`, `::test_resume_fetches_only_missing_wallets` (журнал: жодного `get_signatures_for_address` для гаманців з `expanded`), `::test_resume_continues_buyer_enumeration_from_cursor`, `::test_resume_after_failure_cleared_yields_complete_and_moves_to_complete_cache`, `::test_partial_with_stale_config_version_is_discarded_and_recollected`, `::test_resume_result_equals_uninterrupted_collection` (той самий `expected.json`); файли: `src/unmask/ingest/cache.py` (`# impl: FR-001-12, FR-001-13`), `src/unmask/ingest/collector.py`, `tests/test_resume.py` (`# verifies: FR-001-13`)

**Checkpoint**: US1 і US2 незалежно працюють; жоден тест не дає `complete` при неповних даних.

---

## Phase 5: User Story 3 — Повторний запит і неіснуючий токен (Priority: P2)

**Goal**: другий запит — з кешу без звернень до джерела; некоректна адреса й неіснуючий токен — явна відмова без збою; серіалізація за контрактом.

**Independent Test**: два `collect` на `basic` → другий з 0 викликів у журналі й ідентичним `to_dict` (крім `served_from_cache`); `collect("not-an-address")` і `collect(<адреса без рахунку>)` → `Rejection` потрібного `kind`.

- [ ] T-018 [FR-001-12] [US3] critical: Кеш повних результатів і публічний сервіс: `cache.py::ResultCache.get_complete/put_complete` (`put_complete` з `status=incomplete` → `CacheInvariantError`), `service.py::IngestService(config, source, clock, cache).collect(mint)`: порядок кроків за `contracts/ingest-service.md`; `complete` → копія з `served_from_cache=true`; `incomplete` → лише `put_partial` — тест: `tests/test_cache.py::test_second_collect_makes_zero_source_calls` (журнал порожній між викликами), `::test_second_result_identical_except_served_from_cache_flag`, `::test_incomplete_result_is_not_in_complete_cache`, `::test_put_complete_rejects_incomplete_with_cache_invariant_error`, `::test_cache_is_per_mint`; файли: `src/unmask/ingest/cache.py`, `src/unmask/ingest/service.py` (`# impl: FR-001-11, FR-001-12, FR-001-13`), `tests/test_cache.py` (`# verifies: FR-001-12`)
- [ ] T-019 [FR-001-11] [US3] Явні відмови без збою: у `service.collect` — некоректна адреса → `Rejection(invalid_address)` **до** будь-якого виклику джерела; `get_account_info(mint)` → `None` → `Rejection(token_not_found, detail=account_missing)`; `owner` не Token/Token-2022 або `data.parsed.type != "mint"` → `Rejection(token_not_found, detail=not_a_mint)`; відмови не кешуються; збій джерела на цьому кроці → `Rejection` не виникає, повертається `IngestResult` зі `status=incomplete` і `buyers.complete=false` — тест: `tests/test_service_rejections.py::test_invalid_address_rejected_without_source_call[...]` (порожній, не-base58, 31 байт), `::test_missing_account_is_token_not_found_account_missing`, `::test_system_owned_account_is_token_not_found_not_a_mint`, `::test_token_2022_mint_accepted`, `::test_rejection_not_cached_and_retried_next_call`, `::test_no_exception_escapes_collect_for_any_data_problem`; файли: `src/unmask/ingest/service.py`, `tests/test_service_rejections.py` (`# verifies: FR-001-11`)
- [ ] T-020 [FR-001-06, FR-001-14] [US3] [P] Серіалізація за контрактом: `serialize.py::to_dict(outcome)`/`to_json(outcome)` (sort_keys, ensure_ascii=False) для `IngestResult` і `Rejection` за `contracts/ingest-result.schema.json`; усі поля `RunMetadata` і 7 полів доказу `Transfer` присутні; `basic/expected.json` і `hub/expected.json` самі валідні проти схеми — тест: `tests/test_serialize_contract.py::test_result_dict_validates_against_schema` (`jsonschema.Draft202012Validator`), `::test_rejection_dict_validates_against_schema`, `::test_incomplete_result_validates_and_complete_with_missing_is_rejected_by_schema`, `::test_expected_fixtures_validate_against_schema`, `::test_to_json_is_deterministic_and_round_trips`; файли: `src/unmask/ingest/serialize.py` (`# impl: FR-001-06, FR-001-14`), `tests/test_serialize_contract.py` (`# verifies: FR-001-06, FR-001-14`)

**Checkpoint**: усі три user stories перевіряються незалежно на фікстурах; `uv run pytest` зелений; `python3 scripts/trace.py` зелений.

---

## Phase 6: Живий RPC-адаптер (deferrable — research R-4)

**Purpose**: реалізація `RpcSource` для RPC Fast, щоб фіча 002 і контрольна точка 8 жовтня мали реальні дані. Не блокує фази 1–5; тестується без сокетів через підмінний транспорт.

- [ ] T-021 [FR-001-15] `rpc/http.py::HttpRpcSource(url, rpc_cfg, transport=None)`: JSON-RPC 2.0 через `httpx.Client(transport=…)`; параметри `encoding=jsonParsed`, `maxSupportedTransactionVersion=0`, `commitment` з конфігу; `get_transactions` — batch-запит розміром ≤ `rpc.page_size` або пул на `rpc.max_concurrency` зі збереженням порядку; `get_token_accounts_by_owner` об'єднує Token і Token-2022; мапування: HTTP 429 → `RpcRateLimited(retry_after)`, таймаут → `RpcTimeout`, 5xx/JSON-RPC `error`/невалідний JSON → `RpcUnavailable`; повтори `rpc.max_retries` з експоненційною паузою `rpc.retry_backoff_seconds` у межах `deadline`; перед кожним запитом `deadline.expired()` → `RpcTimeout("budget")`; URL із ключем лише ззовні (`UNMASK_RPC_URL`), у логи не потрапляє — тест: `tests/test_rpc_http.py::test_request_envelope_and_params_for_each_method` (`httpx.MockTransport` перевіряє тіло запиту), `::test_batch_get_transactions_preserves_order_and_none_for_missing`, `::test_429_maps_to_rate_limited_with_retry_after`, `::test_timeout_maps_to_rpc_timeout`, `::test_5xx_and_jsonrpc_error_map_to_unavailable`, `::test_retries_then_succeeds_within_deadline`, `::test_expired_deadline_sends_no_request`, `::test_no_socket_opened` (гард з `conftest` лишається активним); файли: `src/unmask/ingest/rpc/http.py` (`# impl: FR-001-15`), `tests/test_rpc_http.py` (`# verifies: FR-001-15`)

---

## Dependencies & Execution Order

### Phase Dependencies

| Фаза | Залежить від | Блокує |
|---|---|---|
| 1 Setup (T-001…T-003) | — | усе |
| 2 Foundational (T-004…T-010) | фаза 1 | фази 3–6 |
| 3 US1 (T-011…T-014) | фаза 2 | фази 4–5 |
| 4 US2 (T-015…T-017) | T-014 | T-018 |
| 5 US3 (T-018…T-020) | T-014 (T-018 також T-017 — спільний `cache.py`) | — |
| 6 HTTP (T-021) | T-004 | — (deferrable) |

### Внутрішні залежності

| Задача | Після | Примітка |
|---|---|---|
| T-002, T-003 | T-001 | паралельні між собою |
| T-004 | T-003 | потребує типів; дає мінімум `budget.py` |
| T-005 | T-004 | генерований `basic` має завантажуватись у `FixtureRpcSource` |
| T-006 | T-001 | паралельний із T-004/T-005 |
| T-007 → T-008 → T-009 | T-005 | послідовні — один файл `parse.py` |
| T-010 | T-009, T-006 | |
| T-011 | T-010, T-005 | |
| T-012 | T-011, T-005 | спільний контракт `CollectionState` |
| T-013 | T-012 | той самий `funding.py` |
| T-014 | T-013 | |
| T-015 → T-016 → T-017 | T-014 | послідовні — усі торкаються `collector.py` |
| T-018 | T-017 | спільний `cache.py` |
| T-019 | T-018 | той самий `service.py` |
| T-020 | T-014 | паралельний із T-018/T-019 |
| T-021 | T-004 | будь-коли, у тому числі паралельно з фазами 3–5 |

### Parallel Opportunities

- Фаза 1: T-002 ∥ T-003.
- Фаза 2: T-006 ∥ (T-004 → T-005); T-007…T-010 — послідовно.
- Фази 3–5 і T-021: `rpc/http.py` не перетинається з ядром — другий виконавець бере T-021, поки перший веде US1→US3.
- Фаза 5: T-020 ∥ T-018/T-019.

## Parallel Example: Phase 2

```bash
# Паралельно (різні файли, без спільних залежностей):
Task: "T-006 addresses.py + tests/test_addresses.py"
Task: "T-004 rpc/protocol.py + rpc/fixture.py + tests/test_rpc_fixture.py"
# Потім T-005 (генератор сценаріїв), потім ланцюжок T-007 → T-008 → T-009 → T-010 в parse.py/purchases.py
```

## Implementation Strategy

### MVP First (фази 1–3)

1. T-001…T-003: каркас, YAML, типи з інваріантом повноти.
2. T-004…T-010: межа зовнішнього світу, сценарії, розбір, правило купівлі — усе на чистих функціях.
3. T-011…T-014: US1 end-to-end на `basic` зі збігом із `expected.json`.
4. **STOP and VALIDATE**: `uv run pytest tests/test_collector.py`, `python3 scripts/trace.py`, ревʼю.

### Incremental Delivery

- + фаза 4 → неповнота чесна, resume працює → можна показувати на зламаному джерелі.
- + фаза 5 → кеш і відмови → контракт для фічі 002 стабільний (`ingest-result.schema.json`).
- + фаза 6 → реальні токени для контрольної точки 8 жовтня.

### Розподіл за ролями

- `implementer-senior` (opus): усі рядки з `critical:` — T-003, T-007…T-018.
- `implementer` (sonnet): T-001, T-002, T-004, T-005, T-006, T-019, T-020, T-021.
- `reviewer` після кожної задачі; перед позначенням `[x]` — `python3 scripts/trace.py` зелений.

## Покриття вимог

| FR | Задачі |
|---|---|
| FR-001-01 | T-002, T-010, T-011 |
| FR-001-02 | T-010, T-011 |
| FR-001-03 | T-007, T-012 |
| FR-001-04 | T-008, T-012 |
| FR-001-05 | T-002, T-012 |
| FR-001-06 | T-003, T-007, T-008, T-009, T-020 |
| FR-001-07 | T-012 |
| FR-001-08 | T-002, T-013 |
| FR-001-09 | T-003, T-009, T-014, T-016 |
| FR-001-10 | T-003, T-014, T-016 |
| FR-001-11 | T-006, T-019 |
| FR-001-12 | T-018 |
| FR-001-13 | T-017 |
| FR-001-14 | T-002, T-003, T-020 |
| FR-001-15 | T-001, T-004, T-005, T-021 |
| FR-001-16 | T-002, T-015 |

## Notes

- Один прохід субагента — одна задача; тест пишеться першим і спостерігається червоним із очікуваної причини.
- Тест, що лишається зеленим при заміні `FixtureRpcSource` на джерело з порожніми відповідями, нічого не доводить — ревʼюер відхилить.
- Жодна константа, що впливає на результат, не з'являється в коді: лише `config/ingest.yaml` із записом у `config/CHANGELOG.md`.
- `spec.md` і `plan.md` під час імплементації не змінюються; нездійсненна як специфіковано задача → `BLOCKED`.
