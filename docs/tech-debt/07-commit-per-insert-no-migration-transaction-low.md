---
tech_debt: 7
marker: none -- needs a migration, not a guard
state: open
issue: null          # filled in when the Issue is created
---

## 7. `commit()` per INSERT, no migration transaction (LOW)

**What.** Every write commits. `_migrate_v1_to_v2` runs `ALTER TABLE` in a loop
with per-statement error handling that logs a warning and continues.

**Why it stays.** WAL is already on, and the bot writes a handful of rows per
message. Batching would buy throughput nobody needs and would cost a second
state machine around crash recovery.

**Cost.** Under real write concurrency SQLite can throw `database is locked`
(mitigated by WAL plus `check_same_thread=False`, not by batching). And a
migration that fails halfway leaves a partially-migrated schema with only a
warning in the log.

**Trigger to fix.** When the second generation of forks appears on the same
schema, or when a migration touching existing rows lands.

---

## Fixed, kept here as context

- **Bash rate-limit "default" bucket** — reported in the same review, did not
  reproduce. `session_id` is always `chat-{chat_id}` (T57 #S16), so the branch
  is unreachable. Recorded so nobody re-reports it.
- **WAL not enabled** — already on in `storage._conn()`.

---

## What is NOT tech debt

Tripwires. The AST tests look like scaffolding but they are load-bearing: two
of the bugs found in this block (`0b6684d`, `4cc6291`) would have been caught
by a human reading the code, and one of them was caught by a tripwire that
itself had to be rewritten to mean the right thing. They are cheaper than the
incidents they replace.
