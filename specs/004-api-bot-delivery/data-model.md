# Data Model: Публічний API і Telegram-бот доставки аналізу (004)

Ядро 001→002→003 не змінюється: фіча лише доставляє його висновки. Усі типи доставки —
незмінні структури з гучними інваріантами (стиль 002/003); мережа й час — лише на межах
(HTTP-рука, polling-цикл, годинник процесу), чиста логіка їх не бачить.

Розташування: `src/unmask/delivery/*.py`. Повторно використані типи:
`ingest.model` (`IngestResult`, `Rejection`), `graph.model` (`GraphResult`),
`clusters.model` (`ClusterResult`, `Cluster`, `Evidence`), `clusters.serialize` (`to_dict`).

## Перелічення

| Тип | Значення | Зміст |
|---|---|---|
| `OutcomeKind` | `ok` \| `rejection` \| `incomplete_error` | чим завершився запит: повна відповідь, відмова (невалідний/неіснуючий mint), помилка середовища |
| `Band` (перевикористання) | `clean` \| `suspicious` \| `high_concentration` \| `insufficient_data` | смуга 003 як є; шар доставки її не перетворює (FR-004-12) |

## Сутності

### `ReportProvenance`

Походження висновку (FR-004-12, принцип V): `ingest_config_version`, `hub_config_version`,
`address_lists_version | None`, `cluster_config_version`, `analyzed_at` (з метаданих 001
через 002/003 — єдине «часове» поле, з входу), `wallets_analyzed`, `share_denominator_kind`,
повнота (`graph_status`, `ingest_status`, `missing_count`, `delegated_complete`,
`graph_warnings`) і непорожній `band_reasons`. Інваріант: версії збігаються з конфігами,
якими рахували; документ без походження не конструюється.

### `ApiResponse`

Документ відповіді (контракт `api-response.schema.json`):
`mint`, `analyzed_at`, `wallets_analyzed`, `clusters[]` (спадно за часткою; у кожному
`wallets`, `supply_share` + явний `supply_share_denominator`, `confidence`,
`evidence[]` з `type`, `source` (адреси/`via`), `window`), `risk_score` 0–100, `band`,
`band_reasons[]`, `provenance`. Інваріанти: `clusters[]` — ті самі кластери результату 003
(порядок і склад), кожен з ≥ 1 доказом; `risk_score`/`band` — ті самі числа 003;
`insufficient_data` ніколи не серіалізується як `clean`.

### `CacheEntry`

`mint → (document, cached_at_monotonic)`. Ключ — точна адреса; значенння — незмінний
документ. Інвалідації немає (R-5); перезапуск процесу кеш чистить (документовано).

### `BotMessage`

Текст відповіді на `/check`: рядок смуги+числа, рядки топ-кластерів
(`#1: 12 гаманців, частка 0.44`), рядок повноти, прикріплене `GraphImage`,
inline-кнопка «докази» (`callback_data` з ідентифікатором запиту). Докази понад ліміт
довжини стискаються з лічильником залишку (R-8).

### `GraphImage`

PNG-байтівки + `width`/`height` + `clusters_shown` + `empty: bool`. Інваріант: непорожні
байти, що відкриваються як PNG; кластери результату візуально розрізнені (різні кольори),
легенда називає частки; порожній результат → заглушка з підписом.

### `Transport` (межі, для тестів без мережі)

- `RpcSource` — наявний інтерфейс 001 (`HttpRpcSource` живий; записаний дубль у тестах).
- `BotTransport` — новий мінімальний інтерфейс доставки: `send_message(chat, text, reply_markup)`,
  `send_photo(chat, png_bytes, caption, reply_markup)`, `answer_callback(query_id, text)`,
  `edit_message(chat, message_id, text, reply_markup)`. Жива реалізація — сирі виклики Bot API
  через `httpx` (R-2); несправжня — журнал викликів у тестах.
- HTTP-транспорт stdlib лишається на межі `delivery/http.py`; логіка обробника — чисті функції,
  що тестяться без сокетів.

## Показові сталі (FR-004-11, R-3)

На висновок не впливають, живуть поруч із кодом рендера/бота з документом тут:
розмір полотна PNG, радіуси вершин, палітра кластерів, довжина скорочення адрес у підписах
(префікс/суфікс), максимум доказів у стислому тексті кнопки. Зміна показової сталі не вимагає
запису в `config/CHANGELOG.md` (версіонуються лише YAML 001/002/003).

## Звʼязки

```
mint ── DeliveryService.analyze ──▶ IngestService.collect ──▶ GraphService.analyze
        ──▶ ClusterService.analyze ──▶ build_report ──▶ ApiResponse ──▶ to_json ──▶ HTTP
                                                        │                    ╰──▶ кеш (памʼять, single-flight)
                                                        ├──▶ render_png ──▶ GraphImage ──▶ BotTransport.send_photo
                                                        ╰──▶ format_message + evidence_text ──▶ BotTransport
Rejection ──▶ помилка 4xx / повідомлення бота (без стеку, процес живий)
```

`DeliveryService` — єдина, хто знає про 001/002/003 як про конвеєр; `report.py`/`render.py`/
`bot.py`/`http.py` бачать лише типи результатів. AST-тест меж це перевіряє.
