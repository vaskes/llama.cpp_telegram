# Tool Isolation

The llama.cpp server the bot talks to can be launched with
`--agent` and / or `--mcp-servers-config`. Both give the model
access to **native tools that the bot does not manage** — a
shell tool, an exec tool, MCP-loaded tools, etc. This document
states how the bot keeps the two tool sets disjoint and never
executes anything it did not define.

## Threat model

**Trusted:**

- The bot's own code (this repo).
- The set of tool names hard-coded in `telegram-bot/call_llama.py`
  (`bot_side_tool_defs` plus `bash_command` from
  `telegram-bot/personal_tools/bash_command.py`).
- The vaskes/OpenWebUI-bash-sandbox side-car that runs
  `bash_command` (3-wave reviewed upstream, pinned at v2.3.0
  in the `sandbox/` submodule).

**Untrusted:**

- The LLM. It can be prompt-injected, jailbroken, or simply
  confused.
- llama-server's `--agent` tools (shell, exec, etc.) if
  llama-server is launched with `--agent`.
- llama-server's MCP-loaded tools if it is launched with
  `--mcp-servers-config`.
- Any tool name the LLM hallucinates (the LLM can emit any
  string in `tool_calls[i].function.name` and llama-server
  will not stop it).

**Attack:** the LLM emits a `tool_call` for a native llama
tool (e.g. `shell`) instead of one of the bot's tools. If
the bot blindly dispatches, the native tool runs in
llama-server's process context with no sandbox, no profile
gate, and no rate limit.

## How the bot keeps the sets disjoint

The isolation property lives on the **bot side**, not on the
llama side. llama-server is free to be configured however
the operator wants; the bot just refuses to cooperate with
anything outside its whitelist.

1. **Closed whitelist** in `telegram-bot/tool_isolation.py`:

   ```python
   KNOWN_TOOL_NAMES = frozenset({
       "donsetch_web_search", "donsetch_web_fetch",
       "donsetch_web_crawl",  "donsetch_web_screenshot",
       "react_to_message",
       "bash_command",
       "send_file",
       "delete_messages",
   })
   ```

   This is the only set of tool names the bot will ever
   dispatch. Adding a tool = adding a name here AND shipping
   a handler.

2. **Three guards, and they are not all in one place** — at every place the
   bot reads `tool_calls` out of a llama-server response:
   - non-streaming primary path,
   - streaming fallback path,
   - streaming primary path (where tool names come in
     chunked, one fragment at a time).

   Each guard calls `validate_tool_calls(tool_calls)`. If the
   function name is not in `KNOWN_TOOL_NAMES`, the guard
   **refuses** the whole response and returns a clear
   message to the user:

   > *[bot refused: llama-server returned a tool_call for a
   > name not in the bot's whitelist. This means llama-server
   > is configured with --agent or MCP-servers-config, and
   > the model is reaching for a native tool the bot does
   > not manage. …]*

3. **Tests pin the invariant** in
   `tests/test_tool_isolation.py`:
   - `test_known_tool_names_includes_required_bot_tools` —
     if a new bot tool ships without being added to the
     whitelist, this fails.
   - `test_known_tool_names_does_not_advertise_native_llama_tools` —
     if someone accidentally adds `shell`, `exec`, `agent`,
     etc. to the whitelist, this fails.
   - `test_validate_tool_calls_rejects_native_shell_tool` —
     the single most important test; if this ever fails,
     the bot is one llama config change away from running
     arbitrary host commands.

## What this document does NOT do

- It does not change how llama-server is launched. The
  operator decides whether to use `--agent` and
  `--mcp-servers-config`; that decision is orthogonal to
  tool isolation on the bot side.
- It does not propose a separate llama instance. A
  single-instance deployment is fine **as long as the bot
  guards every tool_call against its whitelist** (which
  this commit makes it do).
- It does not require the operator to remember anything.
  If a future change adds `--agent` to llama-server
  without updating the bot, the bot's guards catch it at
  runtime and refuse to dispatch.

## Maintenance checklist (when adding a new tool)

When adding a new bot tool, the following three things must
ship together in the same commit:

1. The tool definition (name, description, parameters) in
   `telegram-bot/call_llama.py::bot_side_tool_defs`; the two runtime
   sites now live in `llm_toolcalls.py` (`normalize_tool_calls` for the
   streaming buffer, `reject_unknown_names` for the fallback response),
   and argument screening is `llm_toolargs.py` (or in
   a `personal_tools/` module if it is profile-gated).
2. The handler — wherever the bot dispatches that tool,
   e.g. `bot.py:dispatch_one_tool` for donsetch_* tools,
   `personal_tools/bash_command.py:call_bash_command` for
   `bash_command`.
3. The name added to `KNOWN_TOOL_NAMES` in
   `telegram-bot/tool_isolation.py`.

Skipping step 3 will fail the
`test_known_tool_names_includes_required_bot_tools` test on
CI, which is the intended tripwire.

## Failure mode summary

| llama-server config | Bot behavior |
|---------------------|--------------|
| No `--agent`, no MCP | Model emits `bash_command` / `donsetch_*` / `react_to_message`. Bot dispatches as normal. |
| `--agent` enabled, model emits `bash_command` | Bot dispatches via sandbox side-car. (llama may or may not also try to call its own `shell`; if it does, the bot's guard refuses to dispatch it.) |
| `--agent` enabled, model emits `shell` | **Bot refuses.** Returns a clear message. No host shell command runs from the bot. |
| `--mcp-servers-config` with extra tools, model emits one of them | **Bot refuses.** Same path. |
| Model hallucinates a tool name | **Bot refuses.** Whitelist miss. |

The end state: the bot's tools run in their hardened
side-cars (donsetch MCP for web tools, vaskes/OpenWebUI-bash-sandbox
for shell), and any tool the LLM tries to reach for that the
bot did not define is blocked before it can do anything
harmful. llama-server's own tool surface, however powerful,
is invisible to the bot's dispatch path.
