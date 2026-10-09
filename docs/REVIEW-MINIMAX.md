# Code review — `vaskes/llama.cpp_telegram`

## What was reviewed

`https://github.com/vaskes/llama.cpp_telegram` @ `eab99ba` (HEAD at the time).
~2 600 lines total; `bot.py` — 1 409 lines (monolith).
The full review with priorities P0–P3 is in
[`54b7b32c836892a5/pasted-text.txt`](../attachments/54b7b32c836892a5/pasted-text.txt)
in this workspace (kept for the record).

## What was already fixed at `eab99ba` (do not re-do)

- PII masking in logs (no more `raw[:300]` printing base64 images)
- Graceful shutdown via `asyncio.Event`
- Retry for `fetch_tools_from_llama` (1s/2s/4s)
- Username-only whitelist warning at startup

## What this review wave fixed (commits after `eab99ba`)

| # | Severity | Commit | What |
|---|----------|--------|------|
| P0-1 | blocking | fix/start-sxsng-to-donsetch | `/start` reply no longer lies about SearXNG |
| P0-2 | blocking | fix/document-user-text | `handle_document` now passes `caption` as `user_text` (tool calls were impossible) |
| P0-3 | blocking | fix/drop-pending | `drop_pending` removed entirely (was a no-op in LOCKDOWN and a UX bug otherwise) |
| P0-4 | blocking | fix/voice-transcript-parse-mode | voice transcript reply uses `parse_mode=None` (Markdown was breaking on `*` / `_` / `[`) |
| P1-1 | serious | fix/shutdown-in-call-llama | `SHUTDOWN_EVENT` global, checked at start of every `call_llama` iteration; docker stop 10s SIGKILL grace respected |
| P1-2 | serious | chore/license-file | MIT `LICENSE` added (Copyright 2026 Kuduza Ai Lab) |
| P1-3 | serious | chore/remove-dead-code | dropped `import re`, `_dispatcher_lock`, `_noop_async` |
| P2-1 | medium | security/non-root-user | `USER bot` in Dockerfile, chowns `/app` |
| P2-2 | medium | fix/size-limits | `MAX_PHOTO_BYTES=10 MB`, `MAX_DOC_BYTES=5 MB` (env-overridable), reject before download |
| P2-7 | medium | style/operator-precedence | parens on donsetch 404 / session-error check |
| P2-9 | medium | chore/pythonunbuffered-in-dockerfile | `ENV PYTHONUNBUFFERED=1` in Dockerfile (compose already had it) |
| P3-3 | low | feat/update-sh-whisper | `update.sh` now also refreshes `whisper-api/docker-compose.yml` |
| P3-4 | low | docs/call-llama-docstring | docstring expanded with "do not refactor without reading CALL_LLAMA.md" note |
| P3-6 | low | ci/ruff-workflow | `.github/workflows/lint.yml` with `astral-sh/ruff-action@v1` |
| P3-7 | low | fix/app-shutdown | `_dispatcher.shutdown()` in `_run` finally (no more "unclosed client" warning) |

## What I disagreed with and skipped

- **P1-5** (improve `/start` with explicit keyword hints) — would teach users
  the magic words to make tool-calling work, which is an anti-pattern. The
  correct fix is the opposite: pass `tools` by default and let the model
  decide. That's a bigger refactor; left as TODO in `CALL_LLAMA.md §2`.
- **P2-3** (selftest broken under LOCKDOWN) — proposed an env-driven
  `SELFTEST_USER_ID`; I instead made `_selftest` write into its own
  conversation key (`selftest:`) so it doesn't go through
  `reject_if_unauthorized` at all. Smoke test should test **infrastructure**,
  not **authorization**.
- **P3-1** (`EMPTY_RESPONSE_FALLBACK` string leaks into history) — minor
  UX, the marker is small and not actively harmful. Left as-is.
- **P3-2** (O(n²) string concat on `accumulated_reasoning`) — premature
  optimisation, not latency-critical. Left as-is.

## What the review missed (my own additions)

- **`PYTHONUNBUFFERED` in Dockerfile** (P2-9) — caught it but only after
  the fact; this is the kind of thing that should be in every Python
  Dockerfile by default.
- **Streaming-fallback `r = None` pattern** (P3-8) — same bug as the
  non-streaming path, in the streaming fallback at L670. Worth fixing
  but not done in this wave; would require touching the streaming-fallback
  block that is also out of scope per the review's "do not refactor".
- **README "MIT" without a LICENSE file** — caught as P1-2; not flagged
  as also a permission notice issue (the LICENSE file content matches
  SPDX MIT and includes the copyright line).
- **IMAGE_MIN_TOKENS on llama-server** — not in the review; this is a
  llama-server-side knob, not a bot.py issue, and lives in the
  separate `vaskes/llama.cpp-rocm-780m` repo.

## Open questions for follow-up

- `STREAMING FALLBACK` block (~L670) still has the `r = None` issue
  flagged in P3-8; cleanup is left undone because the block sits inside
  the streaming path which the review asked us not to refactor.
- Conversation trimming `conversations[user_id] = conversations[user_id][-20:]`
  may drop the system-prompt slot; in practice the system prompt is
  rebuilt inside `call_llama`, not stored in the user conversation, so
  this is safe today, but fragile if someone moves the system prompt
  into `conversations[user_id]`.
- No rate limiting: a whitelisted user spamming 10 messages in a row
  starts 10 parallel `call_llama` invocations through the polling loop's
  serial `await`. Currently fine for 2-user whitelist; will need a
  per-user semaphore if the whitelist grows.
