# Unmask — insider cluster detection for Solana token launches

Приймає адресу токена й повертає кластери повʼязаних гаманців із доказами.
Сабмішн на Crypto World's Fair (Colosseum + Superteam Earn): треки Superteam Ukraine, RPC Fast
Infrastructure, за часом — Solami і Panta API. Дедлайн: 13 жовтня, 06:59 UTC.

## Як це працює

Конвеєр: `001 збір ончейн-даних` → `002 граф фінансування без хабів` → `003 кластери
з доказами й оцінка ризику 0–100` → `004 публічний API і Telegram-бот`. Жодного числа
без доказів: кожен кластер несе перелік первинних посилань (підписи, слоти, адреси джерел),
неповні дані ніколи не показуються як «чисто».

## Швидкий старт

```bash
uv sync
git config core.hooksPath .githooks
uv run pytest -q                                  # повний набір, без мережі
uv run python -m unmask.clusters.evaluate --fixtures tests/fixtures/real --check
```

Живий сервер і бот (потрібні ключі оператора, у репозиторії їх немає):

```bash
export UNMASK_RPC_URL='https://…key…'
export UNMASK_BOT_TOKEN='123:ABC…'
uv run python scripts/serve_unmask.py --port 8080
curl localhost:8080/api/token/<mint> | python3 -m json.tool
```

Документація фіч: `specs/001-onchain-data-ingest/`, `specs/002-funding-graph-hub-pruning/`,
`specs/003-wallet-clusters-risk/`, `specs/004-api-bot-delivery/` (у кожній — `spec.md`,
`plan.md`, `contracts/`, `calibration.md`/`known-issues.md`). Матриця трасування:
`docs/traceability.md` (генерується `python3 scripts/trace.py`).

## Розкриття використання AI-інструментів

Цей проєкт розроблено з активним використанням AI-асистентів програмування:
архітектурні рішення, код, тести й документація писалися в парі людина + AI
(Claude Code / Codex-агенти: `architect`, `implementer`, `implementer-senior`, `reviewer`;
ролі зафіксовано в `AGENTS.md`/`CLAUDE.md` і конституції `.specify/memory/constitution.md`).
Усі ключові рішення (пороги, вікна, ваги, смуги) приймала людина: калібрування записано
в `specs/*/calibration.md`, гейти контрольної точки, критерію успіху й подачі — рішення
людини. Тести писалися до коду (TDD), зовнішні дані в тестах — лише записані фікстури.
