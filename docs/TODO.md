# docs/TODO.md — the active backlog

> This is the **only** live backlog in the repo. Everything else that looks
> like a task list is one of:
>
> - `CHANGELOG.md` — what shipped, and when;
> - `TECH-DEBT.md` + `docs/tech-debt/` — what was measured, deliberately
>   left alone, and what would change that answer;
> - `docs/tickets/` — the context-memory block (T65–T70), shipped, with
>   the open questions recorded;
> - `docs/archive/` — dated records, kept for the "how did we get here",
>   not for instructions.
>
> **Updated 2026-10-23**, against the `v0.13.0-stable` tag.
>
> The previous version of this file was last touched 2026-10-13 and had
> accumulated four kinds of rot: a status snapshot from before the
> context-memory rewrite (it said the bot ran v0.9.1), a checklist for an
> operator-test file that no longer exists, a "code freeze" from a
> pre-`v0.10.2` cycle, and eleven ticket entries marked open that had been
> closed for months. It was the same failure the `TECH-DEBT.md` index had:
> a second copy of the truth, maintained by hand, drifting.

---

## State of the tree (measured, not remembered)

| | |
|---|---|
| HEAD | 116 commits past the `v0.13.0-stable` tag (trailer cleanup, codebook, the `dev` limits profile, the Stop-button gate, the answer-files feature, the offline replay of it, the chunked summaries, the removal of edit-replace, the answer split, the smoke-test script, the troubleshooting guide, the smoke script's log window, the sandbox file door, the human-readable failure notice, the English UI and its tripwire, the command menu in webhook mode, the native code block, the boot readiness, the sandbox contract, the transcript that duplicated a delivered file, the writable rootfs and the pin that follows it, the lost answer that came back as an empty success, the stdin trap that eats the side-car's end marker, the tool that takes a message back out of the chat, the split of the chunk summaries out of `storage.py`, the Telegram id of every stored message, the search over the bot's own record, the Telegram id of every message the conversation shows to the model, the record that holds a message once, the MCP module, and the MCP import that is on in production) |
| Production | **deployed and verified** (2026-10-27, code commit `77805ba`; the container was recreated at 19:01 MSK when the MCP block was added -- the same image, a new environment). **The bot has two classes of tools now**: the native ones (Telegram, the shell, the record) and the ones imported from MCP servers -- the bot asks a server what it offers, shows the model those tools under the name `<server>_<tool>`, and sends the model's call back to the server that offered it. The servers are the operator's: one config block (`LLAMABOT_MCP_SERVERS`, a JSON array), and the repository ships with none configured. **Production has had one since 2026-10-27 19:01 MSK**: the bot's own side-car (`http://127.0.0.1:8090/mcp` -- the same host-network loopback the shell tool already used), with `include: read_file, file_info`, `exclude: bash, delete_file` and `gate: dev-chat`. Measured on the deployed tree after the switch: the registry inventories 2 tools, the model is shown 10 (8 native + 2 imported), a chat outside the allowlist is shown none and its call is refused before it reaches the wire, and a live turn called `sandbox_read_file` on a real file in the sandbox and the side-car's answer came back. The imported `bash` is excluded on purpose -- the bot already has `bash_command` against the same side-car, and two shells on one machine are two policies. The engine's own tools are **not** offered to the model -- the bot cannot execute them, and its `donsetch_web_search` carries a different schema under the same name. The bot container runs `77805ba` with 0 restarts, `/healthz` is 200, the fatal scan is 0 and the ERROR count is 0. **The conversation shows the model the Telegram id of every message it carries** (a `[telegram id N]` line of its own, directly above the text): a message the user names by what it wrote ("Сообщение 1") now resolves to an id instead of being guessed. Measured on the deployed tree by replaying the operator's own turn ("Удали сообщение 1") against the real engine, the real store and the real tools -- with only the Telegram boundary replaced, so nothing in the operator's channel was actually deleted: the model called `delete_messages({'message_ids': [3369]})`, and 3369 IS "Сообщение 1". The same turn on the previous tree called **no tool at all** (`tool_calls=0`, 9851 characters of reasoning) and asked the operator to clarify, after answering with two ids that belonged to the BOT's own messages. The guard did not move: the dispatcher still asks the record whether it holds the id, and the record is not touched by a deletion. The chat's own record is searchable (`search_messages`): the `messages` table carries the Telegram id of the message every row came from, the tool reads the record with informal filters (a substring of the body, a substring of the sender's name, or nothing for the newest messages), and the ids it returns are in reach for `delete_messages`. The whole chain was walked on a COPY of the production database (never on the live one): the search found the row, the record vouched for its id and not for an invented one, Telegram was asked to delete the message, and **the row was still there afterwards** -- the rule is that the chat is a surface and the record is not. The rows before the column existed stay NULL; every new message carries its id. **The record now holds a message once**: the group-absorb path asks the record whether it already holds the message that was replied to, and stores it only if it does not -- by `telegram_message_id`, because a message id is unique inside its chat and its text is not. Measured on the deployed tree against a COPY of the production database: the record holds the operator's message id 3379 twice (rows 10690 and 10693), the lookup answers True for that id and False for an id nobody wrote -- so the next absorb would not write it again, and a message the record does not hold is still written. The live database was not touched: 349 rows before and after, one duplicated id. The rows that are already duplicated stay where they are -- deleting rows from the operator's record is not ours to do |
| Pytest | `2729 passed, 20 skipped` locally (the live-sandbox file is excluded, as in CI) |
| CI | `lint.yml`: ruff (advisory) + pytest on Python **3.11** and **3.12** -- green on `1a1fa59` (`lint` + `Push on main`, both success) |
| Production interpreter | Python **3.11** (`telegram-bot/Dockerfile` pins `python:3.11-slim`) |
| Production modules | 63 `.py` files in `telegram-bot/` (`mcp_client.py` and `mcp_registry.py`) (plus `personal_tools/`, `prompt_text/`, `commands/`) -- the sandbox contract lives in `personal_tools/`, so the top-level count did not move |
| In-tree selftest | `selftest.py` (239 lines) + 7 leaf modules, `_selftest` = 111 lines |
| Open review cycle | none — waves 1–8 closed just before the `v0.13.0-stable` tag ("production-ready") |

Sizes are `splitlines()`-based and are recomputed by
`tests/test_tech_debt_index.py`, which fails if the numbers in
`TECH-DEBT.md` go stale.

---

## Open — waiting on a decision from the operator

These are not forgotten; they are parked on a question only the operator
can answer. Each one records the decision that unblocks it.

### The bot delivers a file twice — the fix is in production; nobody has watched it happen

**Status:** open since 2026-10-25. The deploy half is done — the fix is
in production. What is left is the human half: nobody has asked for a
file in the chat since and counted the copies.

