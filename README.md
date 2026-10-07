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

## Тест на 40 токенах MELT (top-30 + 10 чистих, 2026-10-07)

Разовий живий прогін конвеєра 001→002→003 (не тест, фікстури й тести не чіпались).
Джерело списку — публічний датасет
[MELT](https://github.com/git-disl/MELT) (41 470 запусків pump.fun, грудень 2024 —
березень 2025): мітки з `data/label/label.csv`, скори — найкраща модель
`results/mlp_pred_8_0.573976.csv` (AUPRC 0.574) з 6491 оціненого токена.

Вибірка: **top-30 за скором MLP спадно** (27× label high, 3× label low — хибні
спрацювання моделі, лишено чесно) + **10 випадкових** з `label=low, prob<0.3`
(пул 2709, `random.seed(42)`). Профіль збору = профіль фікстур
`tests/fixtures/real` (30 перших покупців, глибина 2, кап 30, без SPL, Helius):
конфіги ingest.yaml v3 (з цим профілем), hubs.yaml v3, clusters.yaml v1.
Сирі JSON збору в репозиторій не комітились.

Підсумок: **top-30 — 2/30 з `risk_score` > 20** (макс. 44, смуги: 27 clean,
2 suspicious, 1 insufficient_data); **чисті — 4/10 у межах clean** (смуги:
4 clean, 5 suspicious, 1 high_concentration з часткою 0.77).

Застереження: мітки MELT цінові/модельні, а не підтверджене інсайдерство;
наш сигнал — концентрація фінансування, а не рух ціни, тож розбіжності очікувані
і не є ні провалом, ні перемогою. Вибірка — з test-підмножини MELT, не
статистична; жодних тверджень про точність за нею робити не можна.

### Топ-30 MELT за скором MLP (label 1 = high, 0 = low)

| tag | mint | MELT label / prob | кластери | найбільша частка | risk_score | смуга |
|---|---|---|---|---|---|---|
| top00 | 6vfyJPcj… | 1 / 1.0000 | 1 | 0.1086 | 3 | clean |
| top01 | 33Vg4AZA… | 1 / 0.9970 | 1 | 0.2700 | 16 | clean |
| top02 | sYkrphWx… | 1 / 0.9946 | 0 | 0.0000 | 0 | clean |
| top03 | 7R9wQgbB… | 1 / 0.9945 | 2 | 0.3039 | 16 | clean |
| top04 | 5etjLwoQ… | 1 / 0.9939 | 2 | 0.2300 | 15 | clean |
| top05 | SboRMvam… | 0 / 0.9929 | 0 | 0.0000 | 0 | clean |
| top06 | DmVe4fLj… | 1 / 0.9922 | 0 | 0.0000 | 0 | clean |
| top07 | HtrpQFrr… | 1 / 0.9911 | 1 | 0.0270 | 1 | clean |
| top08 | 7YQteKmT… | 1 / 0.9910 | 1 | 0.3036 | 15 | clean |
| top09 | 3m15Wkxj… | 1 / 0.9907 | 0 | 0.0000 | 0 | clean |
| top10 | AdthQP1P… | 1 / 0.9902 | 0 | 0.0000 | 0 | clean |
| top11 | 9qRfF3ds… | 1 / 0.9900 | 0 | 0.0000 | 0 | clean |
| top12 | ForaJd1y… | 1 / 0.9896 | 0 | 0.0000 | 0 | insufficient_data |
| top13 | E8GVG28g… | 1 / 0.9894 | 0 | 0.0000 | 0 | clean |
| top14 | 2NToFHyD… | 1 / 0.9893 | 0 | 0.0000 | 0 | clean |
| top15 | 8YTwudT2… | 1 / 0.9893 | 0 | 0.0000 | 0 | clean |
| top16 | 38jPa3Xy… | 1 / 0.9890 | 1 | 0.1315 | 13 | clean |
| top17 | 9tmrmxQf… | 1 / 0.9889 | 0 | 0.0000 | 0 | clean |
| top18 | HDcdzqMj… | 1 / 0.9879 | 0 | 0.0000 | 0 | clean |
| top19 | 2DMBnQrM… | 1 / 0.9873 | 0 | 0.0000 | 0 | clean |
| top20 | B8XcfQyx… | 1 / 0.9865 | 2 | 0.1856 | 22 | suspicious |
| top21 | Hdt4Mzq4… | 1 / 0.9861 | 0 | 0.0000 | 0 | clean |
| top22 | 82gDSgCq… | 1 / 0.9856 | 0 | 0.0000 | 0 | clean |
| top23 | GbipdQ75… | 1 / 0.9854 | 2 | 0.1509 | 18 | clean |
| top24 | 7grM4E9W… | 1 / 0.9850 | 0 | 0.0000 | 0 | clean |
| top25 | A6RLPJh8… | 0 / 0.9848 | 0 | 0.0000 | 0 | clean |
| top26 | J8A3ySxv… | 1 / 0.9847 | 3 | 0.6433 | 44 | suspicious |
| top27 | YnnHzYoZ… | 0 / 0.9839 | 1 | 0.1585 | 8 | clean |
| top28 | 2ojgFJwr… | 1 / 0.9837 | 1 | 0.3421 | 17 | clean |
| top29 | 7VgRj9KD… | 1 / 0.9834 | 1 | 0.1890 | 14 | clean |

