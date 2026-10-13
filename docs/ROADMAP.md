# docs/ROADMAP.md — strategic roadmap for the next sprint

> Multi-cycle plan. Updated 2026-10-13. Read this before
> the tactical backlog in `TODO.md` — the two documents
> complement each other (this one is "what and why", TODO
> is "how and when").

---

## Sprint scope (next 7-10 working days)

The operator estimates the full sprint at roughly one week
of focused work. Three tracks run mostly in sequence with
one critical dependency (track 2 must complete before track 3
can land). All four tracks share the same baseline: v0.10.2-
stable is released, v0.10.3 is the next tagged release.

### Track 1 — Tactical cleanup (T28-T31 + IndexError fix)

Low-risk items already in the backlog. Can be picked up in
any order, batched into a single PR, or split per-item.

- **T28:** Fix 2 pre-existing rating-parser subtest failures
  ("plain answer", "[[TYPE:unknown]]"). ~30 min.
- **T29:** ~~Remove `get_weather` (wttr.in) tool~~ **DONE 2026-10-13.**
- **T30:** Remove sender-name tagging. Bloats context,
  rarely useful in practice. ~45 min.
- **T31:** Archive `REVIEW-MINIMAX.md` and `-v2.md`. ~15 min.
- **T32:** Add `try/except IndexError` around empty-response
  paths in `call_llama.py` so the bot does not crash with
  "list index out of range" when a heavy prompt exhausts the
  context window or the LLM returns `choices: []`. ~20 min.

Total: ~2.5 hours. Land as `v0.10.3-stable`.

### Track 2 — Limit tuning for shell-tools (blocker for Track 3)

Shell-tools (Track 3) require the model to consume longer
contexts (multi-step shell output) and produce longer
replies (shell commands + their stdout). The current limits
were tuned for plain chat. Pre-flight work:

- Audit current context-window usage per handler
  (`handle_text`, `handle_photo`, `handle_voice`, `handle_document`)
  in `call_llama.py`.
- Profile max_tokens ceiling: current default is 2048; shell
  tools will need 4-8K for `cat /etc/passwd`-class output
  without truncation.
- Re-evaluate `top_p`, `temperature`, `repeat_penalty` for
  tool-calling reliability (shell commands are deterministic
  but argparse-style parsing needs stable outputs).
- Stress-test on Qwen3.8-27B-Ultra-Heretic-MTP-256k: how
  much of the 256k context does a 20-step shell session
  actually use? The model is supposed to handle it, but
  real-world RAG / shell output may blow the budget faster
  than the spec suggests.
- Decide on per-user context history depth. Current is
  20 messages. Shell sessions may need 50-100.

Total: ~1.5 days. Output: a documented limits policy in
`docs/CALL_LLAMA.md` and tuned `config.py` defaults.

### Track 3 — Shell tools (the main feature)

The headline feature of the next sprint. The bot grows the
ability to **execute shell commands inside a per-user
sandbox container**, scoped and short-lived.

- **Container lifecycle**: a **single shared sandbox**
  for the whole bot process, created at bot start, destroyed
  at bot restart. Per-sub-talk model context stays
  isolated (this is the existing design: each /sub has its
  own context window, /reset in one sub does not touch the
  others — operator wants to keep that property), but the
  shell workspace is shared. Path-jail in the dispatcher
  scopes each tool call to a per-sub working directory
  under `/workspace/<user>/<sub>`, so two concurrent
  contexts writing to the shell cannot collide. This
  keeps resource cost flat (1 container, not N) at the
  price of accepting that two LLM turns may race on the
  filesystem if both touch the same path. The race window
  is small and the consequences are recoverable (re-run
  the affected /sub).
- **Tool surface**: `shell_exec(cmd)`, `shell_read(path)`,
  `shell_write(path, content)`, `shell_list(dir)`, plus a
  sandboxed Python eval for numeric work.
