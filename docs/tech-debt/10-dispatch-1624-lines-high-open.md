---
tech_debt: 10
marker: tests/test_dispatch_routing.py
marker_kind: tripwire
state: closed
issue: null
---

## 10. `dispatch.py` is 1624 lines (HIGH, closed)

**Measured 2026-10-22:** 1624 lines, 18 top-level definitions. Two are
oversized on their own — `main` at 316 lines, `cmd_callback` at 160.

**A review report noted it was "about to exceed 1000 lines".** It had
already passed 1600. The estimate was off by a factor of two, which is
worth recording: the report was written against a stale checkout, and
the claim looked plausible enough to be filed as a forecast.

**Closed inside this item, not deferred.** Two security defects that were
filed against this file have been dealt with rather than carried into
the extraction:

- *Group commands bypassing the allowlist.* `reject_if_unauthorized`
  sat after the group branch in eight handlers, so `/delsub` reached
  `deleteForumTopic` on a live forum without ever consulting
  `ALLOWED_USER_IDS`. Fixed in `35ff4fd`, admin-only in both modes, 16
  tests.
- *Duplicate `is_authorized`.* `dispatch.py` defined its own copy at
  line 85 while also importing the one from `access.py` at line 38 —
  AST-identical and dead. Worse than dead: `test_drift_t63_env_names.py`
  justified its own tripwire by claiming the dispatch attribute was "only
  a re-export", which was false, so a monkeypatch there would have been
  silently effective. Fixed in `c1ec4ad`.

**How to fix, in order.** (a) Both security items — done, above.
(b) Commands into `commands/` — done, block A.
(c) `_safe_sender_label` into `prompt_guard.py` — done, block C,
    and the ticket's naming note about it was wrong. Naming note: "sanitisation"
here is display-name normalisation, not a security filter. Do not let a
future reader assume the latter and bolt filtering onto it.

**Routing tripwire landed first — `tests/test_dispatch_routing.py`.**
The decomposition must not be allowed to break routing, because routing
has already cost production twice, and the tripwire has to exist while
the file is being moved.

Properties pinned, not the presence of a function:

- every delivery mode reaches `_dispatch_update` (webhook awaits it,
  polling goes through `_spawn_dispatch`);
- the edit rewrite runs **before** `Update.de_json`, not after;
- `Update.de_json` is called **exactly once** in the entry point;
- `edited_message` is gone from the dict afterwards, on both rewrite
  paths -- leaving it makes PTB route the same text twice;
- the rewrite preserves `message_thread_id`, which is the attribute
  whose absence produced the original `AttributeError`;
- an update that already carries `message` is left alone;
- no module other than `dispatch.py` and `webhook.py` calls `de_json`.

**10 mutations, 10 caught**, on a clean-tree preflight. Two things the
run changed rather than confirmed:

- the `de_json` census was a substring search, so it would have matched
  the word in a comment -- in the one file whose history is a comment
  that named the wrong place for years. It walks AST now.
- `webhook.py` calls `de_json` to *validate* before handing the dict on,
  which is a decision, not a route. The test allows exactly those two
  modules and says why, so a third one has to argue for itself.

**Block B — `main` is wiring again.** 269 → 78 lines. It decides
which delivery mode runs and wires shutdown to it; the rest is three
module-level functions: `_install_signal_handlers` (26),
`_prepare_runtime` (38), `_poll_loop` (164). `dispatch.py` 932 → 974 —
this block adds lines, on purpose. Three named functions beat one
larger one, and the split is what made the next paragraph possible.

**Seven mutations, seven caught — but only after the first run had all
seven survive.** The split made the code better-shaped and untested at
the same time, and 1899 passing tests did not notice the difference. A
green suite was measuring nothing: nothing in it reached the startup
path. `tests/test_startup_order.py` pins the order (handlers, then
runtime, then poll), the publication of `SHUTDOWN_EVENT`, both
signals, and the size of `main`.

**The signal handler was logging the signal and never setting the
event.** That one survived a run on its own — a bot that reports a
clean shutdown and then polls until `docker` kills it outright. It is
checked now, and the test says in its docstring that it is the only
assertion in that file about something *doing* rather than existing.

