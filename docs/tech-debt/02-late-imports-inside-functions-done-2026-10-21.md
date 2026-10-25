---
tech_debt: 2
marker: tests/test_refactor_graph.py
marker_kind: tripwire   # the module-level import graph stays acyclic
state: closed
issue: null          # filled in when the Issue is created
---

## 2. ~~Late imports inside functions~~ ✅ DONE 2026-10-21

**Was.** 8 late `from dispatch import X` inside handlers.py function bodies.
The module-level graph had **zero** cycles — the cycle existed only because of
those eight lines. Everything they reached for turned out to be leaf code, so
it moved to `chat_utils.py` and `access.py` and handlers now imports nothing
from dispatch at runtime.

**Now.** The module-level import graph is acyclic. Pinned by
`test_refactor_graph.py`: no cycles, no late imports back into the old cycle,
leaf modules stay leaves. Verified that re-introducing one late import fails
three of those tests.

**One breakage worth remembering:** `test_dispatch_start.py` patched
`dispatch.is_authorized` to bypass the whitelist. That patch point stopped
working the instant the function moved, because it resolves the name in its
*own* module's globals — so the test would have kept passing while no longer
bypassing anything. That is worse than no test, and it is the shape a move
like this always takes.

---

<!-- current: handlers.py=1384 -->
