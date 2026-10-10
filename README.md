# llama.cpp_telegram

> **State at v0.5.0 (Oct 2026):** Qwen3.8-27B-Ultra-Heretic-MTP-256k on
> `192.168.10.6:8080`, Whisper on `192.168.10.7:8000`, donsetch v4.7.4
> (split-shape) on `127.0.0.1:8765`. Private + group-with-Topics modes.
> GROUP_CONTEXT system prompt locked. react_to_message tool available
> in non-rating mode. Per-handler Stop button race fixed. PTB 21
> download compat. ~75 selftest cases on every container start.

A Telegram bot wrapper around the [llama.cpp](https://github.com/ggml-org/llama.cpp) OpenAI-compatible API
plus a local Whisper server for voice transcription.

## What is this

A ready-to-deploy bundle of two containers:

| Service | Port | Purpose |
|---|---|---|
| `telegram-bot` | (none) | Polls Telegram, talks to the LLM, supports tool-calling |
| `whisper-api` | 8000 | Transcribes voice messages via faster-whisper, returns the transcript to the bot |

The bot can:

- 💬 Text, conversation context (last 20 messages per sub-talk / topic)
- 🖼 Image analysis (vision models in llama.cpp)
- 🎤 Voice messages via Whisper
- 📄 Document reading (TXT, PDF — text layer)
- 🛠 **Tool-calling**: `get_weather` (wttr.in) + `web_search` / `web_fetch` / `web_crawl` / `web_screenshot`
  via [donsetch-http](https://github.com/dondai44423/donsetch) (Rust + Playwright headless Chrome MCP server) — see "Related" below
- 🔒 **Whitelist** by Telegram `user_id` / `@username` (env vars, LOCKDOWN by default in private mode)
- 💾 **Persistent conversation history** in SQLite, per (chat_id, thread_id) — survives restarts
- 🏷 **Sender name in history** — every user message is tagged with `From: <name>: ` so the LLM can tell Vasisualy from Dimon in a group. NULL → `From: user:`. Schema v3→v4 migration is idempotent.
- 🧠 **GROUP_CONTEXT system prompt** — LlmChatPlace rules (10-step rating scale, [llm] tag, truth-over-style, no tone policing) injected on every call_llama, in both tool and rating modes. Survives model switches and /reset.
- 👍 **react_to_message tool** — LLM can set a single-emoji Telegram reaction on any message in the current chat (standard 10-step scale, default message_id = user's current). Available in non-rating mode.
- 🛡 **Malformed tool-call JSON rejection** — defensive parse in the dispatcher; runaway Qwen-500 loops (10× duplicated keys) caught and rejected with feedback to the LLM.
- ⏹ **Per-handler Stop button** — the ⏹ on a thinking message aborts THAT handler only. Keyed on (chat_id, thinking.message_id), not (chat_id, user_id) so concurrent handlers don't overwrite each other.
- 🎬 **Non-streaming tool-call path** — vision tasks (handle_photo) now run the unified tool loop instead of returning empty when the LLM emits a tool_call on a non-streaming POST.
- 🤖 **Donsetch 4.7.4 split-shape** — `DONSETCH_MCP__TEXT_ONLY=false` in the compose file gives back the human-readable text response that the bot's `donsetch_call` was designed for.
- 🧵 **Two deployment modes**:
  - **Private chat** — 1 user, multi-thread via `/newsub` `/sub` `/subs` (in-DB sub-talks)
  - **Group with Telegram Topics** — N users, native forum topics via `/newsub` (creates a Telegram topic), `/subs` (lists topics), `/delsub` (deletes a topic). Replies stay in the topic where the user is.

## Architecture

```
┌──────────┐    HTTP     ┌──────────────┐   chat/completions   ┌─────────────┐
│ Telegram │◀───────────▶│  telegram-   │──────────────────────▶│  llama.cpp  │
│  user    │  Bot API    │  bot (host   │  +tool_calls loop    │  server     │
└──────────┘             │   network)   │                      │  (8080)     │
                         │              │   /v1/audio/         │             │
                         │              │   transcriptions     │             │
                         │              │─────────────────────▶│  whisper-   │
                         │              │                      │  api (8000) │
                         │              │   /mcp (search/fetch)│             │
                         │              │─────────────────────▶│  donsetch-  │
                         └──────────────┘                      │  http(8765) │
                                                               └─────────────┘
```

`network_mode: host` — the bot sees llama / whisper / donsetch as localhost.
That is the simplest setup for a single host running all three services.

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

- **[docs/SETUP.md](docs/SETUP.md)** — installing on a fresh host (for humans); also covers group-mode deployment
- **[docs/AGENT_GUIDE.md](docs/AGENT_GUIDE.md)** — short command reference for AI agents
- **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** — what's inside bot.py at a high level; covers both private and group modes
- **[docs/CALL_LLAMA.md](docs/CALL_LLAMA.md)** — detailed design notes for `call_llama`, polling loop, abort ladder, image MIME detection, config block. The file to read for a code review.
- **[docs/SECURITY.md](docs/SECURITY.md)** — whitelist (private mode), group-mode security, env vars, what NOT to commit
- **[docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)** — common issues, including group-mode gotchas
- **[docs/REVIEW-MINIMAX.md](docs/REVIEW-MINIMAX.md)** — first review wave (private-mode polish)
- **[docs/REVIEW-MINIMAX-v2.md](docs/REVIEW-MINIMAX-v2.md)** — second review wave (group-mode migration)
- **[CHANGELOG.md](CHANGELOG.md)** — release notes for humans

## Requirements

- Linux with Docker + Docker Compose v2
- Passwordless `sudo` (for systemd)
- Any llama.cpp-compatible server on port 8080 (or your value in `.env`)
- Optional: [donsetch-http](https://github.com/dondai44423/donsetch) on port 8765
  (for the web-search tools), Whisper on port 8000 (for voice messages)

## Where to get llama-server

This repository does **not** include llama.cpp itself. Any OpenAI-compatible
endpoint will work. Recommended for Radeon 780M:
**[vaskes/llama.cpp-rocm-780m](https://github.com/vaskes/llama.cpp-rocm-780m)** —
a ready Docker build with native gfx1103 support.

## Related

For tool-calling (web search, fetch, crawl, screenshots):
**[dondai44423/donsetch](https://github.com/dondai44423/donsetch)** —
Rust MCP server with Playwright headless Chrome; aggregates 6 search engines
(Bing, DDG, Mojeek, Yahoo, Brave, Google) and bypasses CAPTCHA via real
browser rendering. The deprecated SearXNG-based stack is in
[vaskes/llama.cpp_search](https://github.com/vaskes/llama.cpp_search) for
historical reference only.

## License

MIT
