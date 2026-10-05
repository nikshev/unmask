# Quickstart: Кластери пов'язаних гаманців, докази й оцінка ризику токена (003)

Як переконатися, що фіча працює, без мережі. Контракти — `contracts/`, сутності — `data-model.md`, рішення — `research.md`. Команди відповідають структурі `plan.md`; імена тестів з'являться з `tasks.md`.

## Передумови

```bash
cd /opt/unmask
uv sync                                   # залежності без змін відносно 001/002
git config core.hooksPath .githooks       # якщо репозиторій клоновано заново
```

Пакет не встановлюється: `src` підключено для pytest (`pythonpath=["src"]`); для ручних команд — `PYTHONPATH=src` або `uv run python -m …`. Для оцінювання на реальних результатах потрібен злитий T-058 фічі 002 (`config/hubs.yaml` v3): інакше головний фінансист ins1 відсікається і таблиця розходиться з еталоном (research R-12 п. 5).

## 1. Конфігурація версіонована й захищена (FR-003-18, SC-007)

```bash
uv run pytest tests/test_clusters_config.py tests/test_clusters_changelog_guard.py -q
```

Очікувано: зелено; `config/clusters.yaml` має `version: 1`, у `config/CHANGELOG.md` є розділ `# config/clusters.yaml` **між** `# config/hub_addresses.yaml` і `# config/ingest.yaml` (ingest лишається останнім), запис `## 1` закінчується `sha256:` канонічного вмісту. Перевірка вручну: змініть `funding_window_seconds` без зміни `version` і журналу → `test_clusters_changelog_guard.py` червоний (розбіжність sha256); поставте `indirect_link: 0.7` → `ConfigError` з назвою поля. Поверніть значення.

## 2. Фікстури й незалежні еталони (research R-14)

```bash
uv run python tests/fixtures/build_cluster_fixtures.py --check   # 0 — файли на диску збігаються з генератором
uv run python tests/fixtures/build_graph_fixtures.py --check     # 002 без змін
uv run pytest tests/test_clusters_fixture_builder.py tests/test_clusters_real_fixtures.py -q
```

Очікувано: кожен `tests/fixtures/clusters/*/ingest.json` валідний проти схеми 001 (1.1); генератор не імпортує `unmask` (перевірка `ast`); `tests/fixtures/real/manifest.yaml` описує 9 файлів із `sha256`, що збігаються; у файлах немає рядків `http://`, `https://`, `api-key`.

## 3. User Story 1 — кластери за спільним фінансуванням із доказами

```bash
uv run pytest tests/test_clusters_links.py tests/test_clusters_window.py tests/test_clusters_union.py tests/test_clusters_model.py -q
```

Ручна перевірка на `c_shared`:

```bash
PYTHONPATH=src python3 - <<'EOF'
import json
from pathlib import Path
from unmask.hubs.config import load_hub_config
from unmask.graph.service import GraphService
from unmask.clusters.config import load_cluster_config
from unmask.clusters.service import ClusterService
from unmask.clusters.serialize import to_dict
from unmask.ingest.serialize import from_dict
ingest = from_dict(json.loads(Path("tests/fixtures/clusters/c_shared/ingest.json").read_text()))
graph = GraphService(load_hub_config(Path("config/hubs.yaml"), Path("config/hub_addresses.yaml"))).analyze(ingest)
res = ClusterService(load_cluster_config(Path("config/clusters.yaml"))).analyze(graph, ingest)
d = to_dict(res)
print("risk_score:", d["risk_score"], "band:", d["band"], "/", d["computed_band"], d["band_reasons"])
for c in d["clusters"]:
    print(c["cluster_id"], [m["wallet"][:6] for m in c["members"]], "share", c["share"], "conf", c["confidence"])
    for e in c["evidence"]:
        print("   ", e["type"], "via", [v[:6] for v in e["via"]], "refs", len(e["refs"]), "window", e["window"], "weight", e["weight"])
EOF
```

Очікувано: один кластер {P1, P2, P3} (S1 → P1, P2, P3 у вікні) з доказом `shared_funder`, `via = [S1]`, трьома підписами й вікном у секундах; P4 (той самий S1, але через 2 год) і група S2 — окремо або окремим кластером за декларацією сценарію; `band_reasons` пояснюють смугу.

Прямий переказ і позначений покупець (`c_direct_flagged`): кластер {P7, P8} з доказом `direct_transfer`; покупець-PDA з `buyer_flags` жодного зв'язку не утворює, а в `diagnostics` є `flagged_buyers_excluded` з його адресою й критерієм `known_list`.

## 4. User Story 2 — оцінка ризику з поясненням

```bash
uv run pytest tests/test_clusters_score.py tests/test_clusters_completeness.py -q
```

Очікувано на `c_two_clusters`: кластери відсортовані за часткою спадно (0.3333 потім 0.1429), `risk_score == floor(100·Σ share·confidence + 0.5)`, смуга за межами з `metadata.thresholds`; на `c_incomplete` — `computed_band: clean`, але `band: insufficient_data` з причиною `delegated_incomplete`/`ingest_incomplete`; на `c_empty` — `insufficient_data` з `empty_input`, `clusters: []`; на `c_shared` з повними даними і без кластерів у варіанті — `clean` з `no_clusters_on_complete_data`.

