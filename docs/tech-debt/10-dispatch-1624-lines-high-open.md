---
tech_debt: 10
marker: none yet
marker_kind: none
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
(b) Commands into `commands/`; `dispatch.py` keeps routing.
(c) `_safe_sender_label` into `sanitizer.py`. Naming note: "sanitisation"
here is display-name normalisation, not a security filter. Do not let a
future reader assume the latter and bolt filtering onto it.

**Depends on** item 8 being finished first, so the command handlers do
not move against a moving `call_llama` signature.