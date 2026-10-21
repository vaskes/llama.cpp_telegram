---
tech_debt: 5
marker: none -- 'print instead of logging' is a style item with no assertion that would fail if it regressed
state: open
issue: 3          # filled in when the Issue is created
---

## 5. `print()` instead of `logging` (MEDIUM)

**What.** Every module logs with `print(..., flush=True)`.

**Why it exists.** The compose file pipes stdout through `tee -a bot.log`, so
output lands somewhere, and the format is greppable.

**Cost.** No log levels, so the one line that matters cannot be turned up
without turning up everything. No structured fields, so correlating a failure
across the bot, the sandbox and the engine means grepping. On a busy group the
useful line is buried in thousands of identical shapes.

**Trigger to fix.** When log volume becomes a problem in practice — i.e. when
someone actually needs to filter. Doing it earlier means reformatting every
line and re-reading every `grep` in a runbook for no observed benefit.

**How to fix.** A `logging.getLogger(__name__)` shim that keeps `flush=True`
and the existing `[prefix]` text, so the migration is mechanical and the
existing greps keep working. Do it as one sweep, not incrementally — half
migrated is the worst state.

---
