# llama.cpp_telegram

> **Stable release: `v0.10.2-stable` (Oct 2026).** First release
> that has been manually tested end-to-end by humans on both
> private and group-with-Topics modes. Code freeze declared at
> commit `7efab9b`.
>
> ⚠️ **Do NOT `git clone <url>` without `--branch`.** The default
> branch is bleeding-edge and may include unreleased refactors
> that have not been human-tested. Pin to a stable tag:
> ```bash
> git clone --branch v0.10.2-stable https://github.com/vaskes/llama.cpp_telegram.git
> ```
> Bleeding edge is for contributors only. See [Releases](../../releases)
> for the full stable-tag history.
>
> **Known minor issue in `v0.10.2-stable`:** `/subs` in group mode
> shows only bot-created topics; UI-created topics are not
> enumerated (Telegram Bot API limitation, not a SQLite bug).
> Fix planned in the next release. See `CHANGELOG.md` for details.

**What `v0.10.2-stable` is:** Qwen3.8-27B-Ultra-Heretic-MTP-256k on
any OpenAI-compatible endpoint, Whisper on any faster-whisper
server, donsetch MCP for web tools. Private + group-with-Topics
modes. GROUP_CONTEXT system prompt locked. react_to_message tool
available in non-rating mode. Per-handler Stop button race fixed.
**Webhook transport** supported (v0.9.0) alongside long-polling.
Image abort rollback (v0.9.1). 19/19 selftest groups + 5/5 pytest
in CI. Plus: `scripts/verify.py` for production health checks,
Qwen wave-3 fixes, env-driven `TELEGRAM_API`.

