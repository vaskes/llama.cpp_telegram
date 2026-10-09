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

**Group mode skips the whitelist entirely.** Any member of the
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
