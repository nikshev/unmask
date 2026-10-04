# Матриця трасування

<!-- ГЕНЕРУЄТЬСЯ scripts/trace.py — не редагувати вручну -->

Вимог: **37** · задач: **50** (виконано 24) · вимог у роботі: **21** · порушень: **0**

## 001-onchain-data-ingest

| Вимога | Опис | Задачі | Імплементація | Тести |
|---|---|---|---|---|
| `FR-001-01` | Система MUST вибирати перших N покупців токена за часом першої купівлі (за замовчуванням N із діапазону 200–50 | `T-002`, `T-010`, `T-011` | `src/unmask/ingest/collector.py`, `src/unmask/ingest/purchases.py`, `src/unmask/ingest/buyers.py`, `src/unmask/ingest/config.py` | `tests/test_buyers.py`, `tests/test_purchases.py`, `tests/test_config.py` |
| `FR-001-02` | Система MUST впорядковувати покупців детерміновано: однаковий вхід дає однаковий порядок, зокрема при купівлях | `T-010`, `T-011` | `src/unmask/ingest/collector.py`, `src/unmask/ingest/purchases.py`, `src/unmask/ingest/buyers.py` | `tests/test_buyers.py`, `tests/test_purchases.py` |
| `FR-001-03` | Для кожного покупця система MUST збирати вхідні перекази SOL, що відбулися до моменту його першої купівлі. | `T-007`, `T-012` | `src/unmask/ingest/parse.py`, `src/unmask/ingest/funding.py` | `tests/test_funding.py`, `tests/test_parse_sol.py` |
| `FR-001-04` | Для кожного покупця система MUST збирати вхідні перекази SPL-токенів, що відбулися до моменту його першої купі | `T-007`, `T-008`, `T-012`, `T-050` | `src/unmask/ingest/parse.py`, `src/unmask/ingest/funding.py` | `tests/test_parse_spl.py`, `tests/test_funding.py`, `tests/test_parse_real_corpus.py` |
| `FR-001-05` | Система MUST обходити джерела фінансування назад на задану глибину (за замовчуванням 2, допустимо до 3; значен | `T-002`, `T-012` | `src/unmask/ingest/funding.py`, `src/unmask/ingest/config.py` | `tests/test_funding.py`, `tests/test_config.py` |
| `FR-001-06` | Кожен переказ у результаті MUST нести: відправника, отримувача, актив, суму, час, слот і підпис транзакції — п | `T-003`, `T-007`, `T-008`, `T-009`, `T-020`, `T-050` | `src/unmask/ingest/parse.py`, `src/unmask/ingest/serialize.py`, `src/unmask/ingest/model.py` | `tests/test_parse_spl.py`, `tests/test_parse_corrupt.py`, `tests/test_parse_sol.py`, `tests/test_model_completeness.py`, `tests/test_parse_real_corpus.py`, `tests/test_serialize_contract.py` |
| `FR-001-07` | Система MUST не включати переказ, що відбувся пізніше за першу купівлю відповідного покупця, і MUST не дублюва | `T-012` | `src/unmask/ingest/funding.py` | `tests/test_funding.py` |
| `FR-001-08` | Гаманець фінансувався з дуже «жвавої» адреси (біржа, роутер) із тисячами контрагентів: обхід через неї не розг | `T-002`, `T-012`, `T-013` | `src/unmask/ingest/funding.py`, `src/unmask/ingest/config.py` | `tests/test_funding_limits.py`, `tests/test_config.py` |
| `FR-001-09` | Результат MUST містити статус повноти («повний» або «неповний») і для кожного гаманця з недоотриманою історією | `T-003`, `T-007`, `T-009`, `T-014`, `T-016`, `T-050` | `src/unmask/ingest/parse.py`, `src/unmask/ingest/collector.py`, `src/unmask/ingest/funding.py`, `src/unmask/ingest/buyers.py`, `src/unmask/ingest/model.py` | `tests/test_parse_corrupt.py`, `tests/test_model_completeness.py`, `tests/test_parse_real_corpus.py`, `tests/test_collector.py`, `tests/test_collector_failures.py` |
| `FR-001-10` | Система MUST NOT видавати неповний результат як повний; відсутність даних MUST бути видимою у відповіді, а не  | `T-003`, `T-014`, `T-016` | `src/unmask/ingest/collector.py`, `src/unmask/ingest/funding.py`, `src/unmask/ingest/buyers.py`, `src/unmask/ingest/model.py` | `tests/test_model_completeness.py`, `tests/test_collector.py`, `tests/test_collector_failures.py` |
| `FR-001-11` | Коли токена не існує або адреса некоректна, система MUST повертати явну відповідь («токен не знайдено» або «не | `T-006`, `T-018`, `T-019` | `src/unmask/ingest/addresses.py`, `src/unmask/ingest/service.py` | `tests/test_service_rejections.py`, `tests/test_addresses.py` |
| `FR-001-12` | Система MUST кешувати повний результат за адресою токена; повторний запит MUST віддаватися з кешу без звернень | `T-017`, `T-018` | `src/unmask/ingest/service.py`, `src/unmask/ingest/cache.py` | `tests/test_cache.py` |
| `FR-001-13` | Неповний результат MUST NOT потрапляти в кеш як остаточний; повторний запит MUST домагатися лише відсутніх дан | `T-014`, `T-017`, `T-018`, `T-022` | `src/unmask/ingest/collector.py`, `src/unmask/ingest/service.py`, `src/unmask/ingest/funding.py`, `src/unmask/ingest/cache.py` | `tests/test_resume.py`, `tests/test_scan_memo.py` |
| `FR-001-14` | Результат MUST містити метадані: адресу токена, час аналізу, кількість покупців у вибірці, використані значенн | `T-002`, `T-003`, `T-014`, `T-020` | `src/unmask/ingest/serialize.py`, `src/unmask/ingest/collector.py`, `src/unmask/ingest/model.py`, `src/unmask/ingest/config.py` | `tests/test_config.py`, `tests/test_collector.py`, `tests/test_serialize_contract.py` |
| `FR-001-15` | Доступ до зовнішнього джерела даних MUST бути прихований за інтерфейсом, що дозволяє підставити записані фікст | `T-001`, `T-004`, `T-005`, `T-021`, `T-049` | `src/unmask/ingest/rpc/fixture.py`, `src/unmask/ingest/rpc/protocol.py`, `src/unmask/ingest/rpc/http.py` | `tests/test_rpc_http.py`, `tests/test_fixture_builder.py`, `tests/test_rpc_fixture.py`, `tests/test_no_network.py` |
| `FR-001-16` | Збір MUST вкладатися в налаштований бюджет часу (за замовчуванням 40 с із 60 с на весь холодний запит); при ви | `T-002`, `T-004`, `T-014`, `T-015` | `src/unmask/ingest/collector.py`, `src/unmask/ingest/funding.py`, `src/unmask/ingest/buyers.py`, `src/unmask/ingest/budget.py`, `src/unmask/ingest/config.py` | `tests/test_config.py`, `tests/test_budget.py` |

## 002-funding-graph-hub-pruning

| Вимога | Опис | Задачі | Імплементація | Тести |
|---|---|---|---|---|
| `FR-002-01` | Система MUST будувати орієнтований граф з результату збору: вершини — гаманці (покупці й джерела фінансування) | `T-028` | — | — |
| `FR-002-02` | Кожне ребро MUST нести суму, кількість переказів, час першого й останнього переказу та перелік первинних посил | `T-025`, `T-028` | — | — |
| `FR-002-03` | Кожна вершина MUST нести ролі (покупець, джерело) і мінімальну глибину від покупців; для покупців — порядковий | `T-025`, `T-028`, `T-029` | — | — |
| `FR-002-04` | Побудова графа MUST бути детермінованою: той самий вхід дає побітово той самий граф і порядок. | `T-028`, `T-031`, `T-039`, `T-040` | — | — |
| `FR-002-05` | Граф MUST успадковувати статус повноти результату збору й перелік причин неповноти; граф над неповним збором M | `T-025`, `T-028`, `T-030` | — | — |
| `FR-002-06` | Граф MUST зберігати нерозгорнуті вершини з результату збору з причиною (висока зв'язність, ліміт підписів) як  | `T-025`, `T-028`, `T-030` | — | — |
| `FR-002-07` | Система MUST позначати вершину хабом за кожним із незалежних критеріїв: (а) належність до відомих списків адре | `T-032`, `T-033`, `T-034`, `T-035` | — | — |
| `FR-002-08` | Пороги, списки адрес і версія MUST зберігатися у версіонованому YAML із журналом змін; зміна без запису в журн | `T-023`, `T-024` | — | — |
| `FR-002-09` | Відсікання MUST виключати хаб і всі ребра, інцидентні йому, з графа для кластеризації й MUST зберігати їх у ок | `T-035`, `T-038` | — | — |
| `FR-002-10` | Покупці MUST NOT відсікатися; покупець, що відповідає критерію хаба, отримує пояснювальну позначку. | `T-035` | — | — |
| `FR-002-11` | Система MUST формувати звіт ефекту відсікання: кількість вершин і ребер, кількість компонент, частка найбільшо | `T-027`, `T-036`, `T-038`, `T-042` | — | — |
| `FR-002-12` | Якщо список відомих адрес не застосовано (порожній чи недоступний), результат MUST це явно показувати; відсутн | `T-023`, `T-035`, `T-036`, `T-037` | — | — |
| `FR-002-13` | Результат MUST містити версію конфігу відсікання й версію списків адрес, щоб два результати з різними версіями | `T-023`, `T-039`, `T-040` | — | — |
| `FR-002-14` | Правило порогу (рівно поріг) MUST бути визначене однозначно й однаково для всіх критеріїв. | `T-023`, `T-033` | — | — |
| `FR-002-15` | Система MUST розпізнавати в транзакціях mint «делеговану купівлю»: платник витратив кошти й не отримав токен,  | `T-043`, `T-045` | — | — |
| `FR-002-16` | Неоднозначні випадки (кілька платників чи отримувачів без однозначної відповідності) MUST фіксуватися як «канд | `T-043`, `T-045` | — | — |
| `FR-002-17` | Розпізнавання swap-and-send MUST NOT змінювати склад, порядок і порядкові номери перших N покупців із фічі 001 | `T-046` | — | — |
| `FR-002-18` | Зв'язки «делегована купівля» MUST потрапляти в граф як окремий вид ребра з первинним посиланням; ребра цього в | `T-025`, `T-028`, `T-047` | — | — |
| `FR-002-19` | Якщо транзакції не вдалося розібрати для цього аналізу, результат MUST це позначати як неповний щодо swap-and- | `T-039`, `T-044`, `T-045`, `T-046`, `T-048` | — | — |
| `FR-002-20` | Усі зовнішні дані MUST надходити лише з результату збору (фіча 001); фіча не звертається до мережі напряму, те | `T-026`, `T-039`, `T-041` | — | — |
| `FR-002-21` | Результат MUST серіалізуватися у JSON за контрактом, версіонованим окремо від контракту фічі 001; зміни контра | `T-039`, `T-040`, `T-044`, `T-048` | — | — |

## Критичні задачі

- `T-003` (001-onchain-data-ingest) — виконано
- `T-007` (001-onchain-data-ingest) — виконано
- `T-008` (001-onchain-data-ingest) — виконано
- `T-009` (001-onchain-data-ingest) — виконано
- `T-010` (001-onchain-data-ingest) — виконано
- `T-011` (001-onchain-data-ingest) — виконано
- `T-012` (001-onchain-data-ingest) — виконано
- `T-013` (001-onchain-data-ingest) — виконано
- `T-014` (001-onchain-data-ingest) — виконано
- `T-015` (001-onchain-data-ingest) — виконано
- `T-016` (001-onchain-data-ingest) — виконано
- `T-017` (001-onchain-data-ingest) — виконано
- `T-018` (001-onchain-data-ingest) — виконано
- `T-022` (001-onchain-data-ingest) — виконано
- `T-049` (001-onchain-data-ingest) — виконано
- `T-050` (001-onchain-data-ingest) — виконано
- `T-024` (002-funding-graph-hub-pruning) — у роботі
- `T-025` (002-funding-graph-hub-pruning) — у роботі
- `T-027` (002-funding-graph-hub-pruning) — у роботі
- `T-028` (002-funding-graph-hub-pruning) — у роботі
- `T-030` (002-funding-graph-hub-pruning) — у роботі
- `T-032` (002-funding-graph-hub-pruning) — у роботі
- `T-033` (002-funding-graph-hub-pruning) — у роботі
- `T-034` (002-funding-graph-hub-pruning) — у роботі
- `T-035` (002-funding-graph-hub-pruning) — у роботі
- `T-036` (002-funding-graph-hub-pruning) — у роботі
- `T-039` (002-funding-graph-hub-pruning) — у роботі
- `T-044` (002-funding-graph-hub-pruning) — у роботі
- `T-045` (002-funding-graph-hub-pruning) — у роботі
- `T-046` (002-funding-graph-hub-pruning) — у роботі
- `T-047` (002-funding-graph-hub-pruning) — у роботі
