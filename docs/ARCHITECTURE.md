# Architecture

## bot.py — what's inside

### Two code paths in `call_llama`

The function splits internally into a **streaming** path (text, voice,
document) and a **non-streaming** path (vision). They share only the
function signature and the return contract. See
[CALL_LLAMA.md](CALL_LLAMA.md) §1 for the full design.

### Tool-calling loop (streaming path only)

The bot **runs its own** tool-calling loop (up to 15 iterations, with
a wall-clock cap of 600 s, an identical-call detector, and a
hallucination abort). This is because llama-server with
`--mcp-servers-config` only loads the **list of tools**, but does
**not** execute them — that is the client's job (us).

The cycle:

```
1. Detect tool intent from user_text (see §2 in CALL_LLAMA.md)
2. If intent: req_tools = all_tools; else req_tools = None
3. POST /v1/chat/completions with messages + (optional) tools
4. If finish_reason == "tool_calls" AND req_tools:
     a. For each tool_call:
        - parse name + arguments
        - execute (HTTP to wttr.in / donsetch-http MCP)
        - append to messages: {role: "tool", tool_call_id, content}
     b. goto 3
5. If finish_reason == "tool_calls" AND NOT req_tools:
     → hallucination abort, return content or reasoning tail
6. Return content as the reply
```

### Which tools the bot can execute

| Tool | Implementation | Available when |
|---|---|---|
| `get_weather` | HTTP GET `https://wttr.in/{location}?format=j1` | Always (custom, added in code) |
| `web_search` | HTTP POST to donsetch-http MCP `/mcp` | donsetch-http is up |
| `web_fetch` | HTTP POST to donsetch-http MCP `/mcp` | donsetch-http is up |
| `web_crawl` | HTTP POST to donsetch-http MCP `/mcp` | donsetch-http is up |
| `web_screenshot` | HTTP POST to donsetch-http MCP `/mcp` | donsetch-http is up |
| `read_file`, `write_file`, `edit_file`, `exec_shell_command` | — | **DISABLED** in `DISABLED_TOOLS` |
| `file_glob_search`, `grep_search`, `get_info` | — | **DISABLED** (security + not implemented) |

### Polling loop

Bypasses `Application.run_polling()` from python-telegram-bot 21.0.
Hand-rolled `httpx` long-polling loop with `max_keepalive_connections=0`.
See [CALL_LLAMA.md](CALL_LLAMA.md) §4 for why.

### IPv4 monkey-patch

```python
socket.getaddrinfo = _ipv4_only_getaddrinfo
```

**Why:** the docker container has no IPv6 routing, but `api.telegram.org`
resolves to IPv6 (AAAA) first. `httpx`-based clients (Telegram Bot API,
donsetch MCP) fail with `Network is unreachable` on outbound. The
monkey-patch filters IPv6 out of `getaddrinfo` results, leaving only IPv4.

**When to remove:** once the host has IPv6 routing, or once upstream
fixes the A-record for `api.telegram.org`.

### Whitelist

```python
ALLOWED_USER_IDS = {int(x) for x in ENV.split(',') if x.strip().isdigit()}
ALLOWED_USERNAMES = {x.lstrip('@').lower() for x in ENV.split(',') if x.strip()}

if not ALLOWED_USER_IDS and not ALLOWED_USERNAMES:
    LOCKDOWN = True
```

Each handler starts with:
```python
if await reject_if_unauthorized(update, context):
    return
```

`reject_if_unauthorized`:
- If `LOCKDOWN` → reject
- If `user_id` in `ALLOWED_USER_IDS` → allow
- If `username.lower()` in `ALLOWED_USERNAMES` → allow
- Otherwise → log `[SECURITY] rejected id=...` + `return True` (handler exits silently)

**Silent rejection** (no `reply_text`) — so a stranger does not learn
the bot exists. If the bot replied "access denied", that would confirm
the bot is alive.

## whisper-api

Uses image `fedirz/faster-whisper-server:latest-cpu` — a wrapper around
`faster-whisper` by SYSTRAN, OpenAI-compatible API.

