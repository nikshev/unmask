# Оцінювання кластерів 003

clusters.yaml v2 sha256 f04bbf8c32271c8d8389357ddf478973266b7ea99f8a7e7074b11fb541c65f86; hubs.yaml v3; hub_addresses.yaml v1; ingest_config_version 2; schema 003.1
band_if_complete = computed_band: смуга за повних даних при тому самому risk_score (Q1 (а)); усі 9 результатів зібрані без аналізу делегованих купівель.

| label | class | mint | clusters | largest_share | risk_score | computed_band | band | band_if_complete | status | warnings |
|---|---|---|---|---|---|---|---|---|---|---|
| ins0 | insider | AedQTgnV | 1 | 0.4444 | 32 | suspicious | suspicious | suspicious | incomplete | delegated_incomplete;giant_component |
| ins1 | insider | 3vgBAf4c | 1 | 0.3434 | 23 | suspicious | suspicious | suspicious | incomplete | delegated_incomplete;giant_component |
| ins2 | insider | 9Q6cZ2Jg | 2 | 0.1462 | 13 | clean | insufficient_data | clean | incomplete | delegated_incomplete;giant_component |
| ins3 | insider | Aj9cy2nr | 1 | 0.0694 | 5 | clean | insufficient_data | clean | incomplete | delegated_incomplete;giant_component |
| ins4 | insider | 3JZKYE9R | 1 | 0.7455 | 54 | high_concentration | high_concentration | high_concentration | incomplete | delegated_incomplete |
| cln1 | clean | DwxtQ9nM | 0 | 0.0000 | 0 | clean | insufficient_data | clean | incomplete | delegated_incomplete |
| cln2 | clean | D7wetpmA | 1 | 0.2075 | 10 | clean | insufficient_data | clean | incomplete | delegated_incomplete |
| cln3 | clean | 3p6YXraq | 1 | 0.0611 | 5 | clean | insufficient_data | clean | incomplete | delegated_incomplete |
| cln4 | clean | 6s8sb29m | 3 | 0.2334 | 25 | suspicious | suspicious | suspicious | incomplete | delegated_incomplete |

insider_above_clean 3/5; clean_within_clean 3/4; clean_band_shown 0/4; prd_criterion_met yes

Застереження:
- мітки MELT цінові/модельні, не підтверджене інсайдерство;
- вибірка 9 токенів не статистична; калібрування на тому ж наборі, що й перевірка;
- усі результати без аналізу делегованих купівель → смуга insufficient_data замість clean;
- cln4 неповний на рівні 001 (missing 3, corrupt_data).
