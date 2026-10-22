---
tech_debt: 13
marker: tests/test_handler_skeleton.py
marker_kind: tripwire
state: open
issue: null
---

## 13. The shared handler wrapper is still duplicated (MEDIUM, open)

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

**Where this stands.** Five blocks, `handlers.py` 1464 → 1356 lines, 40
mutations and 40 caught. What is still four times over is the middle:
`_load_history`, `_is_rating_active`, `_sender_display_name`,
`_stop_button_markup`, the thinking message, `_get_global_llm_sem` and
`call_llama`. That part is the remaining half, and it is the more careful
half, because the parameters genuinely differ there:

- `max_tokens` is `MAX_TOKENS_CAPTION` for photo and document,
  `MAX_TOKENS_DEFAULT` for text, and caption/transcript/user message for
  the `user_text`;
- `use_stream` is `False` for photo only, and the other three take the
  default — a lost branch condition here once cost an afternoon of
  production `JSONDecodeError`s that read like the engine returning an
  empty body;
- three of the four have an empty-response retry and voice does not.

Those are arguments, not variations, so a helper for the middle takes
the same shape as the four it replaces and earns much less. Recommended
next step is unchanged: extract nothing until the middle has a test of
its own in the way the other five did, and treat `use_stream` as the one
line that must keep a visible test on both sides of any change.

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