### 10 випадкових MELT-low (seed 42, prob < 0.3)

| tag | mint | MELT label / prob | кластери | найбільша частка | risk_score | смуга |
|---|---|---|---|---|---|---|
| cln00 | EY3LtKkE… | 0 / 0.0000 | 0 | 0.0000 | 0 | clean |
| cln01 | 8CCrMeYV… | 0 / 0.0722 | 1 | 0.1332 | 7 | clean |
| cln02 | 25PULbwG… | 0 / 0.0000 | 1 | 0.3792 | 28 | suspicious |
| cln03 | FypF3PAS… | 0 / 0.0001 | 2 | 0.4879 | 49 | suspicious |
| cln04 | DcXiGD7j… | 0 / 0.0002 | 1 | 0.7666 | 56 | high_concentration |
| cln05 | 6T9HXGpo… | 0 / 0.1475 | 2 | 0.1373 | 18 | clean |
| cln06 | G85APK2X… | 0 / 0.0009 | 2 | 0.4742 | 49 | suspicious |
| cln07 | G7eqTXRx… | 0 / 0.0002 | 1 | 0.5596 | 41 | suspicious |
| cln08 | BsE8arwK… | 0 / 0.0001 | 0 | 0.0000 | 0 | clean |
| cln09 | 5qYEEpfD… | 0 / 0.0001 | 2 | 0.2522 | 28 | suspicious |

<details>
<summary>Повні адреси mint (40)</summary>

