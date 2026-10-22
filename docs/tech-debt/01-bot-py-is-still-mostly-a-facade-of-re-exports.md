---
tech_debt: 1
marker: tests/test_facade_ownership.py
marker_kind: tripwire   # this test pins a closed fix; see the item header
state: closed
issue: 1          # filled in when the Issue is created
---

## 1. `bot.py` is still mostly a facade of re-exports — PARTIALLY DONE 2026-10-21

**I marked this DONE and it was not.** I had moved the 1894-line `_selftest()`
out and called the item closed on that basis. The 1894 lines were the easy
part; the facade was the actual item and it was still there.

**What actually landed:**

- `bot.py` 2245 → 124 lines.
- `_selftest()` moved to `selftest.py`, unchanged.
- Six dead duplicates deleted, after confirming nothing outside `bot.py` read
  them: `_SUBTALK_NAME_RE` and `_TOPIC_NAME_RE` (owner: `chat_utils.py`),
  `_LLM_TOKEN_RE` (owner: `prompts.py`), `_user_semaphores` and
  `_global_llm_sem` (owner: `state.py`), and `_detect_tool_intent`, whose own
  docstring said "removed next refactor" and which returned `True`
  unconditionally. Seven unused imports went with them.
- `_user_semaphores` / `_global_llm_sem` were not merely duplicated: they were
  *unwritable*. `state.py` rebinds its own module globals inside its accessors,
  so any holder of the imported name was holding a stale reference. The copy
  in `bot.py` could never have been the live one.

**What is still open, enumerated so "PARTIALLY DONE" is not mistaken for
"nearly done":**

1. **~15 `from X import (...)` blocks** (config, prompts, persistence, rating,
   call_llama, dispatch, handlers, state). Roughly 100 of the 124 lines.
2. **The entry point**: `python -u bot.py` in the compose command, plus
   `main()` under `__main__`.
3. **Fork compatibility**: `selftest.py`'s own docstring says forks patch
   `bot.BOT_COMMANDS`, so at least some of the re-exports are deliberate public
   API rather than residue.
4. **`bot.store`** — a module-level singleton captured at import time.

**Correction to my own earlier claim.** I wrote that the facade was
"load-bearing" because `dispatch.py` imports `bot` at module level. I did not
check. Nothing in `telegram-bot/` imports `bot` at module level at all, and the
module-level import graph has been acyclic since `b31bff3`. What remains is a
script that happens to re-export things for forks. `tests/
test_facade_ownership.py::test_bot_is_a_script_not_a_library` fails if a
module-level `import bot` reappears, so the claim cannot silently become true
again.

**Trigger to finish.** Before the next feature that adds a cross-cutting
module — this is the original trigger, unchanged. To finish: pick the public
API the forks actually depend on, move it behind an explicit `__all__`, and
repoint `selftest.py` and the test suite at the owning modules. Most of the
suite already does.

---

<!-- current: bot.py=124 -->
