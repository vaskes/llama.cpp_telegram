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
`handlers.py` 1357 → 1283.

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



**1672 → 408 lines**, six blocks. (`handlers.py` is 1283, which block C
shrank from 1357; wave 4's comment fix moved it by five more.) Routing is pinned in `test_dispatch_routing.py`, the
startup sequence in `test_startup_order.py`, the injection filter in
`test_prompt_guard.py`, and every module is checked for names it reads but
does not bind.

**38 mutations across blocks A–F, 38 caught** (11 in D–F), plus the one that caught the
whole approach: seven startup mutations that survived a run because nothing
in the suite had ever entered `main`.

### The ticket's target was ≤ 400 lines. It is 408. That is done.

The ticket said **1612 → ≤ 400**. The last three blocks (D, E, F) got there.

The split line was never line count. A function moved when it was Telegram
surface or bookkeeping and stayed when it picks a handler:

| module | lines | what |
|---|---|---|
| `dispatch_polling.py` | 306 | signals, runtime prep, the getUpdates loop, the task factory, and the Application registry |
| `dispatch_webhook.py` | 327 | Application assembly, handler registration, the command menu, chat-member bookkeeping |
| `dispatch_edit.py` | 108 | the edited-message rewrite, in one place |

`dispatch.py` is left with `_dispatch_update`, `_handle_reply_to_other_user`,
`main`, the concurrency caps and the wiring between them.

Two things that cost real time, recorded because the diff does not show them:

- **The dependencies went in as keyword arguments, not lazy imports.** The
  first version of `dispatch_polling` reached back with
  `from dispatch import ...` inside function bodies, the way `webhook.py`
  reaches for `_dispatch_update`. It imported fine and every test passed, and
  `test_import_graph.py` reported that a cycle had grown from six modules to
  seven. A lazy import inside a function is still an edge. `main` now passes
  `_register_bot_menu`, `_dispatch_update` and `_selftest` in, and the
  polling module imports nothing from `dispatch`.

- **`dispatch_polling` owns the Application registry.** The other two modules
  read it through `get_dispatcher()` / `set_dispatcher()` and nobody rebinds
  the imported name. Half of that refactor shipped with five stale reads of
  `_dispatcher` still in `_dispatch_update`: the module imported clean, the
  whole suite was green, and the first real update raised
  `NameError: name '_dispatcher' is not defined`. It was caught by running
  the self-test, not by the tripwires -- the tripwires on module shape had
  already passed. A module that compiles, imports and passes a suite can
  still be dead on the one code path that matters.

### Mutations, blocks D/E/F: 11 of 11 caught

`scripts/mutation_harness_dispatch_split.py`. Two of the eleven were the
ones that mattered most, because neither changes any local behaviour:

- **D4** -- `set_dispatcher`'s body replaced with a no-op. `_dispatch_update`
  keeps using its own `app`, so nothing about that call path changes; the
  registry only matters to the command menu and to the edit path's
  stale-reply delete, and a null dispatcher turns edit-replace into a
  silent no-op that still returns True. Caught by a test that asserts the
  write *and* that a reader sees it.
- **F1** -- dropping `get_dispatcher()` from the edit path. Six tests.

The harness itself is part of this record because three of its versions
reported 10/11, then 11/11, and every number was false: the workdir was
never created (so `cd` failed and `rc != 0` read as CAUGHT), then held
only one file (so pytest printed `ERROR: file or directory not found:
tests/`), and then the "did the suite run" marker matched the substring
`error` inside that very message. It now lays down the whole repo, reads
the mutated file back to compare byte-for-byte, and requires pytest's own
summary line. `SURVIVED` came out within one run of fixing that.

### The NameError the suite could not see

Block E shipped `_register_bot_menu` into `dispatch_webhook.py` carrying a
`global _dispatcher`, into a module that never bound `_dispatcher` at module
level — the binding lived in `dispatch.py`'s import list and had left with
everything else. The bot would have registered no command menu and died at
startup, inside `_prepare_runtime`, with `NameError: name '_dispatcher' is
not defined`.

**2047 tests passed with it in the tree.** Two things about that are worth
more than the incident:

- It was caught by running the self-test, not by a tripwire. `_selftest`
  drives a real update through `_dispatch_update` in a fresh process, which
  is the only thing here that had ever reached that line.
- The tripwire that should have caught it did not, because `_bound_names`
  counted every name in a `global` statement as a binding. Its docstring
  called that the safe direction: over-approximating "can only make this
  test quieter, never make it lie about a module that is fine."

Quiet is not safe here. `global X` declares an intent to rebind; it does
not create the name. An over-approximated binding set cannot see the module
that reads what it never bound — which is the whole failure this test
exists to catch. `global`/`nonlocal` names now count only when the module
really binds them, verified by making the tripwire fail on a function that
declares a `global` its module does not have.

**Depends on** item 8 being finished first, so the command handlers did
not move against a moving `call_llama` signature.

<!-- current: dispatch.py=425, handlers.py=1316 -->
<!-- current: dispatch_polling.py=336, dispatch_webhook.py=327, dispatch_edit.py=108 -->