**Measured by the operator, 2026-10-25.** He asked for an HTML file in the
group chat. The document arrived — 595 bytes, the right bytes — and then a
second message arrived carrying the same HTML as a code block: the
transcript of the heredoc the model wrote the file with. A heredoc command
carries the whole file in its own text, and the sandbox answers with the
pane, which shows what was typed — so the file was in the transcript
twice. The same thing happened with a `.sql` file. The user asked for a
file and got the file and a copy of it.

**What is done:** `9752b06` (`fix(chat): a delivered file is not delivered
twice`). A file that reached the chat takes the transcript with it. Three
things stay as they were: the transcript still goes to the chat when **no**
file was delivered (there it is the only witness of what the model did —
the operator's rule of 2026-10-24); a delivery that **failed** is not a
delivery (its output is then the only witness); and the transcript still
goes into what the bot remembers (`raw`) — delivery and memory are two
artefacts. The files an answer carries — pulled out of the model's own
text — are not counted as a delivered file.

**What is not:** nothing about the deploy. The production tree is
`77805ba` (2026-10-27), and `9752b06` is an ancestor of it, so the code
that duplicated the file is not in production. What is missing is a pair
of human eyes: nobody has asked for a file in the chat since and counted
the copies. This entry used to say "the deploy waits for the operator's
word" — it was written before the deploy, and a copy of a moment is not a
fact about the tree.

**Verification:** `tests/test_trace_not_duplicating_a_delivered_file.py`
(six tests, with the command taken from the production log and the output
from the live sandbox) and
`scripts/mutation_harness_trace_dup.py` (eight mutations, five caught,
three survived as intended).

### The answer-files feature has never been checked by a human — the machine check passed on the live engine

**Status:** open since 2026-10-24. The feature is implemented, tested and
pushed (`e8e4ff0`) and **deployed** (the production tree is `77805ba` as of
2026-10-27; it has moved on since the deploy). What is still missing is a
human looking at the chat:
the machine check cannot see what the user sees.

**What the operator measured:** the production host runs on a generator, and
any request to the model overloads it. So for most of that day the bot
could not be exercised against the real engine — not because the code is
wrong, but because the box could not take the load. (Earlier the same day
the host also had no power at all.) The host came back at 19:13.

**What was done while the host was up:** the deployed container was run
through a real turn with only the Telegram transport replaced — the real
handler, the real prompts, the real engine, the real chunking and the real
file extraction, with the database pointed at a temporary file. The answer
was 5856 characters and went out as two messages (4000 + 1856) that rejoin
character for character, and the code block left the chat as a document
named `answer.py` (412 bytes). That is a machine reading of the transcript;
it is not the same as a person seeing the file arrive in the chat.

**What was done while the host was down:** `tests/test_artifact_e2e.py` —
the real handler, the real parser, the real sender and the real database,
with the engine replaced by the answer shapes that were measured on the
live engine on 2026-10-24. It runs in CI, and as a script it prints the
transcript of each shape:

    python tests/test_artifact_e2e.py

**What is still missing, and what it needs:** the live check itself, once
the generator can take the load. The checklist:

1. ~~deploy the current `main` to the production bot~~ -- done 2026-10-24
   19:15; the tree in the container is `77805ba` as of 2026-10-27;
2. ~~ask the model for a json, an html page, a site in three files, and one
   plain question with no code at all~~ -- the operator asked for a sql file
   and an html page; both arrived;
3. ~~check that the code arrives as a **document** with the right name and
   extension, and that the chat gets the prose and not the code~~ -- done:
   `beer_log.sql` and `index.html`, in the topic, prose in the chat;
4. ask a follow-up about the file ("поменяй в нём цвет") and check that
   the bot still remembers the code -- that is the whole point of keeping
   the raw answer in the history;
5. ~~check the topic: a document sent from a forum topic has to land in
   that topic, not in General~~ -- done, both documents landed in the topic;
6. check that the reasoning-only answer still arrives as a diagnostic and
   not as a document.

**Decision needed:** who runs the rest of the checklist. The host can take
the load now, so the only thing missing is a pair of human eyes -- the
machine half of it was done on 2026-10-24 (see above), and the operator has
already done steps 1, 2, 3 and 5.

### The context blocker is not closed — and its list is growing

**Status:** open. The operator restated this after the 2026-10-23 test
session: T65–T70 shipped and are recorded as closed, but the underlying
blocker -- *how the bot keeps a coherent context over a long
conversation* -- was covered by that work, not solved by it. The pieces
below are all parts of it, and the list is growing rather than shrinking:

| Piece | What it still leaves open |
|---|---|
| T68b | the engine keeps a KV cache for context that already fell out of the window, and the reset command is unreachable |

Closing the context-memory tickets closed *a* set of tasks. It did not
close this. Treat the row above as the blocker, and do not read a green
board here as "the context problem is done".
(T70.6 left this table on 2026-10-23: the operator's decision was that
the thinking cap belongs to llama-server, so the bot-side knob was
deleted rather than implemented -- see that entry.
T70.5 left this table on 2026-10-24: the operator decided on chunked
summaries with a "СВЕДЕНО" marker in the database, and it is
implemented -- see that entry.
T66.1 left this table on 2026-10-24: the operator's answer was recorded
on 2026-10-20 -- "not adding" -- and it was never a piece of this
blocker. See the closed list.)


### T68b — `BOT_ENGINE_RESET_CMD` is unreachable in every mode

**Status:** deferred by the operator 2026-10-22, as part of a related
group rather than as a standalone defect.
**Where:** `docs/adr/ADR-0003-context-reanchoring.md`.

Two mutually exclusive gates. `start_reanchor_scheduler()` returns
immediately when `SOFT_REANCHOR_ENABLED` is set — that is, whenever the
mode is not `soft` — so in `process+engine` there is no in-process
scheduler at all. And inside the loop the engine reset only runs when
`REANCHOR_MODE == "process+engine"`. The two sets do not intersect.

The command is needed exactly in `process+engine`, which is the default:
the bot restarts the operator on a schedule, and the engine keeps holding
a KV cache for context that has already fallen out of the window. Nobody
can execute it.

**In production `BOT_ENGINE_RESET_CMD` is not set**, so the wiring is dead
but harmless: there is nothing to execute.

Two options, both waiting on an operational decision:

- **(a)** a second in-process task that sleeps until the same boundary
  (`BOT_REANCHOR_AT`) and calls `run_engine_reset()` in `process+engine`.
  The boundary arithmetic already exists and is DST-correct
  (`seconds_until_next`); it was written for the other loop.
- **(b)** move the command into the operator's cron/systemd unit and drop
  `BOT_ENGINE_RESET_CMD` from `.env.example` and ADR-0003.

(a) is technically cleaner — the bot already owns the boundary computation
and the setting is advertised as the bot's own. But it is an operational
decision: the bot would restart `llama-server` once a day and stop
answering for the duration. **Implementation of (a) needs the operator's
explicit go-ahead.**

### RETHINK — the wording of `--reasoning-budget-message`

**Status:** open, **assigned to the operator** (vaskes), 2026-10-23.
**Where:** the engine unit on the host,
`/etc/systemd/system/llama-qwen5090-256k.service`. The repo's
`systemd/` holds only the compose units, so this file is host-only and
is not versioned here.

Added to the unit, with a `RETHINK` marker in the comment above the flag:

    --reasoning-budget-message "Reasoning budget exhausted. Stop thinking and give the user the final answer now."

**What it does, measured** on the build this box runs (llama.cpp
0.4.1-dev, commit b49650a). The reasoning-budget sampler counts the
tokens inside the thinking block; when the budget runs out it forces a
token sequence, and the sequence is the message followed by the first
end-of-thinking tag (`tools/server/server-schema.cpp`: the message is
*prepended* to the tag, so the tag must NOT be written into the message
yourself). Measured here with a per-request budget of 64 tokens:

| | what the model did after the cut |
|---|---|
| without the message | continued the interrupted sentence (`17 × 23 = 17 × (20 + 3`) and only then answered |
| with the message | wrote the final answer immediately (`**17 × 23 = 391**` plus the full calculation) |

Both the budget and the message can be set per request
(`thinking_budget_tokens`, `reasoning_budget_message` in the request
body), and the per-request budget overrides the CLI one -- so the
behaviour was verified **without restarting the engine**.

**Why it is flagged RETHINK and not "done":** the operator asked for the
flag to be added and the call handed back to them, to be revisited after
the current test run. Two things are still open:

1. the wording is English, and it is forced *inside* the model's own
   reasoning -- it is an instruction to the model, not a user-visible
   message, and it will show up in the reasoning trace if the model
   echoes it;
2. it only fires when the 64K thinking budget is actually exhausted. The
   2026-10-23 Newton's-cradle run reached ~57-58K reasoning tokens and
   then moved to the tool loop on its own, so the cut never fired there.

**What it needs:** the operator's call after the test run -- keep the
wording, change it, or drop the flag again.


## Accepted as documented risk — not a bug, not a task

### T-FREEZE-1 — the semaphore LRU can evict a held semaphore

`_user_semaphores` is an LRU capped at `_USER_SEMAPHORES_MAXSIZE`, and the
eviction does not check whether the semaphore it drops is currently held.
The next message from that user therefore gets a *fresh* semaphore with
the full `_PER_USER_SEMAPHORE_LIMIT` budget, so the per-user concurrency
cap can be exceeded.

Accepted for the current deployment: the whitelist is short, so the dict
holds a handful of entries and never approaches the 500-entry cap
(`_USER_SEMAPHORES_MAXSIZE`). Revisit if the deployment scales to many
users or if LOCKDOWN is relaxed.

`state._get_user_semaphore` is the function; the cap is
`_PER_USER_SEMAPHORE_LIMIT` in the same module.

### T-FREEZE-STOP-1 — the Stop button "does not always work"

**Not reproducible.** The operator pressed Stop 100 times after the
deploys stopped, and every press stopped the turn.

What the surviving log shows: the container was recreated on every
deploy, so only the last instance still has its stdout. It holds four
Stop presses -- four `abort_event set`, four `ABORTED by user via Stop
button`, four rolled-back persisted turns, zero failures, zero
rejections.

The earlier misses cannot be diagnosed: the containers that saw them
were removed and their logs went with them. Two paths in
`telegram-bot/commands/callback.py` can produce that symptom with the
button itself blameless -- the press arrived while the webhook was
down (Telegram gets a 502 on the POST and drops the update, so the
callback never reaches the bot), or the turn had already finished (the
handler answers "already done" and logs `no abort_event for ...`).

The rule stays maker-taker: only the author of the message may stop
its processing, and the check is `clicker_id != original_user_id`
(`telegram-bot/commands/callback.py`).

### T-FREEZE-2 — `call_llama` was a god function

**Closed, differently.** It was not "observed in production long enough":
it was measured, split, and measured again. `call_llama.py` is now 478
lines with 17 `llm_*` leaf modules, and the tool loop is `ToolLoop.run_one`
/ `ToolLoop.run`. See `TECH-DEBT.md` items 3 and 8.

### T-FREEZE-3 — `BOT_COMMANDS` mixes Russian and English

**Closed, by decision (2026-10-25).** The operator ruled that every text
the *user* reads is English, and the command menu is user-facing text --
`setMyCommands` is what puts the descriptions next to the commands in the
chat input. The descriptions are English now; the pin on the content
changed with them (`785a53d39ee03f95` → `03d635c9d463329a`).

The model's own voice stayed Russian: the persona, the summariser prompt
and the context header are addressed to the model, not to the user.
`tests/test_ui_language.py` draws that line mechanically -- a Cyrillic
literal outside a docstring has to be vouched for by module and fragment.

Fixing the text also exposed a defect the text was hiding: webhook mode
never registered the menu at all, so the chat kept the previous run's
menu. See `CHANGELOG.md` and `tests/test_webhook_menu_registration.py`.

### T-FREEZE-4 — `_RATING_PREFIX_RE` / `_EMOJI_CHAR_RE` were unified

False alarm — the Qwen report was wrong. Already correct.

### T-FREEZE-MT-1..23 — the operator's manual checklist

Executed against `v0.10.2` by the operator and a second tester (Vaskes and
Dimon, 2026-10-12). The only item that was deliberately not done is the
live edit-replace scenario — and the feature it covered was removed on
2026-10-24, so the scenario is no longer part of the checklist.

---

## Closed in the current cycle — where to read it

Not repeated here; each line points at the record.

| Work | Record |
|---|---|
| Context memory T65–T70 | `CHANGELOG.md` § v0.13.0-stable; `docs/tickets/README.md` |
| `dispatch.py` 1612 → 345 | `TECH-DEBT.md` #10; `docs/tech-debt/10-*.md` |
| `call_llama` 1616 → 478 | `TECH-DEBT.md` #3, #8 |
| `_selftest` 1940 → 111 | `TECH-DEBT.md` #9 |
| `bot.py` → 124-line facade | `TECH-DEBT.md` #1 |
| `handlers.py` shared wrapper | `TECH-DEBT.md` #13 |
| `prompts.py` prose → `prompt_text/` | `TECH-DEBT.md` #12 |
| Env-var name validation | `TECH-DEBT.md` #11 |
| The edit-replace feature, removed | `CHANGELOG.md` § Unreleased; `docs/tech-debt/10-*.md` |
| A binary file from the sandbox reaches the chat | `CHANGELOG.md` § Unreleased; `docs/BASH-SANDBOX.md` § Moving a file out of the sandbox |
| T66.1 — the guaranteed minimum, decided | `docs/tickets/README.md` (open question 3); `docs/tickets/TICKET-T66-*.md` |
| T70.5 — chunked summaries with a "СВЕДЕНО" marker | `CHANGELOG.md` § Unreleased; `docs/tickets/TICKET-T70-*.md` |
| T70.6 — the reasoning cap lives on the engine | `CHANGELOG.md` § Unreleased; `docs/CALL_LLAMA.md` |
| Review waves 1–8 | `docs/archive/2026-10/`; `docs/review/README.md` |
| The side-car on the production host, with a writable rootfs | `CHANGELOG.md` § Unreleased; `docs/BASH-SANDBOX.md` § The sandbox's posture |
| The bot can take a message back out of the chat | `CHANGELOG.md` § Unreleased; `docs/CODEBOOK.md` § X-10 |
| The record holds a message once | `CHANGELOG.md` § Unreleased; `docs/CODEBOOK.md` § X-13 |
| One person, one name in the record | `CHANGELOG.md` § Unreleased; `docs/CODEBOOK.md` § X-16 |
| The empty-answer fallback reaches the user | `CHANGELOG.md` § Unreleased; `docs/CODEBOOK.md` § X-17 |
| The MCP import, on in production | `CHANGELOG.md` § Unreleased; `docs/CODEBOOK.md` § X-14 |
| The deploy workflow, with the topology on the host | `CHANGELOG.md` § Unreleased; `docs/CODEBOOK.md` § X-18 |
| The bot's pin, and the side-car it talks to | `CHANGELOG.md` § Unreleased; `docs/CODEBOOK.md` § X-8 |

---

## Closed — and why (the reasoning behind the closed items)

The items below were decided and are done. They are here because the
*reasoning* is worth keeping, not because they are pending.

### The MCP import is on in production

**Status:** ✅ **closed 2026-10-27, 19:01 MSK.** The operator decided to
switch it on: the bot's own side-car, the shell excluded, the gate on
the allowlisted chat. The production `.env` carries the one line that
says so, and the deployed bot talks to its side-car over it.

The line, as it stands in `/opt/telegrambot/bot/app/.env`:

```json
[{"name": "sandbox", "url": "http://127.0.0.1:8090/mcp",
  "token_env": "AGENT_STACK_TOKEN",
  "include": ["read_file", "file_info"],
  "exclude": ["bash", "delete_file"],
  "gate": "dev-chat"}]
```

**The gate stayed `dev-chat`.** `always` would have handed the sandbox's
files to every chat in the forum supergroup; the capability is wanted in
the operator's chat, not in everyone else's. The profile is `dev`, and
`dev-chat` in a `dev` profile means exactly the allowlisted chats —
measured, not assumed: the registry shows 2 tools to the allowlisted
chat and 0 to any other, and a call from a chat outside it is refused
before it reaches the wire.

**Why `bash` is excluded:** the bot already has `bash_command`, and it
talks to the SAME sandbox. Two shells with two policies is one too many
— the model would have a native tool and an imported twin of it. The
exclusion is visible in the inventory: the model is shown 10 tools, of
which 8 native, and there is exactly one shell between them.

**Why `delete_file` is excluded:** the bot has no destructive file tool
and does not need one; the operator can turn it on later.

**Measured on the deployed tree, after the switch:** the registry
inventories 2 tools (`sandbox_read_file`, `sandbox_file_info`); the model
is shown 10 (8 native + 2 imported); `sandbox_file_info` on a 668 KB
binary answers with the file's facts (name, size, mime, mtime) and
`sandbox_read_file` on a text file answers with the text; the earlier
live check on the same tree showed a binary arriving as
`[binary file: ... -- the content is not inlined]` and a path outside
the workspace refused by the sandbox with its reason. A live turn — the
real engine, the real tools, the real dispatcher, only the Telegram
boundary replaced — called
`sandbox_read_file({"path": "/workspace/.agent-sessions.json"})` and the
side-car's answer came back to the model.

**What it cost:** one line in the operator's `.env`, and a container
recreate. `env_file` is read when the container is created, so a
`restart` would have kept the old environment and the old silence — a
restart that looks successful and changes nothing. The backup is in
`/opt/telegrambot/backups/pre-mcp-20261027-190154/`.

### The side-car on the production host — v2.10.2, with a writable rootfs

**Status:** ✅ **closed 2026-10-26** — and **the version moved twice on
2026-10-27**: the host ran **v2.12.1** (`a396101`) and now runs
**v2.13.0** (`b2174e8`), which is the side-car the bot's MCP door talks
to — and the repository's pin has **followed it** (it was v2.10.2, it is
v2.13.0 now; the gap is closed, see "The bot's pin described an older
side-car than the one it talks to" below). The posture described here
(the writable rootfs and the rest) did not change; what changed in
v2.13.0 is the MCP door's read budget, the search engine's mount, and
`pids_limit` becoming a knob.

**What moved:** `/opt/telegrambot/sandbox` is a standalone clone of the
sandbox repository, and the bot's deploy does not touch it, so it was
moved by hand: fetch the tag, check it out, rebuild both images
(`--env-file` and `--build` included, or the old image and the old env
stay silently), recreate. The base image moved with it
(`ubuntu:26.04`, `python:3.14-slim`), and the file door now answers a
file the agent cannot read with a **403** and a sentence that says what
to do about it, instead of a 200 with a Content-Length and no body
behind it.

**The writable rootfs:** the operator asked for the agent to be able to
`apt-get install`. The privilege mode makes a command root inside the
container, and the container's root filesystem was read-only — a root
that cannot write a byte of the filesystem cannot install a package
(apt writes to `/var/lib/apt/lists`, `/var/lib/dpkg` and `/usr`). The
side-car v2.10.0 made the rootfs the fourth switch of the privilege
mode, `read_only: "${AGENT_READ_ONLY:-true}"`; the production
deployment sets `AGENT_READ_ONLY=false`. The container is still the
perimeter: the writable layer dies with the container, and the host's
`ssh-keys` were still mounted read-only at that point (the side-car
dropped the `/opt/search` mount in v2.13.0).

**Measured on the host after the move:** `id` → uid 1000, `sudo -n id`
→ uid 0, a write into `/usr` succeeds, `apt-get update` and
`apt-get install -y python3-pip` (which pulls build-essential) finish
in 28 seconds, and a root-owned 0600 file comes back from the door as
a 403 with the "chmod 644" sentence while a readable one comes back
whole.

**What is still true and still bites:** the side-car's per-command limit
is 60 seconds, and the proxy stops waiting at the very same second — so
a command that runs past the limit loses its answer. The bot reports
that as a failure with a cause instead of an empty success (`42d55db`).

**The stdin trap (measured 2026-10-26, fixed upstream the same day):**
the limit is not what usually bites. A command that **reads standard
input** swallowed the side-car's end marker — the marker lines of the
*next* command showed up in the pane where the missing ones should have
been, because debconf's readline frontend had read them as its own
input. `apt-get install -y cowsay` therefore finished in a second,
installed the package, and lost its answer. The side-car's **v2.10.2**
fixes it: the command runs inside a brace group whose stdin is
`/dev/null`, so it reads EOF and the protocol stays intact.

**Measured on the production host after v2.10.2:** `read x` through the
proxy answers in **0 seconds** (`exit_code: 1`, `timed_out: false`,
4 ms) where it used to sit for 60 seconds and come back with an empty
reason; the bot's own `bash_command` tool answers the same command in
4 ms. The proxy's own startup line says `exec_timeout=60 wait=75` — the
wait is strictly larger than the limit, so the answer that says "this
timed out" survives the trip.

**The bot followed (2c83d97):** the tool description no longer warns
about the trap (a warning for a bug that no longer exists is a stale
claim about the side-car), an empty error reason now names what it
really means — a proxy older than v2.10.2 — and the bot's own HTTP
timeout was raised from 70 s to 90 s, because the proxy now waits
75 s and the bot must not drop the answer the proxy is still holding.

### A binary file from the sandbox cannot reach the chat

**Status:** ✅ **closed 2026-10-25.** Option (b) was built: the sandbox's
file door, shipped on the sandbox side as
`vaskes/OpenWebUI-bash-sandbox` v2.7.1 (`c8d70ef`).
**Where:** `telegram-bot/personal_tools/file_transport.py`,
`telegram-bot/llm_dispatch.py` (`_execute_send_file`),
`docs/BASH-SANDBOX.md` § "Moving a file out of the sandbox".

The model creates the file and calls `send_file` with its path. The bot
asks the sandbox what the file is (size, limit), pulls the bytes as a
stream, hashes them, sends them as a Telegram document in the topic the
question came from, and then deletes the sandbox copy.

**What it replaced:** base64 through the terminal pane. That route was
bounded by the pane's scrollback (~400 KB of real file), paid four
thirds of the file in size, put the payload into the model's context,
and -- when the pane overflowed -- dropped the **head** silently, so the
payload decoded into garbage with nothing in the answer to say so. The
door has no such ceiling: the smaller of the sandbox's
`AGENT_BINARY_LIMIT` and Telegram's 50 MB is the real one.

**The one-copy rule:** the delivery is the copy that counts, the
workspace is a scratchpad, so the sandbox copy is deleted after the
delivery. A file that lives in two places at once lives in the one
nobody is looking at as often as in the one they are.

### T70.5 — what if the summary itself does not fit in the context window?

**Status:** ✅ **decided by the operator 2026-10-24, implemented.**
**Where:** `docs/tickets/TICKET-T70-context-protocol-redesign.md`,
`docs/tickets/README.md` (open question 6), `telegram-bot/summarize.py`,
`telegram-bot/context_budget.py`.

The operator's answer: chunk the old part, summarise each chunk, and mark
it in the database so the same chunk is never summarised again.

> сводки надо делать чанками, и помечать. т.е. если ОБЪЕМ данных для сводки
> > 200к надо сделав по ней сводку проставить в базе поле СВЕДЕНО, ссылку на
> сводку которая сделана и по этому чанку больше сводки не делать. Если объем
> данных для сводки <200к не проставлять поле сведено и не сводить, а грузить
> как есть в контекст. Ну и при загрузке соответсвенно грузить только готовые
> сводики и только сообщение которые не сведены.

**The 200k became 64k**, and that is the one number in this entry that is
not the operator's: 200k of raw history plus the fresh window plus llama's
64k thinking block does not fit in a 256k window, and it fails the way
T70.6 failed ("the model thought and did not answer"). 64k fits:

    262144 - 65536 (thinking) - 16384 (answer) = 180224 for the prompt
    180224 - ~48000 (fresh window) - ~4000 (system + tools)
      = ~128000 for the old part  ->  SUMMARY_CONTEXT_TOKENS = 131072

**What is in the code now:**

- `SUMMARY_TAIL_TOKENS` (64k) -- how much old, never-summarised history is
  loaded raw. Below it: nothing is summarised, nothing is marked, the old
  part goes into the context as is.
- `SUMMARY_CHUNK_TOKENS` (64k) -- how much old history goes into one
  summarisation call.
- `SUMMARY_CONTEXT_TOKENS` (128k) -- the ceiling on the old part as a
  whole; when it does not fit, the oldest goes.
- `summaries` table + `messages.summary_id`: the "СВЕДЕНО" marker with a
  link to the summary that stands for those messages. Written in one
  transaction with the summary itself.
- The context is assembled as: the summaries (each standing where its chunk
  was) + the messages that were never summarised + the fresh window.
- A failed chunk stays unmarked: its messages are loaded raw and it is
  retried at the next re-anchor.

**What is NOT done:** the strategy is still off by default
(`CONTEXT_STRATEGY=window`), because the production host is on a generator
and the summarise path has never run against a live engine. That is the
same live check as the answer-files feature.

**Bonus defect the rewrite removed:** the old code summarised only the last
400 old messages (`MAX_OLD_MESSAGES_PER_CALL`) and silently dropped the rest.
There is no such cap now -- a chunk is bounded by tokens, not by a row
count.

**Verification:** `tests/test_t70_5_chunked_summaries.py` (the volume rule,
the marker, the assembly, the budgets, the whole path) and
`tests/test_t70_summarize.py` (the model call, the failure paths, the
trigger placement) -- plus
`scripts/mutation_harness_t70_5.py`: 14 mutations, 14 caught, 0 survived.
Two mutations survived the first run and both were real holes: the
ordering of a summary against an older raw chunk was not pinned (the
partial-failure case), and one mutation was hitting an optimisation
rather than a rule. Both are fixed and the mutation now targets the
re-anchor call.

The `summarize` strategy builds a synthetic `user→assistant` summary pair
from history older than the anchor window. That summary is built from
*all* of the old history. If the summary alone exceeds the engine's
context window, something has to give, and the four candidates are not
equivalent:

- split the old part into several summaries;
- drop the oldest facts;
- bound by freshness inside the old part;
- accept the overflow and say so honestly.

**Why it is not urgent:** `T70.3` works without it. The question only
arises at a scale the deployment has not reached — the current history
fits in the window, so nothing is summarised into a corner.

**What unblocks it:** the operator picks one of the four, or states a
fifth. Until then the summarise path stays opt-in and off by default
(`CONTEXT_STRATEGY=window`).

### T70.6 — the reasoning limit is declared and nobody reads it

**Status:** CLOSED 2026-10-23 — the operator picked option (b) and named
the engine budget. The knob is gone from the bot; the limit lives on
the engine, where llama.cpp enforces it.
**Where:** `telegram-bot/limits.py` (the constant is removed),
`tests/test_limits.py` (two tripwires keep it from coming back), and the
engine unit (`llama-qwen5090-256k.service`, `--reasoning-budget 65536`).

**The operator's decision**, verbatim: "управление лимитами если
возможно в ллама должно происходить в лама", "бот по возможности не
является контекстным или лимитным ограничителем", engine budget 64K,
`MAX_TOKENS_DEFAULT` 128K. So: option (b) — the bot-side
`REASONING_RATIO_HARD_LIMIT` (and its env var) is **deleted**, not
implemented. A knob that is declared, validated, documented and enforced
nowhere is a trap, and the operator's rule is that the bot is not the
limiter.

**The second half of the problem is fixed by the numbers.** The engine's
budget has to be smaller than the bot's `max_tokens`, or the answer has
nowhere to go: the engine's 65536 equals the bot's old text cap (65536)
and exceeds the old document cap (`MAX_TOKENS_CAPTION` = 16384), which
is how a reasoning-only answer reached the user twice on 2026-10-23. The
`dev` profile now sets 131072 / 98304 / 3600s — every per-path cap
above the engine's thinking budget, with room for the answer after it.
`tests/test_limits.py` pins that invariant.

**Also fixed on the way:** the `dev` profile was a phantom. The name was
accepted by the validation and then fell through to the PUBLIC defaults,
so the operator's production ran the conservative numbers while
believing it ran the aggressive ones. It has its own table now, and a
test asserts it is not the public profile.

### T-FREEZE-CHUNK-1 — a long answer to a photo or a document

**Status:** ✅ **closed 2026-10-24.**

Telegram rejects a text message over 4096 characters. The text and voice
handlers split their answer; photo and document sent one message, so a
long answer to a photo came back as `Message_too_long` and the user saw
the API error instead of the answer.

The split moved into `_send_answer_text` (in `handlers.py`), and all four
handlers use it. The policy -- 4000 characters per piece -- lives in
`chat_utils.TELEGRAM_TEXT_CHUNK`, next to the splitter itself.

Two things were fixed on the way, and both were found by the new tests:

- the pieces are sent untrimmed. A trimmed piece loses the space between
  the last word of one piece and the first word of the next, and the user
  reads "строка 372строка 373";
- the pieces go through the same rate limiter as any other message.
  Telegram counts messages, not answers, and a 40k-character answer is
  ten messages in a row.

**Verification:** `tests/test_answer_chunking.py` -- the splitter itself,
the two paths that lacked the split (through the real handlers, with the
engine replaced), and the structural tripwire that no handler sends its
answer on its own. `scripts/mutation_harness_chunking.py`: 9 mutations, 7
caught, 2 survived-as-intended (the group-B reversals: remove the
tripwire *and* the behaviour, and the suite has to stay green).

### T-FREEZE-AUTO-1 — `scripts/smoke_test.py`

**Status:** ✅ **closed 2026-10-24.**

A real round trip against a real bot, with no mocks and no fixtures:
`scripts/smoke_test.py`.

What it checks:

- the token (`getMe`), and with it the bot's identity;
- the webhook (`getWebhookInfo`): the registered URL against the one the
  deployment expects, the last error, the pending queue, and the
  subscription (a stale `edited_message` in it is a warning: the feature
  is gone, the bot ignores edits anyway, and the subscription survives a
  restart);
- the round trip: a probe goes out, the answer comes back, and the
  answer is an answer rather than the empty-response diagnostic;
- the memory: the last assistant turns in the bot's own database, and
  whether the answer was kept whole;
- the log: the fatal lines that would not show up as a failed answer.

**What it cannot do, and says so in the script:** Telegram does not let
a bot act as a user -- a bot's own messages never come back as updates --
so the round trip needs a SECOND bot in the same chat (`--tester-token`,
`--chat`). And a bot cannot press an inline button, so the Stop button
stays a manual click; the script prints the three log lines to look for
and checks the log afterwards.

Standard library only: it runs on the operator's host, where installing
packages is not an option.

**Verification:** `tests/test_smoke_test_script.py` -- the script's own
logic, with the Telegram API replaced by a fake `urlopen`. What is
tested is the script, not the bot: the bot is the thing the script goes
and asks.

### T-FREEZE-DOC-1 — the troubleshooting guide after `v0.10.2`

**Status:** ✅ **closed 2026-10-24.** `docs/TROUBLESHOOTING.md` now
covers everything that arrived after `v0.10.2`.

What was added, and why each piece is a troubleshooting entry rather
than a feature description:

- **the context-memory block** -- the two strategies (`window`,
  `summarize`), the five knobs (`BOT_REMEMBER_DAYS`,
  `CONTEXT_MAX_MESSAGES`, `SUMMARY_TAIL_TOKENS`,
  `SUMMARY_CHUNK_TOKENS`, `SUMMARY_CONTEXT_TOKENS`), the anchor that
  does not slide, and what `/stats` prints. "The bot forgot" is the
  question this section has to answer, and the answer is usually the
  window.
- **the chunk summaries and the "СВЕДЕНО" marker** -- what a summary
  replaces, that a failed chunk stays raw and is retried, that a
  deleted summary brings its messages back, and the two SQL queries
  that show the state.
- **the daily re-anchor** -- the three modes, what each one needs, the
  four variables, and the known defect T68b: `BOT_ENGINE_RESET_CMD`
  never runs, because the loop that would call it only starts in
  `soft` mode.
- **the persona file** -- where it is, why `enabled: false` means *no*
  persona rather than a default one, and why the file is gitignored.
- **the dispatch split** -- which module owns what, so a symptom that
  used to mean "look in dispatch.py" has a home now.
- **the answers that arrive as documents** -- where the file name
  comes from, what is not a file, and why the chat and the history are
  two different strings.
- **the Stop button** -- the maker-taker rule, and what each of the
  three log lines means.

One stale claim was corrected on the way: the guide still said that an
edited command message is "normalised into a fresh message and
re-processed". The edit-replace feature was removed on 2026-10-24; an
update that carries only `edited_message` is dropped, with a log line.

### T-FREEZE-AUTO-2 — a second integration-test mechanism

**Status:** ✅ **closed 2026-10-24 — subsumed, on purpose.**

The recommendation was a `tests/test_integration.py` with an
`integration` marker. What exists now:

- `pytest.ini` registers the `tech_debt` marker and `asyncio_mode =
  auto`, so a marker-based split is available;
- the suite has grown by an order of magnitude since the
  recommendation was written (377 -> 2200+ tests);
- the tests that need a live side-car live in
  `tests/test_shell_integration.py`, and CI excludes them **by name**,
  not by a marker;
- the sandbox tests that need credentials skip themselves when the
  credentials are absent.

A second, parallel mechanism would have to be kept in step with the
first, and nothing it would catch is not caught today. Closed as
subsumed rather than done: the machinery is there if a future test
needs it.

### The record held a message twice — closed 2026-10-27

**Status:** ✅ **closed 2026-10-27.** The absorb path asks the record
whether it already holds the message that was replied to, and stores it
only if it does not. The question is asked by `telegram_message_id` — a
message id is unique inside its chat, its text is not — and a lookup
that fails counts as "not recorded": a duplicate row is noise, a
message missing from the record is a hole.

**What it was.** Measured on the production database: the operator's
message "Удали сообщение 1" (Telegram id 3379) was in `messages`
**twice** — row 10690, written by the text handler under the display
name ("Vasisualy Lohankin"), and row 10693, written six hours later by
the group-absorb path under the short name ("Vasisualy"). The second
row was written when another human replied to that message: the absorb
path stores the message that was replied to, so that "the message where
he said X" is findable at all — and it stored it without asking whether
the record already held it.

**Where the fix lives.** `persistence.already_recorded` is the reader
(a read, like every other read of the record), `storage.search_messages`
is the query, and `dispatch_group` is the decision. One concept — one
owner: the SQL stayed in `storage`, and the absorb path reaches the
record through `persistence` rather than through the database module.

**What did not move.** The rows that are already duplicated stay where
they are. The record is the operator's, and deleting rows from it is
not ours to do — the fix is about what happens next, not about
rewriting what happened.

**Also closed, on the same day:** the two writers named the same person
differently ("Vasisualy Lohankin" against "Vasisualy"). That was a
separate question, and it is answered below — see "One person, one
name in the record".

### T-FREEZE-EMPTY-1 — the empty-response fallback never reaches the user — closed 2026-10-27

**Decision (the operator delegated it on 2026-10-27, "делай с 6 по
10"):** the chat gets the sentence, the record keeps the diagnostic.

**What was wrong:** the empty-response retry chose the fallback and
assigned it to `bot_response` — and the next statement,
`_persist_for_reply`, returned the answer's prose over it. The
assignment was thrown away in the same breath, the log said "sending
fallback", and the user got the diagnostic. On the production bot
(2026-10-24) that was 3618 characters, with a 45108-character reasoning
tail behind it.

**The fix:** the fallback is chosen in `_persist_for_reply` — the one
seam that decides what the chat gets — and the three dead assignments in
the handlers are gone. The record still holds the diagnostic, because
what happened is what the bot remembers; the log names the cause.

**Why it was not fixed inside the answer-files change:** it is a
different feature (the empty-answer retry, 2026-10-22), and the choice
was the operator's. Changing it under cover of another feature is
exactly what the CODEBOOK's "one concern" rule exists to prevent.

**The test that pinned the old behaviour was changed on purpose:**
`tests/test_artifact_e2e.py` asserted that the diagnostic reaches the
chat, and it now asserts the opposite — the fallback in the chat, the
diagnostic in the record.

**Where to look:** `CHANGELOG.md` § Unreleased; `docs/CODEBOOK.md` § X-17;
`tests/test_empty_answer_fallback.py`;
`scripts/mutation_harness_empty_fallback.py`.

### One person, one name in the record — closed 2026-10-27

**Decision (the operator, 2026-10-27):** the full Telegram nickname is
the canonical name. "Vasisualy Lohankin" is what the record carries.
Anything shorter — the first name, the handle — is an artefact of a
second resolver, not a second fact.

**What was wrong:** two writers, two resolvers. The text handler read
the display name; the group-absorb path preferred the handle and fell
back to the first name. Measured on the production record: rows 10690
and 10693 — one message, two names for the same person.

**What it cost:** a search by sender could find the same person twice
or not at all, which is the shape the search-by-sender has already
tripped over once ("Dmitriklim" against "Димриклим"). And a model
reading the record saw two people where there is one.

**The fix:** the policy lives in one module, `sender_name.py`, and both
writers call it. `handlers._sender_display_name` is an alias for that
one function, not a second copy — a tripwire asserts they are the same
object, because two resolvers that agree today disagree the first time
one of them is edited.

**Not fixed, deliberately:** the rows already in the production record
keep the name they were written with. The record is the source of
truth, and rewriting what it says about the past is not a repair — it
is a second truth. New rows carry the canonical name; old ones are
searchable by the name they carry.

**Where to look:** `CHANGELOG.md` § Unreleased; `docs/CODEBOOK.md` § X-16;
`telegram-bot/sender_name.py`; `tests/test_one_name_per_person.py`.

### T-FREEZE-AUTO-3 — a deploy workflow — done 2026-10-27; one rule on the
### operator's router is left

**Decision (the operator delegated it on 2026-10-27, "делай с 6 по
10"): the deploy logic stays on the host; the repository gets the trigger, the
docs and the tripwire.** The topology — the compose file, the `.env`,
the volume, the backup — is the operator's, and a second copy of it in
the repository is a second copy that nobody updates.

**Status:** the code is done and measured. **One thing is left, and it is
the operator's:** the router in front of the production host does not let
a GitHub runner reach the SSH port. Measured, not suspected — from the
runner's address (20.83.53.76, Microsoft Azure): ports **80 and 443
answer**, and 22, 3000, 8080, 8443, 12006 and 12007 **do not**. From
the operator's other network (47.253.4.207) every one of them answers.
So the router opens the web ports to the world and keeps the SSH
forwards for a narrower set of addresses — and the CI is not in that
set.

**What was there:** nothing. A workflow that deploys on a tag push, with
no secret behind it and no tripwire on the properties that make it safe.

**What is there now:**

  * `.github/workflows/deploy.yml` — the trigger (a tag, or a manual run
    from the Actions tab), the commit (`git rev-parse HEAD`), and one
    `ssh` call. Nothing about the deployment itself: the workflow does
    not even name the compose file;
  * a **preflight** that prints the runner's address and says plainly
    that the host did not answer — a timeout with a reason is a
    different thing from a timeout without one;
  * the deploy key is **limited to one script** on the host. sshd runs
    `/opt/telegrambot/deploy_bot.sh` with a forced command, and the
    client's command line arrives as *data* in `$SSH_ORIGINAL_COMMAND` —
    verified: a command line of `not-a-sha; rm -f /tmp/pwned` was refused
    and the file it named was not touched. The script validates the
    value as a hex object name before it reaches git;
  * the host's key is **pinned** (`DEPLOY_KNOWN_HOSTS`), not discovered;
  * the token can read the repository and nothing else
    (`permissions: contents: read`);
  * the host's script was extended with the **checks** the operator
    already ran by hand: the backup, the health endpoint, the restart
    count, and the smoke test **before and after** — a deploy is judged
    on whether it made anything worse, because a red check that was
    already red before the deploy is the operator's business, not this
    deploy's failure. (Measured: the webhook's `last_error_message` is
    a 504 from 14:54 MSK, six hours before the deploy, and it would
    have failed the gate on its own.) The deploy of this wave ran
    through that script by hand: backup, build, recreate, healthz 200,
    0 restarts, no fatal, no ERROR.

**What it needs from the operator — one of the three:**

  1. **open the SSH port to the CI.** One rule on the router: allow
     tcp/12007 from the ranges GitHub publishes
     (`api.github.com/meta`, the `actions` key). The key, the forced
     command and the validation stay exactly as they are — the door
     stays narrow;
  2. **a self-hosted runner on the host.** Then the workflow runs on
     llmhost1 and reaches it directly. The price is honest and worth
     naming: the CI's steps would then run *on the production host*, so
     anyone who can push to `main` could run any command there. The
     forced command is a narrower door than that;
  3. **the host pulls.** A timer on the host that looks for a new tag
     and deploys it. No inbound path is needed at all — and the key
     becomes unnecessary, which loses the operator's "one script, one
     key" design.

**Where to look:** `CHANGELOG.md` § Unreleased; `docs/CODEBOOK.md` § X-18;
`docs/ARCHITECTURE.md` § The deploy workflow;
`tests/test_deploy_workflow.py`;
`scripts/mutation_harness_deploy_workflow.py`.

### The MCP door has no model-facing read cap — closed 2026-10-27

**Status:** ✅ **closed 2026-10-27.** The cap exists, it is deployed, and
the bot's belief about it is a claim like any other.

**What was wrong:** the deployment's binary limit is 50 MiB and the MCP
face's own ceiling is 64 MiB, so the effective cap was 50 MiB — and the
face read the whole file into memory and base64-encoded it. One call
could cost the machine ~117 MiB of RAM for an answer no chat model can
use. The HTTP door's 50 MiB is right for a download; the MCP door is a
different door.

**Who could fix it:** not the bot. The bot sees the server's answer, not
the file — the cap had to live in the side-car, where the read happens.

**What was done:** the side-car's **v2.13.0** gives that door a budget
of its own: `AGENT_MCP_MAX_READ_BYTES` (0..64 MiB, 1 MiB by default),
and a refusal that names the file's size and the limit instead of a
truncated file. A budget may only narrow, never widen: the file policy
(`AGENT_BINARY_LIMIT`) still decides when it is the smaller one, and the
64 MiB ceiling still decides when the other two are switched off. The
HTTP door is untouched — the very file the MCP tool refuses is still
served by `/files`, and the side-car's own test proves it.

**Measured on the production host after the deploy:** a 2 701 756-byte
file is refused with *"the file is 2701756 bytes, which is over the
limit of 1048576 bytes; read it in pieces with bash (head, tail, sed)
or compress it first"*, a 21-byte file comes back as its text, and
`file_info` reports the limit that actually applies (1048576).

**The bot's side:** the number is now in the contract
(`MCP_MAX_READ_BYTES`) and the tripwire reads it out of the pinned
side-car's own source — so the bot's belief and the side-car's
behaviour cannot drift apart.

**Where to look:** `CHANGELOG.md` § Unreleased; `docs/BASH-SANDBOX.md`
§ The model's read budget; `tests/test_sandbox_contract.py`.

### The bot's pin described an older side-car than the one it talks to —

### closed 2026-10-27

**Status:** ✅ **closed 2026-10-27.** The pin, the contract's number and
the production deployment are now the same version.

**What was wrong:** the bot's repository pinned the sandbox as a
submodule at **v2.10.2** (`4a6c465`) and `sandbox_contract.py` said
`SANDBOX_PINNED_VERSION = "2.10.2"`, while the side-car the bot
actually talked to on the production host was **v2.12.1** (`a396101`).
The tripwire `tests/test_sandbox_contract.py` compares the contract
with the PINNED submodule's own source — so the bot's claims were being
checked against a version three releases old. The claims themselves
were still true, which is exactly what made it quiet: a claim that
happens to be true is not a claim that is checked.

**What it needed:** move the pin, move the contract's number with it,
and re-read every claim against that source rather than against the old
one.

**What was done:**

  * the pin moved to **v2.13.0** (`b2174e8`) — the version the
    production side-car now runs (the MCP read budget, below);
  * `SANDBOX_PINNED_VERSION = "2.13.0"` ✔;
  * **every claim was re-read against v2.13.0's source**, and two of
    them had moved:
    - the posture paragraph said the host's `ssh-keys` **and
      `/opt/search`** are mounted read-only. v2.13.0 dropped the
      `/opt/search` mount (the search engine is gone from the stack),
      so the sentence was false and is now true again — in the
      contract, in `docs/BASH-SANDBOX.md` and in `docs/ARCHITECTURE.md`,
      which quote it verbatim;
    - the compose's own `pids_limit` became a knob
      (`pids_limit: "${AGENT_PIDS_LIMIT:-256}"`), and the document was
      quoting the old literal. A number about the sandbox has to come
      from the sandbox;
  * a **new claim** came with the new version: the model's read budget
    on the MCP door (`MCP_MAX_READ_BYTES`), checked by the tripwire
    against the side-car's own source — the number is resolved out of
    `agent_config.py` with an AST walk, because the side-car writes its
    sizes in terms of each other (`1 * _MIB`, `_MIB = 1024 * _KIB`) and
    a check that could only read a plain literal would be a check that
    cannot see half of the ways a number is written.

**Measured on the production host after the move:** the side-car runs
v2.13.0, the MCP door refuses a 2 701 756-byte file with "the file is
2701756 bytes, which is over the limit of 1048576 bytes; read it in
pieces with bash (head, tail, sed) or compress it first", a 21-byte
file comes back as its text, and `file_info` reports the limit that
actually applies.

**Where to look:** `CHANGELOG.md` § Unreleased;
`telegram-bot/personal_tools/sandbox_contract.py`;
`tests/test_sandbox_contract.py`; `docs/BASH-SANDBOX.md`.

---


## House rules for this file

1. **A task lives here or nowhere.** If it is not open, it is in the
   changelog. A backlog where done items sit next to open ones is a
   backlog nobody reads.
2. **Every open item says what unblocks it.** "Waiting on the operator"
   is an answer only if the question is written down.
3. **Numbers are measured.** Line counts and test counts in this file are
   recomputed by a test; a hand-written number goes stale silently.
4. **A claim nobody checks is a defect in the same class as a bug.** Where
   a document states a fact about the tree, a test recomputes it rather
   than matching a string.
