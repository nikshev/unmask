# Contract: Telegram bot messages (FR-004-03, FR-004-04)

Bot language: English (all user-visible texts). Transport — straight Bot API
(research R-2). The formats below are what the user sees and what the
fake-transport tests check; Bot API method names are platform property.

## Input: `/check <mint>`

- Text exactly `/check`, space, address. Empty argument or garbage → error reply
  (see "Errors"), not silence.
- The bot silently ignores other messages (the demo needs no help menu; `/start`
  answers with a one-line hint showing the command format).

## Output: one assessment message

1. `sendPhoto`: PNG bytes + caption:
   - line 1: `risk <number>/100 — <band words>` (bands: "clean" / "suspicious" /
     "high concentration" / "insufficient data");
   - then one line per top-3 cluster: `#<i>: <n> wallets, share <share>`;
   - last line: provenance on one line (`v` config versions, completeness status).
2. `reply_markup`: one inline button `"evidence"` with
   `callback_data = "evidence:<request_id>"`, where `request_id` is the cache-entry
   key holding the full document.

No clusters: the caption says "no related groups found" + the completeness line;
no "evidence" button (nothing to show).

## "evidence" button callback

1. `answerCallbackQuery` (empty text — dismisses the spinner in the client).
2. `sendMessage` (a new message into the same chat, not a photo edit):
   one block per cluster — `Cluster #<i> (share, confidence)` and evidence rows
   `• <type>: <shortened sources>, window <start>–<end> (<basis>)`.
3. Truncation (R-8): if the text exceeds ~4000 chars — first evidence items in full +
   a line `…and K more pieces of evidence (continued)`; the full text goes as
   a second message or a `.txt` document on the same callback.

## Errors

- Invalid `mint` / token not found / cut-off ingest: `sendMessage` with human text
  (`"doesn't look like a Solana address"`, `"token not found"`,
  `"incomplete data: <reasons>"`). No stack traces; the process and polling loop
  stay alive.
- Помилка середовища (немає ключа RPC/бота): таке повідомлення ніколи не відправляється
  користувачу як відповідь — процес падає голосно на старті з назвою змінної (fail-fast).

## `request_id`

Opaque request id (`mint` + process counter), lives only in memory next to the
cache; after a restart, old buttons get "request expired, send /check again".
