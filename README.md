# llama.cpp_telegram

A Telegram bot wrapper around the [llama.cpp](https://github.com/ggml-org/llama.cpp) OpenAI-compatible API
plus a local Whisper server for voice transcription.

## What is this

A ready-to-deploy bundle of two containers:

| Service | Port | Purpose |
|---|---|---|
| `telegram-bot` | (none) | Polls Telegram, talks to the LLM, supports tool-calling |
| `whisper-api` | 8000 | Transcribes voice messages via faster-whisper, returns the transcript to the bot |

The bot can:

- 💬 Text, conversation context (last 20 messages per user)
- 🖼 Image analysis (vision models in llama.cpp)
- 🎤 Voice messages via Whisper
- 📄 Document reading (TXT, PDF — text layer)
- 🛠 **Tool-calling**: `get_weather` (wttr.in) + `searxng_search` / `searxng_fetch_url` / `searxng_engines`
  if [llama.cpp_search](https://github.com/vaskes/llama.cpp_search) is running nearby
- 🔒 **Whitelist** by Telegram `user_id` / `@username` (env vars, LOCKDOWN by default)

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
                         │              │   /search?format=json│             │
                         │              │─────────────────────▶│  searxng    │
                         └──────────────┘                      │  (8888)     │
                                                               └─────────────┘
```

`network_mode: host` — the bot sees llama / whisper / searxng as localhost.
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

- **[docs/SETUP.md](docs/SETUP.md)** — installing on a fresh host (for humans)
- **[docs/AGENT_GUIDE.md](docs/AGENT_GUIDE.md)** — short command reference for AI agents
- **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** — what's inside bot.py, how the tool-calling loop works
- **[docs/SECURITY.md](docs/SECURITY.md)** — whitelist, env vars, what NOT to commit
- **[docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)** — common issues

## Requirements

- Linux with Docker + Docker Compose v2
- Passwordless `sudo` (for systemd)
- Any llama.cpp-compatible server on port 8080 (or your value in `.env`)
- Optional: SearXNG (for the web-search tool), Whisper (for voice messages)

## Where to get llama-server

This repository does **not** include llama.cpp itself. Any OpenAI-compatible
endpoint will work. Recommended for Radeon 780M:
**[vaskes/llama.cpp-rocm-780m](https://github.com/vaskes/llama.cpp-rocm-780m)** —
a ready Docker build with native gfx1103 support.

For SearXNG + tool-calling:
**[vaskes/llama.cpp_search](https://github.com/vaskes/llama.cpp_search)** —
SearXNG + Playwright MCP + a ready `--mcp-servers-config` for llama-server.

## ⚠️ Security Disclaimer

These repositories have **not** been audited or tested for security. They
are intended **only** for local deployments in controlled environments
(your own machine behind your own firewall, a trusted LAN, or an isolated
test host).

**There are no warranties of any kind**, express or implied, that this
code is secure, correct, or fit for any purpose. The author(s) are **not
responsible** for any damage, data loss, security breach, or other harm
resulting from the use of this software.

In particular:
- Container images may run with elevated privileges, host networking, or
  bind-mounts from the host filesystem.
- Some tools are designed to **execute arbitrary commands** or **read /
  write host files**; do not enable them unless you fully understand the
  implications.
- Defaults may bind services to `0.0.0.0`; verify before exposing to any
  untrusted network.

**Use at your own risk. Do not expose to the public internet without a
proper security review.**

## License

MIT
