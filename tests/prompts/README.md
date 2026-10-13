# Test prompts for split-pending detection (T33, T34)

This directory holds test prompts for the Telegram 4096-char
split heuristic. As of T34 (2026-10-13), the bot uses a
**rate-based burst detector** rather than a size-window: it
drops a follow-up that arrives within 2 seconds of a > 3000
byte message, and aborts the in-flight LLM call for the
first message.

## Why a tight byte window (legacy, T33)

The original T33 design used a size window [3500, 4096] bytes
to detect a probable auto-split. This failed in practice
because Telegram splits at paragraph boundaries (`\\n\\n`),
not at the 4096 hard cap. The observed split point for a 6 KB
prompt was 3910 bytes, and another split could land
anywhere from ~1500 to 4096 depending on the input text.

T34 replaces the size window with a burst pattern: two
messages from the same (chat, user) within 2 seconds, with
the first > 3000 bytes. See docs/KNOWN-LIMITATIONS.md for
the full rationale.

## Files

- `test-prompt-en.md` (4092 bytes, 4092 chars) — Newton's
  Cradle prompt in English. **Above 3000 bytes** so the
  burst detector considers it a split candidate if a
  follow-up arrives within 2 s.
- `test-prompt-ru.md` (4084 bytes, 2233 chars) — Russian
  translation. Same byte range, fewer characters because
  Cyrillic is 2 bytes/char in UTF-8.
- `test-prompt-6k-en.md` (6410 bytes) — Newton's Cradle
  prompt in English, ~1.5x the size of the 4K prompt.
  Telegram will auto-split this into two consecutive
  messages of roughly 4096 + 2314 bytes (observed: 3910 +
  2498 in our test, depending on paragraph boundaries).
  The first chunk is > 3000 bytes; the second chunk,
  arriving within 2 s, triggers the burst detector and
  is dropped with the in-flight LLM call aborted.

## How to use

Send one of these prompts to the bot via the Telegram app.
The bot will start processing the first chunk (large LLM
call, can take minutes for a 256k context model). When the
second chunk arrives, the bot will:
1. Abort the in-flight LLM call (the Stop button indicator
   on the "thinking" message will show aborted).
2. Reply with a "🚫 ⚠️ Detected a Telegram auto-split"
   error.
3. Drop the second message.

To recover: run /reset and resend as a single message
(shorter, or sent via `curl` to the Bot API directly to
bypass the 4096-char UI cap).

To test the NEGATIVE case (no split), send any message
under 3000 bytes. The burst detector does not fire and the
bot processes the message normally. Even two short messages
back-to-back ("ok" + "thanks") do not trigger the detector
because the first is < 3000 bytes.
