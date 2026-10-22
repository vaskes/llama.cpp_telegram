---
tech_debt: 10
marker: tests/test_dispatch_routing.py
marker_kind: tripwire
state: open
issue: null
---

## 10. `dispatch.py` is 1624 lines (HIGH, open)

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
(c) `_safe_sender_label` into `sanitizer.py` — still to do. Naming note: "sanitisation"
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

**Depends on** item 8 being finished first, so the command handlers do
not move against a moving `call_llama` signature.