```
top00 6vfyJPcjD5VZykv2UNv9jNWx3Yx1rKs2LsRJ532Tpump
top01 33Vg4AZATCyUmarXXHGU19J9n8mcAktCFeZSmsTRpump
top02 sYkrphWxsUgdrUi5fHpF4Dp9FWDG8X27iH7EUU7pump
top03 7R9wQgbBBXtA38H6VD88jat83qwtuvRdUXuLE9Crpump
top04 5etjLwoQXCYux5k4o3j85CyyAfWyfn1NnevBnR7Qpump
top05 SboRMvamFnWikvm12WnVKTXm3k5Cv19swXv3r67pump
top06 DmVe4fLjWAPaRt2KYMnDeewTDzMrJ2ZwFmC1FmvMpump
top07 HtrpQFrrd6hefsbAMifWibPetY4Kd8pjTbX7Wxmjpump
top08 7YQteKmTNCKoGABCgY88AT2ehTNbB5bEPWTFCGGKpump
top09 3m15WkxjqzUVDdjQuogdfiEHFf9EY6j7W21d6j8spump
top10 AdthQP1Pf9Wx64n8nvmPLHvr3rCBG7Uc6W4Paoprpump
top11 9qRfF3dsB4sNZfegWcpyQtL57KnaNjssi8q9UVrqpump
top12 ForaJd1yMPLQeGcC2zESCjDqSZAD7JLtDJQCMwW6pump
top13 E8GVG28gW4Ja1d7y1naUnUegTv1G2VaEoFR3SFqmpump
top14 2NToFHyD5Ccu9GhNjneXvSyXokJK2CiJqDm1Mfccpump
top15 8YTwudT2oTGQHK6Kv1MZcpbuFYu12iYMDbSKcpREpump
top16 38jPa3Xyj15ZqQWYrohYStKtJvj4ffP1pdhwucUKpump
top17 9tmrmxQfd9eVHg6SeeeEk5Sr4CHUK2FeaGp6vhrGpump
top18 HDcdzqMjkR6ik3B2skL8WTLy8u33gr49pTSTwgANpump
top19 2DMBnQrMPLEQefX7Z7rBYgahiYw725REGm22DboKpump
top20 B8XcfQyxL47oeKwwg4n66xL6hamzBVwLc1ZLaP9dpump
top21 Hdt4Mzq4WyxM6EUAag3oQCwnAtqQXcsgE57CLVH6pump
top22 82gDSgCqzkMowoQs9yWN4dicMUS54bBNUasf7yo9pump
top23 GbipdQ75ycSWU9oPL64f5ZgoQxDpWkPpKEUtmB4Mpump
top24 7grM4E9WzTTAZxKEqDeCuofR6BxgNxYraWGW2fHypump
top25 A6RLPJh8LUVJngitjEfvSPdF8Wyy873ckcF8ohUFpump
top26 J8A3ySxv6a8Fy1usD3kjznc2ySQMze3nsRngr7Xvpump
top27 YnnHzYoZHaJNoEB9VkhuMUc2AVRSBKhJ3NBgHB9pump
top28 2ojgFJwroeG9VwbPYf7L42SjffX2m6rPVjrZ724cpump
top29 7VgRj9KDFoTx6ipEgGvJF1ormQw8WuNZHSWEpFEpump
cln00 EY3LtKkEBPvg7mMt784mAL3N4NuewBjzVjtFCEopump
cln01 8CCrMeYVUMsuxC87fR327CLJfm3DQ4XBiDteTKLxpump
cln02 25PULbwG3GcktFto9h36rzansePk8i4VHFhtxgXdpump
cln03 FypF3PASQ6D64ZqxP1BPbYshKwJXSuoi9c4uJkB7pump
cln04 DcXiGD7jvV6MPFYMgrnFPiU7tHHLAFrwLovFBPpUpump
cln05 6T9HXGpoHKfm8nenyj8HepPkVAtNbzCx5aFrMGPopump
cln06 G85APK2XHT89eLUHiSM6vYAWxmU5GgrmteccKyVipump
cln07 G7eqTXRxe5kcvrrCu7WqG24G4uV66McrNo2NJ4Crpump
cln08 BsE8arwK73fjNKSGfTkYMrxLo4CLpWg9oZrRkKSVpump
cln09 5qYEEpfDMg7StrFPMn4jz2azTHpbe3QpMZtdunBRpump
```

</details>

## Розкриття використання AI-інструментів

Цей проєкт розроблено з активним використанням AI-асистентів програмування:
архітектурні рішення, код, тести й документація писалися в парі людина + AI
(Claude Code / Codex-агенти: `architect`, `implementer`, `implementer-senior`, `reviewer`;
ролі зафіксовано в `AGENTS.md`/`CLAUDE.md` і конституції `.specify/memory/constitution.md`).
Усі ключові рішення (пороги, вікна, ваги, смуги) приймала людина: калібрування записано
в `specs/*/calibration.md`, гейти контрольної точки, критерію успіху й подачі — рішення
людини. Тести писалися до коду (TDD), зовнішні дані в тестах — лише записані фікстури.
