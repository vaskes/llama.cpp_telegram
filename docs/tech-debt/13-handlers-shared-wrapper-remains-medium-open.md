---
tech_debt: 13
marker: tests/test_handler_middle.py
marker_kind: tripwire
state: closed
issue: null
---

## 13. The shared handler wrapper (MEDIUM, closed)

**Measured 2026-10-22.** Four handlers, 158–291 lines each:
`handle_photo` 291, `handle_text` 288, `handle_document` 231,
`handle_voice` 158. Each calls the same 9–11 helpers —
`_check_user_slot`, `_is_rating_active`, `_route_to_thread`,
`_load_history`, `_reply`, `_apply_rating_and_persist`,
`_stop_button_markup`, `call_llama`.

**Correction to the review's estimate.** It put the duplication at
"~120 lines in each". The duplication is real; the figure was not
measured and is high. The dangerous half — the persist/rollback block,
eight copies of it — was extracted into `turn.py` under item 4 and is
not duplicated.

**Item 8 closed 2026-10-22** (`docs/tech-debt/08-decompose-the-tool-loop.md`,
`state: closed`), so the trigger this file recorded has fired: the loop
has a signature stable enough to template against, and the call sites
have moved. What followed, in two blocks:

**Block A — `fc855d5`.** `unwrap_llm_result` and `_format_executed_tools`
out of `handlers.py` into `telegram-bot/llm_result.py`. The flattening
existed seven times: four primary paths, three empty-response retries.
`_format_executed_tools` moved with it rather than being passed in —
keeping the formatter behind would make the new leaf import the module
it was extracted from.

**Block B — `ea58e86`.** The tail of an aborted turn out of `handlers.py`
into `telegram-bot/llm_abort.py`. Seven copies again: four primary abort
branches, three retry aborts. The load-bearing line is
`turn.rollback('aborted')`. Also in this block: the user-facing empty
response fallback, hardcoded three times, is now
`config.EMPTY_RESPONSE_USER_FALLBACK`; and four comments citing a line
number were rewritten to cite a property, with a tokenize-based check so
the next one cannot land.

**Block D — `4ba6451`.** `_acquire_turn_slot(update, context, label=...)`:
route, take the per-user slot, or drop the update. Four copies of seven
lines. It stays in `handlers.py` rather than a leaf module because
`_route_to_thread` is here, and moving that out would make the leaf
import the module it was extracted from.

**Block E — `36e244b`.** `_persist_for_reply(...)`: rating dispatch,
persist, and the abort check between the write and the reply. Four copies.
Only the streaming handlers pass `abort_event`; photo and document do not
stream and have no await in that window, so their guard was always
`if bot_response`, and giving them an event would assert a fiction.

**Block F — `8c09172`.** `_open_thinking(update, label=...)`: the thinking
message, the None guard, the sender name. `_turn_context(update, chat_id,
thread_id, label=...)`: history and rating mode together.

`_turn_context` exists for the three handlers that persist a turn first.
Photo does not persist one — it reads rating mode BEFORE the history and
appends the image turn afterwards — so a flag parameter exactly one
caller never uses was worse than leaving it out.

## Closed

Six blocks. `handlers.py` 1464 → 1357 (then 1278, when TECH-DEBT #10 block C moved the sender-label filter out) lines, **48 mutations and 48
caught**, suite 1859 passed. What is left shared across the four handlers
is the `call_llama` call itself, and that is deliberate:

- `max_tokens` is `MAX_TOKENS_CAPTION` for photo and document,
  `MAX_TOKENS_DEFAULT` for text;
- `user_text` is a caption, a transcript, or a user message;
- `use_stream` is `False` for photo only, the other three take the
  default — a lost branch condition there cost an afternoon of
  production `JSONDecodeError`s that read like the engine returning an
  empty body;
- three of the four have an empty-response retry and voice does not.

A helper over those arguments has the same shape as the four calls it
would replace and buys almost nothing. `use_stream` keeps a test on both
sides of the change in `tests/test_handler_skeleton.py`.

## Markers

- `tests/test_finally_hosts.py` — the `finally` invariant below.
- `tests/test_llm_result.py` — block A: one unwrap for all four handlers,
  and the tripwire that had to be corrected twice before it could fire.
- `tests/test_llm_abort.py` — block B: the abort tail, the log wording,
  and the no-line-numbers check.
- `tests/test_handler_skeleton.py` — block C: the stage order, the three
  divergences, and the `use_stream` split.
- `tests/test_handler_entry.py` — block D: routing and the slot.
- `tests/test_handler_tail.py` — block E: persist and the abort window.
- `tests/test_handler_middle.py` — block F: thinking, sender name,
  history and rating mode.

<!-- current: handlers.py=1575 -->