Default model `Systran/faster-distil-whisper-large-v3` (1.5 GB) — a good
balance of speed/quality for Russian. The model is downloaded on first
start to `/opt/whisper-api/cache/`.

If you need better accuracy, set `WHISPER_MODEL` in `.env` to
`Systran/faster-whisper-large-v3` (~3 GB, slower).

## Network model

`network_mode: "host"` in `telegram-bot/docker-compose.yml`.

**Pros:**
- Bot sees `localhost:8080` (llama), `localhost:8000` (whisper), `localhost:8765` (donsetch-http)
  without any extra DNS or links
- Easier to debug (`netstat`, `ss` on the host = what the bot sees)

**Cons:**
- Container is not isolated from the host network
- A port the bot would listen on (if it did) would take a host port
- You cannot run two bot instances with the same `BOT_TOKEN`

For a production-grade setup, switch to a bridge network + explicit
internal DNS names (`http://llama:8080/v1`). For a single standalone
machine, this is overkill.

## Sub-talks and persistent history

A sub-talk is a **named conversation thread** that a user creates
and switches between. Each user has their own set of sub-talks (no
sharing across users). The active sub-talk is per-user; new messages
go into whichever sub-talk the user has currently selected. Sub-talks
are stored persistently in SQLite so a bot restart does not lose
history, and so a user can return to "research" after a week of
working on "main".

In **group mode** (see below) the equivalent concept is a Telegram
forum topic, and the same SQLite store keys threads by
`(chat_id, thread_id)` — where `thread_id` is the Telegram topic
id (or the `"general"` sentinel for the General topic).

### Commands

| Command | Private mode | Group mode |
|---|---|---|
| `/newsub <name>` | Create a sub-talk in the DB; switch to it | Call Telegram `createForumTopic`; reply in current topic |
| `/sub <name>` | Switch active sub-talk (auto-create if missing) | Show a hint: tap the topic in the sidebar |
| `/sub` | Show current sub-talk | Show current topic id |
| `/here` | Show current sub-talk + last message snippet | Show current topic id + last message snippet |
| `/subs` | List sub-talks with inline-keyboard switch buttons | List Telegram topics via `getForumTopics()` (no buttons) |
| `/delsub <name>` | Delete sub-talk + messages from DB | Find topic by name; `deleteForumTopic`; wipe local history |
| `/reset` | Clear messages in current sub-talk | Clear messages in current topic (topic unchanged) |
| `/stats` | List sub-talks with message counts | Show topic count + per-topic msg count + grand total |

Private-mode names match `^[^\s]{1,32}$` — no whitespace, but
any UTF-8 character (Russian, emoji, etc.) is allowed. Forum
topic names (group mode) match `^[^\s]{1,128}$` (Telegram's
own topic-name limit).

### Storage

`telegram-bot/storage.py` provides a `Storage` class wrapping a
SQLite file (default `/app/data/conversations.db`, persisted via
the `./data:/app/data` bind-mount in `docker-compose.yml`).

```sql
chat_threads(chat_id, thread_id, created_at, last_used)  PK(chat_id, thread_id)
messages(id, chat_id, thread_id, role, content, created_at)  idx(chat_id, thread_id, id)
active_thread(chat_id PK, thread_id)         -- private mode only
```

The schema is **mode-agnostic** — the keys are `(chat_id, thread_id)`
either way:
  - Private mode: `chat_id = user_id`, `thread_id = sub_talk_name`
  - Group mode:   `chat_id = group_chat_id` (negative), `thread_id = str(message_thread_id)` or `"general"`

The `content` column stores the message as a JSON-encoded dict
matching the OpenAI Chat Completions format (`{"role", "content"}`).
Text-only and multimodal (text + image_url) messages round-trip
transparently because both are stored as JSON.

A v1-to-v2 migration runs on first `Storage()` init: the legacy
`user_id`/`sub_talk` columns are renamed via `ALTER TABLE ... RENAME
COLUMN` (SQLite 3.25+, Ubuntu 24.04 ships 3.46+). v1 was a
narrower special case where `chat_id` was always equal to
`user_id`, so the rename is lossless.

