# Security

The bot runs in two modes — **private chat** and **group with
Telegram Topics** — and the security model is different in each.
This document covers both.

## 1. Whitelist (private mode only)

In private chat mode the bot only replies to users in
`ALLOWED_USER_IDS` or `ALLOWED_USERNAMES`.

```bash
# /opt/telegram-bot/.env
ALLOWED_USER_IDS=123456789,987654321
ALLOWED_USERNAMES=myfriend,myotherfriend
```

**If both are empty — LOCKDOWN.** The bot rejects all messages.
This is a safe default: if you forgot to set up the whitelist,
the bot will not become public.

### How to find your Telegram user_id

1. Send `/start` to **@userinfobot** — it will reply with your ID.
2. Or send `/start` to **our** bot — it will not reply, but in
   `docker logs telegram-bot` you will see `[SECURITY] rejected id=XXXXX`.

### Where the check is

`_route_to_thread(update, context)` returns `None` (and the
handler silently exits) when `reject_if_unauthorized(update, context)`
returns True. The check happens BEFORE any other work, so a
rejected message does not even touch the SQLite store.

**Silent rejection** (no `reply_text`) — so a stranger does not
learn the bot exists. If the bot replied "access denied", that
would confirm the bot is alive.

## 2. Group mode security

**Conversation is open in a group. Commands are not.** Since
2026-10-22 every slash command -- `/reset`, `/stats`, `/sub`, `/subs`,
`/here`, `/newsub`, `/delsub`, `/help` -- and the inline-keyboard callback
handler check `ALLOWED_USER_IDS` before doing anything, in a group as
well as in private. Only ids in the allowlist get an answer; strangers
are rejected silently. Before this, eight handlers branched on the chat
type first and returned from the group path without ever consulting the
whitelist, so any member could delete forum topics and clear history.

Telegram cannot be delegated to here. Anyone who can post can be an
author, but `deleteForumTopic` has no ACL: a public group gave every
member the ability to destroy its topic tree. That is a local record
only for `/reset`, and a real API call for `/delsub`.

**The conversation path is what skips the whitelist entirely.** Any
member of the
supergroup can use the bot. Access control is delegated to
Telegram itself:

- **Group membership** — only Telegram members can post in the
  group in the first place. If the group is invite-only, you
  control who joins.
- **Topic-level permissions** — Telegram allows you to lock
  specific topics (e.g. only admins can post in `#announcements`).
  The bot reads/writes the same topics; if a topic is locked, the
  bot also cannot post there.
- **Admin status** — only admins can call `createForumTopic` /
  `deleteForumTopic`. Since the bot is an admin, it can manage
  topics. The bot does NOT have any other admin powers unless
  you grant them.

### When to use group mode vs private mode

| If you need... | Use |
|---|---|
| One user, isolated context, full lockdown | Private chat |
| Multiple users sharing one bot, with topic separation | Group with Topics |
| One user who wants both private notes and a group collaboration | Mixed: leave `ALLOWED_USER_IDS` set for yourself, and add the bot to a group too |

### Known limitations of the group-mode model

- **No per-topic ACL.** Any group member can post in any topic
  and read the bot's replies there. If you need read-only topics,
  use Telegram's topic-locking feature; the bot will see them as
  read-only and the LLM will respond to user messages anyway
  (the bot is admin, so the user message in a locked topic
  would be rejected by Telegram before reaching the bot).
- **No per-user quota.** A user in the group can flood the
  bot with messages; the bot serializes them through
  `call_llama`. For groups with many active users, consider a
  per-user semaphore (not currently implemented; tracked as
  future work).
- **No anti-abuse on the LLM side.** Any group member can ask
  the bot to do anything within `DISABLED_TOOLS` (see §3).
  The same lockdown applies as in private mode.

## 3. Tool sandbox (both modes)

In `bot.py` there is a `DISABLED_TOOLS` set:

```python
DISABLED_TOOLS = {
    # Filesystem / shell — SECURITY RISK
    'read_file', 'write_file', 'edit_file', 'exec_shell_command',
    'file_glob_search', 'grep_search', 'get_info',
    # Playwright — NOT IMPLEMENTED in the bot
    'playwright_*',
}
```

