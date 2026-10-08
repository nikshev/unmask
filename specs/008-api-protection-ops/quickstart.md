# Quickstart: перевірка 008

## 1. Без мережі (типовий прогін)

```bash
uv run pytest -q tests/test_protection_config.py tests/test_delivery_limits.py \
  tests/test_delivery_service_slots.py tests/test_delivery_client_ip.py \
  tests/test_delivery_http_protection.py tests/test_delivery_bot_limits.py \
  tests/test_deploy_artifacts.py tests/test_delivery_protection_e2e.py
uv run pytest -q tests/test_delivery_*.py tests/test_hubs_config.py tests/test_tx_batch_size.py  # 004/002/001 зелені
python3 scripts/trace.py --check
```

## 2. Ручний дим ліміту (локально, з ключами оператора)

Тимчасова копія конфігу з малими межами (не комітиться):

```bash
mkdir -p /tmp/unmask-cfg && cp config/*.yaml /tmp/unmask-cfg/
# у /tmp/unmask-cfg/protection.yaml: api.requests_per_window: 3, heavy.max_concurrent: 1
uv run python scripts/serve_unmask.py --no-bot --host 127.0.0.1 --port 18999 --config-dir /tmp/unmask-cfg &
for i in 1 2 3 4; do curl -s -o /dev/null -w '%{http_code}\n' localhost:18999/api/token/abc; done
# очікування: 400 400 400 429; curl -si … | grep Retry-After
curl -s localhost:18999/healthz          # 200 завжди
# два різні холодні mint одночасно → другий 503 з Retry-After; повтор кешованого mint → 200
```

## 3. Розгортання (оператор, після ревʼю)

README «Deploy»; після встановлення: `deploy/smoke.sh <domain>` → усі перевірки `ok`.
