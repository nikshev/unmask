# Матриця трасування

<!-- ГЕНЕРУЄТЬСЯ scripts/trace.py — не редагувати вручну -->

Вимог: **61** · задач: **87** (виконано 87) · вимог у роботі: **0** · порушень: **0**

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
| `FR-001-09` | Результат MUST містити статус повноти («повний» або «неповний») і для кожного гаманця з недоотриманою історією | `T-003`, `T-007`, `T-009`, `T-014`, `T-016`, `T-050` | `src/unmask/ingest/parse.py`, `src/unmask/ingest/collector.py`, `src/unmask/ingest/funding.py`, `src/unmask/ingest/buyers.py`, `src/unmask/ingest/model.py` | `tests/test_delegated_wiring.py`, `tests/test_parse_corrupt.py`, `tests/test_model_completeness.py`, `tests/test_parse_real_corpus.py`, `tests/test_collector.py`, `tests/test_collector_failures.py` |
| `FR-001-10` | Система MUST NOT видавати неповний результат як повний; відсутність даних MUST бути видимою у відповіді, а не  | `T-003`, `T-014`, `T-016` | `src/unmask/ingest/collector.py`, `src/unmask/ingest/funding.py`, `src/unmask/ingest/buyers.py`, `src/unmask/ingest/model.py` | `tests/test_model_completeness.py`, `tests/test_collector.py`, `tests/test_collector_failures.py` |
| `FR-001-11` | Коли токена не існує або адреса некоректна, система MUST повертати явну відповідь («токен не знайдено» або «не | `T-006`, `T-018`, `T-019` | `src/unmask/ingest/addresses.py`, `src/unmask/ingest/service.py` | `tests/test_service_rejections.py`, `tests/test_addresses.py` |
| `FR-001-12` | Система MUST кешувати повний результат за адресою токена; повторний запит MUST віддаватися з кешу без звернень | `T-017`, `T-018` | `src/unmask/ingest/service.py`, `src/unmask/ingest/cache.py` | `tests/test_cache.py` |
| `FR-001-13` | Неповний результат MUST NOT потрапляти в кеш як остаточний; повторний запит MUST домагатися лише відсутніх дан | `T-014`, `T-017`, `T-018`, `T-022`, `T-052` | `src/unmask/ingest/collector.py`, `src/unmask/ingest/service.py`, `src/unmask/ingest/funding.py`, `src/unmask/ingest/buyers.py`, `src/unmask/ingest/cache.py`, `src/unmask/ingest/config.py` | `tests/test_resume.py`, `tests/test_tx_batch_size.py`, `tests/test_scan_memo.py` |
| `FR-001-14` | Результат MUST містити метадані: адресу токена, час аналізу, кількість покупців у вибірці, використані значенн | `T-002`, `T-003`, `T-014`, `T-020` | `src/unmask/ingest/serialize.py`, `src/unmask/ingest/collector.py`, `src/unmask/ingest/model.py`, `src/unmask/ingest/config.py` | `tests/test_config.py`, `tests/test_collector.py`, `tests/test_serialize_contract.py` |
| `FR-001-15` | Доступ до зовнішнього джерела даних MUST бути прихований за інтерфейсом, що дозволяє підставити записані фікст | `T-001`, `T-004`, `T-005`, `T-021`, `T-049`, `T-053` | `src/unmask/ingest/rpc/fixture.py`, `src/unmask/ingest/rpc/protocol.py`, `src/unmask/ingest/rpc/http.py` | `tests/test_rpc_http.py`, `tests/test_fixture_builder.py`, `tests/test_rpc_fixture.py`, `tests/test_no_network.py` |
| `FR-001-16` | Збір MUST вкладатися в налаштований бюджет часу (за замовчуванням 40 с із 60 с на весь холодний запит); при ви | `T-002`, `T-004`, `T-014`, `T-015`, `T-051`, `T-052`, `T-053` | `src/unmask/ingest/collector.py`, `src/unmask/ingest/funding.py`, `src/unmask/ingest/buyers.py`, `src/unmask/ingest/budget.py`, `src/unmask/ingest/config.py`, `src/unmask/ingest/rpc/protocol.py`, `src/unmask/ingest/rpc/http.py` | `tests/test_rpc_http.py`, `tests/test_tx_batch_size.py`, `tests/test_config.py`, `tests/test_budget.py` |

## 002-funding-graph-hub-pruning

| Вимога | Опис | Задачі | Імплементація | Тести |
|---|---|---|---|---|
| `FR-002-01` | Система MUST будувати орієнтований граф з результату збору: вершини — гаманці (покупці й джерела фінансування) | `T-028` | `src/unmask/graph/build.py` | `tests/test_graph_build.py` |
| `FR-002-02` | Кожне ребро MUST нести суму, кількість переказів, час першого й останнього переказу та перелік первинних посил | `T-025`, `T-028` | `src/unmask/graph/build.py`, `src/unmask/graph/model.py` | `tests/test_graph_build.py`, `tests/test_graph_model.py` |
| `FR-002-03` | Кожна вершина MUST нести ролі (покупець, джерело) і мінімальну глибину від покупців; для покупців — порядковий | `T-025`, `T-028`, `T-029` | `src/unmask/graph/build.py`, `src/unmask/graph/model.py` | `tests/test_graph_model.py`, `tests/test_graph_build_nodes.py` |
| `FR-002-04` | Побудова графа MUST бути детермінованою: той самий вхід дає побітово той самий граф і порядок. | `T-028`, `T-031`, `T-039`, `T-040` | `src/unmask/graph/serialize.py`, `src/unmask/graph/service.py`, `src/unmask/graph/build.py` | `tests/test_graph_service.py`, `tests/test_graph_determinism.py`, `tests/test_graph_serialize_contract.py` |
| `FR-002-05` | Граф MUST успадковувати статус повноти результату збору й перелік причин неповноти; граф над неповним збором M | `T-025`, `T-028`, `T-030` | `src/unmask/graph/build.py`, `src/unmask/graph/model.py` | `tests/test_graph_model.py`, `tests/test_graph_completeness.py` |
| `FR-002-06` | Граф MUST зберігати нерозгорнуті вершини з результату збору з причиною (висока зв'язність, ліміт підписів) як  | `T-025`, `T-028`, `T-030` | `src/unmask/graph/build.py`, `src/unmask/graph/model.py` | `tests/test_graph_model.py`, `tests/test_graph_completeness.py` |
| `FR-002-07` | Система MUST позначати вершину хабом за кожним із незалежних критеріїв: (а) належність до відомих списків адре | `T-032`, `T-033`, `T-034`, `T-035`, `T-055`, `T-057`, `T-058` | `src/unmask/graph/measures.py`, `src/unmask/hubs/prune.py`, `src/unmask/hubs/criteria.py` | `tests/test_hubs_dust_fanout.py`, `tests/test_graph_calibration_real.py`, `tests/test_hubs_criteria_lists.py`, `tests/test_hubs_measures.py`, `tests/test_hubs_threshold_rule.py` |
| `FR-002-08` | Пороги, списки адрес і версія MUST зберігатися у версіонованому YAML із журналом змін; зміна без запису в журн | `T-023`, `T-024`, `T-054`, `T-058` | `src/unmask/hubs/config.py` | `tests/test_hubs_config_robustness.py`, `tests/test_hubs_changelog_guard.py`, `tests/test_graph_calibration_real.py`, `tests/test_hubs_config.py` |
| `FR-002-09` | Відсікання MUST виключати хаб і всі ребра, інцидентні йому, з графа для кластеризації й MUST зберігати їх у ок | `T-035`, `T-038` | `src/unmask/hubs/prune.py`, `src/unmask/hubs/report.py` | `tests/test_hubs_edge_cases.py`, `tests/test_hubs_prune.py` |
| `FR-002-10` | Покупці MUST NOT відсікатися; покупець, що відповідає критерію хаба, отримує пояснювальну позначку. | `T-035` | `src/unmask/hubs/prune.py` | `tests/test_hubs_prune.py` |
| `FR-002-11` | Система MUST формувати звіт ефекту відсікання: кількість вершин і ребер, кількість компонент, частка найбільшо | `T-027`, `T-036`, `T-038`, `T-042` | `src/unmask/graph/components.py`, `src/unmask/hubs/report.py` | `tests/test_hubs_edge_cases.py`, `tests/test_graph_components.py`, `tests/test_graph_performance.py`, `tests/test_hubs_report.py` |
| `FR-002-12` | Якщо список відомих адрес не застосовано (порожній чи недоступний), результат MUST це явно показувати; відсутн | `T-023`, `T-035`, `T-036`, `T-037` | `src/unmask/hubs/prune.py`, `src/unmask/hubs/report.py`, `src/unmask/hubs/config.py` | `tests/test_hubs_config_robustness.py`, `tests/test_hubs_lists_unavailable.py` |
| `FR-002-13` | Результат MUST містити версію конфігу відсікання й версію списків адрес, щоб два результати з різними версіями | `T-023`, `T-039`, `T-040`, `T-054`, `T-055`, `T-058` | `src/unmask/graph/serialize.py`, `src/unmask/graph/service.py`, `src/unmask/hubs/config.py` | `tests/test_graph_service.py`, `tests/test_graph_serialize_contract.py`, `tests/test_hubs_config.py` |
| `FR-002-14` | Правило порогу (рівно поріг) MUST бути визначене однозначно й однаково для всіх критеріїв. | `T-023`, `T-033`, `T-057` | `src/unmask/hubs/criteria.py`, `src/unmask/hubs/config.py` | `tests/test_hubs_dust_fanout.py`, `tests/test_hubs_config.py`, `tests/test_hubs_threshold_rule.py` |
| `FR-002-15` | Система MUST розпізнавати в транзакціях mint «делеговану купівлю»: платник витратив кошти й не отримав токен,  | `T-043`, `T-045` | `src/unmask/ingest/delegated.py` | `tests/test_delegated_fixtures.py`, `tests/test_delegated_rule.py` |
| `FR-002-16` | Неоднозначні випадки (кілька платників чи отримувачів без однозначної відповідності) MUST фіксуватися як «канд | `T-043`, `T-045` | `src/unmask/ingest/delegated.py` | `tests/test_delegated_fixtures.py`, `tests/test_delegated_rule.py` |
| `FR-002-17` | Розпізнавання swap-and-send MUST NOT змінювати склад, порядок і порядкові номери перших N покупців із фічі 001 | `T-046` | `src/unmask/ingest/buyers.py`, `src/unmask/ingest/cache.py` | `tests/test_delegated_wiring.py` |
| `FR-002-18` | Зв'язки «делегована купівля» MUST потрапляти в граф як окремий вид ребра з первинним посиланням; ребра цього в | `T-025`, `T-028`, `T-047` | `src/unmask/graph/build.py`, `src/unmask/graph/model.py` | `tests/test_graph_build.py`, `tests/test_graph_model.py`, `tests/test_graph_delegated_edges.py` |
| `FR-002-19` | Якщо транзакції не вдалося розібрати для цього аналізу, результат MUST це позначати як неповний щодо swap-and- | `T-039`, `T-044`, `T-045`, `T-046`, `T-048` | `src/unmask/graph/service.py`, `src/unmask/ingest/parse.py`, `src/unmask/ingest/collector.py`, `src/unmask/ingest/service.py`, `src/unmask/ingest/delegated.py`, `src/unmask/ingest/model.py` | `tests/test_delegated_wiring.py`, `tests/test_graph_service.py`, `tests/test_ingest_delegated_model.py`, `tests/test_graph_delegated_contract.py` |
| `FR-002-20` | Усі зовнішні дані MUST надходити лише з результату збору (фіча 001); фіча не звертається до мережі напряму, те | `T-026`, `T-039`, `T-041`, `T-056` | `src/unmask/graph/service.py`, `src/unmask/ingest/serialize.py` | `tests/test_ingest_from_dict.py`, `tests/test_graph_integration_001.py`, `tests/test_graph_service.py` |
| `FR-002-21` | Результат MUST серіалізуватися у JSON за контрактом, версіонованим окремо від контракту фічі 001; зміни контра | `T-039`, `T-040`, `T-044`, `T-048` | `src/unmask/graph/serialize.py`, `src/unmask/graph/service.py`, `src/unmask/ingest/serialize.py`, `src/unmask/ingest/model.py` | `tests/test_graph_serialize_contract.py`, `tests/test_ingest_delegated_model.py`, `tests/test_graph_delegated_contract.py` |
| `FR-002-22` | Система MUST позначати вершину хабом за п'ятим незалежним критерієм «пилове роздавання» (`dust_fanout`): верши | `T-032`, `T-033`, `T-035`, `T-040`, `T-054`, `T-055`, `T-056`, `T-057` | `src/unmask/graph/measures.py`, `src/unmask/graph/model.py`, `src/unmask/hubs/criteria.py`, `src/unmask/hubs/config.py` | `tests/test_hubs_dust_fanout.py`, `tests/test_graph_model.py`, `tests/test_graph_serialize_contract.py`, `tests/test_hubs_config.py`, `tests/test_hubs_measures.py` |

## 003-wallet-clusters-risk

| Вимога | Опис | Задачі | Імплементація | Тести |
|---|---|---|---|---|
| `FR-003-01` | Система MUST будувати кластери ранніх покупців із графа фічі 002: гаманці, між якими є зв'язок за фінансування | `T-066`, `T-067`, `T-086` | `src/unmask/clusters/cluster.py`, `src/unmask/clusters/links.py` | `tests/test_clusters_performance.py`, `tests/test_clusters_links.py`, `tests/test_clusters_union.py` |
| `FR-003-02` | Прохід 1 MUST об'єднувати покупців, яких одна й та сама вершина графа 002 (джерело, що не є відсіченим хабом;  | `T-063`, `T-065`, `T-066`, `T-080`, `T-081` | `src/unmask/clusters/links.py` | `tests/test_clusters_recovered.py`, `tests/test_clusters_window.py`, `tests/test_clusters_links.py` |
| `FR-003-03` | Прямий переказ між покупцями і зв'язок «делегована купівля» (фіча 002, ребро `delegated_buy`) MUST бути окреми | `T-064`, `T-066` | `src/unmask/clusters/links.py` | `tests/test_clusters_links.py` |
| `FR-003-04` | Прохід 2 MUST знаходити непрямі зв'язки через спільне джерело на більшій глибині графа й додавати їх окремим т | `T-059`, `T-077`, `T-078` | `src/unmask/clusters/indirect.py`, `src/unmask/clusters/config.py` | `tests/test_clusters_indirect.py`, `tests/test_clusters_config.py` |
| `FR-003-05` | Перекази нижче мінімальної суми зв'язку MUST NOT утворювати кластер і MUST NOT бути доказом; поріг MUST зберіг | `T-064`, `T-066`, `T-078` | `src/unmask/clusters/indirect.py`, `src/unmask/clusters/links.py` | `tests/test_clusters_links.py` |
| `FR-003-06` | Вершини, відсічені фічею 002, MUST NOT з'єднувати покупців, за винятком US6 (відновлені ребра з сумою не нижче | `T-064`, `T-066`, `T-077`, `T-078`, `T-080`, `T-081`, `T-082` | `src/unmask/clusters/indirect.py`, `src/unmask/clusters/links.py` | `tests/test_clusters_recovered.py`, `tests/test_clusters_indirect.py`, `tests/test_clusters_links.py`, `tests/test_clusters_hub_fixtures.py` |
| `FR-003-07` | Кожен кластер MUST містити непорожній перелік доказів; кожен доказ MUST мати тип, перелік первинних посилань ( | `T-061`, `T-066`, `T-071` | `src/unmask/clusters/model.py` | `tests/test_clusters_service.py`, `tests/test_clusters_model.py` |
| `FR-003-08` | Система MUST розпізнавати поведінкові докази: «однакові суми» (покупці зі збігом суми фінансування чи першої к | `T-071`, `T-075`, `T-076` | `src/unmask/clusters/behavior.py` | `tests/test_clusters_behavior.py` |
| `FR-003-09` | Поведінкові докази MUST підвищувати впевненість кластера, але MUST NOT самостійно утворювати кластер; поведінк | `T-061`, `T-071`, `T-075`, `T-076` | `src/unmask/clusters/behavior.py`, `src/unmask/clusters/model.py` | `tests/test_clusters_model.py`, `tests/test_clusters_behavior.py` |
| `FR-003-10` | Збіг, нижчий за поріг «природного збігу» (кілька купівель в одному слоті — норма на жвавому запуску), MUST NOT | `T-059`, `T-071`, `T-075`, `T-076` | `src/unmask/clusters/behavior.py`, `src/unmask/clusters/config.py` | `tests/test_clusters_config.py`, `tests/test_clusters_behavior.py` |
| `FR-003-11` | Кожен кластер MUST мати частку: частку токенів, куплених його покупцями, серед токенів, куплених усіма проанал | `T-061`, `T-062`, `T-063`, `T-068` | `src/unmask/clusters/score.py`, `src/unmask/clusters/model.py` | `tests/test_clusters_model.py`, `tests/test_clusters_score.py` |
| `FR-003-12` | Кластери MUST бути відсортовані за часткою спадно з детермінованим тай-брейком; ідентифікатор кластера MUST бу | `T-061`, `T-067`, `T-084` | `src/unmask/clusters/cluster.py`, `src/unmask/clusters/model.py` | `tests/test_clusters_model.py`, `tests/test_clusters_determinism.py`, `tests/test_clusters_union.py` |
| `FR-003-13` | Система MUST обчислювати `risk_score` 0–100 на рівні токена за формулою з контракту з часток і впевненостей кл | `T-059`, `T-063`, `T-068`, `T-069` | `src/unmask/clusters/score.py`, `src/unmask/clusters/config.py` | `tests/test_clusters_completeness.py`, `tests/test_clusters_config.py`, `tests/test_clusters_score.py` |
| `FR-003-14` | Результат MUST супроводжувати `risk_score` переліком кластерів і їхніх доказів, на яких число побудоване; числ | `T-061`, `T-062`, `T-068`, `T-069` | `src/unmask/clusters/score.py`, `src/unmask/clusters/model.py` | `tests/test_clusters_model.py`, `tests/test_clusters_completeness.py`, `tests/test_clusters_score.py` |
| `FR-003-15` | Результат MUST успадковувати повноту графа 002 (статус, причини, попередження `giant_component`, `delegated_in | `T-061`, `T-062`, `T-068`, `T-069` | `src/unmask/clusters/score.py`, `src/unmask/clusters/model.py` | `tests/test_clusters_model.py`, `tests/test_clusters_completeness.py` |
| `FR-003-16` | Порожній вхід (0 покупців) MUST давати явний результат «немає даних», а не «чисто». | `T-061`, `T-062`, `T-068`, `T-069`, `T-071` | `src/unmask/clusters/service.py`, `src/unmask/clusters/score.py` | `tests/test_clusters_service.py`, `tests/test_clusters_completeness.py` |
| `FR-003-17` | Кластер, що охоплює понад поріг частки покупців при попередженні `giant_component` або при переважно непрямих  | `T-068`, `T-077`, `T-079` | `src/unmask/clusters/score.py` | `tests/test_clusters_score.py` |
| `FR-003-18` | Усі пороги (вікно, мінімальна сума зв'язку, пороги поведінкових доказів, ваги впевненості, межі смуг, поріг ар | `T-059`, `T-060`, `T-070`, `T-071`, `T-085`, `T-087` | `src/unmask/clusters/serialize.py`, `src/unmask/clusters/service.py`, `src/unmask/clusters/config.py` | `tests/test_clusters_config.py`, `tests/test_clusters_changelog_guard.py`, `tests/test_clusters_serialize_contract.py` |
| `FR-003-19` | Усі зовнішні дані MUST надходити лише з результату фічі 002 і результату збору 001; фіча не звертається до мер | `T-063`, `T-064`, `T-071`, `T-075`, `T-077`, `T-080` | `src/unmask/clusters/service.py` | `tests/test_clusters_service.py`, `tests/test_clusters_fixture_builder.py` |
| `FR-003-20` | Результат MUST серіалізуватися у JSON за контрактом, версіонованим окремо від контрактів 001 і 002; серіалізац | `T-070`, `T-084`, `T-085` | `src/unmask/clusters/serialize.py` | `tests/test_clusters_determinism.py`, `tests/test_clusters_serialize_contract.py` |
| `FR-003-21` | Модуль кластеризації MUST NOT змінювати граф чи відсікання 002 і MUST залежати лише від публічного результату  | `T-071`, `T-083` | `src/unmask/clusters/service.py` | `tests/test_clusters_service.py`, `tests/test_clusters_boundaries.py` |
| `FR-003-22` | Система MUST надавати оцінювальну команду, що на наборі збережених результатів збору без мережі виводить для к | `T-073`, `T-074`, `T-087` | `src/unmask/clusters/evaluate.py` | `tests/test_clusters_evaluate.py` |
| `FR-003-23` | Набір збережених результатів збору для оцінювання MUST зберігатися в репозиторії як фікстури (публічні ончейн- | `T-072`, `T-073`, `T-074`, `T-087` | `src/unmask/clusters/evaluate.py` | `tests/test_clusters_evaluate.py`, `tests/test_clusters_real_fixtures.py` |

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
- `T-051` (001-onchain-data-ingest) — виконано
- `T-052` (001-onchain-data-ingest) — виконано
- `T-053` (001-onchain-data-ingest) — виконано
- `T-024` (002-funding-graph-hub-pruning) — виконано
- `T-025` (002-funding-graph-hub-pruning) — виконано
- `T-054` (002-funding-graph-hub-pruning) — виконано
- `T-055` (002-funding-graph-hub-pruning) — виконано
- `T-027` (002-funding-graph-hub-pruning) — виконано
- `T-028` (002-funding-graph-hub-pruning) — виконано
- `T-030` (002-funding-graph-hub-pruning) — виконано
- `T-032` (002-funding-graph-hub-pruning) — виконано
- `T-033` (002-funding-graph-hub-pruning) — виконано
- `T-057` (002-funding-graph-hub-pruning) — виконано
- `T-034` (002-funding-graph-hub-pruning) — виконано
- `T-035` (002-funding-graph-hub-pruning) — виконано
- `T-036` (002-funding-graph-hub-pruning) — виконано
- `T-039` (002-funding-graph-hub-pruning) — виконано
- `T-044` (002-funding-graph-hub-pruning) — виконано
- `T-045` (002-funding-graph-hub-pruning) — виконано
- `T-046` (002-funding-graph-hub-pruning) — виконано
- `T-047` (002-funding-graph-hub-pruning) — виконано
- `T-058` (002-funding-graph-hub-pruning) — виконано
- `T-059` (003-wallet-clusters-risk) — виконано
- `T-061` (003-wallet-clusters-risk) — виконано
- `T-062` (003-wallet-clusters-risk) — виконано
- `T-063` (003-wallet-clusters-risk) — виконано
- `T-065` (003-wallet-clusters-risk) — виконано
- `T-066` (003-wallet-clusters-risk) — виконано
- `T-067` (003-wallet-clusters-risk) — виконано
- `T-068` (003-wallet-clusters-risk) — виконано
- `T-069` (003-wallet-clusters-risk) — виконано
- `T-071` (003-wallet-clusters-risk) — виконано
- `T-073` (003-wallet-clusters-risk) — виконано
- `T-074` (003-wallet-clusters-risk) — виконано
- `T-075` (003-wallet-clusters-risk) — виконано
- `T-076` (003-wallet-clusters-risk) — виконано
- `T-077` (003-wallet-clusters-risk) — виконано
- `T-078` (003-wallet-clusters-risk) — виконано
- `T-079` (003-wallet-clusters-risk) — виконано
- `T-080` (003-wallet-clusters-risk) — виконано
- `T-081` (003-wallet-clusters-risk) — виконано
- `T-084` (003-wallet-clusters-risk) — виконано
- `T-087` (003-wallet-clusters-risk) — виконано