### How handlers use the store

Every message handler follows the same shape:

```python
result = await _route_to_thread(update, context)   # private or group
if result is None:
    return                                       # whitelist rejected
chat_id, thread_id, is_group = result
await _persist_message(chat_id, thread_id, "user", user_content)
history = await _load_history(chat_id, thread_id)   # up to CONTEXT_MESSAGES
bot_response = await call_llama(history, ...)
await _persist_message(chat_id, thread_id, "assistant", bot_response)
```

The previous in-memory `conversations = {}` dict was removed
when sub-talks were introduced; SQLite is now the only place
conversation state lives.

### Why sub-talks / topics at all?

Without threads, conversation history is one undifferentiated
stream — every topic you ever discussed with the bot bleeds into
every new conversation's context. With sub-talks / topics, the
model sees only the recent messages of the active thread, which
is what you want when you ask it to "проверь математику" inside
`research` while your casual chat in `main` is unrelated.

In group mode, the same property is achieved with native forum
topics — each topic has its own message history, sidebar entry,
notification settings, and per-topic permissions.

## Group mode (Telegram Topics)

The bot detects group mode by inspecting `update.effective_chat`:

```python
def _is_group_chat(update) -> bool:
    chat = update.effective_chat
    if chat is None:
        return False
    if getattr(chat, "is_forum", False):
        return True
    return chat.type != "private"
```

The first check (`is_forum`) catches the common case; the
second (`chat.type != "private"`) is a defensive fallback.
**`message_thread_id` is NOT used as the discriminator** —
the General topic of a forum-enabled supergroup has
`message_thread_id == None` per Telegram Bot API, so that
check would miss the most common entry point.

### Routing per message

The `_route_to_thread(update, context)` helper returns
`(chat_id, thread_id, is_group)`:

| Mode | `chat_id` | `thread_id` |
|------|-----------|-------------|
| Private (1:1) | `user_id` | active sub-talk name from DB (or auto-created `main`) |
| Group, General topic | `chat_id` (negative) | sentinel string `"general"` |
| Group, regular topic | `chat_id` (negative) | `str(message_thread_id)` |

The same storage layer serves both modes — keys are
`(chat_id, thread_id)` either way.

### Telegram API calls in group mode

`cmd_newsub` calls `bot.createForumTopic(chat_id, name)` and
gets back a `ForumTopic` object whose `.message_thread_id` is
the topic's id. From then on, the bot identifies the topic by
that id (the storage key for messages in the topic is
`(chat_id, str(message_thread_id))`).

`cmd_subs` enumerates topics via `bot.getForum_topics(chat_id)`
(an async generator in PTB 21). The General topic is hidden
unless the user is currently in it.

`cmd_delsub` looks up the topic by name in the same enumeration,
calls `bot.delete_forum_topic(chat_id, message_thread_id)`, and
wipes the local message history for that key.

### Why native topics, not a separate in-DB sub-talk for groups

Each Telegram forum topic has its own message_thread_id, so
the topic itself is the natural thread key. Driving the
Telegram API keeps:
  - The topic UI in the Telegram client (sidebar, unread badges)
  - Topic-specific notification settings per user
  - Per-topic permissions (admins can lock some topics)
  - No need to mirror the topic list in a separate DB table

The local DB only stores message **history** keyed by
`(chat_id, thread_id)`; Telegram itself owns the topic
metadata (name, icon, ordering, permissions).

### `_reply` — keeping replies in the same topic

`Message.reply_text` does NOT pass `message_thread_id` through
to `sendMessage`. Without an explicit `message_thread_id`, a
reply in a regular topic would land in the General topic.
The bot's `_reply` helper sets it explicitly when the source
message is in a topic:

```python
async def _reply(update, text, **kwargs):
    if update.message.message_thread_id is not None:
        kwargs.setdefault('message_thread_id', update.message.message_thread_id)
    return await update.message.reply_text(text, **kwargs)
```