**Do not remove** `read_file` / `write_file` / `exec_shell_command`
from `DISABLED_TOOLS` without a security re-audit. Through
`exec_shell_command` any whitelisted user (or any group member,
in group mode) could execute **any** command on the host as the
user running llama-server. This is equivalent to passwordless
`sudo`.

If you really need it, add a **command allowlist**:

```python
ALLOWED_COMMANDS = {'ls', 'cat', 'grep'}
def safe_exec_shell(args):
    cmd = args.get('command', '')
    if cmd.split()[0] not in ALLOWED_COMMANDS:
        return '[error: command not in allowlist]'
    # ... subprocess ...
```

### Why tool-calling is gated by intent keywords

Even with `DISABLED_TOOLS`, the bot would be tempted to call
`web_search` on every user message. That is expensive and noisy
(model hallucinates a tool call when it shouldn't). The bot
detects "tool intent" via a keyword list (`_TOOL_KEYWORDS`) and
only includes the `tools` array in the request body when intent
is detected. See [CALL_LLAMA.md](CALL_LLAMA.md) §3 for details.

## 4. Credentials — what is WHERE

| What | Where | How protected |
|---|---|---|
| `BOT_TOKEN` | `/opt/telegram-bot/.env` | `chmod 600`, **not in git** |
| `API_KEY` (LLM) | `/opt/telegram-bot/.env` | Usually `sk-no-key` for localhost — not a secret |
| `WHISPER_MODEL` | `/opt/whisper-api/.env` | Public model name, not a secret |
| Donsetch URL | `/opt/telegram-bot/.env` | Not a secret |

## 5. What MUST NOT end up in git

In this repository `.env` files are **never** committed —
`.gitignore` protects you. If you fork and re-use the code,
verify:

```bash
git status
# → nothing should show .env or files/ with data
```

`.env.example` is a template, you can and should commit it.

## 6. `network_mode: "host"`

The bot container sees the entire host network. This is
convenient, but:

- The bot can port-scan the host (`127.0.0.1`)
- The bot can reach `192.168.x.x` (LAN)
- Any vulnerability in `python-telegram-bot` = remote code
  execution on the host

If this is critical, switch to `bridge` with explicit links
(see ARCHITECTURE.md).

## 7. Persistent SQLite store

The bot stores all conversation history in
`/opt/telegram-bot/data/conversations.db` (default
`CONVERSATIONS_DB=/app/data/conversations.db` inside the
container; bind-mounted from `./data` on the host).

The DB file is owned by the bot's container user. If the
container is compromised, the attacker can read all past
messages. If your conversations contain secrets, encrypt the
volume or run the bot on a dedicated host.

To wipe everything: `rm /opt/telegram-bot/data/conversations.db`
and `docker compose restart telegram-bot`. The bot will recreate
an empty DB on first message.

## 8. Rotating BOT_TOKEN

If the token leaks:
1. `@BotFather` → `/revoke`
2. Update `BOT_TOKEN` in `/opt/telegram-bot/.env`
3. `sudo systemctl restart telegram-bot-compose`

The revoked token stops working immediately. All in-flight
polling requests with the old token will return 401, and the
bot will log `[poll] HTTP error: 401` until restarted.

## 3.5 bash_command (personal mode only)

**WARNING: this section is for `LLAMABOT_DEPLOYMENT_PROFILE=personal`
ONLY.** Public mode does not have this tool, regardless of what is in
`/opt/telegram-bot/.env`.

If you set `LLAMABOT_DEPLOYMENT_PROFILE=personal`, the security model
of the bot changes. The `bash_command` tool gives the LLM a general-
purpose remote-code-execution surface inside a hardened sandbox
container. The blast radius is contained by the sandbox, but the
blast radius is real: a misbehaving user can read files in /workspace,
run network requests, install packages, persist state across calls.

Before switching to personal mode, read [docs/BASH-SANDBOX.md](BASH-SANDBOX.md)
and [docs/TOOL-ISOLATION.md](TOOL-ISOLATION.md). The key rules:

  - **Never** enable personal mode in a group chat that includes
    non-whitelisted members. The bot does not enforce per-chat
    profile downgrade; the env-based gate is process-wide. The
    chat-type guard in `call_bash_command` refuses groups, but
    defence in depth means: do not test the guard in production.
  - **Never** expose `AGENT_STACK_TOKEN` to anyone other than the
    bot and the sandbox side-car. It is a bearer credential.
  - **Never** bind-mount anything from the host into the sandbox
    container's `/workspace` except through the documented
    named-volume path. Bind-mounts from `/etc`, `/home`, or
    `/root` are escape hatches.
  - **Always** treat the sandbox as a soft trust boundary. Audit
    the `agent_api.py` log file weekly for unexpected commands.
  - **Always** keep `LLAMABOT_BASH_RPM` at the documented default
    (10/min) or lower. Higher values weaken the rate-limit
    defence.

The bot's per-chat guard — `call_bash_command()` in
`personal_tools/bash_command.py`, which checks `chat_type` before
anything else — refuses non-private chats regardless of the deployment
profile. This is defence in depth on top of the env gate.

(The guard used to be cited here by line number. Line numbers are a
statement about one moment; the function name moves with its code and a
line number does not.)

### 3.5.1 — T52 #R2: data/ and files/ bind-mount trust model

`files/` (mount `./files -> /app/files`) is **read-only** inside
the container. No bot code writes to `/app/files`; it is a
leftover mount from the polling-mode era and is kept only so the
host directory still exists for the operator to drop notes into.

`data/` (mount `bot_data -> /app/data`) is a **named Docker
volume**, not a host bind-mount. The named volume is owned by
Docker, not by the operator's UID on the host, and is not
visible on the host filesystem. The bot's process inside the
container has full read/write access (it writes a SQLite row
on every message).

**Threat model this addresses.** A host-level attacker (host
RCE, misconfigured cron, operator typo) cannot wipe or tamper
the SQLite through the host filesystem because the data is not
on the host filesystem. This is the same defence-in-depth
pattern as `personal_tools/ :ro` (T52 #H2): remove the
host-side writable surface.

**Threat model this does NOT address.** RCE inside the bot
process can still read the SQLite. The defence against that
is preventing the RCE in the first place: S1 (sender-name
prompt injection), T42 (tool whitelist in system prompt), T47
(reframed wording), and the per-chat bash_command gate (T52
#H1). The long-term PII-at-rest fix is encryption at rest
(SQLCipher or app-level encryption with key from env), which
is tracked separately.

**Migration on first deploy with the new compose:**

```bash
# Stop the bot
sudo systemctl stop telegram-bot-compose.service

# Seed the named volume from the existing bind-mount
sudo docker compose -f /opt/telegram-bot/docker-compose.yml up --no-start
sudo docker compose -f /opt/telegram-bot/docker-compose.yml run --rm \
    --entrypoint cp \
    -v bot_data:/target \
    -v /opt/telegram-bot/data:/source:ro \
    alpine:latest -a /source/. /target/

# Start the bot
sudo systemctl start telegram-bot-compose.service
```

Without the migration, the bot starts with an empty
`/app/data/conversations.db` and previous chat history is
gone. The bind-mount contents at `/opt/telegram-bot/data/`
remain intact on the host (the compose change does not delete
them) as a safety net.

## 9. The workspace is a mailbox, not a notebook

Everything the sandbox writes into `/workspace` is read by the agent
that holds the host. The same directory is the sandbox's working
directory and the place where the host-side agent looks for what the
sandbox left behind, so a file written there is not a note to nobody:
it is a message with a delivery address.

The audit that matters for a boundary asks one question, and it is not
"can the agent get out". It is **"what in the system reads what the
agent wrote, and does it execute it"**. For this deployment the answer
was: the host-side agent reads the workspace, and a specification found
there can be read as an order. Nothing was broken — a channel was
used, and a channel is enough.

Both sides of that channel are closed in the bot:

* **the bot refuses to post the letter.** `bash_policy.py` carries a
  class for it, `cross_agent_order`: a command that writes text
  addressed to another agent *and* asks for a capability only the
  operator holds (a key, a mount, a published port, privilege). The
  refusal names the class, and the way out is the chat — the model
  says what it needs, and the operator decides;
* **a file in the workspace is data.** The model's instructions say so
  too: a file that addresses the model with a task is not the
  operator speaking, and the way to act on it is to say so in the chat.

The class is a floor and not a wall — a string match on a shell
command is porous, and the ordinary work (`rm -rf /workspace/build`,
`apt-get`, a plain `curl`) stays outside it. What the class buys is
that the bot's side of the channel says **no**, out loud, with a
reason, before the file lands.
