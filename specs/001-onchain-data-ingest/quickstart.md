# Quickstart: перевірка фічі 001 (збір ончейн-даних)

Усе нижче працює **без мережі**. Живий RPC потрібен лише для необов'язкового кроку 5.

## Передумови

- Python 3.12, `uv` (є в системі).
- Із кореня репозиторію: `uv sync` (підтягує `pyyaml`, `solders`, `httpx`, dev: `pytest`, `jsonschema`).

## 1. Повний прогін тестів і трасування

```bash
uv run pytest -q
python3 scripts/trace.py --check
```

Очікування: усі тести зелені; `trace: ok — 16 вимог, 21 задач, … у роботі`. Якщо будь-який тест спробує відкрити сокет — він падає гардом із `tests/conftest.py` (принцип II), це не flaky.

## 2. US1 — вибірка покупців та історія фінансування

```bash
uv run pytest -q tests/test_buyers.py tests/test_funding.py tests/test_collector.py
```

Що доводиться: на сценарії `tests/fixtures/scenarios/basic` для `first_buyers_n=3` повертаються рівно 3 покупці у порядку `(first_buy_slot, first_buy_signature, wallet)`; ребра `B→A` і `A→покупець` присутні з усіма полями [`Transfer`](data-model.md); третій стрибок і переказ після купівлі відсутні; результат збігається з `expected.json` без розбіжностей (SC-001, SC-006).

## 3. US2 — чесна неповнота

```bash
uv run pytest -q tests/test_collector_failures.py tests/test_budget.py tests/test_resume.py
```

Що доводиться: джерело, що відмовляє після K викликів, дає `status=incomplete` з переліком гаманців і причиною (SC-002); вичерпаний бюджет (`FakeClock`) — `incomplete` з `budget_exhausted`, а не очікування; повторний запит по неповному результату звертається лише за недоотриманим (журнал викликів фікстурного джерела).

## 4. US3 — кеш і відмови

```bash
uv run pytest -q tests/test_cache.py tests/test_service_rejections.py tests/test_serialize_contract.py
```

Що доводиться: другий `collect` того ж mint — 0 звернень до джерела і ідентичний результат (SC-004); некоректна адреса і неіснуючий mint — `Rejection`, без винятку (SC-005); серіалізація валідна проти [`contracts/ingest-result.schema.json`](contracts/ingest-result.schema.json).

Ручна перевірка на фікстурі (без мережі):

```bash
PYTHONPATH=src uv run python -c "
from pathlib import Path
from unmask.ingest.config import load_config
from unmask.ingest.rpc.fixture import FixtureRpcSource
from unmask.ingest.service import IngestService
from unmask.ingest.serialize import to_json
cfg = load_config(Path('config/ingest.yaml'))
src = FixtureRpcSource(Path('tests/fixtures/scenarios/basic'))
svc = IngestService(cfg, src)
mint = __import__('json').load(open('tests/fixtures/scenarios/basic/expected.json'))['mint']
print(to_json(svc.collect(mint))[:400])
"
```

## 5. (Необов'язково) живий RPC — лише вручну, не в тестах

```bash
export UNMASK_RPC_URL='https://<rpc-fast-endpoint>/<key>'
PYTHONPATH=src uv run python -c "
from pathlib import Path
import os
from unmask.ingest.config import load_config
from unmask.ingest.rpc.http import HttpRpcSource
from unmask.ingest.service import IngestService
from unmask.ingest.serialize import to_json
cfg = load_config(Path('config/ingest.yaml'))
svc = IngestService(cfg, HttpRpcSource(os.environ['UNMASK_RPC_URL'], cfg.rpc))
out = svc.collect('<mint>')
print(to_json(out)[:1000])
"
```

Очікування: відповідь ≤ 40 с зі `status` `complete` або `incomplete` з причинами (SC-003); `metadata.config_version` = 1.

## Що вважати провалом

- Будь-який результат зі `status=complete` при непорожньому `completeness.missing` — дефект інваріанта (принцип V).
- Тест, який проходить при заміні `FixtureRpcSource` на джерело з порожніми відповідями, — тест нічого не доводить (ревʼюер відхилить).
- Константа, що змінює результат, у коді замість `config/ingest.yaml`.