- **Security model**:
  - Per-user UID mapping (no cross-user filesystem access)
  - No network by default (`network_mode: none`); explicit
    `--allow-net` flag for opt-in
  - Resource limits: 256 MB RAM, 50% CPU, 30 s wall clock
  - Drop all capabilities, no root, no `/proc` or `/sys`
  - Read-only rootfs except for `/workspace` bind-mount
  - Path jail: commands may only touch `/workspace/**`
    unless operator pre-authorises a path via env var
  - Audit log: every command, exit code, duration, timestamp
- **Tool dispatcher** mirrors the existing `web_search` /
  `web_fetch` donsetch pattern: tool definitions in
  `_TOOLS_CACHE`, system prompt exposes them, dispatcher
  routes by name.
- **Tests**: at least 10 selftest cases covering happy path,
  every error class, container-escape attempt, resource
  exhaustion, path-jail violation. Plus pytest covering the
  dispatcher and the audit log.
- **Operational**: systemd unit or compose profile for
  the single sandbox container; resource cleanup on bot
  restart; container health-check + auto-restart on
  failure (single bot == single sandbox == single
  failure domain — if the sandbox dies, the bot dies
  with it; respawn handled by systemd).

Total: ~3-4 days. This is the largest single piece of work
in the sprint. Land as `v0.11.0` (minor version bump
because the public tool surface changes).

### Track 4 — Multi-LLM arbitration in groups

The hardest item on the board, deliberately scheduled last.
In groups with multiple active LLMs (e.g., one per user, or
multiple tool personas), the bot needs to:

- Decide **which** LLM responds to a given user message
  (round-robin? topic-aware? user-scoped? explicit @-mention?)
- **Tune** per-LLM: each model has different strengths; the
  arbiter needs a per-message quality score or a hard
  routing rule.
- **Arbitrate conflicts** when two LLMs both want to reply
  (first-write-wins, score-merge, user-as-king, etc.)
- Surface the choice transparently: a header in the reply
  block (`[Qwen-3.8] [Ornith] ...`) so users can see who
  answered.
- Avoid N*N message amplification in busy groups.

This is more of a research spike than a feature ticket. The
operator expects 2-3 days of experimentation before settling
on a workable design. Land as `v0.12.0` or later, possibly
split into multiple smaller releases.

---

## Sequencing and dependencies

```
Track 1 (T28-T32)  ------>  v0.10.3-stable  (any order, batch ok)
                                 |
Track 2 (limits)    ------>  v0.10.4-rc (no tag)  --+
                                                     |
Track 3 (shell)    ------>  v0.11.0  <-------------+
                                                     |
Track 4 (multi-LLM) ----->  v0.12.0  (research spike, may slip)
```

Track 1 unblocks Track 2 by clearing the v0.10.x debt. Track 2
unblocks Track 3 by making the model ready for the longer
contexts shell tools require. Track 3 is the headline; Track 4
runs mostly in parallel but its design is informed by what
we learn from 3.

## Risks and unknowns

- **Track 2 / Qwen256k**: the spec says 256k context but we
  have not tested it with realistic shell output. If the
  model degrades above 32-64k, Track 3 needs a context
  summarisation layer (additional ~1 day of work).
- **Track 3 / container escape**: even with all the
  hardening above, a determined attacker may find a kernel
  CVE. Plan for a kernel-audit cycle on the sandbox host
  image before exposing shell tools to non-whitelisted users.
- **Track 4 / UX**: arbitration may feel inconsistent or
  "uncanny" to users. Plan for at least one user-test
  round before tagging a release.

## Out of scope this sprint

- Web-based UI for the bot (Telegram WebApp). Deferred to
  v0.13+.
- Voice cloning / TTS output. Currently text-only replies.
- Multi-tenant operator accounts (one operator can manage
  many bot instances). Deferred until commercial need.

## Tracking

The tactical backlog (T28-T32 + Track 3 subtasks) lives in
`docs/TODO.md`. The strategic view (this file) is updated
when a track completes or a direction changes.
