# Quickstart: Граф фінансування та відсікання хабів (002)

Як переконатися, що фіча працює, без мережі. Контракти — `contracts/`, сутності — `data-model.md`.

## Передумови

```bash
cd /opt/unmask
uv sync                                   # залежності без змін відносно 001
git config core.hooksPath .githooks       # якщо репозиторій клоновано заново
```

Пакет не встановлюється: `src` підключено лише для pytest (`pythonpath=["src"]`); для ручних команд — `PYTHONPATH=src`.

## 1. Конфігурація версіонована й захищена (FR-002-08, FR-002-13, SC-008)

```bash
uv run pytest tests/test_hubs_config.py tests/test_hubs_changelog_guard.py -q
```

Очікувано: зелено. Перевірка вручну: змініть `degree_threshold` у `config/hubs.yaml`, не чіпаючи `version` і `config/CHANGELOG.md` → `test_hubs_changelog_guard.py` червоний із повідомленням про розбіжність sha256 для версії 1. Поверніть значення.

## 2. Фікстури й незалежні еталони (research R-18)

```bash
uv run python tests/fixtures/build_graph_fixtures.py --check   # 0 — файли на диску збігаються з генератором
uv run python tests/fixtures/build_fixtures.py --check         # 001 + swapsend: basic/hub/corrupt/notfound незмінні
uv run pytest tests/test_graph_fixture_builder.py tests/test_delegated_fixtures.py -q
```

Очікувано: кожен `tests/fixtures/graph/*/ingest.json` валідний проти `specs/001-onchain-data-ingest/contracts/ingest-result.schema.json`; `expected.json` 001-сценаріїв без змін у `git status`. Після T-056 сценаріїв 002 одинадцять (`g_dust`, `g_financier`, `g_dust_mixed` додано; `g_delegated` — T-047), `expected.config.version == 2`, у `measures` кожної вершини є `buyer_fanout` і `median_to_buyers`.

## 3. User Story 1 — граф з прослідковністю

```bash
uv run pytest tests/test_graph_build.py tests/test_graph_build_nodes.py tests/test_graph_completeness.py tests/test_graph_determinism.py -q
```

Ручна перевірка на `g_basic`:

```bash
PYTHONPATH=src python3 - <<'EOF'
import json
from pathlib import Path
from unmask.graph.build import build_graph
from unmask.ingest.serialize import from_dict  # обернене до to_dict, додається задачею T-026
r = from_dict(json.loads(Path("tests/fixtures/graph/g_basic/ingest.json").read_text()))
g = build_graph(r)
print(len(g.nodes), "вершин,", len(g.edges), "ребер")
for e in g.edges[:3]:
    print(e.kind, e.sender[:6], "->", e.receiver[:6], e.asset, e.amount, e.count, [ref.signature[:8] for ref in e.refs])
EOF
```

Очікувано: вершина A з двома вихідними ребрами (A→P1, A→P2), кожне з `count=1` і одним `ref`; кілька переказів однієї пари й активу згорнуті в одне ребро з усіма підписами в `refs`.

> `from_dict` — читання контракту 001 назад у `IngestResult` (`from_dict(to_dict(r)) == r`); у тестах 002 його обгортає helper `tests/conftest.py::load_ingest_fixture(name)`.

## 4. User Story 2 — відсікання з поясненням

```bash
uv run pytest tests/test_hubs_measures.py tests/test_hubs_threshold_rule.py tests/test_hubs_criteria_lists.py \
              tests/test_hubs_prune.py tests/test_hubs_report.py tests/test_hubs_lists_unavailable.py tests/test_hubs_edge_cases.py -q
```

Ручна перевірка на `g_hub` (SC-001):

```bash
PYTHONPATH=src python3 - <<'EOF'
import json
from pathlib import Path
from unmask.hubs.config import load_hub_config
from unmask.graph.service import GraphService
from unmask.graph.serialize import to_dict
from tests.conftest import load_ingest_fixture
cfg = load_hub_config(Path("config/hubs.yaml"), Path("config/hub_addresses.yaml"))
res = GraphService(cfg).analyze(load_ingest_fixture("g_hub"))
d = to_dict(res)
print("до:", d["report"]["before"]["largest_component_buyer_share"], "після:", d["report"]["after"]["largest_component_buyer_share"])
print("попередження:", d["report"]["warnings"])
for p in d["pruned"]:
    print(p["address"][:8], [(c["criterion"], c["measured"], c["threshold"], c["detail"]) for c in p["criteria"]], len(p["incident_edges"]), "ребер")
print("покупців у графі:", sum("buyer" in n["roles"] for n in d["graph"]["nodes"]), "/", d["metadata"]["wallets_analyzed"])
EOF
```

Очікувано: до ≥ 0.9, після < 0.5, `giant_component` відсутнє у `warnings`; H відсічено з критеріями `one_off_senders` (виміряна частка й поріг 0.8) і/або `ingest_high_degree`; усі покупці присутні (SC-003); кожен запис має ≥ 1 критерій (SC-002).

