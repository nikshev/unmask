# Quickstart: пул RPC-провайдерів (007)

## Без мережі (CI)

```bash
uv run pytest tests/test_rpc_pool.py tests/test_rpc_pool_routing.py tests/test_rpc_pool_failover.py \
              tests/test_rpc_pool_equivalence.py tests/test_rpc_pool_config.py tests/test_delivery_main.py -q
uv run pytest -m perf tests/test_rpc_pool_perf.py      # необов'язково: реальні потоки й затримки
python3 scripts/trace.py --check
```

## Налаштування оператора

`.env` (поза git):

```text
UNMASK_RPC_URL=<RPC Fast Start>            # неархівний, ≈ 21,5 год історії
UNMASK_RPC_URL_ALT=<Helius free #1>   # архівні
UNMASK_RPC_URL_ALT1=<Helius free #2>
UNMASK_RPC_URL_ALT2=<Helius free #3>
```

Будь-яку змінну можна не задавати — провайдер пропускається з попередженням. Без `config/rpc_pool.yaml`
працює старий режим (лише `UNMASK_RPC_URL`, довіра як раніше). Зміна YAML — нова `version` + запис у
`config/CHANGELOG.md` (розділ `# config/rpc_pool.yaml`), інакше тест журналу червоний.

## Живий замір SC-002 (вручну, не CI) — T-145

1. Токен із ≥ 300 транзакціями, старший за добу (щоб перевірити архівну маршрутизацію).
2. Прогін A: лише `UNMASK_RPC_URL_ALT`, без інших змінних. Прогін B: усі чотири.
3. Порівняти `buyers`, `transfers`, `completeness.status` (побайтово через `to_json`); час холодного збору
   B ≤ 0,5 × A; у лозі `rpc_pool …` — `requests > 0` у кожного провайдера, зокрема `rpcfast`.
4. Записати числа в `specs/007-rpc-provider-pool/` (нотатка гейта), секретів не копіювати.
