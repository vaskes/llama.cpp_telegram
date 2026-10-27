---
tech_debt: 11
marker: tests/test_env_inventory.py
marker_kind: tripwire
state: closed
issue: null
---

## 11. `config.py` — env inventory (MEDIUM, CLOSED 2026-10-22)

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


---

## Closed, 2026-10-22

Not by adding the validation the item proposed -- failing at startup on
an unknown `LLAMABOT_` / `DONSETCH_` name would have been inert on 92%
of the surface, because only 3 of the 36 names carry either prefix.

What shipped is a two-way inventory between `.env.example` and what the
code reads, plus the 21 settings that were readable and undiscoverable,
now documented with their real defaults. Measured before: zero
documented-but-unread, 21 read-but-undocumented.

`tests/test_env_inventory.py`, 54 tests, four mutations caught. The
sharpest one is a typo at an *indirect* read site: `limits.py` reads a
dozen settings through `_env_or(env_name, ...)`, so a regex over
`os.environ.get` sees one name there instead of thirteen. A non-
recursive glob also hid `personal_tools/bash_command.py` and the whole
bash-sandbox subpackage until reverting it to a recursive one was tried
as a mutation.

<!-- current: config.py=460 -->
