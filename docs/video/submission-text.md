# Чернетка тексту сабміту (Superteam Earn)

Заповніть `<…>` і скоригуйте під вимоги кожного треку. Не додавайте ключів і токенів.

## Short description (≤ 280 chars)

Unmask takes a Solana token address and returns clusters of related early-buyer wallets with on-chain evidence
(shared funder, identical amounts, same slot) and a 0–100 risk score. Telegram bot, public HTTP API.

## What it does

- `/check <mint>` in the Telegram bot returns risk (0–100) and band, the top clusters with their share of the analyzed
  buyers' purchases, a server-rendered graph PNG, and an **Evidence** button that expands the proof.
- `GET /api/token/{mint}` returns the same as JSON: clusters, confidence, supply share, evidence list with source
  addresses and time windows. Incomplete data is reported as incomplete, never as clean.

## How it works

1. Take the first buyers of the token and fetch each wallet's incoming-transfer history.
2. Build a funding graph (depth 2) and prune hubs: exchanges, routers, program accounts.
3. Cluster wallets that share a funder within a narrow time window; add indirect links; raise confidence with
   behavioral signals (identical amounts, same slot).
4. Score: supply share × link strength × behavioral similarity, with the evidence attached.

## RPC usage (for the RPC Fast track)

Data is collected through an RPC pool: RPC Fast serves recent history (and a share of transaction fetches), archival
providers back it up for older tokens. The pool splits batches across providers in parallel, fails over on errors and
rate limits, and never reports a truncated history as complete. Per-provider counters are logged by the service.

## Verification and honesty

- Method and thresholds are in versioned YAML with a change log; every requirement is traced to a task, code and a test.
- Validated on real tokens (see the README, "Cohort experiment"); limitations are documented there.
- This is a signal about financial links between wallets, not proof of insider trading, and not financial advice.
- Analysis profile: first 30 buyers, funding depth 2. The full 200–500 buyer profile needs higher RPC throughput.

## Links

- Demo video: `<VIDEO_URL>`
- Bot: `<@BOT_USERNAME>` (try `/check GnQUsLcyZ3NXUAPXymWoefMYfCwmJazBVkko4vb7pump`)
- API: `<DEMO_URL>/api/token/<mint>`
- Repository: https://github.com/nikshev/unmask

## AI tooling disclosure

Built with Claude Code and Codex agents inside a spec-driven workflow; see "AI tooling disclosure" in the README.
