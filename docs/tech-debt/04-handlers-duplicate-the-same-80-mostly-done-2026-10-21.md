---
tech_debt: 4
marker: tests/test_turn_ledger.py, tests/test_turn_scope.py
marker_kind: tripwire   # one ledger owns persist/rollback
state: closed
issue: null          # filled in when the Issue is created
---

## 4. ~~Handlers duplicate the same 80%~~ 🟡 MOSTLY DONE 2026-10-21

**Done.** The consequential half: the persist/rollback block existed eight
times (four handlers × abort path and exception path). `turn.py` owns it now.
Control flow untouched. 10 tests, mostly about failure *direction* — an undo
that does not run leaves half a conversation, so persisting sets the flag
before awaiting, and a failed undo logs at ERROR with its consequence spelled
out.

**Still duplicated.** The semaphore → history → abort_event → call_llama
scaffolding itself. `handle_text`, `handle_voice` and `handle_document` remain
structurally near-identical for ~120 lines each.

**Why not finished.** Finishing it means restructuring three 200-line handlers
into a single template function, which is real control-flow surgery — and the
one remaining benefit is readability, not correctness. The rollback, which is
where a mistake costs a user message, is already in one place.

**Trigger.** When item 3 lands and `call_llama` gains a stable call signature
worth templating against. Until then the duplication is bounded and the
dangerous part is extracted.

---

<!-- current: handlers.py=1318 -->