**`ast.parse` is not a syntax check.** Splitting `main` left three
`await` calls inside a plain `def`. `ast.parse` accepted it —
Python 3.11 parses it happily — while `compile()` and `py_compile`
reject it. Every refactor script here validates with `ast.parse`, which
means all of them would have shipped a module that cannot be imported.
`test_every_module_actually_compiles` now calls `compile()`.

**Block C — the ticket's own naming note was wrong, and it was
written to be obeyed.** It said to put `_safe_sender_label` into
`sanitizer.py`, and warned that "sanitisation here is display-name
normalisation, not a security filter -- do not let a future reader
assume the latter and bolt filtering onto it."

It is the latter. `git show 4eedfa7` -- the commit that introduced the
function, T55 in the wave-2 security review -- adds `unicodedata` and
the `system|assistant|user|human|ai|from|instruction` redaction regex
in the same breath as the function itself. It has been a prompt-injection
filter since it was written, and never anything else.

Executing that note as written would have caused the harm it meant to
prevent, just running the other way: a reader told it is cosmetic stops
reading the body, and a reader who takes "sanitizer" at face value looks
for the real filter elsewhere and is free to weaken this one. So it went
to `prompt_guard.py`, and the docstring says why in the first paragraph.
`handlers.py` 1357 → 1278.

**6 mutations, 6 caught**, and the point of the tripwire is not the
coverage -- it is that nobody may quietly reduce it. Removing the
character-category filter, the redaction, the case-insensitivity, the
length bound, making the redaction eat the rest of the message, and
widening it until Cyrillic and Latin names are eaten (which is how an
operator ends up disabling the control entirely): all six go red. The
old `test_s1_sender_sanitisation.py` catches most of them too; the new
file pins the payloads as payloads and checks the filter has exactly one
definition, because two copies drift and that is how the Application
builders drifted two blocks ago.

## Not verified: live edit-replace against a real user (manual only)

The edit-replace path -- user edits a message, the bot deletes its old
answer and recomputes -- has **never been observed running against live
Telegram traffic**. It is characterised by unit tests and by
`_apply_edited_message` exercising the rewritten update dict, and that
is the whole of the evidence.

This is not an oversight that a better test would fix. To see the bug
you need three things at once, and production has none of them:

1. **two people**, because you must edit your own message while the bot
   is mid-answer, so one person cannot stage it alone;
2. **a deliberately slow model**, because the window where
   `message_thread_id` is missing is the window between "user edited"
   and "bot finished answering" -- on a fast model it is already closed;
3. **an active thread**, since the original `AttributeError` was about
   `message_thread_id`, which is only present in a forum topic.

Production is the opposite: fast model, one user, topic traffic
happening. So this case will hang open indefinitely unless the bot is
deliberately run against a slow model with two people present. That is
the intended arrangement, not a gap someone forgot.

Consequences for how to read the tripwires: `test_dispatch_routing.py`
proves the rewrite runs before `de_json`, that `edited_message` is
removed, and that `message_thread_id` survives. It cannot prove the
Telegram client behaves the way the rewrite assumes, because that needs
a real update from the real API. Treat the live path as untested by
design, and do not read the green suite as evidence about it.



**1672 → 974 lines**, three blocks. (1278 is `handlers.py`, which
block C shrank from 1357.) Routing is pinned in
`test_dispatch_routing.py`, the startup sequence in
`test_startup_order.py`, the injection filter in `test_prompt_guard.py`,
and every module is checked for names it reads but does not bind.

**27 mutations across the three blocks, 27 caught**, plus the one that
caught the whole approach: seven startup mutations that survived a run
because nothing in the suite had ever entered `main`.

`dispatch.py` still owns the four things that are genuinely routing --
`_dispatch_update`, `_apply_edited_message`, `_spawn_dispatch`,
`_handle_chat_member_update` -- and the wiring between them.

**Depends on** item 8 being finished first, so the command handlers did
not move against a moving `call_llama` signature.

<!-- current: dispatch.py=974, handlers.py=1278 -->
