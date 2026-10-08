# Contract: overload responses (HTTP + bot), feature 008

Additive to feature 004 contracts: success documents (schema 004.1), 400/404/405/500
responses and all existing bot texts stay byte-for-byte the same.

## HTTP

| Situation | Status | Body | Headers |
|---|---|---|---|
| client over request limit (`GET /api/token/{mint}` only) | `429` | `overload-error.schema.json`, `kind=rate_limited` | `Retry-After: N` |
| all heavy-analysis slots busy, mint is cold and owner of a new analysis | `503` | `overload-error.schema.json`, `kind=busy` | `Retry-After: N` |
| `/healthz` | `200` `{"ok": true}` | never limited | — |
| unknown path / wrong method | `404` / `405` as before | not limited | — |

- `N` is a positive integer (delta-seconds) and equals `error.retry_after_seconds`.
- `rate_limited`: `N = ceil(oldest_counted_request + window − now)`, within `[1, window]`;
  retrying after `N` seconds is admitted (denied requests are not counted).
- `busy`: `N = heavy.busy_retry_after_seconds` from `config/protection.yaml`.
- Precedence: request limit is checked first (429 never runs analysis); then the
  service decides (cache hit → 200; invalid address → 400; join in-flight → result of
  that analysis; new cold analysis without slot → 503).
- `detail` texts (English, stable for tests):
  - `rate_limited`: `too many requests from this client, retry after N s`
  - `busy`: `too many analyses in progress, retry after N s`
- No authentication: limits key on client address only, identical for every client.

## Client address

Peer address of the TCP connection, unless the peer is listed in `proxy.trusted`; then the
right-most non-trusted valid IP of `X-Forwarded-For` (garbage entry → peer). The nginx
example overwrites the header with `$remote_addr`.

## Bot (`/check <mint>`, English)

| Situation | Reply (one `sendMessage`, no analysis, no progress messages) |
|---|---|
| this user's previous `/check` still running | `your previous check is still running — please wait for its result` |
| user over `bot.checks_per_window` per `bot.window_seconds` (only cold, valid mints count) | `too many checks — try again in N s` |
| global heavy slots busy (shared with the API) | `service is busy with other analyses — try again in ~N s` |

User key: `message.from.id`, else `message.chat.id`. Cached mints and invalid addresses do
not consume the user's window; the one-at-a-time rule applies to every `/check`.
"evidence" callbacks are never limited (served from cache).
