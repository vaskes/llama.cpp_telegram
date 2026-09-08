# Security

## Whitelist (the main thing)

The bot only replies to users in `ALLOWED_USER_IDS` or `ALLOWED_USERNAMES`.

```bash
# /opt/telegram-bot/.env
ALLOWED_USER_IDS=123456789,987654321
ALLOWED_USERNAMES=myfriend,myotherfriend
```

**If both are empty — LOCKDOWN.** The bot rejects all messages. This is
a safe default: if you forgot to set up the whitelist, the bot will not
become public.

### How to find your Telegram user_id

1. Send `/start` to **@userinfobot** — it will reply with your ID.
2. Or send `/start` to **our** bot — it will not reply, but in
   `docker logs telegram-bot` you will see `[SECURITY] rejected id=XXXXX`.

## Credentials — what is WHERE

| What | Where | How protected |
|---|---|---|
| `BOT_TOKEN` | `/opt/telegram-bot/.env` | `chmod 600`, **not in git** |
| `API_KEY` (LLM) | `/opt/telegram-bot/.env` | Usually `sk-no-key` for localhost — not a secret |
| `WHISPER_MODEL` | `/opt/whisper-api/.env` | Public model name, not a secret |
| SearXNG URL | `/opt/telegram-bot/.env` | Not a secret |

## What MUST NOT end up in git

In this repository `.env` files are **never** committed — `.gitignore`
protects you. If you fork and re-use the code, verify:

```bash
git status
# → nothing should show .env or files/ with data
```

`.env.example` is a template, you can and should commit it.

## Sandbox tools

In `bot.py` there is a `DISABLED_TOOLS` set. Currently:

```python
DISABLED_TOOLS = {
    # Filesystem / shell — SECURITY RISK
    'read_file', 'write_file', 'edit_file', 'exec_shell_command',
    'file_glob_search', 'grep_search', 'get_info',
    # Playwright — NOT IMPLEMENTED in the bot
    'playwright_*',
}
```

**Do not remove** `read_file` / `write_file` / `exec_shell_command` from
`DISABLED_TOOLS` without a security re-audit. Through `exec_shell_command`
any whitelisted user could execute **any** command on the host as the user
running llama-server. This is equivalent to passwordless `sudo`.

If you really need it, add a **command allowlist**:

```python
ALLOWED_COMMANDS = {'ls', 'cat', 'grep'}
def safe_exec_shell(args):
    cmd = args.get('command', '')
    if cmd.split()[0] not in ALLOWED_COMMANDS:
        return '[error: command not in allowlist]'
    # ... subprocess ...
```

## `network_mode: "host"`

The bot container sees the entire host network. This is convenient, but:

- The bot can port-scan the host (`127.0.0.1`)
- The bot can reach `192.168.x.x` (LAN)
- Any vulnerability in `python-telegram-bot` = remote code execution on the host

If this is critical, switch to `bridge` with explicit links (see
ARCHITECTURE.md).

## `RestrictToSpecificChats`

If the bot runs in a group rather than in private chat, add a check:

```python
ALLOWED_CHAT_IDS = {int(x) for x in os.environ.get('ALLOWED_CHAT_IDS','').split(',') if x}

def is_authorized(update):
    ...
    if update.effective_chat.id not in ALLOWED_CHAT_IDS:
        return False
    ...
```

Currently the bot **assumes** private-chat operation. For groups, this
extra filter is required.

## Rotating BOT_TOKEN

If the token leaks:
1. `@BotFather` → `/revoke`
2. Update `BOT_TOKEN` in `/opt/telegram-bot/.env`
3. `sudo systemctl restart telegram-bot-compose`
