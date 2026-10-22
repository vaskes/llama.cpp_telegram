---
tech_debt: 13
marker: tests/test_finally_hosts.py
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

**The remainder is safe to duplicate right now** and becomes worth
collapsing when `call_llama` has a signature stable enough to template
against. That is item 8's trigger, recorded in item 4 before this file
existed.

**What is pinned meanwhile.** `tests/test_finally_hosts.py` enforces the
invariant the wrapper duplicates most easily: every name a `finally`
reads must be bound before the `try` it guards. That rule has already
paid for itself — the test found, unprompted, that `handle_photo` and
`handle_voice` release the per-user slot as the *last* statement of the
`finally` rather than the first, so only the wrapped cleanup ahead of it
keeps the release reachable.
