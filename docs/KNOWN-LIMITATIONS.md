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
message at paragraph boundaries (`\\n\\n`), producing two or
more consecutive messages. Each is delivered to the bot as an
independent `update.message.text`. The bot handles them as
independent LLM turns, so each turn sees only its own chunk and
produces a half-answer. The user gets two unrelated responses
instead of one full answer.

**Why we do NOT auto-reassemble:** Doing so would require waiting
N seconds between consecutive messages from the same (chat, user)
in case a follow-up is coming. That couples adjacent messages
and surprises users (their follow-up "thanks" or "ok" gets
appended to the previous prompt). It is also fragile: if the user
*intended* two short messages, the bot silently merges them.

**Workaround (T34, 2026-10-13, rate-based burst detection):**
Instead of guessing the split point with a size window (which
failed because Telegram splits at paragraph boundaries, not at
the 4096 cap, and the observed split point for a 6 KB prompt
was 3910 bytes), the bot now detects the BURST pattern:
if the same (chat, user) sends a follow-up within 2 s of a
previous > 3000-byte message, the bot assumes the second is a
Telegram split follow-up. The bot:
1. ABORTS the in-flight LLM call for the first message
   (via the existing `abort_event` mechanism used by the
   Stop button).
2. Drops the second message.
3. Replies with an error explaining the situation and the
   workarounds (single message, /reset, or Bot API).

The 2 s window is short enough to not interfere with normal
back-and-forth (paced by human typing speed, 3-5 s between
turns) and long enough to cover the gap between two poll
batches of a single Telegram update (which can be 100 ms -
2 s apart). The 3000 byte threshold excludes burst patterns
of short messages like "ok" + "thanks".

**Known false negatives:** A 4095-char prompt is below the
4096 cap so Telegram does not split it; the burst detector
sees only one message and processes it normally. Users who
hit the cap should send via `curl` to the Bot API directly,
bypassing the 4096-char UI cap.

**Known false positives:** If a user really does send two
messages within 2 s, with the first > 3000 bytes, the bot
aborts the first LLM call and drops the second. This is
intentional: the alternative (running both) is strictly
worse for the split case (two half-answers), and the false
positive (a fast-paced dialog) is recoverable by /reset.

**State is per-(chat, user)**, not per chat, so a user
typing a long prompt does not block other users in the same
group.

The implementation lives in:
- `telegram-bot/state.py` under `_user_recent_msg`,
  `_record_user_message`, `_check_burst_split`.
- `telegram-bot/handlers.py` `handle_text()` — burst check
  runs before the normal flow; `_record_user_message` is
  called after the abort_event is registered so a follow-up
  can cancel the in-flight LLM call.
