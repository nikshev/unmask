# config/hubs.yaml

## 1 — 2026-10-04
Початкова версія, не калібровано на реальних токенах: degree_threshold=100, one_off_senders_share=0.8,
one_off_min_senders=10, giant_component_warn_share=0.5, prune_off_curve=true, prune_ingest_high_degree=true.
Правило порогу — строго «більше» (рівно поріг — не хаб), одне для всіх критеріїв; one_off_min_senders — передумова «>=».
Обґрунтування — specs/002-funding-graph-hub-pruning/research.md R-12.
sha256: c410d677279d02687928b12fcf490e4685007c0bb499e12c2fbfb1ca6d25c63c

## 2 — 2026-10-04
Калібрування на 9 реальних токенах pump.fun (5 інсайдерських за MELT, 4 чисті; Helius, N=30, depth=2, кап 30;
specs/002-funding-graph-hub-pruning/calibration.md). Додано критерій dust_fanout (FR-002-22, research R-22):
dust_amount_lamports=1000000 (0,001 SOL; хаб, якщо медіана SOL-сум до різних покупців СТРОГО МЕНША),
dust_min_fanout=5 (передумова, включно). Чому: пилові джерела (fan-out 5–23, медіана < 0,001 SOL) є в кожному
токені й склеюють до 21/30 покупців; жоден критерій v1 їх не ловить. degree_threshold=100 лишено свідомо: на цих
даних неактивний (макс. ступінь 45), зниження до ~30 відсікло б справжнього фінансиста ins1 (ступінь 45, медіана
≥ 0,7 SOL). Решта значень v1 без змін. Пил/фінансист розділяє сума, не ступінь.
sha256: fdf65bb5369e4e40e629ca4cd45f4952466ee21447ae8bbe79a4ee0035f45409

# config/hub_addresses.yaml

## 1 — 2026-10-04
Початкова версія: system_programs(3), token_programs(3), dex_routers(1), amm_programs(2), launchpads(1),
exchanges(0), market_makers(0). Джерела — коментарі у файлі. Біржі й ММ — порожньо до калібрування.
sha256: 89c0a8be31ec2ffca32117ed38191e4caad0c840d9f4576048d0f312af80f2d5

# config/ingest.yaml

## 1 — 2026-10-03
Початкова версія: N=300, depth=2, counterparty_threshold=200, max_signatures_per_wallet=300,
collect_spl_inbound=true, time_budget_seconds=40, commitment=finalized, rpc.*=(1000, 10, 2, 0.5, 8).

## 2 — 2026-10-04
Додано обов'язковий `rpc.tx_batch_size: 25` (ціле 1..1000) — скільки підписів ядро (`buyers`, `funding`) просить
одним `get_transactions`; `rpc.page_size` (1000) тепер ЛИШЕ limit сторінки `getSignaturesForAddress`. Решта значень
без змін. Чому (T-052, known-issues §6.6): у версії 1 `page_size` задавав і розмір пачки транзакцій; на реальному
токені `95DELX…pump` (5880 транзакцій) виклик на 1000 транзакцій під пейсером адаптера (30 + 12/с) тривав 80+ с >
бюджету 40 с, «все або нічого» викидав 475 уже отриманих — нуль прогресу, resume не допомагав. 25 = під-batch
адаптера (`max_batch`): кожен завершений виклик лягає в кеш транзакцій. На результат збору `tx_batch_size` не
впливає (property-тест на 1, 2, 7, 25, 1000: basic, hub, corrupt і варіанти — `tests/test_tx_batch_size.py`);
версія підіймається, бо змінилась схема файла. Перевірено на фікстурах (basic, hub, corrupt) і емуляторі пейсера;
живий повтор на `95DELX…pump` — після злиття.
sha256: 6932a067da2dabe970cc0bea24a44170ef71948acd128664e0a71f2e36c62a3d
