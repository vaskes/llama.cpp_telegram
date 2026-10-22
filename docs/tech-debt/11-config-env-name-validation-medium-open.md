---
tech_debt: 11
marker: none yet
marker_kind: none
state: open
issue: null
---

## 11. `config.py` reads 24 env vars and validates one (MEDIUM, open)

**Measured 2026-10-22:** `telegram-bot/config.py` is 274 lines with 24
distinct env names read through `os.environ.get` / `os.getenv`. Exactly
one of them is checked: `BOT_TOKEN`, at line 18.

**The cost, concretely.** The other 23 fall back to a default on a
misspelled name. `LLMABOT_RPM_LIMIT` does not exist; `LLMABOT_BASH_RPM`
does. Typing the first gives a bot that silently runs with no rate
limit, and nothing in the logs distinguishes that from a healthy start.
`LLAMABOT_BASH_RPM=0` already means "disable" in the prose and "reject
everything" in the code — that one is filed against item 8's loop, not
here, but it is the same class.

**Why it is not just "add a check to each line".** The value is
knowing that the *name* is right. Per-line validation of the value does
not give that; a misspelled variable has no line to validate.

**How to fix.** Route all reads through one helper, and fail at startup
on any environment variable carrying the `LLAMABOT_` or `DONSETCH_`
prefix that the helper does not know about. Prefix-scoped, so operators
can keep unrelated variables in the same env file.

**Explicitly not in scope.** Typing, or restructuring into
`DatabaseConfig` / `LLMConfig` classes — cosmetic, and filed under
considered-and-rejected in the index.
