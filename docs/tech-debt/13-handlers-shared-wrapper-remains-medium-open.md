---
tech_debt: 13
marker: tests/test_llm_abort.py
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

**What remains.** The skeleton around the call is still four times over:
`_route_to_thread` → `_check_user_slot` → typing/thinking →
`_load_history` → rating dispatch → `call_llama` → persist/rollback
(`turn.py`, already shared) → reply. Extracting it now is a different
risk from what A and B were — those were leaf functions with no I/O and
one job each; the skeleton owns the handler's control flow, including
the three places where the handlers legitimately diverge (photo has no
`turn` to roll back, voice has no empty-response retry, text streams
while the other three do not).

**Recommended next step: a tripwire before any extraction.** Pin what
each handler's skeleton actually does, so a divergence that is already
intended stops being indistinguishable from one that is a bug. That is
the same order that worked for item 10.

**What is pinned meanwhile.** `tests/test_finally_hosts.py` enforces the
invariant the wrapper duplicates most easily: every name a `finally`
reads must be bound before the `try` it guards. That rule has already
paid for itself — the test found, unprompted, that `handle_photo` and
`handle_voice` release the per-user slot as the *last* statement of the
`finally` rather than the first, so only the wrapped cleanup ahead of it
keeps the release reachable.

## Markers

- `tests/test_finally_hosts.py` — the `finally` invariant below.
- `tests/test_llm_result.py` — block A: one unwrap for all four handlers,
  and the tripwire that had to be corrected twice before it could fire.
- `tests/test_llm_abort.py` — block B: the abort tail, the log wording,
  and the no-line-numbers check.
