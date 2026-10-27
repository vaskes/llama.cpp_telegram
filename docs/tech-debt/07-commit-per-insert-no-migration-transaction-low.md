---
tech_debt: 7
marker: tests/test_t7_migration_atomic.py
marker_kind: tripwire
state: closed
issue: 5
---

## 7. `commit()` per INSERT, no migration transaction (LOW) — **CLOSED, PARTLY BY DECISION**

The item had two halves. One was a defect, one was a performance
trade-off wearing a defect's clothes. Closing it honestly means
recording which is which.

### Fixed — migration atomicity

`19355b2` wrapped `_migrate_v1_to_v2` so the six `ALTER TABLE ... RENAME
COLUMN` steps share one transaction. Previously a failure halfway left
a partially-migrated schema — `chat_id` renamed, `thread_id` not — with
a warning in the log and a bot that wrote into columns the rest of the
code no longer reads. That is the half that was a real defect, and it is
pinned by five tests in `tests/test_t7_migration_atomic.py`:

- a failure on step N rolls back steps 1..N-1, not just step N
- with nothing failing, all six land
- running it twice is a no-op, not an error
- the `add_column` steps share one transaction too
- the `executescript` comment describes what the code does

The last one exists because that comment was wrong at some point and
nothing noticed.

### Declined — batching writes

The other half was "every write commits". It still does, eight times
across `storage.py`, one commit per write.

Not doing it, and the reason has not changed: WAL is already on, the bot
writes a handful of rows per message, and batching would buy throughput
nobody is paying for while adding a second state machine around crash
recovery. Under real write concurrency SQLite can throw `database is
locked`; that is mitigated by WAL plus `check_same_thread=False`, and
batching does not change it.

**Trigger to revisit, unchanged:** a second generation of forks on the
same schema, or a migration that touches existing rows.

This is recorded as a decision rather than closed as "done", because
"no tech debt" should mean every item is either fixed or a deliberate,
trigger-backed choice — not that everything got built.

<!-- current: storage.py=886 -->
