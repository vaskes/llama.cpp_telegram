# llama.cpp_telegram

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python: 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB)](https://www.python.org/)
[![Docker Compose: v2](https://img.shields.io/badge/Docker_Compose-v2-2496ED)](https://docs.docker.com/compose/)
[![Release](https://img.shields.io/github/v/release/vaskes/llama.cpp_telegram?label=release)](../../releases)
[![Tests: 2983](https://img.shields.io/badge/tests-2983-brightgreen)](tests/)

> **Current stable: `v0.18.0-stable`** (2026-10-28).
> Pin to a tag — see the clone command below.
>
> ⚠️ **Do NOT `git clone <url>` without `--branch`.** The default
> branch is bleeding-edge and may include unreleased refactors
> that have not been human-tested. Pin to a tag:
> ```bash
> git clone --branch v0.18.0-stable https://github.com/vaskes/llama.cpp_telegram.git
> ```
> Bleeding edge is for contributors only. See [Releases](../../releases)
> for the full stable-tag history.

**A Telegram bot that talks to your local LLM — and the harness around it.**

The model is the part you swap. The harness is the part you own: the
loop, the tools, the memory, the context, the state, the errors, the
guardrails. This repository is that harness, written for a 27B model on
a small GPU — a model that cannot be assumed to know which of its tools
it just called, that a message in the chat is data and not an order,
or that `rm -rf` in a sandbox is not a thing to try.

> **What the current release gives you:** the rules that protect the
> host live in code, not in a prompt. Eight destructive command classes
> are refused *before* the command leaves the process, and the refusal
> names the class it fell into; a chat message cannot rewrite them. The
> full release history is in [CHANGELOG.md](CHANGELOG.md).

## What this is

A friendly Telegram bot that talks to your local LLM. Bring your own
model server; the bot works with any OpenAI-compatible endpoint.

The frame is borrowed from *The Anatomy of an Agent Harness*: a
production harness has twelve components, and naming them is what lets
you reason about — and improve — the part that is not the model. Here
is where each one lives in this tree. The full analysis, with the
gaps and the reasoning, is in **[docs/HARNESS.md](docs/HARNESS.md)**.

| # | Component | Where it lives here | |
|---|---|---|---|
| 1 | Orchestration loop | `llm_dispatch.py`, `call_llama.py` | ✅ |
| 2 | Tools | `personal_tools/`, `donsetch.py`, `mcp_registry.py` | ✅ |
| 3 | Memory | `storage.py`, `storage_summaries.py`, `persistence.py` | ✅ |
| 4 | Context management | `context_budget.py`, `summarize.py`, `reanchor.py` | ✅ |
| 5 | Prompt construction | `prompts.py` + `prompt_text/`, `personality.py` | ✅ |
| 6 | Output parsing | `llm_toolcalls.py`, `llm_json.py` | ✅ |
| 7 | State management | `state.py`, `turn.py` | ✅ |
| 8 | Error handling | `llm_finish.py`, `llm_abort.py` | ✅ |
| 9 | Guardrails and safety | `bash_policy.py`, `access.py`, `tool_isolation.py` | ✅ |
| 10 | Verification loops | the test badge, the mutation harnesses, the tripwires | ✅ |
| 11 | Subagent orchestration | — | ⛔ absent, deliberately |
| 12 | Observability | `logconf.py`, `/healthz`, the deploy record | ⚠️ thin |

The two words that matter in that table: **the rules are in code**, and
**every claim is checked**. A destructive command is refused by a
function, not by a sentence in a system prompt; a version number lives
in one place and a test fails when the tree drifts away from it.

## The services

| Service | What it does | How it's deployed |
|---|---|---|
| `telegram-bot` | Polls (or receives webhooks from) Telegram, talks to the LLM, executes tool calls | Docker container |
| `whisper-api` | Transcribes voice messages via faster-whisper, returns the transcript to the bot | Docker container (optional) |
| `bash-sandbox` (personal only) | Runs `bash_command` tool calls in a hardened side-car container with tmux session persistence | Separate repo + Docker ([vaskes/OpenWebUI-bash-sandbox](https://github.com/vaskes/OpenWebUI-bash-sandbox), git submodule) |
| `donsetch-http` (optional) | Web search / fetch / crawl / screenshot, as an MCP server | Separate repo + Docker ([dondai44423/donsetch](https://github.com/dondai44423/donsetch), git submodule) |
| **any other MCP server** | Anything that speaks MCP over HTTP: the bot asks it what it offers and shows the model those tools | Operator's config (`LLAMABOT_MCP_SERVERS`) |

## What the bot can do

- 🐚 **Run shell commands in a sandbox** (`bash_command`, personal mode only) — see the [sandboxed shell](#-bash_command-the-sandboxed-shell-personal-mode-only) below. Defence in depth: profile gate, tool whitelist (T38.5), soft-reject unknown tools (T43/T49), per-chat tmux session.
- 💬 Text, conversation context (last 20 messages per sub-talk / topic)
- 🖼 Image analysis (vision models in llama.cpp)
- 🤤 Voice messages via Whisper
- 📄 Document reading (TXT, PDF — text layer)
- 🛠 **Tool-calling: any tool of any MCP server.** The bot asks an MCP
  server what it offers, shows the model those tools under the name
  `<server>_<tool>`, and sends the model's call back to the server that
  offered it. The servers are the operator's config — the model never
  adds one — and a tool that duplicates a native one is excluded rather
  than renamed. The web tools (`web_search`, `web_fetch`, `web_crawl`,
  `web_screenshot`) are one such server, [donsetch-http](https://github.com/dondai44423/donsetch);
  the bot also keeps a native client for them, because that client
  wants the server's split shape (see "Related" below). `bash_command`
  (sandboxed shell, personal mode only) is the one tool that comes with
  a threat model.
- 🔒 **Whitelist** by Telegram `user_id` / `@username` (env vars, LOCKDOWN by default in private mode)
- 💾 **Persistent conversation history** in SQLite, per (chat_id, thread_id) — survives restarts
- 🧠 **GROUP_CONTEXT system prompt** — LlmChatPlace rules (10-step rating scale, [llm] tag, truth-over-style, no tone policing) injected on every call_llama, in both tool and rating modes. Survives model switches and /reset.
- 👍 **react_to_message tool** — LLM can set a single-emoji Telegram reaction on any message in the current chat (standard 10-step scale, default message_id = user's current). Available in non-rating mode.
- 🛡 **Malformed tool-call JSON rejection** — defensive parse in the dispatcher; runaway Qwen-500 loops (10× duplicated keys) caught and rejected with feedback to the LLM.
- ⏹ **Per-handler Stop button** — the ⏹ on a thinking message aborts THAT handler only. Keyed on (chat_id, thinking.message_id), not (chat_id, user_id) so concurrent handlers don't overwrite each other. **v0.9.1**: image abort now also rolls back the persisted user message, so the next text prompt is not answered about the cancelled image.
- 🎬 **Non-streaming tool-call path** — vision tasks (handle_photo) now run the unified tool loop instead of returning empty when the LLM emits a tool_call on a non-streaming POST.
- 🧵 **Two deployment modes**:
  - **Private chat** — 1 user, multi-thread via `/newsub` `/sub` `/subs` (in-DB sub-talks)
  - **Group with Telegram Topics** — N users, native forum topics via `/newsub` (creates a Telegram topic), `/subs` (lists topics), `/delsub` (deletes a topic). Replies stay in the topic where the user is.

**Two transports:** long-polling (default, no infrastructure required)
and webhook (sub-second latency, requires a public endpoint with TLS).
See [docs/WEBHOOK.md](docs/WEBHOOK.md) for when to use which and how
to set up webhook mode.

## 🐚 `bash_command`: the sandboxed shell (personal mode only)

> **The shell, in a container.** The model can call a `bash_command`
> tool that runs shell commands in a hardened sandbox container: read
> files, run scripts, manage projects, remember state across calls in a
> persistent tmux session. A harness needs a shell to do anything at
> all, so this is ordinary equipment — and the one tool that comes
> with a threat model.
>
> **🚨 DO NOT ENABLE IN PUBLIC ANONYMOUS CHATS.** The
> `bash_command` tool is gated to `LLAMABOT_DEPLOYMENT_PROFILE=personal`
> and turns the bot into a general-purpose remote-code-execution
> surface. A malicious user with chat access could trick the
> LLM into running destructive commands inside the sandbox.
> The sandbox contains the blast radius, and the bot refuses the
> destructive command classes **in code** — but a determined
> attacker can still exfiltrate data, mine crypto, or attack the
> network the sandbox is on. Personal mode means: the operator
> trusts every user who can reach the bot. Public mode means:
> strangers, and you do not.
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

## Transports

The bot can receive Telegram updates via two mechanisms:

| Transport | Latency | Setup | When to use |
|---|---|---|---|
| **Long-polling** (default) | Up to 25s | None — just run the bot | You don't have a public domain/TLS, behind strict NAT, or doing development |
| **Webhook** | <1s | DNS + reverse proxy + `WEBHOOK_MODE=1` | You have a public domain with TLS and want real-time updates |

Polling has been the default for years and is rock-solid. Webhook
became available in v0.9.0 and is recommended in production where
real-time response matters. See [docs/WEBHOOK.md](docs/WEBHOOK.md)
for the full setup guide.

## Architecture

The bot is one process. It owns a connection to Telegram, one to the
LLM, one to Whisper (voice only), and one to **each MCP server** it
has been configured with — the sandbox side-car and donsetch among
them. The exact addressing is a deployment choice; the diagram below
shows the conceptual wiring.

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
                          │   │ ────────────────────▶│  │MCP  │   │
                          │   │  (any server:        │  │server│  │
                          │   │   donsetch, the      │  └─────┘   │
                          │   │   sandbox, ...)      │  ┌─────┐   │
                          │   │  POST /bash          │  │bash │   │
                          │   │ ────────────────────▶│  │sandbox│ │
                          │   │  (PERSONAL only,     │  └─────┘   │
                          │   │   bearer auth)       │            │
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
- **bot → an MCP server** — `POST /mcp` (JSON-RPC over HTTP, MCP
  protocol). The bot asks the server what it offers and shows the model
  those tools under the name `<server>_<tool>`. Which servers, which of
  their tools and in which chats is the **operator's** config
  (`LLAMABOT_MCP_SERVERS`) — the model never adds a server. A tool that
  duplicates a native one is excluded, not renamed. See
  [docs/TOOL-ISOLATION.md](docs/TOOL-ISOLATION.md).
- **bot → bash-sandbox** (personal mode only) — `POST /bash` with
  `Authorization: Bearer <token>`. The bot forwards the model's
  `bash_command` tool calls; the sandbox returns stdout / stderr /
  exit_code. See [docs/BASH-SANDBOX.md](docs/BASH-SANDBOX.md) for
  the full architecture, threat model, and operator runbook.

**Network layout:** the bot does not require any particular
container networking mode. The two reference layouts are

1. **Single host** — all the services run on the same host; the
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
git clone --branch v0.18.0-stable https://github.com/vaskes/llama.cpp_telegram.git
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

- **[docs/HARNESS.md](docs/HARNESS.md)** — the harness, component by component: what the twelve components are, where each one lives here, what is guarded, what is missing, and what we decided about it
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
- **[docs/BASH-SANDBOX.md](docs/BASH-SANDBOX.md)** — the sandboxed shell: setup, threat model, repository dependency, compatibility matrix, operator runbook (v0.11.0+)
- **[docs/TOOL-ISOLATION.md](docs/TOOL-ISOLATION.md)** — defence in depth: bot-side tool whitelist against llama-server's native `--agent` / MCP-loaded tools; the security boundary that lets `bash_command` stay safe
- **[docs/README.md](docs/README.md)** — the map of the documentation tree: what is live, what is archived, and what each document answers
- **[docs/TODO.md](docs/TODO.md)** — the active backlog (the only live task list in the repo)
- **[TECH-DEBT.md](TECH-DEBT.md)** — what was measured, deliberately left alone, and what would change that answer

## Requirements

- Linux with Docker + Docker Compose v2
- Passwordless `sudo` (for systemd)
- Any llama.cpp-compatible server on port 8080 (or your value in `.env`)
- Optional: an MCP server on port 8765
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

It is pinned here as a git submodule (`donsetch/`), the same way the
sandbox side-car is pinned: the version the bot is tested against is the
version the tree carries, and `tests/test_donsetch_contract.py` fails if
the two drift apart. The pin itself lives in
`telegram-bot/personal_tools/donsetch_contract.py` -- one place, so the
documents cannot quote a version of their own. The sandbox is MIT; this
one is AGPL-3.0 -- a submodule is a separate work with its own terms.

The bot also keeps a **native client** for those four tools, next to the
generic MCP door. The native client wants the server's *split shape* — a
human-readable text result instead of the MCP `[meta]` envelope — which
the server produces with `DONSETCH_MCP__TEXT_ONLY=false`. Two doors to
one server is a decision, not an accident: it is written down, with the
plan to collapse it into one, in [docs/HARNESS.md](docs/HARNESS.md#1-the-twelve-components).

## Credits

- **Developer:** Mavis Agent powered by Minimax M3
- **Code reviews:** Mavis Agent powered by Minimax M3 and Qwen 3.8-Max
- **Vibecoding:** Vaskes
- **Network infra:** Vaskes
- **Human testers:** Vaskes and Dimon (2026-10-12 v0.10.2 release)
- **Deployments and implementation:** Vaskes

## License

MIT
