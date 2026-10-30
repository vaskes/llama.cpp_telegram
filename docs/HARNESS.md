# The harness, component by component

> This document answers one question: **what is this repository, in the
> vocabulary of a production agent harness, and which parts of it are
> deliberate?**
>
> The frame is borrowed from Akshay Pachaar's *The Anatomy of an Agent
> Harness* (2026; the Russian translation circulates on Habr as
> article 1023316). The harness is "the complete software infrastructure
> wrapping an LLM": the loop, the tools, the memory, the context
> management, the state, the error handling, the guardrails. The model is
> the part you swap; the harness is the part you own.
>
> The article walks through **eleven** components under a heading that
> says twelve; the twelfth is either the lifecycle/termination logic or
> observability, depending on which mirror of it you read. We take
> **observability**, because a harness you cannot watch is a harness you
> cannot debug -- and because the eleventh component (subagent
> orchestration) is the one we do not have at all.
>
> Nothing here is a wish list. Every row names a file in this tree, and
> every "guarded by" names a test that fails when the claim breaks.
> The order of work is in [TODO.md](TODO.md) -- the "harness road"
> section; this document is the map the road is drawn on.

---

## 1. The twelve components

| # | Component | What it is | Where it lives here | Guarded by | Verdict |
|---|---|---|---|---|---|
| 1 | **Orchestration loop** | assemble prompt → call the model → parse → run tools → feed back → repeat, until done | `llm_dispatch.py` (the loop), `call_llama.py` (the turn), `llm_iteration.py`, `llm_loop_guard.py` | `tests/test_limits.py`, `tests/test_loop_budget.py` | present, ReAct-shaped |
| 2 | **Tools** | the model's hands: schemas, dispatch, results | `personal_tools/` (`bash_command`, `bash_policy`, `file_transport`), `donsetch.py` (the web tools), `mcp_registry.py` + `mcp_client.py` (the imported ones), `llm_tools.py`, `llm_toolcalls.py`, `llm_toolargs.py`, `tool_isolation.py` | `tests/test_tool_isolation.py`, `tests/test_drift_r8_known_tools.py`, `tests/test_mcp_import.py` | present, and growing without a budget |
| 3 | **Memory** | what survives the session | `storage.py` (SQLite), `storage_summaries.py`, `persistence.py`, `summarize.py` | `tests/test_storage*.py`, `tests/test_history_search.py` | present |
| 4 | **Context management** | what the model is allowed to see this turn | `context_budget.py` (the window), `summarize.py` (the chunked summaries), `reanchor.py` (the re-anchoring) | `tests/test_context_budget.py`, `tests/test_summarize*.py` | present, but no compaction and no observation masking |
| 5 | **Prompt construction** | the layered thing the model reads: system prompt, tool schemas, history, the message | `prompts.py` + `prompt_text/`, `personality.py`, `prompt_guard.py`, `sender_name.py` | `tests/test_prompts.py` (sha256 pins), `tests/test_prompt_rules_are_fixed.py` | present, and pinned |
| 6 | **Output parsing** | the model's words → structured actions | `llm_toolcalls.py`, `llm_json.py` (the malformed-JSON rejection), `llm_result.py`, `llm_stream.py`/`llm_sse.py`/`llm_nonstream.py` | `tests/test_malformed_toolcall.py`, `tests/test_toolargs.py` | present |
| 7 | **State management** | the per-chat runtime state, and the ledger of what a turn did | `state.py`, `turn.py` (the persist/rollback ledger) | `tests/test_turn.py`, `tests/test_state*.py` | present |
| 8 | **Error handling** | what happens when a step fails, and who is told | `llm_abort.py`, the `failure_message()` in `llm_finish.py`, the retries in `http_clients.py` and `donsetch.py` | `tests/test_failure_message_culprit.py`, `tests/test_engine_failure_message.py` | present, and it names the culprit |
| 9 | **Guardrails and safety** | what the model may not do, checked in code, before the wire | `personal_tools/bash_policy.py` (nine command classes: eight destructive ones and the cross-agent order), `access.py` (the allowlist), `tool_isolation.py` (the whitelist), the profile gates in `config.py` | `tests/test_bash_policy.py`, `tests/test_prompt_rules_are_fixed.py` | present -- and the reason the v0.15.1 release exists |
| 10 | **Verification loops** | how we know it works, before the user finds out | the suite the badge counts, the mutation harnesses under `scripts/`, the tripwires in `tests/` | the harnesses themselves; `tests/test_codebook_test_table.py` holds the ledger | present, and the strongest part of the tree |
| 11 | **Subagent orchestration** | agents that spawn agents | **absent** -- one agent per chat; the MCP servers are tools, not agents | -- | **the gap** |
| 12 | **Observability** | what a turn did, at a price and a latency | `logconf.py`, the fatal/ERROR scan, `/healthz`, `/opt/telegrambot/DEPLOYED`, `scripts/smoke_test.py` | `tests/test_console_output_in_chat.py` | thin: logs, no per-turn trace, no cost accounting |

### The two halves of the table

Components 1, 3, 4, 5, 6, 7, 8, 9 and 10 are **built and guarded**: the
code exists, a test fails when the claim breaks, and the claim is
written down once. That is the part of this repository that is worth
keeping whatever model is plugged in.

Components 2, 11 and 12 are the interesting ones:

