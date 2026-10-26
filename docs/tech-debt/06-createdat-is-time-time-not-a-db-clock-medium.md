---
tech_debt: 6
marker: tests/test_t6_db_clock.py
marker_kind: tripwire   # this test pins a closed fix; see the item header
state: closed
issue: 4          # filled in when the Issue is created
---

## 6. `created_at` is `time.time()`, not a DB clock (MEDIUM)

**What.** Timestamps come from the Python process clock.

**Why it stays.** Correct ordering today. `CURRENT_TIMESTAMP` would make
`context_budget`'s age filter depend on SQLite's clock and formatting
conventions, and would complicate the tests that pin window behaviour against a
faked Python clock — which is the whole reason the age tests are cheap.

**Cost.** An NTP step or a VM migration rewrites ordering. `ORDER BY id`
already exists as the stable tiebreaker, so the blast radius is the *age
filter*, not the history order: a backwards clock step would make recent
messages look a week old and drop them from the window.

**Trigger to fix.** If a clock incident is ever observed, or if the bot is
deployed somewhere with aggressive time correction. Until then it is a
theoretical risk with a real blast radius, which is the honest description.

**Mitigation available today:** `reanchor()` refuses to move the anchor
backwards in effect (a stale summary is kept rather than discarded precisely
because the anchor only moves forward), and `ContextBudget._keep` tolerates
unparseable timestamps by keeping the message.

---

<!-- current: storage.py=782 -->
