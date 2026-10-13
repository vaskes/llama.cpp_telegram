# Known Limitations

This document records hard limits of the bot's runtime that are
NOT bugs. Each entry says what the limit is, why it exists, and
what the workaround is for users who hit it.

## Telegram per-message cap: 4096 characters

**Limit:** A single text message sent through the Telegram client
(Android / iOS / Desktop / Web) is hard-capped at 4096 characters
by the Bot API. This is a Telegram-platform limit, not a bot
limit; the bot itself accepts arbitrarily long text.

**What happens if you exceed it:** The Telegram client splits the
message at a sentence boundary near the 4096 limit, producing
two or more consecutive messages. Each is delivered to the bot
as an independent `update.message.text`. The bot handles them
as independent LLM turns, so each turn sees only its own chunk
and produces a half-answer. The user gets two unrelated responses
instead of one full answer.

**Why we do NOT auto-reassemble:** Doing so would require waiting
N seconds between consecutive messages from the same (chat, user)
in case a follow-up is coming. That couples adjacent messages
and surprises users (their follow-up "thanks" or "ok" gets
appended to the previous prompt). It is also fragile: if the
user *intended* two short messages, the bot silently merges them.

**Workaround (heuristic, not a guarantee):** When a user sends
a message of length 4060-4096 bytes, the bot marks that
(chat_id, user_id) as a "split-pending" for 30 seconds. Any
subsequent message from the same (chat, user) in that window
is dropped with a reply explaining the situation. The first
message itself is also dropped, not forwarded to the LLM.

False positives are possible but rare because the 4060-4096
window is the last 36 bytes of the 4096 cap, which is where
Telegram clients land when splitting at sentence boundaries.
The flag clears on `/reset` / `/start` or after the 30 s TTL.

If your prompt is over 4096 characters:
- Shorten it
- Or send via `curl` to the Bot API directly (bypasses the UI cap)
- Or use `/reset` between batches and accept the per-batch answer

The split-detection logic lives in `telegram-bot/state.py` under
`_split_pending` / `_mark_split_pending` / `_is_split_pending`,
and is wired into `handle_text` in `telegram-bot/handlers.py`.

**Polling mode and split ordering:** The bot can run in either
webhook mode (one aiohttp server, single connection) or polling
mode (long-poll loops on the Telegram Bot API). In webhook
mode, two halves of an auto-split arrive as two consecutive
HTTP POSTs within milliseconds of each other, and the
split-pending 30 s window covers both. In polling mode, two
halves can land in different poll batches separated by 1-100
ms (or longer under load); the 30 s TTL is still wide enough
to cover any realistic gap. The operator confirmed this
behavior is acceptable: the false-positive rate of "two
unrelated consecutive messages from the same user within
30 seconds" is low enough that dropping both is the right
call.

This is also why the heuristic's 30 s window is generous. A
tighter window (e.g. 5 s) would risk missing the second half
in polling mode; a wider one (e.g. 5 min) would block
legitimate follow-ups. 30 s is the operating point.