A Telegram bot wrapper around the [llama.cpp](https://github.com/ggml-org/llama.cpp) OpenAI-compatible API
plus a local Whisper server for voice transcription.

**Two transports:** long-polling (default, no infrastructure required)
and webhook (sub-second latency, requires a public endpoint with TLS).
See [docs/WEBHOOK.md](docs/WEBHOOK.md) for when to use which and how
to set up webhook mode.

## What is this

A ready-to-deploy Telegram bot that talks to your local LLM. Bring your
own model server; the bot works with any OpenAI-compatible endpoint.

| Service | What it does | How it's deployed |
|---|---|---|
| `telegram-bot` | Polls (or receives webhooks from) Telegram, talks to the LLM, executes tool calls | Docker container |
| `whisper-api` | Transcribes voice messages via faster-whisper, returns the transcript to the bot | Docker container (optional) |

The bot can:

- 💬 Text, conversation context (last 20 messages per sub-talk / topic)
- 🖼 Image analysis (vision models in llama.cpp)
- 🎤 Voice messages via Whisper
- 📄 Document reading (TXT, PDF — text layer)
- 🛠 **Tool-calling**: `get_weather` (wttr.in) + `web_search` / `web_fetch` / `web_crawl` / `web_screenshot`
  via [donsetch-http](https://github.com/dondai44423/donsetch) (Rust + Playwright headless Chrome MCP server) — see "Related" below
- 🔒 **Whitelist** by Telegram `user_id` / `@username` (env vars, LOCKDOWN by default in private mode)
- 💾 **Persistent conversation history** in SQLite, per (chat_id, thread_id) — survives restarts
- 🧠 **GROUP_CONTEXT system prompt** — LlmChatPlace rules (10-step rating scale, [llm] tag, truth-over-style, no tone policing) injected on every call_llama, in both tool and rating modes. Survives model switches and /reset.
- 👍 **react_to_message tool** — LLM can set a single-emoji Telegram reaction on any message in the current chat (standard 10-step scale, default message_id = user's current). Available in non-rating mode.
- 🛡 **Malformed tool-call JSON rejection** — defensive parse in the dispatcher; runaway Qwen-500 loops (10× duplicated keys) caught and rejected with feedback to the LLM.
- ⏹ **Per-handler Stop button** — the ⏹ on a thinking message aborts THAT handler only. Keyed on (chat_id, thinking.message_id), not (chat_id, user_id) so concurrent handlers don't overwrite each other. **v0.9.1**: image abort now also rolls back the persisted user message, so the next text prompt is not answered about the cancelled image.
- 🎬 **Non-streaming tool-call path** — vision tasks (handle_photo) now run the unified tool loop instead of returning empty when the LLM emits a tool_call on a non-streaming POST.
- 🤖 **Donsetch 4.7.4 split-shape** — `DONSETCH_MCP__TEXT_ONLY=false` in the compose file gives back the human-readable text response that the bot's `donsetch_call` was designed for.
- 🧵 **Two deployment modes**:
  - **Private chat** — 1 user, multi-thread via `/newsub` `/sub` `/subs` (in-DB sub-talks)
  - **Group with Telegram Topics** — N users, native forum topics via `/newsub` (creates a Telegram topic), `/subs` (lists topics), `/delsub` (deletes a topic). Replies stay in the topic where the user is.

## Transports

The bot can receive Telegram updates via two mechanisms:

| Transport | Latency | Setup | When to use |
|---|---|---|---|
| **Long-polling** (default) | Up to 25s | None — just run the bot | You don't have a public domain/TLS, behind strict NAT, or doing development |
| **Webhook** | <1s | DNS + reverse proxy + `WEBHOOK_MODE=1` | You have a public domain with TLS and want real-time updates |

Polling has been the default for years and is rock-solid. Webhook
became available in v0.9.0 and is recommended for production where
real-time response matters. See [docs/WEBHOOK.md](docs/WEBHOOK.md)
for the full setup guide.

## Architecture

The bot is one process. It owns four network connections: one to
Telegram, one to the LLM, one to Whisper (voice only), and one to
donsetch-http (web tools only). The exact addressing is a
deployment choice; the diagram below shows the conceptual wiring.

```
                          ┌───────────────────────────────────────┐
                          │           Your host / LAN            │
                          │                                       │
  ┌──────────┐  Bot API   │   ┌──────────────────────┐            │
  │ Telegram │◀──────────▶│   │   telegram-bot       │            │
  │  user    │  (HTTPS)   │   │   (Python)           │            │
  └──────────┘            │   │                      │            │
       ▲                  │   │                      │            │
       │  (long-poll:     │   │  chat/completions    │  ┌────┐    │
       │   bot → TG)      │   │ ────────────────────▶│  │LLM │    │
       │  (webhook:       │   │  +tool_calls loop    │  │    │    │
       │   TG → bot,      │   │                      │  └────┘    │
       │   via reverse    │   │  audio/transcriptions│  ┌─────┐   │
       │   proxy on a     │   │ ────────────────────▶│  │Whis│   │
       │   public domain) │   │  (voice only)        │  │per │   │
                          │   │                      │  └─────┘   │
                          │   │  MCP /mcp            │  ┌─────┐   │
                          │   │ ────────────────────▶│  │Don- │   │
                          │   │  (web tools)         │  │setch│   │
                          │   │                      │  └─────┘   │
                          │   └──────────────────────┘            │
                          │                                       │
                          └───────────────────────────────────────┘
```

**Each arrow is one of:**

- **Telegram ↔ bot** — `api.telegram.org`. Direction depends on the
  transport: in long-polling mode the bot pulls updates; in webhook
  mode Telegram POSTs to a public URL on a reverse proxy that forwards
  to the bot.
- **bot → LLM** — `POST /v1/chat/completions` (OpenAI-compatible).
  The bot runs its own tool-call loop on top of whatever the server
  exposes.
- **bot → Whisper** — `POST /v1/audio/transcriptions`
  (OpenAI-compatible). The bot downloads the voice file from
  Telegram, sends it to Whisper, gets the text back, then feeds the
  text into the LLM call as a normal user message.
- **bot → donsetch-http** — `POST /mcp` (JSON-RPC over HTTP, MCP
  protocol). Exposes `web_search`, `web_fetch`, `web_crawl`,
  `web_screenshot`. The bot executes the tool calls the LLM emits.

**Network layout:** the bot does not require any particular
container networking mode. The two reference layouts are

1. **Single host** — all four services run on the same host; the
   bot container uses `network_mode: host` and reaches the others
   via `localhost`. Simplest setup, no extra plumbing.
2. **Split across containers** — each service in its own container
   on a shared `macvlan` network. The bot gets a static IP on
   that network; the other services are reached by IP. This is
   what you want when the bot is behind a webhook reverse proxy
   that needs to forward to a stable container address.

Either layout works. The exact `docker-compose.yml` depends on
your host topology; see `telegram-bot/docker-compose.yml` and
`whisper-api/docker-compose.yml` for the reference files.

## Quick start

See [docs/SETUP.md](docs/SETUP.md) for step-by-step.

Short version:

```bash
git clone https://github.com/vaskes/llama.cpp_telegram.git
cd llama.cpp_telegram
sudo ./scripts/install.sh
sudo $EDITOR /opt/telegram-bot/.env   # BOT_TOKEN, ALLOWED_USER_IDS
sudo systemctl start whisper-api-compose
sudo systemctl start telegram-bot-compose
```

## Documentation

- **[docs/WEBHOOK.md](docs/WEBHOOK.md)** — webhook transport: when to use, how to set up, security, fallback
- **[docs/SETUP.md](docs/SETUP.md)** — installing on a fresh host (for humans); also covers group-mode deployment
- **[docs/AGENT_GUIDE.md](docs/AGENT_GUIDE.md)** — short command reference for AI agents
- **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** — what's inside bot.py at a high level; covers both private and group modes
- **[docs/CALL_LLAMA.md](docs/CALL_LLAMA.md)** — detailed design notes for `call_llama`, polling loop, abort ladder, image MIME detection, config block. The file to read for a code review.
- **[docs/SECURITY.md](docs/SECURITY.md)** — whitelist (private mode), group-mode security, env vars, what NOT to commit
- **[docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)** — common issues, including group-mode gotchas
- **[CHANGELOG.md](CHANGELOG.md)** — release notes for humans

## Requirements

- Linux with Docker + Docker Compose v2
- Passwordless `sudo` (for systemd)
- Any llama.cpp-compatible server on port 8080 (or your value in `.env`)
- Optional: [donsetch-http](https://github.com/dondai44423/donsetch) on port 8765
  (for the web-search tools), Whisper on port 8000 (for voice messages)

## Where to get llama-server

This repository does **not** include llama.cpp itself. Any OpenAI-compatible
endpoint will work. For a self-hosted setup, see your llama.cpp
distributor's documentation.

## Related

For tool-calling (web search, fetch, crawl, screenshots):
**[dondai44423/donsetch](https://github.com/dondai44423/donsetch)** —
Rust MCP server with Playwright headless Chrome; aggregates 6 search engines
(Bing, DDG, Mojeek, Yahoo, Brave, Google) and bypasses CAPTCHA via real
browser rendering. The deprecated SearXNG-based stack is in
[vaskes/llama.cpp_search](https://github.com/vaskes/llama.cpp_search) for
historical reference only.

## Credits

- **Developer:** Mavis Agent powered by Minimax M3
- **Code reviews:** Mavis Agent powered by Minimax M3 and Qwen 3.8-Max
- **Vibecoding:** Vaskes
- **Network infra:** Vaskes
- **Human testers:** Vaskes and Dimon (2026-10-12 v0.10.2 release)
- **Deployments and implementation:** Vaskes

## License

MIT
