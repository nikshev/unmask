# Quickstart: Публічний API і Telegram-бот доставки аналізу (004)

Як переконатися, що фіча працює, без мережі. Контракти — `contracts/`, сутності —
`data-model.md`, рішення — `research.md`.

## Передумови

```bash
cd /opt/unmask
uv sync                                   # 004 додає Pillow (R-3, єдина нова залежність)
git config core.hooksPath .githooks       # якщо репозиторій клоновано заново
```

Тести не ходять у мережу й не відкривають сокетів: RPC — записаний транспорт
(`tests/fixtures/real/*.json`), Telegram — несправжній транспорт, HTTP-логіка —
викликом функцій обробника.

## 1. Документ відповіді за контрактом (FR-004-01, FR-004-12)

```bash
uv run pytest tests/test_delivery_report.py -q
```

Очікувано: документ зібраний з результату 003 слово в слово (кластери, `risk_score`,
смуга — ті самі числа), плюс `provenance` з версіями конфігів; `to_dict` валідний проти
`contracts/api-response.schema.json`; `insufficient_data` ніде не перетворено на `clean`.

## 2. Бот без мережі (FR-004-03, FR-004-04)

```bash
uv run pytest tests/test_delivery_bot.py -q
```

Очікувано: `/check <mint>` зі збереженим токеном пише в журнал несправжнього транспорту
рівно один `send_photo` (caption зі смугою, частками топ-3 і кнопкою «докази»);
callback кнопки дає `send_message` з переліком доказів; невалідний mint — текст помилки,
жодного винятку.

## 3. PNG (FR-004-05)

```bash
uv run pytest tests/test_delivery_render.py -q
```

Очікувано: байти відкриваються як PNG; кластери двох кластерів розрізнені за кольором;
порожній результат дає заглушку з підписом.

## 4. Кеш, час, межі (FR-004-06, FR-004-21)

```bash
uv run pytest tests/test_delivery_cache.py tests/test_delivery_boundaries.py -q
```

Очікувано: другий запит не чіпає транспорт і повертає той самий документ; `ast`-тест:
`delivery/*` імпортує з `unmask` лише моделі/сервіси 001–004 і `httpx`/`PIL` у своїх модулях.

## 5. Живий дим (вручну, з ключами оператора — поза типовим прогоном)

```bash
export UNMASK_RPC_URL='https://…key…'
export UNMASK_BOT_TOKEN='123:ABC…'
uv run python scripts/serve_unmask.py --port 8080
curl localhost:8080/api/token/<mint> | python3 -m json.tool
```

Очікувано: перший запит рахує (≤ 60 с), повторний — миттєво з кешу; невалідний mint —
4xx з поясненням; бот відповідає на `/check`.
