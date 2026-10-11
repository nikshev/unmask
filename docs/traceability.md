# Матриця трасування

<!-- ГЕНЕРУЄТЬСЯ scripts/trace.py — не редагувати вручну -->

Вимог: **116** · задач: **150** (виконано 148) · вимог у роботі: **4** · порушень: **0**

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
| `FR-001-14` | Результат MUST містити метадані: адресу токена, час аналізу, кількість покупців у вибірці, використані значенн | `T-002`, `T-003`, `T-014`, `T-020` | `src/unmask/ingest/serialize.py`, `src/unmask/ingest/collector.py`, `src/unmask/ingest/model.py`, `src/unmask/ingest/config.py` | `tests/test_config.py`, `tests/test_ingest_profile_v5.py`, `tests/test_collector.py`, `tests/test_serialize_contract.py` |
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

## 004-api-bot-delivery

| Вимога | Опис | Задачі | Імплементація | Тести |
|---|---|---|---|---|
| `FR-004-01` | Система MUST надавати `GET /api/token/{mint}`, що повертає `mint`, `analyzed_at`, `wallets_analyzed`, `cluster | `T-089`, `T-092` | `src/unmask/delivery/http.py`, `src/unmask/delivery/report.py` | `tests/test_delivery_http.py`, `tests/test_delivery_report.py` |
| `FR-004-02` | Запит MUST проганяти живий конвеєр 001→002→003 тими самими версіями конфігів і порогів, що й бібліотека; жодни | `T-090` | `src/unmask/delivery/service.py` | `tests/test_delivery_service.py` |
| `FR-004-03` | Telegram-бот MUST відповідати на `/check <mint>` одним повідомленням: смуга й число ризику, частки найбільших  | `T-094`, `T-100` | `src/unmask/delivery/main.py`, `src/unmask/delivery/bot.py` | `tests/test_delivery_bot_commands.py`, `tests/test_delivery_bot.py` |
| `FR-004-04` | Кнопка «докази» MUST показувати перелік доказів по кластерах, на яких побудовано число (тип, джерело, вікно);  | `T-094` | `src/unmask/delivery/bot.py` | `tests/test_delivery_bot.py` |
| `FR-004-05` | PNG MUST рендеритися на сервері зі результату аналізу (статичне зображення, без інтерактивності); кластери MUS | `T-093`, `T-099` | `src/unmask/delivery/render.py` | `tests/test_delivery_render_graph.py`, `tests/test_delivery_render.py` |
| `FR-004-06` | Результат MUST кешуватися за адресою токена: повторний запит повертає збережений документ без повторного збору | `T-090`, `T-091`, `T-098` | `src/unmask/delivery/service.py`, `src/unmask/delivery/cache.py` | `tests/test_delivery_cache.py`, `tests/test_delivery_perf.py` |
| `FR-004-07` | Невалідний або неіснуючий `mint` MUST давати явну помилку (API — статус 4xx з поясненням; бот — зрозуміле пові | `T-090`, `T-092`, `T-094` | `src/unmask/delivery/service.py`, `src/unmask/delivery/http.py`, `src/unmask/delivery/bot.py` | `tests/test_delivery_http.py`, `tests/test_delivery_service.py` |
| `FR-004-08` | API і бот MUST працювати без авторизації (PRD: «жодної авторизації»). | `T-092` | `src/unmask/delivery/http.py` | `tests/test_delivery_http.py` |
| `FR-004-09` | Тести MUST працювати без мережі: живий RPC і Telegram — лише записані фікстури/несправжній транспорт (принцип  | `T-096` | `src/unmask/delivery/bot.py` | `tests/test_delivery_e2e.py` |
| `FR-004-10` | Секрети (токен бота, RPC-ключ) MUST надходити лише з оточення й MUST NOT потрапляти в репозиторій. | `T-095` | `src/unmask/delivery/main.py` | `tests/test_delivery_main.py` |
| `FR-004-11` | Жодна нова константа API/бота, що змінює кластери, впевненість, `risk_score` чи смугу, MUST NOT з'являтися поз | `T-088`, `T-093`, `T-097` | `src/unmask/delivery/config.py` | `tests/test_delivery_config.py`, `tests/test_delivery_boundaries.py` |
| `FR-004-12` | Відповідь MUST нести походження висновку: версії конфігів 001/002/003, `analyzed_at`, статус повноти з причина | `T-089` | `src/unmask/delivery/report.py` | `tests/test_delivery_report.py` |

## 005-benchmark-funding-coordination

| Вимога | Опис | Задачі | Імплементація | Тести |
|---|---|---|---|---|
| `FR-005-01` | Система MUST надавати офлайн бенчмарк-команду, що приймає ground truth JSONL і фікстури/живий збір. | `T-112`, `T-113`, `T-116`, `T-120` | `src/unmask/benchmark/runner.py`, `src/unmask/benchmark/cli.py` | `tests/test_benchmark_runner.py`, `tests/test_benchmark_e2e.py`, `tests/test_benchmark_cli.py` |
| `FR-005-02` | Ground truth формат MUST бути версіонованим (`version: 1`), з полями `mint`, `class` (insider|clean), `source` | `T-111`, `T-121` | `src/unmask/benchmark/ground_truth.py` | `tests/test_benchmark_ground_truth.py` |
| `FR-005-03` | Бенчмарк MUST виводити confusion matrix, precision/recall/F1 по кластерах, calibration curve, PR-AUC, ROC-AUC, | `T-114`, `T-117`, `T-120` | `src/unmask/benchmark/metrics.py`, `src/unmask/benchmark/report.py` | `tests/test_benchmark_report.py`, `tests/test_benchmark_e2e.py`, `tests/test_benchmark_metrics.py` |
| `FR-005-04` | Калібрування порогів MUST базуватися на ground truth метриках, а не на MELT; нові пороги MUST записуватися у в | `T-115`, `T-118`, `T-120` | `src/unmask/benchmark/calibrate.py` | `tests/test_benchmark_e2e.py`, `tests/test_benchmark_calibrate.py` |
| `FR-005-05` | Бенчмарк MUST підтримувати `--live` режим (живий збір нових токенів) і `--fixtures` режим (офлайн на записаних | `T-113` | `src/unmask/benchmark/runner.py` | `tests/test_benchmark_runner.py`, `tests/test_benchmark_e2e.py` |
| `FR-005-06` | Всі константи, що впливають на метрики, MUST бути в YAML (clusters.yaml v2), не в коді (принцип III). | `T-119`, `T-120` | `src/unmask/benchmark/calibrate.py` | `tests/test_benchmark_boundaries.py` |

## 006-live-cohort-experiment

| Вимога | Опис | Задачі | Імплементація | Тести |
|---|---|---|---|---|
| `FR-006-01` | Cohort lists MUST be versioned JSONL (`version: 1`) with fields | `T-122`, `T-126` | `scripts/cohort_check.py` | `tests/test_cohort_lists.py` |
| `FR-006-02` | Screening MUST use the frozen fixture profile (N=30, depth=2, | `T-126`, `T-127` | `scripts/screen_worker.py` | `tests/test_cohort_lists.py` |
| `FR-006-03` | Selection MUST be top-30 by `risk_score` desc, tie-breaks | `T-124`, `T-128` | `scripts/cohort_select.py` | `tests/test_cohort_lists.py` |
| `FR-006-04` | Control cohort MUST be exactly the 30 committed liquid mints; | `T-123` | `scripts/cohort_check.py` | `tests/test_cohort_lists.py` |
| `FR-006-05` | Committed artifacts per token MUST be limited to: mint, rank, | `T-127`, `T-128` | `scripts/screen_worker.py` | `tests/test_cohort_lists.py` |
| `FR-006-06` | Comparison MUST report preregistered expectations vs outcome | `T-125`, `T-129` | `scripts/cohort_compare.py` | `tests/test_cohort_lists.py` |
| `FR-006-07` | README MUST gain a section with the cohort table + caveats, | `T-125`, `T-129` | `scripts/cohort_compare.py` | `tests/test_cohort_lists.py` |
| `FR-006-08` | Live steps MUST be manual gate tasks (`--live` runner), never | `T-130` | `scripts/cohort_check.py` | `tests/test_cohort_lists.py` |

## 007-rpc-provider-pool

| Вимога | Опис | Задачі | Імплементація | Тести |
|---|---|---|---|---|
| `FR-007-01` | Система MUST надавати джерело RPC, що реалізує той самий публічний протокол джерела фічі 001, але розподіляє з | `T-133`, `T-134`, `T-142` | `src/unmask/ingest/rpc/pool.py` | `tests/test_rpc_pool_boundaries.py`, `tests/test_rpc_pool.py`, `tests/test_rpc_pool_equivalence.py` |
| `FR-007-02` | Пакетні запити (транзакції за підписами) MUST ділитися між провайдерами й виконуватися одночасно; відповіді MU | `T-132`, `T-144` | `src/unmask/ingest/rpc/pool.py` | `tests/test_rpc_pool_perf.py`, `tests/test_rpc_pool.py` |
| `FR-007-03` | Розподіл MUST враховувати пропускну здатність кожного провайдера (профіль швидкості фічі 001), а не ділити пор | `T-132`, `T-138` | `src/unmask/ingest/rpc/pool.py`, `src/unmask/ingest/rpc/pool_config.py` | `tests/test_rpc_pool_config.py`, `tests/test_rpc_pool.py` |
| `FR-007-04` | Результат збору через пул MUST бути побітово рівним результату збору через одного провайдера на тих самих дани | `T-132`, `T-133`, `T-134`, `T-136`, `T-145` | `src/unmask/ingest/rpc/pool.py` | `tests/test_rpc_pool_routing.py`, `tests/test_rpc_pool.py`, `tests/test_rpc_pool_equivalence.py` |
| `FR-007-05` | Інваріанти фічі 001 (`resume` дорівнює свіжому збору, ліміти й бюджети, класифікація помилок) MUST зберігатися | `T-134`, `T-140` | `src/unmask/ingest/rpc/pool.py` | `tests/test_rpc_pool_failover.py`, `tests/test_rpc_pool_equivalence.py` |
| `FR-007-06` | Кожному провайдеру MUST задаватися ознака архівності й, для неархівних, глибина історії; запит, що виходить за | `T-135`, `T-136` | `src/unmask/ingest/rpc/pool.py` | `tests/test_rpc_pool_routing.py`, `tests/test_rpc_pool_equivalence.py` |
| `FR-007-07` | Різні провайдери повертають розбіжні дані для одного підпису (форк, різний `commitment`): результат визначаєть | `T-135`, `T-136` | `src/unmask/ingest/rpc/pool.py` | `tests/test_rpc_pool_routing.py`, `tests/test_rpc_pool_equivalence.py` |
| `FR-007-08` | Відмова провайдера (перевищення ліміту, таймаут, недоступність, пошкоджена відповідь) MUST переводити запит на | `T-140` | `src/unmask/ingest/rpc/pool.py` | `tests/test_rpc_pool_failover.py` |
| `FR-007-09` | Перелік провайдерів, профілі швидкості, архівність і глибина історії MUST зберігатися у версіонованому YAML із | `T-137` | `src/unmask/ingest/rpc/pool_config.py` | `tests/test_rpc_pool_config.py` |
| `FR-007-10` | Без конфігу пулу (або з єдиним провайдером) система MUST поводитись як до цієї фічі; змінна `UNMASK_RPC_URL` л | `T-138` | `src/unmask/ingest/rpc/pool_config.py` | `tests/test_rpc_pool_config.py` |
| `FR-007-11` | Недоступну (не задану в середовищі) змінну провайдера MUST бути пропущено з попередженням без значень; якщо пр | `T-138` | `src/unmask/ingest/rpc/pool_config.py` | `tests/test_rpc_pool_config.py` |
| `FR-007-12` | URL і ключі провайдерів MUST NOT потрапляти в повідомлення про помилки, логи, `repr`, результати й кеші (діє ч | `T-138`, `T-140`, `T-141` | `src/unmask/ingest/rpc/pool.py`, `src/unmask/ingest/rpc/pool_config.py` | `tests/test_rpc_pool_stats.py`, `tests/test_rpc_pool_config.py`, `tests/test_rpc_pool_failover.py` |
| `FR-007-13` | Пул MUST надавати оператору лічильники запитів за провайдерами (без URL і ключів), щоб було видно, що кожен пр | `T-141`, `T-145`, `T-146` | `src/unmask/delivery/main.py`, `src/unmask/ingest/rpc/pool.py` | `tests/test_rpc_pool_stats.py`, `tests/test_delivery_main_stats.py` |
| `FR-007-14` | Сервіс доставки й воркер експерименту MUST створювати джерело через пул за конфігом; чинні тести доставки лиша | `T-139`, `T-143`, `T-145` | `scripts/screen_worker.py`, `src/unmask/delivery/main.py` | `tests/test_screen_worker_source.py`, `tests/test_delivery_main_pool.py`, `tests/test_ingest_profile_v5.py` |
| `FR-007-15` | Тести MUST працювати без мережі на фікстурних провайдерах із керованими затримками й відмовами (принцип II). | `T-131`, `T-144` | `tests/rpc_pool_doubles.py` | `tests/test_rpc_pool_perf.py`, `tests/test_rpc_pool_doubles.py` |

## 008-api-protection-ops

| Вимога | Опис | Задачі | Імплементація | Тести |
|---|---|---|---|---|
| `FR-008-01` | Сервіс MUST обмежувати кількість запитів `GET /api/token/{mint}` з однієї адреси клієнта за ковзне вікно часу; | `T-162`, `T-166` | `src/unmask/delivery/limits.py`, `src/unmask/delivery/http.py` | `tests/test_delivery_http_protection.py`, `tests/test_delivery_limits.py` |
| `FR-008-02` | Сервіс MUST обмежувати кількість одночасних незакешованих аналізів; перевищення — `503` з машинозчитним тілом  | `T-162`, `T-163`, `T-164`, `T-173` | `src/unmask/delivery/service.py`, `src/unmask/delivery/limits.py` | `tests/test_delivery_protection_e2e.py`, `tests/test_delivery_limits.py`, `tests/test_delivery_service_slots.py` |
| `FR-008-03` | `/healthz` MUST бути поза обмеженнями. | `T-166` | `src/unmask/delivery/http.py` | `tests/test_delivery_http_protection.py` |
| `FR-008-04` | Система MUST NOT запроваджувати автентифікацію чи авторизацію (чинна вимога фічі 004 про відсутність авторизац | `T-166` | `src/unmask/delivery/http.py` | `tests/test_delivery_http_protection.py` |
| `FR-008-05` | Адреса клієнта за зворотним проксі MUST визначатися з довіреного заголовка лише коли з'єднання від налаштовано | `T-165` | `src/unmask/delivery/http.py` | `tests/test_delivery_client_ip.py` |
| `FR-008-06` | Слот важкого аналізу MUST звільнятися за будь-якого завершення (успіх, помилка, таймаут бюджету, виняток). | `T-162`, `T-163`, `T-164` | `src/unmask/delivery/service.py`, `src/unmask/delivery/limits.py` | `tests/test_delivery_limits.py`, `tests/test_delivery_service_slots.py` |
| `FR-008-07` | Для бота MUST діяти обмеження на користувача (кількість важких аналізів за вікно та один одночасний аналіз) і  | `T-162`, `T-168`, `T-169`, `T-174` | `src/unmask/delivery/limits.py`, `src/unmask/delivery/main.py`, `src/unmask/delivery/bot.py` | `tests/test_delivery_main.py`, `tests/test_delivery_limits.py`, `tests/test_delivery_bot_limits.py` |
| `FR-008-08` | Усі межі (запитів на вікно, довжина вікна, одночасних аналізів, значення `Retry-After`, довірені адреси проксі | `T-161`, `T-167` | `config/protection.yaml`, `src/unmask/delivery/limits.py`, `src/unmask/delivery/main.py`, `src/unmask/delivery/config.py` | `tests/test_delivery_main.py`, `tests/test_delivery_limits.py`, `tests/test_protection_config.py` |
| `FR-008-09` | Відповіді про перевантаження MUST бути чесними: `429`/`503` ніколи не маскуються під результат аналізу («чисто | `T-164`, `T-166`, `T-169`, `T-173` | `src/unmask/delivery/service.py`, `src/unmask/delivery/http.py`, `src/unmask/delivery/bot.py` | `tests/test_delivery_http_protection.py`, `tests/test_delivery_protection_e2e.py`, `tests/test_delivery_bot_limits.py`, `tests/test_delivery_service_slots.py` |
| `FR-008-10` | Контракт відповіді API MUST описувати нові види помилок (`rate_limited`, `busy`) і заголовок `Retry-After`; чи | `T-166` | `src/unmask/delivery/http.py` | `tests/test_delivery_http_protection.py` |
| `FR-008-11` | Репозиторій MUST містити юніт автозапуску з перезапуском при збої й змінними середовища з файлу оператора (без | `T-170` | `deploy/smoke.sh` | `tests/test_deploy_artifacts.py` |
| `FR-008-12` | Репозиторій MUST містити приклад конфігурації зворотного проксі з TLS, що передає адресу клієнта довіреним заг | `T-170`, `T-171` | `deploy/smoke.sh` | `tests/test_nginx_example.py` |
| `FR-008-13` | README MUST описувати розгортання (запуск, змінні, перезапуск, перевірка `/healthz`, ліміти) без секретів; URL | `T-170`, `T-172` | `deploy/smoke.sh` | `tests/test_deploy_artifacts.py`, `tests/test_ingest_profile_v5.py`, `tests/test_readme_no_vendor_names.py`, `tests/test_readme_deploy.py` |
| `FR-008-14` | Тести MUST працювати без мережі й годинника реального часу (контрольований годинник у тестах лімітів) (принцип | `T-162`, `T-173` | `src/unmask/delivery/limits.py` | `tests/test_delivery_protection_e2e.py`, `tests/test_delivery_limits.py` |

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
- `T-088` (004-api-bot-delivery) — виконано
- `T-089` (004-api-bot-delivery) — виконано
- `T-090` (004-api-bot-delivery) — виконано
- `T-091` (004-api-bot-delivery) — виконано
- `T-093` (004-api-bot-delivery) — виконано
- `T-094` (004-api-bot-delivery) — виконано
- `T-099` (004-api-bot-delivery) — у роботі
- `T-113` (005-benchmark-funding-coordination) — виконано
- `T-115` (005-benchmark-funding-coordination) — виконано
- `T-125` (006-live-cohort-experiment) — виконано
- `T-129` (006-live-cohort-experiment) — виконано
- `T-132` (007-rpc-provider-pool) — виконано
- `T-133` (007-rpc-provider-pool) — виконано
- `T-134` (007-rpc-provider-pool) — виконано
- `T-135` (007-rpc-provider-pool) — виконано
- `T-136` (007-rpc-provider-pool) — виконано
- `T-137` (007-rpc-provider-pool) — виконано
- `T-138` (007-rpc-provider-pool) — виконано
- `T-140` (007-rpc-provider-pool) — виконано
- `T-162` (008-api-protection-ops) — виконано
- `T-163` (008-api-protection-ops) — виконано
- `T-164` (008-api-protection-ops) — виконано
- `T-165` (008-api-protection-ops) — виконано
- `T-166` (008-api-protection-ops) — виконано
- `T-167` (008-api-protection-ops) — виконано
- `T-168` (008-api-protection-ops) — виконано
- `T-169` (008-api-protection-ops) — виконано
- `T-174` (008-api-protection-ops) — виконано
