# Test prompts for split-pending detection (T33)

This directory holds test prompts for the Telegram 4096-char
split heuristic. The prompts are sized to land in the [4060,
4096] byte window so that the Telegram client, when auto-
splitting long text at sentence boundaries, will produce
multiple consecutive messages that the bot must catch and
refuse.

## Why a tight byte window

- Telegram client hard cap: 4096 characters per text message.
- When a user pastes more than 4096 chars, the Telegram
  client splits at a sentence boundary, landing the first
  chunk in the last 36 bytes of the cap, roughly 4060-4096.
- A message in that exact window is therefore very likely
  the FIRST half of a Telegram auto-split.
- The bot's `_split_pending` state (state.py) marks the
  (chat, user) as pending and drops both the first and any
  subsequent consecutive messages with an explanatory error.

## Files

- `test-prompt-en.md` (4092 bytes) — Newton's Cradle prompt
  in English, simplified from a longer reference prompt.
- `test-prompt-ru.md` (4084 bytes) — Russian translation of
  the same prompt, with the same intent but in fewer chars
  (Cyrillic is 2 bytes/char in UTF-8, so the same word
  count in Russian is heavier in bytes).

## How to use

Send one of these prompts to the bot via the Telegram app.
The bot should respond with the 4060-4096 split warning and
NOT forward the prompt to the LLM. If you also send a
follow-up message within 30 s, the bot should drop that too
with the "your previous message was 4060-4096 bytes"
explanation. The pending state clears after 30 s, on /reset,
or on /start.

To test the NEGATIVE case, send any message under 4060 bytes:
the bot should process it normally and not flag pending.

To test the positive case, send `test-prompt-en.md` (or
`-ru.md`) verbatim via the Telegram app. Paste the file's
text contents into a Telegram message; the client will
auto-split if it exceeds 4096.

## 6K test (two-chunk split)

- `test-prompt-6k-en.md` (6410 bytes) — Newton's Cradle
  prompt in English, ~1.5x the size of the 4K test prompt.
  Telegram will auto-split this into two consecutive messages:
  roughly **4096 bytes + 2314 bytes**. The first chunk lands
  in the [4060, 4096] window and triggers the split-pending
  heuristic. The second chunk (~2314 bytes) is well below
  the window, so the heuristic relies on the pending state
  to drop it, not on a second window check. This verifies
  the "second half of a split is dropped even if it is
  short" path.

