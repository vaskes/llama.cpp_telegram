# llama.cpp_telegram

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python: 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB)](https://www.python.org/)
[![Docker Compose: v2](https://img.shields.io/badge/Docker_Compose-v2-2496ED)](https://docs.docker.com/compose/)
[![Release](https://img.shields.io/github/v/release/vaskes/llama.cpp_telegram?label=release)](../../releases)
[![Tests: 2897](https://img.shields.io/badge/tests-2897-brightgreen)](tests/)

> **Current stable: `v0.15.0-stable`** (2026-10-27, commit `e174168`).
> Pin to a tag — see the clone command below.
>
> `v0.15.0` is the **limits-and-resilience** release: a command in the
> sandbox may run for an hour. The operator measured the old limit from
> the inside — a command was cut off at a minute, and the output it had
> already produced went with it — and ruled that an hour is the ceiling.
> The bot's turn budget is now longer than the command it runs, so a
> long command comes back with its answer instead of being thrown away
> at the boundary. A Telegram API hiccup on the typing indicator can no
> longer cost the user their turn.
>
> Before it: `v0.14.1` — the delivery delivers the file and leaves it
> alone; `v0.14.0` — the **tools-and-deployment** release (the bot can
> import tools from MCP servers; which servers, and which of their
> tools, are the operator's config — the model never adds a server; the
> side-car's pin matches the side-car the bot actually talks to; and **a
> release is the deployable unit** — the CI builds the image and attaches
> it to the GitHub Release, and the production host loads that image and
> keeps no git of its own); `v0.13.0` — the **refactor release**: the
> 1612-line `dispatch.py` is now 345 lines across five owner modules,
> `call_llama` is 478 lines across 17 leaf modules, and the bot now
> assembles its own context instead of sending a fixed number of
> messages. Nothing about the user-facing behaviour changed on purpose.
>
> **What is verified:** deployed in production in webhook mode; the whole
> pytest suite green on Python **3.11** (the production interpreter) and
> **3.12**; eight review waves closed with an approve; a production smoke
> after the deploy (`/healthz` → 200, zero fatal lines in the log, webhook
> auth rejecting both a missing and a wrong secret). The deployed
> container's image id is the id the release carries: the image that runs
> in production is the image that was reviewed, not a rebuild of it.
>
> **What is not:** the live edit-replace flow — a user edits a message
> while the bot is answering, and the bot rewrites its answer — was never
> exercised by a human end-to-end. It needed two people, a slow model and
> a real Telegram chat, so it was manual-only by nature and **not** covered
> by any automated test. It was **removed** on 2026-10-24 rather than kept
> as a permanent gap: an edited message is now ignored, and the answer to
> the original stands. Everything else that was operator-pilot-tested was
> done on `v0.10.2-stable`, which remains the release with a full human
> end-to-end pass on both private and group-with-Topics modes.
>
> ⚠️ **Do NOT `git clone <url>` without `--branch`.** The default
> branch is bleeding-edge and may include unreleased refactors
> that have not been human-tested. Pin to a tag:
> ```bash
> git clone --branch v0.15.0-stable https://github.com/vaskes/llama.cpp_telegram.git
> ```
> Bleeding edge is for contributors only. See [Releases](../../releases)
> for the full stable-tag history.
>

**What `v0.15.0-stable` adds over `v0.14.1-stable`:**

- **A command may run for an hour.** The sandbox's ceiling moved to
  3600 s and the production deployment raises the knob to it. The bot's
  client timeout, the turn's budget and the engine's HTTP timeout all
  followed: a limit is a chain, and raising one link without the next
  is a limit that eats the answer. Measured on the production host: a
  90-second command comes back complete — through the side-car and
  through the bot's own tool path.
- **A decoration cannot end the turn.** The typing indicator is
  decoration, but three of the four handlers asked the Telegram API for
  it without a guard, so a one-second network hiccup lost the user's
  voice message before the model was ever asked. One helper, one guard,
  and the refusal is logged.

**What `v0.14.1-stable` added over `v0.14.0-stable`:** the delivery of
a file no longer removes the sandbox copy on the tool's own initiative —
and the answer does not announce it as a fact. The capability itself is
gone from the client: nothing deletes anything except a command the
operator asked for.

**What `v0.14.0-stable` added over `v0.13.0-stable`:**

- **The bot can import tools from MCP servers.** The operator's config
  block says which servers, which of their tools, and in which chats; the
  model is shown those tools as `<server>_<tool>` and calls them through
  one branch. A tool that duplicates a native one is excluded rather
  than renamed — two shells on one machine are two policies.
- **The side-car's pin matches the side-car.** The repository pinned
  v2.10.2 while the running side-car was already at v2.12.1, and the
  tripwire said nothing because the claims were still *true*. The pin is
  now the version the production host runs, and every claim is re-read
  against that source.
- **A release is the deployable unit.** Push a tag, and the CI builds
  the image and attaches it to the release; the production host loads
  that image and runs it. See
  [Architecture](#how-a-release-reaches-production) below.

**What `v0.13.0-stable` added over `v0.12.0-stable`** (194 commits):

- **The bot owns its context.** `ContextBudget` decides which persisted
  messages reach the engine — protect the recent window, summarise what
  falls out of it, assemble within a budget. The engine stays stateless,
  so swapping llama for vLLM or Strata is a URL change and nothing else.
  SQLite is the source of truth and the history is never pruned on read.
- **The monolith is gone.** `dispatch.py` 1612 → 345, `call_llama` 1616 →
  478, `_selftest` 1940 → 111, `bot.py` down to a 124-line facade. Each
  of those is a ledger item in [TECH-DEBT.md](TECH-DEBT.md) with a
  tripwire pinning it, so it cannot silently grow back.
- **The context window is anchored, not sliding.** A week back from bot
  start, per sub-talk, re-anchored on a schedule. The reasoning, and the
  thing that would make us revisit it, is in
  [docs/adr/ADR-0003](docs/adr/ADR-0003-context-reanchoring.md).
- **An optional persona**, from a gitignored `persona.local.json`. The
  repo ships `personality.json.example` with `enabled=false`, so a fork
  never inherits somebody else's character.
- **The review cycle is closed.** Eight waves; the last three found
  three blockers that had been shipping behind a green suite — a required
  keyword argument that was never passed, a `global` that rebound a
  different module's name, and a check-then-act across an `await`.

**What `v0.10.2-stable` had, and still has:** Qwen3.8-27B-Ultra-Heretic-MTP-256k on
any OpenAI-compatible endpoint, Whisper on any faster-whisper
server, donsetch MCP for web tools. Private + group-with-Topics
modes. GROUP_CONTEXT system prompt locked. react_to_message tool
available in non-rating mode. Per-handler Stop button race fixed.
**Webhook transport** supported (v0.9.0) alongside long-polling.
Image abort rollback (v0.9.1). Plus: `scripts/verify.py` for production
health checks, Qwen wave-3 fixes, env-driven `TELEGRAM_API`.
**What `v0.11.0` added:** the `bash_command` killer feature
(sandboxed shell, gated to `LLAMABOT_DEPLOYMENT_PROFILE=personal`).
See [docs/BASH-SANDBOX.md](docs/BASH-SANDBOX.md).

A Telegram bot wrapper around the [llama.cpp](https://github.com/ggml-org/llama.cpp) OpenAI-compatible API
plus a local Whisper server for voice transcription.

**Two transports:** long-polling (default, no infrastructure required)
and webhook (sub-second latency, requires a public endpoint with TLS).
See [docs/WEBHOOK.md](docs/WEBHOOK.md) for when to use which and how
to set up webhook mode.

## 🐚 Killer feature: `bash_command` (personal mode only)

> **The unique selling point of this bot.** As of `v0.11.0`
> (and every release since), the model can call a `bash_command`
> tool that runs shell commands in a hardened sandbox container.
> No other public Telegram bot offers a comparable capability
> with this threat model: read files, run scripts, manage
> projects, remember state across calls in a persistent tmux
> session — all from inside a Telegram chat.
>
> **🚨 DO NOT ENABLE IN PUBLIC ANONYMOUS CHATS.** The
> `bash_command` tool is gated to `LLAMABOT_DEPLOYMENT_PROFILE=personal`
> and turns the bot into a general-purpose remote-code-execution
> surface. A malicious user with chat access could trick the
> LLM into running destructive commands inside the sandbox.
> The sandbox contains the blast radius (read-only fs, dropped
> capabilities, 60s timeout, pids/memory caps), but a
> determined attacker can still exfiltrate data, mine crypto,
> or attack the network the sandbox is on. Personal mode
> means: the operator trusts every user who can reach the
> bot. Public mode means: strangers, and you do not.
>
> Setup, threat model, and operator runbook:
> [docs/BASH-SANDBOX.md](docs/BASH-SANDBOX.md). Tool isolation
> (defence in depth against llama-server's native --agent
> tools): [docs/TOOL-ISOLATION.md](docs/TOOL-ISOLATION.md).
>
> **Repository dependency:** bash_command requires
> [vaskes/OpenWebUI-bash-sandbox](https://github.com/vaskes/OpenWebUI-bash-sandbox)
> pinned as a git submodule. See
> [docs/BASH-SANDBOX.md#repository-dependency](docs/BASH-SANDBOX.md#repository-dependency).

## What is this

A friendly Telegram bot that talks to your local LLM. Bring your
own model server; the bot works with any OpenAI-compatible endpoint.

| Service | What it does | How it's deployed |
|---|---|---|
| `telegram-bot` | Polls (or receives webhooks from) Telegram, talks to the LLM, executes tool calls | Docker container |
| `whisper-api` | Transcribes voice messages via faster-whisper, returns the transcript to the bot | Docker container (optional) |
| `bash-sandbox` (personal only) | Runs `bash_command` tool calls in a hardened side-car container with tmux session persistence | Separate repo + Docker ([vaskes/OpenWebUI-bash-sandbox](https://github.com/vaskes/OpenWebUI-bash-sandbox), git submodule) |

The bot can:

- 🐚 **Run shell commands in a sandbox** (`bash_command`, personal mode only) — see the [killer feature](#-killer-feature-bash_command-personal-mode-only) above. Defence in depth: profile gate, tool whitelist (T38.5), soft-reject unknown tools (T43/T49), per-chat tmux session.
- 💬 Text, conversation context (last 20 messages per sub-talk / topic)
- 🖼 Image analysis (vision models in llama.cpp)
- 🎤 Voice messages via Whisper
- 📄 Document reading (TXT, PDF — text layer)
- 🛠 **Tool-calling**: `web_search` / `web_fetch` / `web_crawl` / `web_screenshot`
  via [donsetch-http](https://github.com/dondai44423/donsetch) (Rust + Playwright headless Chrome MCP server) — see "Related" below.
  `bash_command` (sandboxed shell, personal mode only) is the killer feature; see [Killer feature](#-killer-feature-bash_command-personal-mode-only) above.
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
                          │   │  POST /bash          │  ┌─────┐   │
                          │   │ ────────────────────▶│  │bash │   │
                          │   │  (PERSONAL only,     │  │sandbox│ │
                          │   │   bearer auth)       │  └─────┘   │
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
- **bot → bash-sandbox** (personal mode only) — `POST /bash` with
  `Authorization: Bearer <token>`. The bot forwards the model's
  `bash_command` tool calls; the sandbox returns stdout / stderr /
  exit_code. See [docs/BASH-SANDBOX.md](docs/BASH-SANDBOX.md) for
  the full architecture, threat model, and operator runbook.
- **bot → an MCP server** — `POST /mcp` (JSON-RPC over HTTP, MCP
  protocol). The bot asks the server what it offers and shows the model
  those tools under the name `<server>_<tool>`. Which servers, which of
  their tools and in which chats is the **operator's** config
  (`LLAMABOT_MCP_SERVERS`) — the model never adds a server. A tool that
  duplicates a native one is excluded, not renamed. See
  [docs/TOOL-ISOLATION.md](docs/TOOL-ISOLATION.md).

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

### How a release reaches production

The repository builds **one artifact**, and the release carries it:

```
 push tag vX.Y.Z-stable
            │
            ▼
 ┌──────────────────────────┐        GitHub Release (public)
 │ CI: build the image      │ ─────────────────────────────▶  bot-image.tar.gz
 │ (no secret, no ssh)      │                                  deploy-side.tar.gz
 └──────────────────────────┘
            │
            │  the CI's share ends here
            ▼
 ┌──────────────────────────────────────────────────────────┐
 │ the host's deploy_bot.sh <tag>                           │
 │   fetch the release's assets → load the image → run it →  │
 │   health endpoint, restart count, smoke test (before      │
 │   and after) → roll back on failure                       │
 └──────────────────────────────────────────────────────────┘
```

The CI's share of the work ends where the image is built: the workflow
holds **no secret** and has **no way to reach the production host** — no
ssh, no host address, no key. The last hop is whoever holds the access
to that host (the operator, or the agent holding the operator's keys):
one command, the host's own script — the same script a hand deploy
runs, so the two cannot drift apart.

The production host keeps **no git of its own**. The image arrives as an
artifact, so nothing fetches itself and there is nothing to remember
about it; what runs in production is written down on the host
(`/opt/telegrambot/DEPLOYED`: the tag, the image, its id).

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#the-release-workflow)
and CODEBOOK rule **X-18**.

## Quick start

See [docs/SETUP.md](docs/SETUP.md) for step-by-step.

Short version:

```bash
git clone --branch v0.15.0-stable https://github.com/vaskes/llama.cpp_telegram.git
cd llama.cpp_telegram
sudo ./scripts/install.sh
sudo $EDITOR /opt/telegram-bot/.env   # BOT_TOKEN, ALLOWED_USER_IDS
sudo systemctl start whisper-api-compose
sudo systemctl start telegram-bot-compose
```

To deploy a **new version** to a host that already runs the bot, do not
pull on that host: cut a tag, let the CI build the image onto the
release, and let whoever holds the host's access run the host's deploy
script with that tag. See
[How a release reaches production](#how-a-release-reaches-production).

## Documentation

- **[docs/WEBHOOK.md](docs/WEBHOOK.md)** — webhook transport: when to use, how to set up, security, fallback
- **[docs/SETUP.md](docs/SETUP.md)** — installing on a fresh host (for humans); also covers group-mode deployment
- **[docs/AGENT_GUIDE.md](docs/AGENT_GUIDE.md)** — short command reference for AI agents
- **[docs/CODEBOOK.md](docs/CODEBOOK.md)** — the rules this repo learned by breaking them: what a monolith is here, what stops one regrowing, and what a tripwire has to be able to do. Read this before changing anything.
- **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** — what's inside bot.py at a high level; covers both private and group modes, and the release workflow (how a tag becomes a running container)
- **[docs/CALL_LLAMA.md](docs/CALL_LLAMA.md)** — detailed design notes for `call_llama`, polling loop, abort ladder, image MIME detection, config block. The file to read for a code review.
- **[docs/SECURITY.md](docs/SECURITY.md)** — whitelist (private mode), group-mode security, env vars, what NOT to commit
- **[docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)** — common issues, including group-mode gotchas
- **[CHANGELOG.md](CHANGELOG.md)** — release notes for humans
- **[docs/ROADMAP.md](docs/ROADMAP.md)** — where the project goes next: the three open operator decisions, the one manual test, and the research spike that is deliberately last
- **[docs/BASH-SANDBOX.md](docs/BASH-SANDBOX.md)** — `bash_command` killer feature: setup, threat model, repository dependency, compatibility matrix, operator runbook (v0.11.0+)
- **[docs/TOOL-ISOLATION.md](docs/TOOL-ISOLATION.md)** — defence in depth: bot-side tool whitelist against llama-server's native `--agent` / MCP-loaded tools; the security boundary that lets `bash_command` stay safe
- **[docs/README.md](docs/README.md)** — the map of the documentation tree: what is live, what is archived, and what each document answers
- **[docs/TODO.md](docs/TODO.md)** — the active backlog (the only live task list in the repo)
- **[TECH-DEBT.md](TECH-DEBT.md)** — what was measured, deliberately left alone, and what would change that answer

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
browser rendering.

## Credits

- **Developer:** Mavis Agent powered by Minimax M3
- **Code reviews:** Mavis Agent powered by Minimax M3 and Qwen 3.8-Max
- **Vibecoding:** Vaskes
- **Network infra:** Vaskes
- **Human testers:** Vaskes and Dimon (2026-10-12 v0.10.2 release)
- **Deployments and implementation:** Vaskes

## License

MIT