Списки недоступні (FR-002-12): `load_hub_config(Path("config/hubs.yaml"), None)` → `metadata.lists_applied == false`, `address_lists_version == null`, `warnings` містить `address_lists_not_applied`, записів із `detail` `list:*` немає.

Пилове роздавання (FR-002-22, SC-009; research R-22, `calibration.md`) — той самий скрипт на `g_dust` і `g_financier`:

```bash
uv run pytest tests/test_hubs_dust_fanout.py -q
```

Очікувано на `g_dust`: джерело D відсічено з єдиним критерієм `dust_fanout`, `measured` — медіана в лампортах (< 1000000), `threshold` — `1000000`, у `measures` запису `buyer_fanout` ≥ 5 (один переказ на 5 SOL серед пилу медіану не зрушив); джерело E з fan-out 4 — не відсічене (передумова `dust_min_fanout = 5`, включно); частка до ≥ 0.9, після < 0.5. На `g_financier`: `pruned` порожній — фінансист R (30 покупців по ≥ 0,1 SOL, ступінь 32) лишається; `warnings` містить `giant_component` — це чесний результат (усі покупці профінансовані одним гаманцем), сигнал для кластеризації, а не хаб. На `g_dust_mixed`: X (4 пилових + 3 справжніх) відсічено, Y (3 + 3) — ні; W (5 × 999 999) відсічено, Z (5 × 1 000 000, рівно поріг) і V (5 × 1 000 001) — ні.

`metadata.hub_config_version == 2`, `metadata.thresholds` містить `dust_amount_lamports` і `dust_min_fanout`.

## 5. Контракт, детермінізм, інтеграція з 001, швидкодія

```bash
uv run pytest tests/test_graph_service.py tests/test_graph_serialize_contract.py tests/test_graph_integration_001.py tests/test_graph_performance.py -q
```

Очікувано: `to_json(analyze(r))` двічі — байт у байт (SC-004); `to_dict` валідний проти `contracts/graph-result.schema.json` для всіх сценаріїв; результат справжнього `IngestService.collect` на `tests/fixtures/scenarios/{basic,hub}` проходить через `analyze` без винятків і з усіма покупцями; `g_perf` (300 покупців, ~20k переказів) — < 2 с (SC-007).

## 6. User Story 3 — swap-and-send (розширення 001)

```bash
uv run pytest tests/test_ingest_delegated_model.py tests/test_delegated_rule.py tests/test_delegated_wiring.py \
              tests/test_graph_delegated_edges.py tests/test_graph_delegated_contract.py -q
uv run pytest tests/test_collector.py tests/test_serialize_contract.py tests/test_resume.py tests/test_cache.py -q   # 001 лишається зеленим
git status --short tests/fixtures/scenarios/   # лише swapsend/ новий; basic/hub/corrupt/notfound без змін (SC-006)
```

Ручна перевірка на `swapsend`:

```bash
PYTHONPATH=src python3 - <<'EOF'
from pathlib import Path
from unmask.ingest.config import load_config
from unmask.ingest.rpc.fixture import FixtureRpcSource
from unmask.ingest.service import IngestService
from unmask.ingest.serialize import to_dict
import json
cfg = load_config(Path("config/ingest.yaml"))
mint = json.loads(Path("tests/fixtures/scenarios/swapsend/expected.json").read_text())["mint"]
d = to_dict(IngestService(cfg, FixtureRpcSource(Path("tests/fixtures/scenarios/swapsend"))).collect(mint))
print(json.dumps(d["delegated"], indent=1)[:1200])
print([b["wallet"][:6] for b in d["buyers"]])
EOF
```

Очікувано: один `links` запис A→B з підписом і слотом; `unpaired` — кандидати з транзакцій «2 платники × 2 отримувачі» та «1 платник × 2 отримувачі» з `detail="payers=… receivers=…"`; звичайна купівля, ейрдроп і `mintTo` зв'язків не дають; `complete: true`; список покупців збігається з `expected.json` (без B).

## 7. Повний прогін і трасування

```bash
uv run pytest -q
python3 scripts/trace.py --check
```

Очікувано: зелено; `trace.py` — без порушень (кожна виконана задача має `impl:` і `verifies:`).

## Калібрування (research R-12, R-22; `calibration.md`)

Виконано 2026-10-04 на 9 реальних токенах pump.fun (Helius, збір фічі 001): пилові джерела є в кожному токені, ступінь їх не відрізняє від справжніх фінансистів (макс. ступінь 45 < 100), тож `degree_threshold` лишено 100, а додано критерій `dust_fanout` (`config/hubs.yaml` v2: `dust_amount_lamports=1000000`, `dust_min_fanout=5`; запис `## 2` у `config/CHANGELOG.md` з sha256). Перевірка журналу — розділ 1 вище. Наступний крок (R-23, після T-032/T-057): прогнати збережені результати 001 цих 9 токенів через `GraphService` і звірити `pruned[]` з таблицями `calibration.md`; розбіжність → нова версія `hubs.yaml` із записом, не правка коду. Адреси бірж/ММ — лише з джерелом і датою; у калібруванні джерела не було, категорії порожні.