## 5. User Story 3 і 4 — поведінкові докази, непрямі зв'язки, артефакт

```bash
uv run pytest tests/test_clusters_behavior.py tests/test_clusters_indirect.py tests/test_clusters_hub_fixtures.py tests/test_clusters_recovered.py -q
```

Очікувано: `c_behavior` — без зв'язку кластера немає, збіг видно в `diagnostics` (`same_amounts_unlinked`/`same_slot_unlinked`); зі зв'язком — докази `same_amounts`/`same_slot` з підписами, впевненість вища, ніж без них; `c_diamond` — кластер P1–P2 з `indirect_link`, `via = [C, A, B]`, впевненість 0,3 < 0,6 у прямого джерела; варіант з A-хабом — зв'язку немає; `c_giant` — `possible_pruning_artifact` і впевненість × 0,5; фікстури 002 `g_hub`, `g_dust`, `g_dust_mixed`, `g_financier` — жоден доказ не містить відсічену адресу (крім `recovered_edge` на `g_dust_mixed`/`c_recovered`).

## 6. Контракт, детермінізм, межі модулів, швидкодія

```bash
uv run pytest tests/test_clusters_serialize_contract.py tests/test_clusters_determinism.py tests/test_clusters_boundaries.py tests/test_clusters_service.py -q
uv run pytest -m perf tests/test_clusters_performance.py -q     # SC-006; годинник; лише на вільній машині (поза типовим прогоном)
```

Очікувано: `to_json(analyze(g, r))` двічі — байт у байт і для 20 перестановок входу; `to_dict` валідний проти `contracts/cluster-result.schema.json` для всіх сценаріїв; ручні документи «кластер без доказів», «clean при incomplete», «кластер лише з поведінковими доказами» схемою відхиляються; `ast`-тест: ядро `src/unmask/clusters/*` не імпортує `hubs.{criteria,prune,report}`, `graph.{build,measures,components,service}`, внутрішності збору; 300 покупців / ~15 000 ребер — `ClusterService.analyze` + `to_json` < 1 с.

## 7. User Story 5 — критерій PRD на збережених результатах (FR-003-22, FR-003-23, SC-008)

```bash
uv run python -m unmask.clusters.evaluate --fixtures tests/fixtures/real
uv run python -m unmask.clusters.evaluate --fixtures tests/fixtures/real --check   # 0 — побайтово == tests/fixtures/real/expected_table.md
uv run pytest tests/test_clusters_evaluate.py -q
```

Очікувано (за пропозицією v1 конфігу; точні числа фіксує калібрування): заголовок із версіями `clusters.yaml` (і дайджестом), `hubs.yaml` (3), `hub_addresses.yaml` (1), `schema 003.1`; 9 рядків у порядку маніфесту; `risk_score` інсайдерських {32, 23, 13, 5, 54} (ins2 — 2 кластери, другий непрямий; R-12 передбачав 6 без проходу 2), чистих {0, 10, 5, 25}; `status: incomplete` у всіх (результати зібрано без аналізу делегованих купівель; cln4 ще й `missing: 3`), тож `band` чистих — `insufficient_data`, а колонка `band_if_complete` показує `clean` для cln1–cln3; зведення: `insider_above_clean 3/5`, `clean_within_clean 3/4`, `clean_band_shown 0/4`, `prd_criterion_met yes` і блок застережень (мітки MELT цінові, вибірка не статистична, калібрування на тому ж наборі, `delegated` не аналізовано). Два запуски — побайтово однакові.

Зміна будь-якого значення `config/clusters.yaml` без перегенерації еталона → `--check` ненульовий і `test_clusters_evaluate.py` червоний (дайджест у заголовку еталона ≠ `content_digest`). Перегенерація (`--write`) — лише як крок калібрування із записом у `calibration.md` 003.

## 8. Повний прогін і трасування

```bash
uv run pytest -q
python3 scripts/trace.py --check
```

Очікувано: зелено; `trace.py` без порушень (кожна виконана задача має `impl:` і `verifies:`; FR-003-01…23 покриті).

## Калібрування (research R-12; `calibration.md` 003 — з'явиться задачею калібрування)

Значення v1 — пропозиція з read-only симуляцій на 9 збережених результатах (таблиця варіантів у research R-12). Задача калібрування проганяє ті самі варіанти через реалізацію, фіксує значення (v1 без змін або v2 із записом і sha256), генерує `expected_table.md` і записує в `calibration.md`: таблицю, вибір, явні провали (ins2, ins3 — «чисті» за фінансуванням; cln4 — інсайдерський за поведінкою) і межу стійкості (ins1 = 23, cln4 = 25). Підбір «під мітки» (напр. W = 600 с заради 4/4 чистих) без механістичного обґрунтування заборонений гейтом критерію успіху.
