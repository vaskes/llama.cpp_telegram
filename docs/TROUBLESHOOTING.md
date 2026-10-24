# Troubleshooting

## Bot is silent / only replies to `/start`

### 1. Check the whitelist
```bash
sudo docker logs telegram-bot --tail 200 | grep "whitelist\|LOCKDOWN\|rejected"
```

If `LOCKDOWN` — your `ALLOWED_USER_IDS` and `ALLOWED_USERNAMES` are empty.
Open `/opt/telegram-bot/.env` and add your user_id.

### 2. Check llama-server
```bash
curl -s -m 5 http://localhost:8080/health
# → {"status":"ok"}
```

If it does not reply, the bot is **not at fault**. Start llama-server (see
[vaskes/llama.cpp-rocm-780m](https://github.com/vaskes/llama.cpp-rocm-780m)).

## `httpx.ConnectError` in the logs

Typical causes:

### a) llama-server is not running
See above.

### b) IPv6 vs IPv4 (rare, but happens in docker)
There is a monkey-patch in `bot.py` that filters IPv6. If you see this
error after editing, verify the monkey-patch is **not** removed:

```python
import socket
_orig_getaddrinfo = socket.getaddrinfo
def _ipv4_only_getaddrinfo(host, *args, **kwargs):
    results = _orig_getaddrinfo(host, *args, **kwargs)
    family = kwargs.get('family', socket.AF_UNSPEC)
    if family == socket.AF_UNSPEC:
        return [r for r in results if r[0] == socket.AF_INET] or results
    return results
socket.getaddrinfo = _ipv4_only_getaddrinfo
```

### c) Bot token revoked / invalid
`@BotFather` → verify the token.

## Bot writes `❌ Error: ...`

```bash
sudo docker logs telegram-bot --tail 50
```

Common errors:

- `Connection refused` to `localhost:8080` — llama-server is on a different port or not running
- `name resolution failed` — DNS inside the container, set `network_mode: host`
  (default), or add `dns: [8.8.8.8]` in compose
- `Model not found` — the model in `MODEL` env does not match the `--alias` on the server

## donsetch-http is not responding

```bash
curl -s -m 5 http://localhost:8765/health
sudo -n docker logs donsetch-http --tail 20
```

Donsetch routes searches through real Chromium via Playwright. Common issues:
- Donsetch container not running: `sudo -n docker ps | grep donsetch`
- Chromium not installed inside the container (rebuild with Playwright deps)
- `DONSETCH_URL` in bot's `.env` is wrong (must end in `/mcp`,
  e.g. `http://localhost:8765/mcp`)

The bot falls back to plain `reply` if donsetch is unreachable, so users
still get answers to non-search questions.

## Whisper does not recognize voice

```bash
sudo docker logs whisper-api --tail 20
```

- Model is still downloading — first start pulls 1.5 GB, ~5-10 min
- Voice is too quiet / noisy — faster-whisper handles it poorly
- Language — `WHISPER_LANGUAGE` defaults to `ru` (in
  `telegram-bot/config.py`). If the voice is in English, recognition will
  be off. Set the env var to empty for autodetect, or to the right code
- Bot is on `lan_macvlan` but whisper is on the host network — the
  macvlan sub-interface cannot ARP its parent host, so the bot
  never reaches whisper. See "Networking between containers does
  not work" above for the macvlan fix.

## `docker compose build` fails with a pip error

```bash
# clear cache
sudo docker builder prune

# or explicitly pull a fresh python:3.11-slim
sudo docker pull python:3.11-slim
```

## Container does not start / goes into a restart loop

```bash
sudo docker logs telegram-bot --tail 100
```

If you see `Restarting` — likely an invalid `BOT_TOKEN` or llama-server
unreachable **at init time** (the bot does `get_me()` at startup).

## Networking between containers does not work

The bot is one Docker container. It needs to reach three other
endpoints: the LLM, Whisper (voice), and donsetch-http (web
tools). Which network mode you use depends on how those
services are deployed.

### Single host (simplest)

All services on the same host, all on host network. The bot
container uses network_mode: host and reaches everything
via localhost:

  network_mode: host
  environment:
    LLAMA_URL=http://localhost:8080/v1
    WHISPER_URL=http://localhost:8000
    DONSETCH_URL=http://localhost:8765/mcp

This is the path of least surprise for development. It is
NOT compatible with webhook mode if you want a reverse
proxy on a different container -- see below.

### Network layout (since 2026-10-13)

**Current layout:** the bot and whisper-api both run with
`network_mode: host`. No Docker bridge network, no static
container IPs, no macvlan. The bot binds to `0.0.0.0:8443`
on the host. The reverse proxy (nginx, also on host) proxies
HTTPS 443 -> aiohttp 8443 via `127.0.0.1` (or via the
host LAN IP if the proxy is on a different host).

**Why we dropped macvlan:** macvlan L2 isolation made the
bot unable to reach services on the same host (whisper,
the bash-sandbox side-car, and even llama-server when
bound to the host main IP) without elaborate PBR/DNAT
routing on the router. The macvlan sub-interface cannot
ARP the parent host, so any service on the host main IP
was unreachable from the bot container.

**For your reverse proxy:** point it at `127.0.0.1:8443`
if it runs on the same host, or at `<host-lan-ip>:8443` if
it runs on a different host on the same LAN. Both work
without any special routing.

**Historical (pre-2026-10-13):** before the host-network
switch, the bot was on a `lan_macvlan` Docker network with
a static IP (192.168.10.5). The reverse proxy reached it at
that IP. This worked when all other services were also on
the same macvlan, but broke the moment any service moved
to the host. See git history if you need to roll back; do
not start fresh with macvlan. See [docs/WEBHOOK.md](WEBHOOK.md)
section 4.3.1 for the production layout note.

**Symptom of a misconfigured network:** bot logs show
`httpx.ConnectError: All connection attempts failed` on
a target that is running. Fix by checking that:

1. The target service is bound to `0.0.0.0`, not just
   `127.0.0.1` or a specific interface IP.
2. The host firewall allows traffic on the target port
   from the host itself (loopback is usually open, but
   if you are reaching a remote host, the remote firewall
   must allow it).
3. If the target is on a different host, the URL in
   `.env` is the LAN IP of that host, not `localhost`.

### Verify connectivity

From the bot host (or any host on the same LAN):

  for name, host, port in [("llama", "<llama-host-lan-ip>", 8080),
                            ("whisper", "<whisper-host-lan-ip>", 8000),
                            ("donsetch", "<donsetch-host-lan-ip>", 8765),
                            ("sandbox", "<sandbox-host-lan-ip>", 8090)]:
      if timeout 3 bash -c "echo > /dev/tcp/$host/$port" 2>/dev/null; then
          echo "$name OK"
      else
          echo "$name FAIL"
      fi

For an in-container test (use `network_mode: host` so the
container sees the host network namespace; otherwise exec
into a side-car container that does):

  docker exec telegram-bot python3 -c "
  import socket
  for name, host, port in [("llama","<llama-host-lan-ip>",8080),
                            ("whisper","<whisper-host-lan-ip>",8000),
                            ("donsetch","<donsetch-host-lan-ip>",8765),
                            ("sandbox","<sandbox-host-lan-ip>",8090)]:
      try:
          s = socket.create_connection((host, port), timeout=3); s.close()
          print(f"{name} OK")
      except Exception as e:
          print(f"{name} FAIL: {e}")
  "



## System limits

If the bot stops replying after N messages:

```bash
ulimit -n          # fd limit
df -h /opt         # disk
free -h            # memory
```

A llama-server with a large model and 8K context can eat all RAM. Watch
`MEM%` in `docker stats`.

---

## What arrived after v0.10.2

Everything below landed after `v0.10.2` (2026-10-12). If the bot does
something this guide does not describe above, start here.

### Context memory: the window, the anchor, and `/stats`

The bot does not send "the last 20 messages" to the model. It has a
**context policy** (`telegram-bot/context_budget.py`) with two
strategies:

| `CONTEXT_STRATEGY` | what the model sees |
|---|---|
| `window` (default) | the messages inside the window; the older part is left out |
| `summarize` | the same window, plus chunk summaries of the older part |

The knobs (all described in `telegram-bot/.env.example`):

| variable | default | meaning |
|---|---|---|
| `BOT_REMEMBER_DAYS` | `7` | how many days back the window reaches |
| `CONTEXT_MAX_MESSAGES` | `400` | ceiling on the messages in the window |
| `CONTEXT_STRATEGY` | `window` | `window` or `summarize` |
| `SUMMARY_TAIL_TOKENS` | `65536` | how much never-summarised old history may still be loaded raw |
| `SUMMARY_CHUNK_TOKENS` | `65536` | how much old history goes into ONE summarisation call |
| `SUMMARY_CONTEXT_TOKENS` | `131072` | ceiling on the old part of the prompt as a whole |

**"The bot forgot" is usually the window, not a bug.** The anchor is
computed once, when the bot starts, and it does **not** slide:
`/stats` says `окно памяти: 7 дн. от старта бота (якорь не скользит)`.
A conversation that began eight days ago has nothing older than seven
days in it until the next re-anchor.

Ask the bot what it sees:

    /stats

It prints what is in the context now, what is in the database, how much
is older than the window, the window itself, and the strategy. In
`summarize` mode it also prints how many old messages are already
compressed into summaries (`сведено в сводки: N в K сводках`).

`CONTEXT_MESSAGES` is the old knob and is still read for backward
compatibility, but `CONTEXT_MAX_MESSAGES` is the one that counts.

### The old part of the history and the "СВЕДЕНО" marker

In `summarize` mode the older part of the history is compressed **in
chunks**, one summarisation call per chunk, and every chunk is marked in
the database with a link to the summary that replaced it. A chunk is
summarised **once in its life** -- the marker is what makes that true.

What that means in practice:

- a message covered by a summary is **not** also loaded raw; the summary
  stands where the chunk was;
- a chunk whose summarisation failed stays unmarked, is loaded raw, and
  is retried at the next re-anchor;
- if a summary row is deleted, the messages it covered come back raw.
  Nothing is lost, and nothing is silently dropped.

To look at the state:

```bash
docker exec telegram-bot sqlite3 /app/data/conversations.db \
  "SELECT count(*), sum(n_messages) FROM summaries;"
docker exec telegram-bot sqlite3 /app/data/conversations.db \
  "SELECT count(*) FROM messages WHERE summary_id IS NOT NULL;"
```

The summarisation path has not been exercised against a live engine in
this deployment yet -- see `docs/TODO.md` for the live checklist. Until
that happens, `CONTEXT_STRATEGY=window` is the safe setting.

### The daily re-anchor: `soft`, `process`, `process+engine`

The anchor only moves when the bot re-anchors, and how it does that
depends on what the operator is allowed to do
(`docs/adr/ADR-0003-context-reanchoring.md`):

| `BOT_REANCHOR_MODE` | what happens at `BOT_REANCHOR_AT` | what it needs |
|---|---|---|
| `soft` | the anchor moves inside the running process | nothing; no root, no systemd, no engine access |
| `process` | the process is restarted, so the anchor is recomputed at import | a supervisor (systemd timer, cron) that restarts the bot |
| `process+engine` | the same, and the engine's KV cache is dropped afterwards | the same, plus the right to run `BOT_ENGINE_RESET_CMD` |

| variable | default | meaning |
|---|---|---|
| `BOT_REANCHOR_MODE` | `process+engine` | the mode above |
| `BOT_REANCHOR_AT` | `05:00` | local time of the daily re-anchor, 24h |
| `BOT_REANCHOR_TZ` | `TZ` | the timezone `BOT_REANCHOR_AT` is read in |
| `BOT_ENGINE_RESET_CMD` | empty | shell command that drops the engine's KV cache |

**Known defect (T68b): `BOT_ENGINE_RESET_CMD` never runs.** The engine
reset is called from the in-process re-anchor loop, and that loop only
starts in `soft` mode -- which is exactly the mode that must not touch
the engine. In `process+engine` there is no in-process loop at all. The
consequence is narrow: the anchor moves and the bot restarts, but the
engine keeps its KV cache for context that has already fallen out of the
window. It costs VRAM, not correctness. The fix is an operator decision
(in-process task, or the command moved to cron/systemd) and is tracked
in `docs/TODO.md`.

If the re-anchor did not happen at the expected time, look for:

```bash
sudo docker logs telegram-bot --tail 500 | grep -i "reanchor\|anchor"
```

### The persona file

The bot can speak with a persona, and the persona lives in a file, not
in the code:

| variable | default | meaning |
|---|---|---|
| `BOT_PERSONA_FILE` | `persona.local.json` next to the bot | the file to read |

```bash
# start from the shipped example
cp telegram-bot/personality.json.example telegram-bot/persona.local.json
$EDITOR telegram-bot/persona.local.json
```

Three things to know:

- **`enabled: false` means no persona at all.** That is the correct
  state for a bot nobody configured; it is not a default personality.
- **Missing fields are omitted, never invented.** If the file does not
  say how old the persona is, the prompt does not mention an age. A
  model left to fill the gap invents a biography, and that invention
  becomes "the persona" within a few turns.
- The file is **gitignored** on purpose (`persona.local.json`), so a
  fork never inherits somebody else's character. The example
  (`telegram-bot/personality.json.example`) ships with
  `enabled=false`.

"the bot answers in a strange voice" and "the persona is ignored" are
both usually this file: it is missing, it is not where `BOT_PERSONA_FILE`
points, or `enabled` is still `false`.

### The dispatch split

`dispatch.py` used to be one 1600-line file that did routing, the
polling loop, the webhook, the PTB setup and the group-mode rules. It
is now a set of modules with one owner each:

| module | owns |
|---|---|
| `dispatch.py` | routing, lifecycle, concurrency, the update dispatcher |
| `dispatch_polling.py` | the `getUpdates` loop and the signal handlers |
| `dispatch_webhook.py` | PTB application assembly, the bot menu, chat-member bookkeeping |
| `dispatch_group.py` | the human-to-human absorb in a group |
| `handlers.py` | the four message handlers (text, photo, voice, document) |
| `commands/` | the slash commands |

So a symptom that used to be "look in dispatch.py" now has a home:
a lost update is in `dispatch.py`, a webhook that never fires is in
`dispatch_webhook.py`, a group message the bot ignored is in
`dispatch_group.py`, and the answer itself is in `handlers.py`.

**Edited messages are ignored.** The edit-replace feature (rewrite the
old answer when the question is edited) was removed on 2026-10-24: it
needed two people, a deliberately slow model and a live Telegram, and
the operator decided that waiting for the answer to the wrong question
and asking again was better. The bot now drops an update that carries
only `edited_message`, and the log says so:

```
[dispatch] ignoring edited_message update_id=...
```

`allowed_updates` lives on **Telegram's** side: it survives a bot
restart and only changes when the webhook is registered again. The bot
does not rely on it -- there is an explicit guard in the dispatcher --
so a stale `edited_message` in `getWebhookInfo` is harmless, but
re-registering the webhook with the current list keeps the two honest.

### Answers that arrive as documents

Since 2026-10-24 a code block in an answer is delivered as a **file**,
not as text in the chat: the chat gets the prose, and each fenced block
becomes its own document with a caption `📄 <name>`.

- the file name comes from the heading before the block, or from a
  comment in the first line (`// greet.js`, `-- cradle.sql`); without
  one the file is `answer.<ext>`, or `answer-N.<ext>` for the second
  and later blocks;
- the extension comes from the language of the fence; an unknown or
  missing language means `.txt`;
- the bot's own tool trace and the reasoning tail of a diagnostic are
  **not** files;
- the bot remembers the whole answer, code included, so a follow-up
  ("поменяй в нём цвет") still knows what it sent. The chat and the
  history are two different strings on purpose.

If a file did not arrive: check the log for `[send_files]`, and check
that the answer really had a fenced block -- a raw document with no
fence is not extracted. If the file arrived in General instead of the
forum topic, the topic id was not carried through; that path is
`llm_files.py`.

### The Stop button

The Stop button stops **the turn the button belongs to**, and only the
author of that message may press it (maker-taker). Since 2026-10-23 the
allowlist is **not** part of that decision: the button used to be
rejected for anyone outside `ALLOWED_USER_IDS`, so in a group it did
nothing for ordinary members and the model kept generating.

If the Stop button does nothing:

```bash
sudo docker logs telegram-bot --tail 200 | grep -i "callback\|abort"
```

- `[SECURITY] rejected ... msg=''` means the update never reached the
  handler -- that is an authorisation problem, not a Stop problem;
- `no abort_event for ...` means the turn had already finished;
- `abort_event set` means the turn was stopped.

### "The model thinks and never answers"

This is two budgets, not one: the engine's thinking budget and the
bot's `max_tokens`. If they are equal, the thinking eats the whole
allowance and the answer has nowhere to go -- the user sees a
diagnostic instead of a reply.

The bot's caps come from the deployment profile
(`LLAMABOT_DEPLOYMENT_PROFILE`): `public` (conservative), `personal`
(aggressive), `dev` (the operator's). The engine's thinking budget is
`--reasoning-budget` on the llama-server unit, and it must be **below**
the bot's `max_tokens`. `docs/CALL_LLAMA.md` has the numbers and the
arithmetic.

---

## Group mode (Telegram Topics) issues

### `/newsub <name>` says "Created sub-talk" instead of "Created topic"

This is a **stale toast from a prior bot version** (before commit
`326ca48`). The current code branches on `chat.is_forum` and creates a
real Telegram topic in group mode. The old "Created sub-talk" string
came from the private-mode fallback that was incorrectly triggered
when `message_thread_id is None` (which is the case for the General
topic of every forum-enabled supergroup).

To clean up the chat: long-press the old bot message → Delete.

### `/newsub Болталка` rejected with "Name rules..."

Old bot version (pre-`326ca48`). Names like Cyrillic / emoji are
allowed by the current `^[^\s]{1,128}$` regex. Update the bot
(`cd llama.cpp_telegram && git pull && sudo ./scripts/update.sh`).

### `/delsub <name>` returns "❌ No topic named ..."

The `known_topics` table only has topics **created via `/newsub`**.
Topics created manually in the Telegram UI, or by another bot,
are not in the table.

Options:
  - Use the Telegram UI to delete: open the topic → tap the topic
    name in the header → "Delete topic".
  - Re-create via `/newsub <same name>`. The new topic will be
    tracked. Then delete the old one via UI.
  - Try `/delsub <numeric_id>` if you happen to know the
    `message_thread_id` (visible in the URL of a topic in
    Telegram web).

### Bot in group, but `/subs` shows nothing even after `/newsub X`

Two possibilities:

  - The container was restarted AFTER the topic was created
    but BEFORE commit `0dce580`. Pre-`0dce580` had no
    `known_topics` table; topics created then were not
    persisted. Solution: delete the topic via UI, recreate
    via `/newsub`.

  - The `data/conversations.db` bind-mount on the host is
    wrong (the bot's data dir is not actually bind-mounted
    from the host). Verify:
    ```bash
    docker exec telegram-bot ls -la /app/data/
    # → conversations.db, etc.
    ```
    If empty: check `docker-compose.yml` for the
    `./data:/app/data` line under the bot service.

### Bot crashes on every command with `AttributeError: 'NoneType'`

Old bot version (pre-`cf96920`). It was triggered when the user edited
a command message: Telegram sends an `edited_message` update where
`update.message is None`, and the old code dereferenced it.

Since 2026-10-24 the edit-replace feature is gone entirely, and an
update that carries only `edited_message` is dropped with a log line
(`[dispatch] ignoring edited_message ...`) instead of crashing. The
answer to the original message stands; a correction has to be sent as
a new message.

### `is_forum=True` in the log but bot still uses private-mode strings

`is_forum` is the **chat** attribute (set by Telegram when the
group is a forum-enabled supergroup). It does NOT change per
message. If you see `is_forum=True` but the bot says "Created
sub-talk", the bot is running an old build. Pull, rebuild, restart:

```bash
cd /opt/llama.cpp_telegram
git pull
sudo ./scripts/update.sh
```

### Topic `delete_forum_topic` returns 400 "Topic not found"

You tried to delete a topic that the bot never created (or that
was already deleted). The `known_topics` table still has the row;
clean it up by deleting the row directly:

```bash
docker exec telegram-bot sqlite3 /app/data/conversations.db \
    "DELETE FROM known_topics WHERE message_thread_id = <id>"
```

### Topic `create_forum_topic` returns 400 "Topics must be first enabled"

The group is not (yet) a forum-enabled supergroup. Settings:
1. Convert group to supergroup if it's still a basic group
   (Group Info → Edit → "Chat Type" → Public/Private)
2. Settings → Topics → Enable
3. Re-promote the bot to admin with "Manage Topics"

### Bot cannot delete a message: "message can't be deleted"

The bot can only delete messages that are < 48 hours old
(Telegram's limit on `deleteMessage` for non-service messages).
For older messages: delete via UI.

### Bot doesn't reply to my message in a group

The group-mode noise filter suppresses the bot's reply when
the message is from another Telegram bot OR contains `[llm]`
anywhere in the text or caption. The pinned welcome message
in the group uses `[llm]` as the convention for active LLM
participants — they are expected to mark their replies, and
the bot stays silent on marked messages to avoid
double-answering.

If you (a human) typed `[llm]` by accident, just send a new
message without the marker.

The dispatch path logs every suppression:
```
[dispatch] muting group-mode message from @<username>
```
or
```
[dispatch] muting group-mode message from id=<n>
```
so you can verify the filter is doing what you expect.

In private mode the `[llm]` filter is disabled — there is
no other LLM to yield to, so typing `[llm]` in a private
chat does not affect the bot.