- **Tools grow without a budget.** Every tool the model is shown costs
  context on every turn and adds a place to pick the wrong function. The
  model is currently shown eight native tools plus whatever the MCP
  servers contribute -- and the production incident that cost this
  project forty million tokens of degenerate generation was, in the end,
  a context that had grown past what the model could hold.
- **There are no subagents.** A long job (crawl a site, analyse a
  repository) runs in the same context that the conversation lives in, so
  the context that matters is the context that is polluted. The article
  is explicit that the value of a subagent is not parallelism but the
  **context firewall**: the child explores for fifty thousand tokens and
  returns a thousand.
- **Observability is logs, not traces.** We can say what the bot did;
  we cannot say what a turn cost, how long each step took, or which tool
  the model actually called when it looped.

---

## 2. The seven decisions

The article's second half names seven decisions every production harness
has already made, and says that most builders get three of them exactly
backwards. Ours, with the honest answer:

| Decision | The obvious (wrong) answer | Ours | Why |
|---|---|---|---|
| **Agent count** | more agents | one agent per chat | A Telegram chat is a conversation with one party. There is nobody to hand work to. |
| **Reasoning strategy** | ReAct everywhere | ReAct, with a turn budget | The loop is the right shape for chat. Plan-and-execute is 3.6x faster on bounded work -- and our bounded work (the shell) is exactly where a plan would pay. |
| **Context strategy** | a fixed window | a budget, a window, chunked summaries, re-anchoring | The bot owns its context; the engine stays stateless. This is the v0.13.0 refactor. |
| **Verification design** | "the model says it is done" | tests, mutation harnesses, tripwires, a human pass on the release | The strongest part of the tree, and the reason the other parts can be trusted. |
| **Permission model** | permissive until it hurts | restrictive where it counts: nine command classes refused in code before the wire; the shell gated to the personal profile | We already paid the permissive bill once (a chat message rewrote the persona). |
| **Tool scoping** | expose everything | whitelist, and MCP tools excluded when they duplicate a native one | "Two shells on one machine are two policies." But see the gap: the *number* of tools is not scoped, only their identity. |
| **Harness thickness** | thin -- the model is smart enough | thick, and deliberately so | The model here is a local 27B on a 780M-class GPU. A thin harness assumes a frontier model with the whole task in its head. Ours has to hold the task. |

### The three mistakes, mapped

The article's three "backwards" answers, and where we stand:

1. **More tools looks like capability and behaves like cognitive load.**
   We are drifting in exactly that direction: the tool list has grown
   with every release, and every tool is shown on every turn. The fix is
   not fewer capabilities but **scoping by turn** -- show the model the
   tools the current message can use, the way the article says Claude
   Code does. We have the machinery for it already: the profile gates and
   the per-server include/exclude lists.
2. **Reasoning at every step is expensive.** Our loop reasons before
   every tool call, including the ones that only needed execution. The
   measured cost is the 40-million-token incident.
3. **Permissive feels fast.** We corrected this one in v0.15.1 -- but
   the *dev* profile is still deliberately wide, by the operator's
   ruling: it is there to be broken.

---

## 3. What the frame changes about the project

Nothing about the code changes because of an article. Three things change
about how the work is described:

1. **The feature list is not the shape of the project.** "Whisper, voice,
   topics, reactions" is what a user sees. The shape of the project is
   the twelve rows above, and the roadmap is the gaps in them.
2. **A missing component is a design decision, not a gap in effort.**
   "No subagents" is a decision with a reason (there is nobody to hand
   work to in a one-party chat) -- and it is a decision that can be
   revisited if the bot ever grows a second surface.
3. **The verification loop is the product.** For a local model on a
   small GPU, the thing that makes the bot usable is not the model's
   intelligence but the harness around it: the rules in code, the
   refusals that name the class, the tripwires that fail when a claim
   breaks. That is where the effort has gone for eight waves, and the
   article agrees that this is the layer that separates a demo from
   production.

---

## 4. Where the road goes

The ordered steps live in [TODO.md](TODO.md), in the section **"The
harness road"**. The short version, in the order we intend to walk it:

1. **One door per service.** The bot has two doors to donsetch: a
   hand-written client that wants the split shape, and the generic MCP
   module. The split shape is a server-side knob, so the generic door
   can have it too. One door, one shape, one policy.
2. **Tools scoped by turn.** The tool list the model sees is decided by
   the turn, not by the deployment. This is the direct answer to the
   article's first mistake, and it is the fix for the degeneration
   incident.
3. **A trace per turn.** Not logs: a per-turn record of the model calls,
   the tools, the tokens and the latency, in a file the operator can
   read. The first version is a JSONL line per turn.
4. **Context firewall for the long jobs.** The shell is the long job
   here. Its output is truncated to a budget today; the next step is
   that a long command's output goes to a file in the sandbox and the
   model gets a summary plus a handle -- the subagent idea, without
   spawning an agent.
5. **Then, maybe, a subagent.** Only if a second surface appears. Not
   before: the article's own point is that a subagent is a context
   firewall, and a firewall with nothing behind it is a cost.

---

*Sources: Akshay Pachaar, "The Anatomy of an Agent Harness" (2026) and
the follow-up on the seven design decisions; the Russian translation on
Habr; the corroborating teardowns in the "Awesome Harness Engineering"
list and the arXiv survey on harness architecture. The article itself is
reachable on X; the Habr page is a translation of it.*