When the source message is in the General topic, the helper
leaves `message_thread_id` unset, which is the right default
(`sendMessage` then lands in General — same place).

### `_should_mute_in_group` — the "[llm] honor system"

In a group the bot shares the room with other Telegram bots
and with humans acting as LLM proxies. The pinned welcome
message invites active LLMs to mark themselves with `[llm]`
in their replies, so the room is not double-answered by
multiple LLMs simultaneously.

The check is applied at the dispatch layer
(`_dispatch_update`), BEFORE `process_update`, so EVERY
handler (message + command) is covered uniformly:

```python
def _should_mute_in_group(update) -> bool:
    msg = update.message
    if msg is None or msg.from_user is None:
        return False
    if getattr(msg.from_user, "is_bot", False):
        return True                              # any Telegram bot
    if not _is_group_chat(update):
        return False                             # private mode: no yield
    text = (msg.text or msg.caption or "").lower()
    if _LLM_MARK in text:
        return True                              # [llm]-marked user
    return False
```

The `[llm]` marker is case-insensitive and substring-matched.
False positives ("the [llm] model is great") are possible but
rare and harmless — the bot just doesn't reply to that one
message. The dispatch path logs `[dispatch] muting
group-mode message from <who>` so the operator can audit
what was suppressed.

The filter is applied only at the dispatch layer; per-handler
checks would be a 4-5x duplication and would miss commands
like `/help` from another bot.

## What `bot.py` deliberately does NOT do

- Does **not** run `Application.run_polling()` (see [CALL_LLAMA.md](CALL_LLAMA.md) §4).
- Does **not** keep any tool-calling state in llama-server's own
  MCP layer; the bot executes tools in its own process. This avoids
  the bug where llama-server loads the **list** of tools but
  sometimes fails to **execute** them (donsetch in particular never
  loaded correctly through the server-side MCP).
- Does **not** hardcode any model name in the code; if `MODEL` env
  is empty, the bot queries `/v1/models` and uses the first model
  it finds. See [CALL_LLAMA.md](CALL_LLAMA.md) §7.
- Does **not** log the live `req_body` dict — `json.dumps` with the
  default `ensure_ascii=True` mutates the dict in place. See
  [CALL_LLAMA.md](CALL_LLAMA.md) §6.

## Rating mode (group only)

When `RATING_MODE=1` is set, the bot's four message handlers
(`handle_text`, `handle_photo`, `handle_voice`, `handle_document`)
inject the `RATING_RULES` constant as an extra system message
before calling the LLM. The LLM then classifies each user
message into one of six types and prefixes its response
accordingly. The dispatcher parses the prefix and takes one
of three actions:

| Type            | Action                              |
|-----------------|-------------------------------------|
| question        | text reply                          |
| request         | text reply                          |
| confirmation    | text reply (verify and confirm)     |
| info            | rate 1-10, setMessageReaction, no text |
| statement       | rate 1-10, setMessageReaction, no text |
| bloat           | single 😐 reaction, no text         |

The numeric rating is persisted in `messages.rating` (storage
v3) so analytics commands can group by score. The full policy
is in [RATING_RULES.md](RATING_RULES.md); the canonical
machine-readable version is the `RATING_RULES` constant in
`bot.py`. The two must be kept in sync.

### Why it's a separate mode (not a feature flag per message)

Telegram's `setMessageReaction` API is a side effect, not a
return value — once applied, a reaction is visible to all
group members. So enabling/disabling the classifier must be
a deliberate operator action, not a per-message decision.
The `RATING_MODE=1` env var is the operator's "I trust the
classifier" switch; turning it off reverts to the default
text-reply-for-everything behavior.

### Why it's only in group mode

In a 1:1 private chat, the user explicitly chose to talk to
the bot. Classifying their message and silently reacting
with an emoji (instead of replying) would feel cold and
unhelpful — the user is asking for engagement, not judgment.
Group mode has a different social contract: the bot is a
participant among many, and reactions are a natural way to
contribute without flooding the thread.
