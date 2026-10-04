# config/ingest.yaml — changelog

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
