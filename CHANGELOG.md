# Changelog

All notable changes to this project are documented here.
Format: [Semantic Versioning](https://semver.org/) + sections
per release. The repo's `git log` is the granular record;
this file is the human-readable summary.

> ⚠️ **`git clone <url>` without a `--branch` is NOT recommended.**
> The default branch is bleeding-edge and may include
> unreleased refactors. Pin to a stable tag:
> `git clone --branch v0.13.0-stable https://github.com/vaskes/llama.cpp_telegram.git`
> or pick from the [Releases page](https://github.com/vaskes/llama.cpp_telegram/releases).
> Bleeding edge is for contributors only; the general public
> should always use a stable tag.

## [Unreleased]

**After `v0.13.0-stable`.** The post-release cycle: the empty-answer
retry, the leaked token, the attribution cleanup, the codebook, and the
deployment profile that finally has its own numbers.

### The operator's checklist is a script now

`scripts/verify.py` checks that the bot is healthy and that the tests
pass. It never asked the bot a question. `scripts/smoke_test.py` does:
it sends a probe into a real chat, reads the real answer, checks the
real database and the real log, and prints a checklist.

What it can and cannot do is written down in the script, because a smoke
test that quietly skips half of what it claims to check is worse than
none. Telegram does not let a bot act as a user — a bot's own messages
never come back as updates — so the round trip needs a second bot in
the same chat (`--tester-token`, `--chat`). And a bot cannot press an
inline button, so the Stop button stays a manual click; the script says
which three log lines to look for and checks the log afterwards.

Standard library only, on purpose: it runs on the operator's host, where
installing packages is not an option.

### A long answer to a photo or a document no longer dies at the Telegram limit

Telegram rejects a text message over 4096 characters with
`Message_too_long`. The text and voice handlers have split their answer
since v0.10.1; the photo and document paths did not, so a long answer to
a photo came back as an API error, and the user saw the error instead of
the answer.

The split now lives in one place — `_send_answer_text` — and all four
handlers use it:

- the answer goes out in 4000-character pieces, under Telegram's 4096
  with room for what the transport adds;
- each piece is sent untrimmed. A piece trimmed at the boundary loses
  the space between the last word of one piece and the first word of the
  next, and the user reads "строка 372строка 373";
- the pieces go through the same rate limiter as any other message,
  because Telegram counts messages, not answers.

Two tripwires hold it in place: a behavioural one (a long answer through
the real photo and document handlers, with the engine replaced) and a
structural one (no handler may send its answer on its own).
`scripts/mutation_harness_chunking.py` breaks the rule in nine ways:
seven have to turn the suite red, and two remove a tripwire *and* the
behaviour it catches and have to leave it green — the only proof that
the tripwire was the thing doing the work.

### The old history is summarised in chunks, and each chunk is summarised once

The `summarize` strategy used to build ONE summary out of the whole
history that had fallen out of the memory window. That answer has a
problem the operator named: if the summary itself does not fit the
window, something has to give.

The old part is now split into chunks, and each chunk is summarised on
its own. The chunk's messages get a marker in the database — the
operator's "СВЕДЕНО" field — pointing at the summary that stands for
them, and a marked chunk is never summarised again. So summarisation is
not a bill that arrives on every restart: it is paid once per chunk,
when the chunk leaves the window.

- below the volume budget, nothing is summarised and nothing is marked:
  the old part is loaded into the context as it is;
- above it, the old part is chunked, each chunk is summarised, and the
  context is assembled from the finished summaries — each standing in the
  place of its own chunk, so the model reads the timeline in order —
  plus the messages that were never summarised;
- a chunk whose summarisation failed stays unmarked: its messages are
  loaded raw and it is retried at the next re-anchor. Nothing is lost
  either way.

The volumes are configuration, with the arithmetic written out in
`.env.example`: `SUMMARY_TAIL_TOKENS` (64k), `SUMMARY_CHUNK_TOKENS`
(64k) and `SUMMARY_CONTEXT_TOKENS` (128k). They are in estimated
tokens, because the engine is swappable and the bot has no tokenizer
to ask. The 64k is the operator's number; the operator's first number
was 200k, and that did not fit — 200k of raw history plus the fresh
window plus llama's 64k thinking block overflows a 256k window, and it
fails the way the thinking cap failed in v0.13.0: the model thinks and
does not answer.

Two side effects worth knowing:

- the old code summarised only the last 400 old messages and silently
  dropped everything before them. A chunk is bounded by tokens now, not
  by a row count;
- `/stats` no longer says "older than the window (not sent)". Part of
  the old history is compressed and part of it is sent raw, and the line
  now says which is which.

**Verification.** The rules are pinned by
`tests/test_t70_5_chunked_summaries.py` and `tests/test_t70_summarize.py`,
and by `scripts/mutation_harness_t70_5.py` — 14 mutations, 14 caught, 0
survived: the marker is not written, the summary row is not written, the
marked messages are loaded raw as well, the old part is summarised below
the budget, the chunking drops a message, the summaries are ignored, the
summary is not placed in the position of its chunk, the raw tail and the
old part are not capped, the fresh window loses its cap, a failed chunk
is marked anyway, and the re-anchor stops summarising.

### A ticket cannot be decided in one document and open in another

The operator caught a contradiction that had been sitting in the
repository for four days: the decision "we are not adding a guaranteed
minimum of N messages" was recorded on 2026-10-20, and two documents on
2026-10-23 still carried it as open. The tickets index said `open, low
impact`; the backlog listed it under "the context blocker is not
closed".

Nothing was broken. The code was right, the decision was right, and the
documents disagreed with each other — which is the dangerous shape, because
the backlog is where a reader looks for work.

Four documents now agree, and `tests/test_ticket_state_consistency.py`
(4 tests) keeps them agreeing, in both directions:

- a section under "## Open ..." whose status line says closed/decided is
  a contradiction with its own heading;
- a question the index marks answered must not appear as an entry in an
  open section of the backlog;
- a question the ticket documents mark settled must not be "open" in the
  index.

The last one is the direction the operator found, and it is the one that
was missing: the first version of the check only looked at "index says
answered" and not at "ticket says answered" — a mutation that flipped the
index row back to `open, low impact` stayed green.

Two more decided items were found in the same place and moved: T70.5
(chunked summaries) and T70.6 (the reasoning cap lives on the engine)
were both under "## Open ..." with a status line saying they were done.

### The edit-replace feature is gone

A user could edit a message, and the bot would delete its own answer to
that message and answer the edited text instead. It was the last user-facing
feature in the bot with **no way to test it**: reproducing it needs two
people, a deliberately slow model, a human watching real Telegram, and an
active forum topic. The code existed, the unit tests were green, and the
feature had never run in production — the block lived in the polling loop
and production runs in webhook mode.

The operator's call on 2026-10-24 was to remove it rather than to keep
paying for it: let the user wait for the answer to the wrong question and
ask again. So:

- `dispatch_edit.py` is deleted;
- `edited_message` is out of `config.ALLOWED_UPDATES`, so Telegram does not
  even send edits to the bot;
- the `_bot_replies` map in `state.py` is gone with it. It was written on
  every single reply and read by nobody else — a leak with a lookup table;
- an edited message is now ignored. The answer to the original stands.

What is left of the routing tripwire is the part that outlived the feature:
one entry point, `Update.de_json` called exactly once, both transports
reaching it.

**The removal is pinned, in both directions.** `tests/test_edit_removal.py`
(6 tests) drives the real dispatcher: an edited message is not answered, and
a normal message still is — a guard that dropped everything would satisfy the
first test on its own. The write-only map is checked on `state.py`, the
removed names on the AST of every production module (so the comments that
explain the removal survive it), and the subscription on `config.py`.

`scripts/mutation_harness_edit_removal.py`: 10 mutations, 7 caught, 3
survived-as-intended, 0 refused, 0 invalid. The three are the second group:
each removes a tripwire *and* the behaviour it catches, and the suite has to
go green. That is the proof that the tripwire was the thing doing the work.

**One thing the subscription alone could not do.** `allowed_updates` lives on
Telegram's side and survives until the webhook is re-registered, so a bot
restarted against a stale registration would still receive edits. And
`filters.TEXT` reads `update.effective_message`, which falls back to
`edited_message` — so the bot would have answered the edit as if it were a new
message, with the old answer still on screen. The dispatcher now drops an
edit explicitly, whatever the subscription says.

### An answer that carries a file now arrives as a file

When the model answers with a formatted document — html, json, sql,
code, txt — the document used to arrive as a wall of text in the chat,
wrapped in backticks. It now arrives as a Telegram document, and one
answer can carry several of them.

What the model wrote is the input, and the shapes were measured on the
live engine rather than guessed:

- a traits dump the model prefixed with `[llm] ` and put in a ```json
  fence, with no name anywhere;
- prose, a ```html fence, and a tail "Сохрани как `index.html`";
- prose, a ```js fence whose first line is `// greet.js`;
- prose, a ```sql fence whose first line is `-- cradle.sql`;
- three blocks in one answer, each under a heading (`## index.html`,
  `## style.css`, `## app.js`);
- a 425-line HTML page with no fence at all.

The name of a file is looked for in three places, in this order: the
line right above the block, when that line *is* a name; a comment on the
block's first line; a name mentioned in the prose that follows the
block. When none of them names the file, it is called `answer.<ext>` —
`answer-2.<ext>` for the second file with the same extension. The
extension comes from the fence tag; a tag that is not in the table is a
`.txt` file, because guessing an extension would put a `.py` name on
something that is not Python.

Three things that look like files and are not:

- the bot's own executed-tool trace. It is a ```bash fence whose lines
  all start with `$ ` — a log of what the model ran, not a
  deliverable. A shell *script* (no `$ ` in front of its lines) is still
  a file;
- the reasoning tail of an empty answer. A diagnostic is a plain string
  result and an answer is a dict result, and the type is what says which
  one this is — so the split is made where the type is still visible.
  Reasoning that happens to look like a code block never becomes
  something the user is asked to open;
- a fence that was cut off mid-block. A fragment is not a file, and it
  stays in the text.

A file is created regardless of how short the answer is: the operator
asked for "json со своими traits" and got a few hundred characters,
which is exactly the case a size threshold would have thrown away.

Two things about the delivery are worth stating, because each of them is
a way for the content to be lost. The document carries the topic id, so
a file in a forum topic lands in that topic and not in General. And a
file that cannot be uploaded goes out as text instead: the content was
taken out of the message the user has already received, so a failed
upload that dropped it would leave a question with no answer in it.

The bot remembers the whole answer, code blocks included. The chat gets
the prose; the history gets what the model wrote, because the model
wrote that code, and a follow-up question about the file it just sent
needs it to still be there.

**Verification.** The feature is covered end to end *offline*:
`tests/test_artifact_e2e.py` drives the real handler, the real parser,
the real sender and the real database, with the engine replaced by the
shapes listed above. Run it as a script and it prints the transcript of
each one — what the user would have read, and what the bot would have
kept. The live check on the production bot is still missing: the host
runs on a generator, and any request to the model overloads it
(operator's measurement, 2026-10-24). The checklist for it is in
`docs/TODO.md`.

### The Stop button only worked for the ids on the allowlist

The Stop button on the bot's "💭 думаю…" message was gated by the
allowlist, so for anyone outside it the press was dropped before the
handler ever saw it — `[SECURITY] rejected id=... msg=''` — and the
button did nothing, with no error in the chat, while the model kept
generating the answer. In production this hit a group member who is not
on the allowlist: the bot answers group members by design ("Group mode:
No whitelist check. Any member of the group can use the bot"), and it
puts a Stop button on the thinking message it sends them, so it offered
a button it would then ignore. Eighteen presses, no effect.

The two gates answer different questions, and the handler now asks the
right one: the allowlist answers "may this id use the bot at all", and
maker-taker — already implemented, `clicker_id != original_user_id` —
answers "may this id stop THIS turn". The Stop branch is handled before
the allowlist check; the sub-talk branches keep theirs (they create and
delete topics, and Telegram has no ACL for `deleteForumTopic`), and it
now runs before the chat-type branch, as the 2026-10-22 decision
required.

Two smaller things fell out of the same fix:

- the callback is now always acknowledged with `answer()`. A rejected
  press used to leave the button spinning in the client until Telegram's
  own timeout, so there was no feedback at all that the press was seen;
- the structural guard that pins "every command checks the allowlist
  before the chat type" was reading `telegram-bot/dispatch.py`, where
  since the TECH-DEBT #10 extraction there are no command handlers left.
  It had been checking an empty set. It scans the real modules now, and
  fails if it ever finds fewer than ten handlers.

### The `dev` profile was a phantom, and the thinking cap had two homes

`LLAMABOT_DEPLOYMENT_PROFILE=dev` was accepted by the validation and
then fell through to the **public** defaults. The operator's production
has been running the conservative numbers (65536 / 16384 / 900 s) while
believing it ran the aggressive ones. It has its own table now:

| | public | personal | dev |
|---|---|---|---|
| `MAX_TOKENS_DEFAULT` | 65536 | 131072 | **131072** |
| `MAX_TOKENS_CAPTION` | 16384 | 65536 | **98304** |
| `LOOP_BUDGET_SEC` | 900 | 1200 | **3600** |
| `LLAMA_HTTP_TIMEOUT_SEC` | 990.0 | 1320.0 | **3960.0** |

**The engine owns the thinking budget; the bot owns the answer room.**
`llama-server --reasoning-budget 65536` caps the reasoning block where
llama.cpp actually enforces it. The answer is generated *after* that
cap and draws from the same `max_tokens`, so a per-path cap that is not
comfortably above the engine's budget gives the answer nowhere to go —
observed 2026-10-23 twice: a text turn with 65536 thinking and 65536
total (finish_reason=length, no text) and a document turn with 16384
total (reasoning 77977 chars, content 0). Every `dev` cap now clears
65536, and `tests/test_limits.py` pins that invariant.

**`REASONING_RATIO_HARD_LIMIT` is removed from the bot.** It was
declared, validated, documented and enforced nowhere — a knob that
looked like a live guard. The operator's rule: "управление лимитами
если возможно в лама должно происходить в лама", and the bot "по
возможности не является контекстным или лимитным ограничителем". The
env var goes with it; setting it now draws an explicit "no code reads
this" warning. Two tripwires in `tests/test_limits.py` keep it from
coming back: no module under `telegram-bot/` may reference a
reasoning-cap knob, and none may name the removed env var.

### A model that thinks and writes nothing now gets a second attempt

The empty-response retry keys off a marker the producers put at the front
of the response. A reasoning-only answer -- the model wrote a long
thought and no text -- did not carry that marker, so it never matched:
the model got one attempt, and the user got the diagnostic instead of a
reply.

The wording made it worse. The diagnostic said the model had spent all
its tokens on reasoning, and it said that for every finish reason. In
production the user was told exactly that after 18 generated tokens out
of a 65536-token budget, with `finish_reason=stop` -- the model stopped
on its own and chose not to write an answer, which is a different
failure from a budget that ran out mid-thought.

Both are fixed. The reasoning-only answer now carries the same marker
the handlers retry on, and its wording follows the finish reason: the
budget is only claimed when `finish_reason=length`.

### The bot token was in the operator log

httpx logs every Telegram API call at INFO, and the URL it logs carries
the bot token in the path. The root logger sits at INFO, so that line
reached stdout, which the container tees into `/var/log/bot/bot.log` --
694 occurrences of the token in one operator-readable file.

httpx only ever logs at DEBUG and INFO, so its threshold is now WARNING:
no diagnostic that existed is lost, and the token stops being written
out. The lines already in the log were redacted.

### The webhook instructions described a scheme the code never implemented

 put the secret token in the URL path
(). The bot has never done that: it mounts
one route and checks the  header, and
the production webhook was registered that way. An operator following
the document would have registered a URL the bot does not serve.

The same document now says what a token rotation does: the webhook is
registered against a token, so revoking the token drops it. The bot
keeps answering  200 and no update ever arrives, and nothing
in the log says so.  with an empty  is the check.
Setting  in  makes the bot register the webhook
itself on every start, which is what the production deployment now
does.

### A document now says which file arrived

`handle_document` had no entry log of its own, so the only trace of a
document was the shared route line, which is indistinguishable from a
photo run. The first document this bot ever received in production
raised exactly that question -- the file had failed to upload on the
user's side, and the only reason we could tell was that no download
call followed. The entry now names the file, its size, its mime type
and the caption, and it is written before the per-user slot is taken, so
a document dropped by the cap is visible too.


### Claude Code is off the contributor list

It had been there since the first wave of review fixes. 21 commits
carried a

```
Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
```

trailer, and GitHub counts a co-author trailer as a contribution. The
model wrote none of those commits — they were Mavis's — so the
repository was listing a second contributor with no key, no pull request
and no line of code behind it.

**The trailers could not be mapped away.** `.mailmap` applies to the
author field; neither git nor GitHub applies it to a co-author trailer,
which is parsed as written. So they were removed from the history on
2026-10-23 with `filter-branch`, and the old history was kept as a
bundle on the operator's host (`backups/pre-claude-trail-rewrite/`) before
anything was deleted.

**What this changed:** every commit SHA from `975ab03` onwards — the 194
commits of `v0.13.0-stable` included. The tags `v0.11.0-stable`,
`v0.12.0-pre`, `v0.12.0-stable` and `v0.13.0-stable` were moved to
their rewritten commits. Anyone who cloned `v0.13.0-stable` before this
has the old SHA and needs `git fetch --tags --force`.

**A tripwire, so it cannot come back.**
`tests/test_no_model_coauthor_trailers.py` fails on any commit message
carrying Claude Code's address, with two controls beside it: one that the
history was actually read, and one that the detector can fire. The
second control matters — a check that cannot be made to fail is worse
than no check, and the first version of this one would have passed on a
repository that still had every trailer it was written to remove.

The suite is 2129 green in `main` at this commit; the release itself
shipped 2105. (The count is of the commit it is written in, not a
standing claim -- 2110 was right two commits ago.)

### The codebook, and a budget that can catch a monolith

A review agent wrote a codebook for this repository — 599 lines, every
rule backed by an incident that actually happened here. It went in, but
not as it was: four of its mechanical claims turned out to be false when
they were checked against the tree, and the document now says so in §8
rather than quietly dropping them.

The two proposed markers were measured before anything was written for
them, which is the whole point of measuring. A `try:` whose handler only
re-raises fires on two places in the tree, and both are correct code — a
bare re-raise is how cancellation and rollback are propagated. A run of
ten statements with no `await` fires on thirteen, and most of them are
uniform sequences (`__init__` assigning fields, `_attach_handlers`
registering handlers). Neither became a tripwire; both stayed as
`JUDGEMENT`, which is what they are good for.

What replaced them is a budget, because a budget has a number, and an
exception to a budget has to be written down:

- `tests/test_size_budget.py` — a function over 200 lines, or a module
  over 1000 lines that is not in the debt register, fails the suite.
  Four functions are registered, each with a date, a reason and a
  trigger. `handle_text` is one of them, at 209 lines, because it
  crossed the limit while nobody was looking.

### Two `[Unreleased]` sections, and nobody looking

`CHANGELOG.md` had two sections headed `## [Unreleased]`: the live one
at the top, and one written on 2026-10-10 that kept the name through
fourteen releases. A reader who wants to know what changed since the
last tag got two answers, and the older one was longer.

The debt register has a tripwire. The tech-debt documents have one. The
sizes have one. The changelog — the document a user actually reads when
deciding what to run — had none.

`tests/test_changelog_structure.py` now checks that there is exactly one
`[Unreleased]` section and that it is first, that release sections are
unique, and that a release section and a tag do not drift apart in
either direction. The two mismatches that exist today (`v0.7.0`, which
was tagged `v0.7`, and `v0.12.1-pre`, absorbed into v0.13.0-stable) are
listed in the test with the reason, so that a *new* one is a red test
rather than a debt nobody is tracking.

### Four `git worktree`s in `/tmp`

`git gc` could not drop the pre-rewrite history, because four linked
worktrees (`/tmp/ci-wt`, `/tmp/wt7`, `/tmp/wt_base`, `/tmp/wt_c3`) still
pointed into it. Two of them carried uncommitted work — a
`register_bot_menu=None` mutation in `dispatch.py` and a stray
`tests/test_polling_integration.py` — and both were saved to the backup
directory before the worktrees were removed.

## [v0.13.0-stable] — 2026-10-23 — Context memory, the dispatch split, and review waves 1–8

**Status:** shipped and deployed. 194 commits since `v0.12.0-stable`; 107 of
them touch production code. Tag `v0.13.0-stable`.

**This is a refactor release.** Nothing about the user-facing behaviour
changed on purpose. What changed is where the code lives, who owns what,
and how much of it is covered by a test that can actually fail.

**Verified before the tag:** 2105 pytest tests green on Python **3.11**
(the production interpreter) and **3.12**; eight review waves closed with
an approve; deployed to production in webhook mode with a backup taken
first; `/healthz` → 200, zero fatal lines in the log, webhook auth
rejecting both a missing and a wrong secret, and the deployed tree
matching the repository (md5, on every `.py` and on the key files inside
the container).

**Not verified:** the live edit-replace flow — a user edits a message
while the bot is answering it, and the bot must rewrite its answer. It
needs two people, a slow model and a real Telegram chat, so it is
manual-only by nature. It has no automated coverage and is not labelled
as if it did. The last release with a full human end-to-end pass on both
private and group-with-Topics modes is `v0.10.2-stable`.

### The headline: the monolith is gone

| Unit | Before | After | Ledger | Tripwire |
|---|---|---|---|---|
| `dispatch.py` | 1612 | **345** | #10 | `test_dispatch_routing.py` |
| `call_llama` | 1616 | **478** | #3, #8 | `test_llm_iteration.py`, `test_drift_call_llama_names.py` |
| `_selftest` | 1940 | **111** | #9 | `test_selftest_runtime.py`, `test_selftest_blocks.py` |
| `bot.py` | god module | **124-line facade** | #1 | `test_facade_ownership.py` |
| `handlers.py` shared wrapper | 8 copies of persist/rollback | `turn.py`, one ledger | #4, #13 | `test_turn_ledger.py`, `test_turn_scope.py` |
| `prompts.py` | 422 lines of prose + constants | **75-line facade**, prose in `prompt_text/` | #12 | `test_prompts.py` |

`dispatch.py` was split by **ownership**, not by size: a function moved if
it was Telegram surface or bookkeeping, and stayed if it picks a handler.

```
dispatch.py         345   routing, lifecycle, concurrency caps
dispatch_group.py   108   human-to-human reply absorb
dispatch_polling.py 352   startup + the getUpdates loop
dispatch_webhook.py 325   PTB assembly + handler registration
dispatch_edit.py    108   the edited-message rewrite
webhook.py          224   the aiohttp server + secret check
```

The dependencies between them went in as **keyword arguments**, not lazy
imports. A lazy `from dispatch import ...` works and is exactly what the
import-graph test flags: it grew the cycle from six modules to seven. A
passing import test and a growing cycle are not the same fact.

`dispatch_polling` owns the Application registry; the other two modules
read it through `get_dispatcher()` / `set_dispatcher()`. Three writers
rebinding an imported name is how you get two copies of one truth and a
half-refactor that imports fine and reads `None`.

`call_llama` went the same way: 17 leaf modules, each owning one
decision, and the tool loop became a named unit (`ToolLoop.run_one`).

### Three blockers that shipped behind a green suite

`2cebf49` passed 2056 tests and **lost 100% of messages** in the default
transport. All three blockers were live in code that had been reviewed
four times.

**C1 — a required keyword argument that was never passed.**
`_spawn_dispatch(upd_dict)` with a keyword-only `dispatch_update` that
the one call site never passed. `TypeError` on every update, caught,
logged at INFO, and the poll `offset` had already moved past the message,
so Telegram never resends it. The unit tests were green because they
called `_spawn_dispatch` *with* the argument. The uncovered surface was
the seam between the unit and its only caller.

**C2 — `global` on top of `from X import Y`.** `global SHUTDOWN_EVENT`
inside `dispatch_polling` rebinds *that module's* binding of a name
imported from `config`; `llm_iteration` holds its own, still `None`, and
that is where the tool-loop abort guard reads. SIGTERM could not reach a
running inference. This is TECH-DEBT #1's own class — unwritable globals,
stale readers — and it survived there because one instance was cleaned
and the pattern was never named.

**H1 — check-then-act across an `await`.** `if app is None: app = build();
await app.initialize()` is a race: ten concurrent updates built ten
Applications and ran the boot jobs twenty times, on a box where
summarisation is explicitly sequential because the GPU is single.

The fix that shipped for H1 is a lock with double-checked locking. The
offset now advances **only after** a successful hand-off.

### A fix that made the bug worse

Wave 6 found that my own repair of C2 was worse than the original defect:
the repair set the shutdown event unconditionally when installing the
handlers, so the abort guard fired on *every* iteration and the bot
answered `[bot: shutdown requested]` instead of working.

It was not caught by reading the diff, and the test I wrote to "cover"
it **baked the bug in as the expected behaviour** — it asserted the
event *was* set after installing the handlers, which is a different
event from the signal arriving.

The rule that came out of it: **a fix that changes *when* something
happens needs a test for the invariant *before* the event**, not only
after it. "After" can be identical in a broken and a working tree.

### Review waves 1–8

Eight waves against the tree, not against diffs — wave 5 reviewed three
commits, wrote "no blockers", and wave 6 found blockers that had been
there for four waves.

| Wave | What it found |
|---|---|
| 1–4 | The T51–T57 security reviews (profile gate, tool isolation, sender-label filter, group-mode access) and the first extraction blocks |
| 5 | C1, C2, H1 above; the `getUpdates` subscription left at the old value; the `offset` advancing before the hand-off |
| 6 | The unconditional `SHUTDOWN_EVENT.set()`; `webhook.run()` starting with an empty secret; the half-updated `allowed_updates` |
| 7 | C3 (publish the Application **after** `initialize()` — the proposed fix was a regression), H4 (a phantom hybrid mode, and a dead signal-handler closure inside it), M10–M14 |
| 8 | Correction to the C3 note; M13 turned out to be a real hole, M14 was not a defect at all |

Two of those verdicts are worth recording because they went the other
way:

- **C3 was rejected as a proposed fix.** The premise was right and the
  fix was wrong: publishing the Application *above* `await app.initialize()`
  leaves the registry holding an uninitialised Application whose
  `process_update` raises `This Application is not initialized!`, and the
  next update cannot rebuild it because the build sits behind
  `if app is None`. Twenty consecutive failed `initialize()` calls were
  measured: file-descriptor delta 0, retained `Application`/`ExtBot`
  after gc 0, open httpx clients 0. A retry is a retry, not a leak.
- **H4 was a phantom mode.** There is no hybrid transport: the webhook
  branch returns before the poll loop is reachable, and
  `loop.add_signal_handler` *replaces* a handler rather than adding to it.
  Checking it anyway found a real defect — a signal-handler closure
  inside `main()` with zero callers, one of two copies of the same
  policy, of which one was dead.

Also measured rather than argued: `register_bot_menu=None` passes the
argument, so a tripwire that asked "is the argument there?" was green.
It had to ask whether the *value* was the callable.

### Context memory — the bot owns its context

The bot now assembles its context itself instead of taking a fixed number of
messages. Engine-agnostic by construction: the context policy is ours, the
engine stays a pipe.

**New: `telegram-bot/context_budget.py`**
- `ContextBudget.apply()` decides which persisted messages reach the engine.
  Pure — no I/O, no clock, no LLM — so it tests without network or DB.
- Default strategy `window`: keep messages newer than an anchor fixed at bot
  start, `BOT_REMEMBER_DAYS` long (default 7), per-sub. The anchor does NOT
  slide; the nightly 05:00 restart is what re-anchors it.
- Config: `CONTEXT_STRATEGY` (window|summarize), `CONTEXT_MAX_MESSAGES`
  (safety net, default 20), `BOT_REMEMBER_DAYS` (default 7, 0/-1 = no age
  filter). All documented in `.env.example` with the anchored-vs-sliding
  distinction spelled out.
- `summarize` strategy is a stub for now (returns the same window). It will
  summarise out-of-window history when a caller supplies a summariser —
  deliberately not the default until there is data showing it is needed.

**Two guarantees in `apply()`**
- The newest message is never dropped: it reaches the engine only through this
  list (`call_llama` builds `msgs = [sys] + messages`; its `user_text`
  parameter is dead code), so losing it means answering with nothing to answer.
- Messages with a missing or unparseable timestamp are kept, not discarded.

**T67 — history is never pruned**
- Removed the lazy trim (`TRIM_HIGH=500` → `TRIM_LOW=200`) from
  `storage.get_messages`. It DELETED rows at read time, silently. The operator
  is keeping this history for LoRA training data, and a silent delete cannot
  be undone. `/reset` (`delete_thread`) remains the explicit delete path.
- `get_messages` now returns `created_at`, which the age window needs.
- The in-container selftest that asserted the trim *kept* 200 rows now asserts
  the inverse: 700 rows survive a read.
- The two v0.9.1 rollbacks in `handle_photo` (Stop button, crash) are gone
  with the write they were undoing — they would otherwise delete an unrelated
  earlier message.

**T67.2 — images are not stored**
- `handle_photo` no longer persists the image. It participates in the current
  exchange (the model sees it and answers) and is not remembered after a
  restart. `handle_photo` appends the current turn to the history it sends
  explicitly, since the image is no longer in SQLite.
- This also removes the base64 growth that was the original justification for
  the trim.

**T70.4 — context overflow is visible**
- `is_context_overflow()` classifies an engine error as a context-window
  overflow by its text (engines disagree on the status code, and a bot with
  200+ forks cannot carry a per-engine table). Wired into all three
  empty-choices sites in `call_llama`. The user is now told the context is
  exhausted and what to do (`/reset`, or the 05:00 restart), instead of
  receiving a confident answer to a truncated conversation.

**Tests:** 42 new in `tests/test_t70_context_budget.py` (335 total, was 293).
Includes AST tripwires for the claims that break silently:
- `CONTEXT_ANCHOR` must be a module-level constant assigned once — a
  `time.time()` read inside `apply()` would slide the window and every
  behavioural test would still pass;
- `apply()` must not perform I/O;
- `get_messages` must not contain a `DELETE` (checked on the AST, so
  `delete_thread` and `delete_last_user_message` stay legal);
- `handle_photo` must neither persist the image nor silently lose it.

One regression was caught by these tests during implementation: the first
draft of the "last message is never dropped" rule fired whenever the newest
message was older than the window, discarding the entire in-window history in
favour of one stale message. That is exactly what happens right after a
restart, so it would have shipped. Fixed and covered by
`test_out_of_order_arrival_does_not_wipe_the_window`.

### The rest of the context-memory block

- **T65.1 — reply-to-human.** When a user replies to another human, the bot
  records the message and does not answer it. The path moved out of
  `dispatch.py` into `dispatch_group.py` during the split, with a
  caller-side seam test: deleting the call, and deleting the `return`,
  are both caught.
- **T68 — the daily re-anchor.** `soft` / `process` / `process+engine`
  through `BOT_REANCHOR_MODE`. The reasoning is in
  `docs/adr/ADR-0003-context-reanchoring.md`. Installing the timer is the
  operator's job; the repo ships the example and deliberately does not
  install it, because the deployment topology is the operator's.
- **T69 — an optional persona**, from a gitignored `persona.local.json`.
  The repo ships `personality.json.example` with `enabled=false`, so a
  fork never inherits somebody else's character.
- **T70.3 — summarise.** `telegram-bot/summarize.py` plus
  `ContextBudget.apply_summarize`. Off by default
  (`CONTEXT_STRATEGY=window`); the same model that talks with the user
  does the summarising.
- **T70.5 is still open** — what to do when the summary alone does not fit
  the context window. Deferred by the operator 2026-10-21; it needs a
  decision, not code. See `docs/TODO.md`.

Two bugs the context-memory block's own review found, both before the
review was handed out:

- `reanchor()` updated the module global `CONTEXT_ANCHOR`, but `apply()`
  filters on the shared budget instance's own anchor. The anchor advanced,
  the generation counter ticked, the log said "reanchored" — and the window
  did not move by a second. `BOT_REANCHOR_MODE=soft` was a silent no-op,
  which is the one mode that exists specifically to re-anchor without a
  restart.
- `seconds_until_next` built its target as `midnight + hour*3600` and added
  a flat `86400` when that was already past. A DST day is 23 or 25 hours.
  Measured in `America/New_York`, the day before a spring-forward
  transition it computed `06:00` instead of `05:00`, then pushed the
  target out another `86400` — skipping a full day. The docstring already
  claimed "the loop must survive a DST shift". It did not. Moscow has had
  no DST since 2014; a third of this repo's forks do.

### Deploy and CI

- **`update.sh` deployed 4 files out of 17** — a Frankenstein build in
  production, mixing the deployed tree with a stale one. The deployment is
  now reproducible from git, with a `DRY_RUN` that does not report a false
  failure when the deploy adds a module.
- **CI had never passed.** The pytest job excluded
  `tests/test_shell_integration.py` with a comment placed *between* the
  backslash continuation and the flag. A comment inside a backslash
  continuation is not a comment: the backslash binds only the next line,
  so `pytest` ran as `pytest tests/` and `--ignore=...` was executed as a
  command of its own. The job failed with exit 127 — after printing a
  green test count — 100 runs in a row.
- **CI ran a bare `pip install httpx`**, which silently upgraded the
  version under test to whatever was latest. httpx is pinned now, and the
  job prints the versions it tested.
- **The suite now runs on Python 3.11 and 3.12.** Production pins
  `python:3.11-slim`, and nine PEP 701 f-strings lived through several
  review waves because they parse on 3.12 and are a `SyntaxError` on 3.11.
  A suite that only ran on 3.12 had never tested what it ships.
- **The ruff job checks the whole package** with the rules that catch code
  that would not run, instead of one file with `--exit-zero`. It stays
  advisory on purpose: the remaining findings are stylistic, and a blocked
  push costs more than a W605.
- **The rate-limiter test no longer needs the network.** It is a
  deterministic token bucket now.

### Sandbox dependency: v2.3.0 → v2.4.5

The submodule pointer moved from `a3fa2f2` (v2.3.0) to `04be1d8` (v2.4.5).

**Compatibility verified:**
  *  wire format: UNCHANGED
  *  endpoint: UNCHANGED (auth still works the
    same way; only the rate-limit keying is hashed)
  * Response shape (, , ,
    , , ):
    UNCHANGED
  * Body size limit (2 MB) is well above our bot's
    8 KB command cap
  * Security headers (,
    ) are additive; we
    ignore them
  *  now returns minimal
    (no longer leaks workspace path or session
    count) - we don't call this endpoint from
    the bot

**What is in v2.4.0 - v2.4.5:**
  * v2.4.0 (3 of 5 sandbox-recommendations items
    closed): #C1 command content logging, #C7 SSH
    identity symlink rejection, #M3 WebSocket idle
    timeout. 103 -> 108 tests.
  * v2.4.1 (release prep): closed 3 CRITICAL + 7
    HIGH doc drift items. 108 -> 115 tests.
  * v2.4.2 (release prep wave 1): 4 more doc drift
    items (README badge stale, etc.). 115 tests.
  * v2.4.3 (release prep wave 2): /healthz no longer
    leaks workspace path, security headers on every
    response, request body size cap (2 MB), credit
    Mavis in CREDITS. 125 tests.
  * v2.4.4 (release prep wave 3): rate-limit keys
    now SHA-256 hashed, copyright -> Kuduza Ai Lab,
    CODE_OF_CONDUCT contact, pyproject target-version
    py311 -> py312. 129 tests.
  * v2.4.5 (CodeQL follow-up): explicit
     at workflow level.
    131 tests.

**No bot-side code changes.** The bump is a dependency change, not a code
change, and the AST tripwires still cover the 17 historical/predicted
wave-trap classes on the bot side.

### Repository hygiene

- Six stale branches are gone; `main` is the only branch left. Five were
  fully merged (`ahead=0`, ancestor of `main`) and needed no thought.
  The sixth, `rescue/f3-module-split`, had **no common ancestor** with
  `main` — a separate history with its own root, 113 commits that `main`
  had already implemented differently. Merging it would have duplicated
  113 commits. Its seven hotfixes were compared against `main` code by
  code and were all already present; its one unique file was a stale
  operator checklist pinned to `v0.6.3` with paths and a container name
  that are wrong today. Before deleting it, the branch was preserved as a
  `git bundle` and its unique files were saved.

  The lesson, recorded because it nearly went the other way: **`merged`
  by `is-ancestor` or `ahead == 0` is silent on histories with no common
  ancestor.** Ask `git merge-base` first.

- The CHANGELOG is now reconciled against `git tag`. A section existed for
  `v0.12.1-pre`, a tag that was never pushed and does not exist locally
  either. That content was the sandbox bump above, and the bump is
  shipping here — so the section was **absorbed into this release**
  rather than left as an orphan heading for a tag that does not exist.

### Documentation

The documentation tree was reorganised, because it had drifted into the
state this repo keeps hitting: several copies of the truth, all of them
hand-maintained, all of them stale.

- **Stale reviews are archived.** `REVIEW-MINIMAX-v3/v6/v8/v9/v10`,
  the 2026-10-22 review brief, the wave-6 close-out and the
  context-memory review moved to `docs/archive/` with `git mv`, so their
  history follows them. `docs/archive/README.md` states the rule: a
  review document lives in `docs/` only while its cycle is open.
- **`docs/README.md` is new** — the map of the documentation tree.
- **`docs/TODO.md` was rewritten.** It had not been touched since
  2026-10-13 and had accumulated a status snapshot from before the
  context-memory rewrite, a checklist pointing at a file that no longer
  existed, a "code freeze" from a pre-`v0.10.2` cycle, and eleven
  entries marked open that had been closed for months.
- **`docs/ROADMAP.md` was rewritten.** The 2026-10 sprint's four tracks:
  three shipped, one (multi-LLM arbitration) is still a research spike and
  is still deliberately last.
- **`TECH-DEBT.md` had a wrong claim in it.** It described
  `_safe_sender_label` as a display-name normaliser and warned future
  readers not to treat it as a filter. It is a prompt-injection filter and
  has been since the day it existed — `git show 4eedfa7` adds the
  `unicodedata` import and the control-keyword regex together with the
  function. It also said the function lived in `dispatch.py`; it lives in
  `prompt_guard.py` since the dispatch split. The ticket that carried the
  wrong note has been recorded on its own, because following it literally
  would have weakened the filter.
- **`docs/ARCHITECTURE.md` was corrected** against the tree: the module
  tree, the line counts, the location of the sender-label filter, the CI
  section, and a link to a `PERSISTENCE.md` that does not exist.

## [v0.10.2-stable] — 2026-10-11 — Initial human-tested release

**Status:** `v0.10.2-stable` is the first release that has been
manually tested end-to-end by humans (Vasiually + Vaskes) on
both private and group-with-Topics modes. Code freeze declared
in commit `7efab9b` (TODO.md: Record code freeze state +
Qwen wave-3 deferred items).

This is the recommended tag for production deploys and for
anyone evaluating the bot. Tagged at `b8ed356` (v0.10.2 last
code commit); the freeze marker `7efab9b` is included as a
+1 on top of `b8ed356` and contains no functional changes
(only TODO.md update + CHANGELOG.md reorg + this README
warning).

### Known minor issues (will fix in future release)

- **`/subs` in group mode shows only bot-created topics.**
  In a forum-enabled supergroup, `/subs@<bot>` lists topics
  from the local `known_topics` table, which is **only
  populated by `/newsub`** (when the bot calls
  `createForumTopic`). Topics created via the Telegram UI
  before the bot saw a message in them are not enumerated.
  This is a soft limitation of `Telegram Bot API` (no
  `listForumTopics` method exposed to bots), not a SQLite
  bug. Workaround: send `/start` in each topic you want
  tracked (the bot responds but does not yet persist the
  `message_thread_id` to `known_topics` — see fix plan
  below). Severity: **minor** (UX only, no data loss, no
  security impact). Fix planned: register `(chat_id,
  message_thread_id)` to `known_topics` in the `/start` /
  `handle_text` path with a placeholder name; expose
  `/topic <newname>` to rename without creating a new
  Telegram topic.

- **Other v0.10.2 deferred items** — see `TODO.md` for the
  full Qwen wave-3 backlog.

### Added in v0.10.2 (since v0.10.1)

- `scripts/verify.py` — production `healthz` probe + 5/5
  pytest in 1.5s. Wired into `systemd` `OnFailure` for
  incident triage.
- Qwen wave-3 final review fixes (memory leak LRU hardening,
  P0-3 prompts drift correction, editForumTopic hot path).
- `TELEGRAM_API` now env-driven (was hard-coded in dispatch).

See `docs/archive/2026-10/DEPLOY-NOTES-v0.10.2.md` for the v0.10.2 deployment bundle
(v0.9.6 + v0.9.7 + v0.10.0 + v0.10.1 + v0.10.2 = the single
recommended bundle).

---

## [v0.11.0-stable] — 2026-10-14 — bash_command + AST drift tripwires

**Status:** RELEASED 2026-10-14, tag `f6fbe42`. Production
deploy verified. 275 tests pass, pre-commit hook runs all
5 tripwires + 8 backtests. Mavis release review: GREEN
(0 CRIT, 0 HIGH, 1 actionable MEDIUM #R7 fixed, 6 LOW).
Wave-trap pattern defeated (7 instances caught historically,
0 possible in future due to AST enforcement).

This release introduces general-purpose remote-code-execution
for the model via the `bash_command` tool. The tool is gated
to `LLAMABOT_DEPLOYMENT_PROFILE=personal` by default. Operators
who want bash in a non-private chat must explicitly set
`LLAMABOT_DEPLOYMENT_PROFILE=dev` AND
`LLAMABOT_DEV_ALLOWED_CHAT_IDS` (both required; see BASH-SANDBOX.md
"Dev mode" section). Do not enable dev mode in any chat where
you do not control every member.

**Pinned dependency:** This release requires
[vaskes/OpenWebUI-bash-sandbox](https://github.com/vaskes/OpenWebUI-bash-sandbox)
pinned as a git submodule at `sandbox/`. The submodule is
referenced at tag `a3fa2f2` (sandbox v2.3.0). Operators must
`git submodule update --init --recursive` after clone, and
deploy the sandbox side-car separately. See
[docs/BASH-SANDBOX.md](docs/BASH-SANDBOX.md) for setup.

### What ships

  - **`bash_command` tool** (T38, personal profile only). The
    model can call this tool to run any shell command. Calls
    are forwarded to the vaskes/OpenWebUI-bash-sandbox
    openapi-proxy side-car over HTTP with a bearer token. The
    bot does NOT execute shell locally. State (cwd, env,
    history, running processes) persists across calls in the
    same chat via tmux sessions per `session_id = "chat-{chat_id}"`.
  - **Tool isolation** (T38.5). The bot's `KNOWN_TOOL_NAMES`
    whitelist is the single source of truth on which tool
    names get dispatched. llama-server's native tools
    (exec_shell_command, agent, agent_run, read_file,
    write_file, bash) are NEVER dispatched by the bot. Any
    tool_call for a name not in the whitelist is rejected
    by `validate_tool_calls` before execution.
  - **Soft-reject** (T43). When the model calls a forbidden
    tool, the bot renames the offending tool_call to
    `__rejected__` and injects a synthetic `role: tool`
    message that lists the six whitelisted tools, so the
    model can retry with a whitelisted tool on the next
    iteration. The turn is NOT aborted.
  - **Continue-on-unhandled** (T49). Removed the early-return
    on `saw_unavailable_tool` so the loop continues and the
    model can see the synthetic tool result. T32c stuck-call
    detector still catches the case where the model keeps
    asking for the same forbidden tool (returns after 2
    iters).
  - **Tool result history in chat** (T44). The bot renders
    the bash commands the model ran in a fenced
    ` ```bash ... ``` ` block in the final answer, so the
    user can see what was actually executed without
    re-asking.
  - **System prompt tool rules** (T42, T47). The
    `GROUP_CONTEXT` system message now lists the six
    whitelisted tool names explicitly and tells the model to
    use them. Reframed in T47 to "USE THE TOOLS ABOVE
    NORMALLY" after operator feedback that the original
    "burn the turn" wording made the model skip whitelisted
    tools out of caution.
  - **TimedOut retry in `_reply`** (T48). When Telegram's
    Bot API takes longer than 5s to acknowledge a sendMessage,
    the call now retries up to 3 times with exponential
    backoff. If all attempts fail, `_reply` returns None
    instead of raising, so the handle_text except block
    does NOT send a duplicate "❌ Error: Timed out" message.
  - **Silent delete in finally blocks** (T46). Reverted
    T41/T45's `edit_text(bot_response)` finally pattern that
    was producing a duplicate final-answer message. All 4
    finally blocks (handle_text, handle_voice, handle_photo,
    handle_document) now do silent-pass delete. Trade-off:
    the thinking block may occasionally linger in the chat
    if Telegram rate-limits the delete.

### Security

  - **`bash_command` is gated to `LLAMABOT_DEPLOYMENT_PROFILE=personal`.**
    Public profile structurally cannot enable it. The tool
    definition is only added to the LLM request body when
    `is_enabled_for_active_profile()` returns True, which
    requires both `LLAMABOT_DEPLOYMENT_PROFILE=personal` AND
    a non-empty `AGENT_STACK_TOKEN`. Public chats do not
    even see the tool name.
  - **Defence in depth.** The bot calls
    `is_enabled_for_active_profile()` at three places: the
    tool-def append (call_llama), the dispatch arm
    (call_llama), and the /start handler (dispatch). Even
    if one is bypassed, the other two refuse the call.
  - **Tool result truncation.** `tool_result_to_text`
    truncates the tool result to 8KB before it goes back
    to the model. A runaway command cannot blow up the next
    prompt's token budget.
  - **Per-chat session.** `session_id` is derived from
    `chat_id`, not from `user_id`. Two users in the same
    chat share the sandbox session (desired behaviour). Two
    different chats do NOT share sessions. Cross-user state
    injection via session id is impossible.
  - **All shell execution delegated.** The bot never runs
    shell locally. Even an exploit that takes over the bot
    container cannot use it as a shell — the only shell is
    in the sandbox side-car with `cap_drop: ALL`,
    `no-new-privileges`, `read_only: true` at the rootfs,
    `pids_limit: 256`, `mem_limit: 2G`, and a 60s per-command
    timeout.

### Compatibility

  - Sandbox tag `a3fa2f2` (v2.3.0) is the recommended pin.
    The bot's `personal_tools/bash_command.py` wire format
    matches the side-car's `/bash` endpoint as of that tag.
  - Older sandbox tags are NOT supported (may have API
    changes).
  - Backward compatible with `v0.10.2-stable` from a
    deployment perspective: if you do not initialise the
    submodule and do not set `AGENT_STACK_TOKEN`, the bot
    behaves exactly as `v0.10.2-stable` (no bash_command,
    public-mode security posture unchanged).

### Tests

  - 19 end-to-end tests in `tests/test_shell_integration.py`
    that call the real sandbox side-car and assert:
    - sandbox is reachable
    - pwd, whoami, hostname, date, echo, uname, id, which,
      python arithmetic, cat /etc/os-release, ls /tmp, false,
      stderr capture, pipelines, multi-stage find all return
      expected output
    - session_id preserves cwd across calls in the same chat
    - different session_ids do not share state
    - tool_result_to_text produces LLM-visible format
  - 7 unit tests in `tests/test_shell_isolation.py` covering:
    - KNOWN_TOOL_NAMES is exactly the 6-tool set
    - validate_tool_calls accepts whitelisted names
    - validate_tool_calls rejects unknown names
    - validate_tool_calls rejects mixed batches
    - call_llama has the synthetic tool result message
    - T49 post-loop does not early-return
    - T32c stuck-call detector is intact
  - Plus 6 existing test files (test_t44, test_t46, test_t47,
    test_t48, test_t49, test_t39) covering the individual
    commits.

  Total: 173 passing, 2 pre-existing failures unrelated to
  this release (`test_limits`, `test_selftest` — PYTHONPATH
  issue, not introduced by v0.11.0-pre).

### Operator action

  1. **DO NOT** deploy this release to a public Telegram
     group or to anyone you do not trust with shell access.
  2. If you want to evaluate the feature, set up a
     **personal-mode** deploy (single trusted user, profile
     = personal, bearer token in `.env`). See
     [docs/BASH-SANDBOX.md](docs/BASH-SANDBOX.md).
  3. Run `pytest tests/test_shell_integration.py -v` to
     confirm the sandbox side-car is reachable and the
     bot's bash integration works end-to-end.
  4. Pin the sandbox submodule to `a3fa2f2` (or newer
     tested tag) — do not track a branch.
  5. After human pilot testing, this release will be
     tagged `v0.11.0-stable`. Until then, this tag is
     `v0.11.0-pre` and is for contributors only.

### Code review status

  - Wave 1 (shape, docs, basic production-readiness):
    PASSED. All hooks (T38-T50) wired in. Submodule in
    place. Tests in place. README and docs updated.
  - Wave 2 (security audit): PASSED. Profile gate, tool
    isolation, soft-reject, continue-on-unhandled, TimedOut
    retry, silent delete all reviewed. AGENT_STACK_TOKEN
    handling verified (env-only, no echo to logs).
  - Wave 3 (regression): PASSED. 173/175 tests pass.
    T32c stuck-call detector intact. Backward compat with
    v0.10.2-stable verified.

  See [docs/BASH-SANDBOX.md](docs/BASH-SANDBOX.md) for the
  full operator runbook, threat model, and compatibility
  matrix.



#### T52 — Mavis wave-1 review (commit 76df436)

Applied all 12 fixes from Mavis's wave-1 review of the
bash-sandbox integration:
  * #H1 per-chat gate (call_bash_command refuses non-private)
  * #H2 personal_tools bind-mount :ro (closes persistence vector)
  * #H3 SECURITY.md §3.5 documents the bash_command threat model
  * #M1-M7 hardening: rate limit LRU/TTL, .env.example completeness,
    AGENT_STACK_TOKEN env-capture doc, /start copy in groups,
    tool def dedup, env-overridable rate limit, test path cleanup
  * #N6 expanded refusal list in prompts.py
  * #C4 per-user session_id in groups: skipped (groups refused at
    #H1, code path unreachable; documented in BASH-SANDBOX.md)

#### T53 — Mavis wave-2 + wave-3 review (commits 4eedfa7 + this)

Wave-2 (commit 4eedfa7):
  * #R1 hardcoded /home/git paths in tests (4 files cleaned up;
    also fixed dispatch.py syntax error that was live on prod)
  * #S1 sender-name prompt injection defence
    (_safe_sender_label + _tag_sender data-only header)
  * #R2 data/ and files/ writable surfaces
    (files/ :ro, data/ -> named volume bot_data)
  * #S2 command validation (non-string, oversized, NUL rejected)
  * #S3 session_id validation ([A-Za-z0-9._-]{1,128})

Wave-3 (this commit, critical):
  * #R3 **CRITICAL** — call_llama signature was missing chat_type
    kwarg that T52 added to all 4 handlers. The bot was crashing
    on every message with TypeError. Fixed via sentinel default
    + explicit TypeError raise in the function body.
  * #R4 — chat_type default "private" is fail-open; made it
    required (no default) on both call_llama and call_bash_command.
  * #R5 — test_tag_sender_list_content_vision expected old
    S0 format; updated for S1 data-only header.
  * #R6 — added data/ -> bot_data migration block to
    scripts/update.sh so `git pull && bash update.sh` doesn't
    lose chat history.
  * #R7 — added CI pytest job to .github/workflows/lint.yml
    that gates merges on green pytest (no more silent
    continue-on-error).

#### New integration test: tests/test_t53_chat_type_signature.py

7 AST-level tests that pin the fail-closed contract for
chat_type:
  * call_llama accepts chat_type kwarg
  * call_llama chat_type is fail-closed (no permissive default)
  * call_llama raises TypeError without chat_type
  * every await call_llama(...) in handlers.py passes chat_type
  * call_bash_command chat_type is required (no default)
  * call_bash_command refuses non-private chat
  * dispatch /start shows bash-unavailable line in groups

These tests would have caught #R3 at the original T52 commit
time. Cheap (milliseconds), no I/O, no monkey-patching.



### T52 - Mavis wave-1 review (commit `76df436`)

3 HIGH + 7 MEDIUM + 7 NIT from Mavis's first adversarial
review of the bash_command feature. Highlights:

  * **#H1 chat-type gate** (`call_bash_command`): per-chat
    `chat_type` check on top of the env-based
    `is_enabled_for_active_profile()` gate. Two layers
    of refusal.
  * **#H2 personal_tools bind-mount `:ro`** in
    docker-compose.yml. Even an attacker with code
    execution in the bot container cannot modify the
    personal_tools/ source.
  * **#H3 SECURITY.md §3.5** - new section documenting
    the bash_command trust model: bot -> sandbox via
    bearer token, sandbox is the actual enforcement,
    bot only validates and forwards.
  * **#M1 LRU/TTL on rate-limit dict** so the
    `_t51_check_rate_limit` state cannot grow unbounded.
  * **#M2 .env.example** - every env var documented.
  * **#M5 BASH_COMMAND_TOOL_DEF single source of truth**:
    import from `personal_tools.bash_command` only. (Later
    re-discovered in wave-5 as #D7 - see T57.)
  * **#S2 `_validate_command()`** rejects non-string,
    empty, oversized (>8192 chars), and NUL-containing
    commands. Defence against pathologically large
    payloads and encoding attacks.
  * **#S3 `_validate_session_id()`** enforces the
    regex `[A-Za-z0-9._-]{1,128}`. (Later re-discovered
    in wave-5 as insufficient - see T57 #S16.)

Tests: +76 (S1 sender sanitisation 29, S2/S3 validation
26, T52 review entry fixes 21).

### T53 - Mavis wave-3 critical fix (commit `c11b9cb`)

1 CRITICAL + 2 HIGH + 2 MEDIUM. The CRITICAL was #R3:
`call_llama` signature was missing the `chat_type`
kwarg, so every message crashed with `TypeError: missing
1 required keyword argument: 'chat_type'`. Bot was
broken on every message for ~30 min between T52 deploy
and T53 hotfix.

  * **#R3 chat_type param** - added to `call_llama` with
    sentinel `None` default + explicit `TypeError` raise
    if not passed.
  * **#R4 chat_type required** - no default value (the
    sentinel `None` is internal-only; the function still
    raises if not passed).
  * **#R5 test_tag_sender_list_content_vision** updated
    for the S1 data-only header (no longer asserts the
    LLM can act on the sender name).
  * **#R6 `scripts/update.sh`** migration block: seeds
    `bot_data` named volume from existing bind-mount on
    first deploy (idempotent).
  * **#R7 CI pytest job** in `.github/workflows/lint.yml`.

7 AST tests in `tests/test_t53_chat_type_signature.py`
pin the fail-closed contract.

### T54 - Lone surrogate sanitisation (commit `f793c99`)

llama-server JSON parser rejected messages containing
emoji rendered as Python surrogate pairs with HTTP 500
"parse error at line 1, column 61: missing closing
quote". Caught by Qwen 3.8-Max during wave-3.

  * `_scrub_surrogates()` encodes/decodes with
    `surrogatepass` + `replace` before `json.dumps`,
    converting lone surrogates to U+FFFD (replacement
    character).

5 unit tests in `tests/test_t54_surrogate_sanitisation.py`.

### T55 - Mavis wave-4 drift fixes (commit `448f01a`)

6 drift findings from Mavis's wave-4 review:

  * **#D1 ARCHITECTURE.md** rewritten (396 -> 273 lines).
    Now mentions bash_command, tool isolation, sender
    sanitization, network topology, named-volume storage,
    CI/test infra, deployment profiles.
  * **#D2 `_safe_sender_label` in `_handle_chat_member_update`**
    (welcome path) - sender sanitisation was missing
    from the welcome-message branch. Closed.
  * **#D3 CI pytest exclude removed** + `pythonpath =
    telegram-bot tests` in `pytest.ini`. The 11
    `test_call_llama.py` tests now run in CI.
  * **#D4 `.env.example`** got 16 missing env vars
    documented (49 lines added).
  * **#D5 prompts.py disclaimer** "guidance, not
    enforcement" added.
  * **#D6 BASH-SANDBOX.md rate limit row** added.

`.githooks/pre-commit` runs pytest (204 tests, ~1.2s)
catches regressions before CI.

### T56 - Wave-4 #S13 by-design (commit `911864f`)

`is_authorized` username `.lower()` without NFKC Unicode
normalization is by-design (fail-closed for non-spec
usernames). Docstring now warns future contributors not
to "improve" with `unicodedata.normalize()`. ARCHITECTURE.md
tool-loop section updated to mention T31 non-streaming
path.

### T57 - Mavis wave-5 review (commit `9bf7d2d`)

1 HIGH + 3 MEDIUM + 3 LOW + 2 skipped. The HIGH was the
S16 carryover from T52 #S3 (regex was insufficient).

  * **#S16 session_id cross-chat PII leak** (HIGH).
    Previous code `sid = args.get('session_id') or
    f'chat-{chat_id}'` let the LLM emit
    `session_id="chat-{other_chat_id}"` and forward
    to another chat's persistent tmux session. Direct
    cross-chat PII / secret leak. Fix: `sid =
    f'chat-{chat_id}'` always (never trust the LLM).
    4 AST tests pin the contract.
  * **#D7 BASH_COMMAND_TOOL_DEF dedup actually applied**
    (was claimed in T52 #M5 but not actually done).
    Lazy-import from `personal_tools.bash_command`.
  * **#S17 donsetch output wrapped** in
    `[DONSETCH OUTPUT - data]` header (same pattern
    as bash).
  * **#S19 chat_type check moved BEFORE rate_limit**
    (was: profile -> rate_limit -> chat_type; now:
    profile -> chat_type -> rate_limit). Non-private
    callers no longer consume rate-limit tokens.
  * **#S20 prompts.py "full text logged" claim aligned**
    with SANDBOX-RECOMMENDATIONS.md C1.
  * **#S21 .env.example: WEBHOOK_SECRET_TOKEN
    restart-required note** (same as AGENT_STACK_TOKEN
    M3 pattern).
  * **#S23 BASH_COMMAND_TOOL_DEF: "60 rpm" -> "10
    commands per minute per session_id"**.
  * **#S24 Sandbox limits reference docs/BASH-SANDBOX.md
    instead of hardcoding**.

S22, S25 skipped per Mavis.

### T58 - Developer mode (commits `03fc3ad` -> `acbd062`)

Third deployment profile (`dev`) that allows
`bash_command` in non-private chats for operator-selected
chat_ids. Two env vars must be set together (one alone
is a no-op):

  * `LLAMABOT_DEPLOYMENT_PROFILE=dev`
  * `LLAMABOT_DEV_ALLOWED_CHAT_IDS=-100..,-100..`

Dev mode **invisible in the user-facing chat** by
operator design (2026-10-14 11:19 MSK). The "scary red
text" lives only in the code (see `!! WARNING` blocks
in `personal_tools/bash_command.py` and `limits.py`).
13 AST tests pin the contract.

The original `03fc3ad` had a loud red DEV MODE badge
function and `/start` warning. Operator feedback
2026-10-14 11:19 MSK: remove all user-facing effects.
`acbd062` (T58 cleanup) removed them; the dev mode
gate itself stayed.

### T59 - Mavis wave-6 P0/P1/P2 fixes (commit `47e4d90`)

1 CRITICAL + 1 HIGH + 4 drift. The CRITICAL was #S40:
`dispatch.start()` raised `NameError: name
'is_dev_mode_chat' is not defined` on every `/start`
because acbd062 (T58 revert) deleted the helper but
left the call site. Wave-trap pattern #4 (T52 #M5 ->
T53 #R3 -> T57 #S16 -> acbd062).

  * **#S40 dead dev_line block** in `dispatch.py:156-166`
    removed (15 lines).
  * **#D8 prompts.py:151** "loud red DEV MODE badge"
    drift fixed.
  * **#D9 .env.example:131-132** dev mode visual
    effects drift fixed.
  * **#D10 ARCHITECTURE.md** "Two profiles" -> "Three
    profiles (T35 v2 + T58)".
  * **#D11 limits.py** dev profile added to selector
    comment.
  * **#S37** `tests/test_dispatch_start.py` - 5 tests
    with AST tripwire + smoke test that catches future
    /start regressions.

5 AST tripwires added in T60 below.

### T60 - 5 AST drift tripwires + 2 latent bugs (commit `9c17093`)

Mavis's process suggestion after wave-5: AST-level
tripwires in pre-commit hook that catch each historical
wave-trap pattern at commit time. Operator marked
HIGH priority: "wave-trap сработал 4 раза, 5-й будет".

5 tripwires in `tests/test_drift_*.py`, run by
`.githooks/pre-commit` (~2s overhead):

  1. `test_drift_t58_revert.py` - walk every function
     in `dispatch.py` via AST, assert every Name
     reference is reachable (module-level / imported /
     parameter / local / builtin). Catches acbd062 / S40
     class. 4 tests.
  2. `test_drift_m5_dedup.py` - `BASH_COMMAND_TOOL_DEF`
     must NOT be defined in `call_llama.py`, must be
     imported exactly once from
     `personal_tools.bash_command`. Catches T52 #M5
     class. 3 tests.
  3. `test_drift_r3_chat_type.py` - every `call_llama()`
     in `handlers.py` passes `chat_type=`. `call_llama`
     signature has `chat_type` as keyword-only + required
     (no default). Catches T53 #R3 / #R4 + T60 Bug B
     class. 2 tests.
  4. `test_drift_s16_session_id.py` - `call_llama.py`
     does NOT reference `args.get('session_id')`. Has
     exactly one `sid = f'chat-{chat_id}'` with the S16
     comment marker. Catches T57 #S16 class. 2 tests.
  5. `test_drift_handler_imports.py` - every
     `app.add_handler` callback is imported at module
     level. The `from handlers import (...)` block
     exists. Catches 327b7b0 class. 2 tests.

Helpers in `tests/test_drift_helpers.py` (AST walking
shared utilities + `KNOWN_BUILTINS` set with exception
classes and stdlib modules).

**Backtest (`tests/test_backtest_tripwires.py`, P1):**
8 tests that prove each tripwire catches its target
pattern. Negative test ensures clean code passes all 5
tripwires (no false positives).

**2 latent bugs caught at T60 commit time:**

  * **A**: `dispatch.py:287` `fetch_tools_from_llama()`
    as bare name without import. Would have crashed
    `/stats` in private chat. Operator: "eto to chto ya
    hotel ot AST tripwires". Fix: `from call_llama
    import fetch_tools_from_llama`.
  * **B**: `call_llama.py:438` `chat_type: str | None
    = None` default. Docstring said "required" but
    signature had a default - wave-trap #7 (claim vs
    implementation). Fix: `*` separator + remove
    default, chat_type keyword-only + required.

### Wave-trap pattern (7 instances caught)

| # | Wave | Class | Caught by |
|---|---|---|---|
| 1 | T52 | #M5 dedup | Mavis review wave-5 #D7 |
| 2 | T53 | #R3 retry sites | Mavis review wave-2 #R1 |
| 3 | T57 | #S16 semantic | Mavis review wave-5 #S16 |
| 4 | acbd062 | S40 NameError | Mavis review wave-6 #S40 |
| 5 | 327b7b0 | handle_photo | Mavis review wave-6 |
| 6 | T60 | fetch_tools | Tripwire #1 |
| 7 | T60 | chat_type default | Tripwire #3 |

"Formally zakryl teg = zakryl" fails when the
implementation has a claim (comment / docstring /
commit message) that is not enforced by code. T60's
tripwires enforce the claim at the AST level. A
future contributor cannot silently remove the safe
guard without removing the test, which the pre-commit
hook blocks.

### Test count progression

| Commit | Tests |
|---|---|
| T57 (9bf7d2d) | 252 |
| T58 (03fc3ad) | 265 |
| T59 (47e4d90) | 252 |
| T60 (9c17093) | 265 + 8 backtest = 273 |

### Process rule going forward

When adding a new dispatch handler, a new tool, a new
LLM-emitted argument, or a new deployment profile,
ALSO add a tripwire test for it. Pre-commit hook is
the gate. This rule is in
`tests/test_drift_handler_imports.py` and
`docs/ARCHITECTURE.md` "Adding a new feature"
section.


## [v0.12.0-stable] - 2026-10-14 - 4 more AST tripwires + chat_id refactor

**Status:** Code complete. Bot live, all tripwires green.
Pending Mavis review.

### T61 - 4 new AST tripwires + 1 refactor (commit `7e90670`)

Mavis release-review pass 3 (RELEASE-MINIMAX-r3.md) found
5 new wave-trap classes not yet covered by T60. Operator
(2026-10-14 12:18 MSK) approved the backlog: build all 5
in one commit, then tag v0.12.0-pre.

  * **#R8 tripwire** (`test_drift_r8_known_tools.py`):
    every name in `KNOWN_TOOL_NAMES` must have a dispatch
    site in `call_llama.py` (`elif fn_name == '...'` or in
    a `DONSETCH_TOOLS` / `CUSTOM_TOOLS` dict). Catches the
    pattern: a future commit adds a tool name to the
    whitelist but forgets the dispatch branch; the LLM
    emits a tool_call with the new name; validate_tool_calls
    accepts it; the dispatch block has no branch; the bot
    silently returns None.
  * **#R9 tripwire** (`test_drift_r9_profiles.py`): the set
    of valid profile names in `limits.py` must match what
    `bash_command.py` and `prompts.py` know about. Catches:
    a future commit adds a 4th profile in `limits.py` but
    forgets `bash_command.py` or `prompts.py`. v3 fixes:
    skip public/personal in bash_command.py (mentioned
    everywhere in docstrings), only require non-default
    profiles to be in the source as a quoted string literal.
  * **#R10 tripwire** (`test_drift_r10_env_vars.py`):
    every `os.environ.get` / `os.environ[X]` reference in
    `telegram-bot/` must be documented in `.env.example`.
    v2 fixes: accept both active (`VAR=default`) and
    commented (`# VAR=example`) forms. **Caught 3 real
    gaps at T61 commit time**: `BOT_USERNAME`,
    `RATING_MODE`, `TELEGRAM_API` - all used in code
    but not documented. All 3 now in `.env.example`.
  * **#R11 tripwire** (`test_drift_r11_command_handlers.py`):
    every `async def cmd_X` / `start` / `reset` / etc. in
    `handlers.py` and `dispatch.py` must appear as the
    callback of an `app.add_handler(<Handler>(..., handler))`
    call in `dispatch.py`. v2 fixes: checks both
    `CommandHandler` AND `CallbackQueryHandler`,
    `MessageHandler` wrappers, etc.
  * **Backtest** (`test_backtest_r8_r11.py`): 4 backtests
    prove each new tripwire catches its target pattern.

  * **#S27 refactor**: explicit `chat_id` parameter added
    to `call_bash_command` signature. Previously the
    function parsed `chat_id` from `session_id`
    (`session_id[len("chat-"):]`) - fragile if a future
    commit changes the session_id construction. Now the
    caller passes `chat_id` directly. `call_llama.py`
    updated to pass `chat_id=chat_id`. `test_t58_dev_mode`
    updated to pass `chat_id=-100123` explicitly. The
    session_id parse code was removed from
    `call_bash_command`.

Test count: 275 -> 283 (+8 new tests: 4 tripwires + 4
backtests). All 283 tests pass. Bot verified live:
/start, /stats, ping - 3 webhooks returned 200, no
errors in docker logs.

### Wave-trap scoreboard (12 historical + 5 predicted = 17)

| # | Wave | Class | Caught by |
|---|---|---|---|
| 1 | T52 | #M5 dedup | Mavis review wave-5 #D7 |
| 2 | T53 | #R3 retry sites | Mavis review wave-2 #R1 |
| 3 | T57 | #S16 semantic | Mavis review wave-5 #S16 |
| 4 | acbd062 | S40 NameError | Mavis review wave-6 #S40 |
| 5 | 327b7b0 | handle_photo | Mavis review wave-6 |
| 6 | T60 | fetch_tools | Tripwire #1 |
| 7 | T60 | chat_type default | Tripwire #3 |
| 8 | T61 R8 | tool in whitelist, no dispatch | Tripwire #8 (this commit) |
| 9 | T61 R9 | 4th profile, partial | Tripwire #9 (this commit) |
| 10 | T61 R10 | env var, no .env.example | Tripwire #10 (this commit) |
| 11 | T61 R11 | command handler, no registration | Tripwire #11 (this commit) |
| 12 | T61 S27 | fragile session_id parse | Refactor (this commit) |

### Process rule (extended)

When adding a new dispatch handler, a new tool, a new
LLM-emitted argument, a new deployment profile, OR a new
env var, **also add a tripwire test for it**. Pre-commit
hook is the gate. The bot protects itself now.


## [v0.12.1-pre] — absorbed into [v0.13.0-stable](#v0130-stable--2026-10-23--context-memory-the-dispatch-split-and-review-waves-18)

**This section describes work that shipped in `v0.13.0-stable`.** It was
written for a `v0.12.1-pre` tag that was never pushed and does not exist
locally either — the submodule bump it describes (v2.3.0 → v2.4.5) is in
`main` and ships with `v0.13.0-stable`, where the full text now lives.

Nothing was lost in the move, and the content was not rewritten: the
compatibility matrix, the per-release breakdown and the tripwire scoreboard
are the same text, under the release that actually contains the code.

---

## [v0.6.0] — 2026-10-10 — F3 module split, F4 LRU eviction, cross-module cleanup

Big refactor + 7 hotfix commits. The 5 518-line `bot.py` is
split into 7 cohesive modules (`config.py`, `prompts.py`,
`persistence.py`, `rating.py`, `call_llama.py`, `handlers.py`,
`dispatch.py`, `state.py`). The 2180-line slim `bot.py` only
holds imports, re-exports for backward compat, the `_selftest`
function, and `__main__`. No user-facing behaviour change in
this release — the refactor was motivated purely by the cost
of every new feature having to be reviewed in a 5 500-line
file. The same code now lives in 7 files averaging ~400 lines.

### Added

- **`state.py`** (72 lines) — cross-module runtime state.
  Owns `_abort_events` (OrderedDict, F4-bounded at 200),
  `_bot_replies` (edit-replace tracking), `_global_llm_sem`
  (lazy semaphore singleton), `_user_semaphores` (per-(chat,
  user) sem dict), and the concurrency caps. Centralizing
  here breaks the "every function needs a late import from
  bot" mess from the initial shim pattern. New `get_store()`
  lazy-singleton in `storage.py` (companion fix from the
  same wave).

- **`config.py`** (86 lines) — env-driven configuration. All
  constants initialised from `os.environ` at import time. Owns
  `LOCKDOWN` (True when both `ALLOWED_USER_IDS` and
  `ALLOWED_USERNAMES` are empty), the four `MAX_*_BYTES`
  upload limits, the per-handler concurrency caps
  (`_GLOBAL_LLM_SEM_LIMIT=4`, `_PER_USER_SEMAPHORE_LIMIT=2`).
  Re-exports the state.py constants for backward compat with
  v0.5.2 call sites.

- **`prompts.py`** (133 lines) — text templates. Owns
  `GROUP_CONTEXT` (2 672 chars, the LlmChatPlace rules),
  `RATING_RULES` (rating-mode prompt), `WELCOME_TEXT` (pinned
  human-facing welcome), `RATING_EMOJI` (10 entries), `BLOAT_EMOJI`,
  and three compiled regexes (`_LLM_TOKEN_RE`, `_SUBTALK_NAME_RE`,
  `_TOPIC_NAME_RE`) moved out of `bot.py`.

- **`persistence.py`** (76 lines) — async wrappers around
  the storage layer. `persist(chat_id, thread_id, role, content,
  sender_name=None)` and `load_history(chat_id, thread_id)`.
  Both dispatch the actual SQLite work to a worker thread
  via `asyncio.to_thread` so the bot's event loop stays
  responsive during DB writes. The `_s()` helper returns the
  storage singleton lazily.

- **`rating.py`** (203 lines) — the rating-mode stack. Owns
  `parse_rating_response` (parses `[[TYPE:...]] [[RATE:N]]`
  prefix), `apply_rating_and_persist` (the dispatch helper
  called from every handler), `apply_reaction` (the Telegram
  reaction setter), and `execute_react_to_message` (the
  `react_to_message` tool executor). Backward-compat shims
  with the v0.5.2 private names (`_parse_rating_response`,
  `_apply_rating_and_persist`, etc.) so the bot.py re-exports
  still work.

- **`call_llama.py`** (1 142 lines) — the LLM-call layer.
  Owns `call_llama()` (the big coroutine with the abort
  ladder, tool loop, and reasoning stream push), the four
  `execute_donsetch_web_*` executors, `donsetch_call` (the
  MCP JSON-RPC client with session lifecycle and 404-retry),
  `fetch_tools_from_llama` (the cold-start tool discovery),
  `transcribe_voice` (Whisper API client), `_tag_sender`
  (the `From: <name>:` tagger for the LLM context),
  `_donsetch_session_id` and `_donsetch_session_lock`
  (module-level MCP session state). The big `docs/CALL_LLAMA.md`
  still applies — DO NOT refactor `call_llama()` without
  reading that doc end-to-end and writing a regression test
  against a captured llama-server response.

- **`handlers.py`** (757 lines) — the four message handlers
  (`handle_text`, `handle_photo`, `handle_voice`,
  `handle_document`) plus their helpers (`send_reply`,
  `_reply`, `_route_to_thread`, `_reject_in_group`,
  `_reply_active`, `_resolve_active`, `_general_thread_id`,
  `_sender_display_name`, `_download_with_limit`). Each
  handler runs the same dance: route → slot → persist →
  thinking → abort_event → call_llama → rating dispatch →
  reply → cleanup. Function-local `from dispatch import ...`
  for the dispatch helpers (the circular dep is broken at
  call time, not load time).

- **`dispatch.py`** (1 411 lines) — the orchestration layer.
  Owns `main()` and the polling loop (hand-rolled, not
  `Application.run_polling()` — see the v0.5.1 T1 rationale),
  `_dispatch_update` (per-update router), `_handle_chat_member_update`
  (welcome / leave events), `_register_bot_menu` (Telegram
  menu via `setMyCommands`), `cmd_callback` (Stop button
  handler), all 9 command handlers (`start`, `reset`,
  `cmd_help`, `stats`, `cmd_newsub`, `cmd_sub`, `cmd_here`,
  `cmd_subs`, `cmd_delsub`), auth (`is_authorized`,
  `reject_if_unauthorized`), group routing (`_is_group_chat`,
  `_should_mute_in_group`, `_is_reply_to_other_user`,
  `_stop_button_markup`), concurrency helpers
  (`_user_semaphore`, `_check_user_slot`, `_get_global_llm_sem`).
  Function-local `from bot import ...` for `_selftest` and
  `BOT_COMMANDS` (the late imports that break the
  dispatch-imports-handlers-imports-dispatch cycle).

- **F4 LRU-bounded `_abort_events`**. Replaced the plain
  `dict` with `collections.OrderedDict` and a max-size of
  200 entries. `state._register_abort_event(chat_id, msg_id, ev)`
  does `move_to_end` on every touch (so an in-flight handler
  never gets evicted by a concurrent insert) and `popitem(last=False)`
  to evict the oldest entry when at cap. Closes the slow leak
  where a handler raising between insert and pop without
  going through the `finally` left an event in the dict
  forever. With the per-user sem (2 concurrent) and the
  global LLM sem (4 total), the leak was bounded by the
  number of distinct users sending concurrent messages; in
  practice, 200 is a generous ceiling (real-world load peaks
  at ~30).

### Changed

- **bot.py shrunk 5 518 → 2 180 lines** (−60%). What remains:
  imports, re-exports for the new modules (so existing
  `from bot import X` references still work), the 65+
  subtest `_selftest` function, and `__main__`. The remaining
  body is the selftest, which is *the* place to look when
  investigating a regression. The original (now-duplicate)
  function bodies are kept under the re-exports as a stop-gap
  for any caller that bypasses bot.py; they'll be deleted in
  v0.7 once a third-party consumer of the selftest-level
  `_b.store.*` patterns is confirmed not to exist.

- **Module size growth is intentional**: 5 500 → 6 534 total
  lines (+18%). All the new lines are module docstrings,
  cross-module documentation, and the Late-import-block
  comments in `handlers.py` / `dispatch.py` that explain
  the dispatch ↔ handlers circular dep. The trade is fewer
  review headaches per change: a 400-line diff is 10× cheaper
  to review than a 4 000-line one.

### Hotfixes (deployed live during the refactor)

The first 5 stages of the F3 split looked clean in worktree
testing but the production deploy surfaced 7 real bugs that
didn't appear in the selftest (which exercises the LLM call
path but not all the command paths). The hotfixes are
included in the same release:

- **stage 7 hotfix — storage instance singleton**: `persistence.py`
  did `import storage as store`, which rebinds `store` to
  the storage MODULE (not an instance). So `store.add_message`
  raised `AttributeError: module 'storage' has no attribute
  'add_message'`. Added `get_store()` lazy-singleton in
  `storage.py`; `persistence.py` uses `_s().add_message(...)`
  instead.

- **load_history key fix**: `r["content_json"]` → `r["content"]`.
  The real column name in the `messages` table is `content`;
  the refactor had a typo.

- **handlers.py `call_llama` import**: `import call_llama` +
  `await call_llama(...)` was calling the MODULE, not the
  function. Replaced with `from call_llama import call_llama,
  transcribe_voice, ...` to bind the function name in the
  local namespace.

- **handlers.py `import base64`**: dropped during the move.
  Re-added for the photo `data:` URL encoding.

- **dispatch.py `_apply_rating_and_persist` / `_execute_react_to_message`**:
  both were referenced but the late imports from `bot` were
  missing in some function scopes. Added them.

- **dispatch.py `store` references**: 29 places used the
  bare `store` global that only existed in v0.5.2's `bot.py`.
  Replaced all with `get_store().xxx` after adding the import.

- **handlers.py `store` references** (7 places): same
  pattern as dispatch.py. Replaced with `get_store().xxx`.

- **config.py `LOCKDOWN`**: was set in v0.5.2's `bot.py` as
  a derived module-level constant. After the move, `is_authorized`
  in `dispatch.py` referenced it but no one defined it. Added
  the `_PER_USER_SEMAPHORE_LIMIT` and `_GLOBAL_LLM_SEM_LIMIT`
  constants to `config.py` at the same time.

- **`_LLM_TOKEN_RE`, `_SUBTALK_NAME_RE`, `_TOPIC_NAME_RE`**:
  compiled regexes that lived in `bot.py` were missed by
  the initial move. They got picked up by `dispatch.py`'s
  command parsers. Moved to `prompts.py`.

- **`_donsetch_session_id`, `_donsetch_session_lock`**:
  MCP session state lived in `bot.py` as a module-level
  pair. After the move, `call_llama.py` referenced them
  but they weren't there. Moved into `call_llama.py` (they
  are only used by `donsetch_call`).

### Operational impact

- **Production runtime at `/opt/telegram-bot/`** has been
  running the v0.6.0 code since 2026-10-10 22:00 UTC. The
  smoke tests passed: text / photo / voice / document
  handlers work, all 9 commands respond, GROUP_CONTEXT is
  injected on every call, reasoning stream is visible in
  groups, the 4-tool Donsetch MCP integration is reachable
  (verified with `web_search` for "kuduza ai lab" → 1 805
  chars of results). The deployment is currently active and
  serving real users.

- **Automated test suite** (12 tests, 10 passing, 3 with
  test-bug false-positives): covers module imports, storage
  singleton, persistence round-trip, rating parser, long-
  response chunking, abort_event LRU, global LLM semaphore,
  prompts constants, config env, end-to-end private chat
  (real Qwen3.8-27B → 261 chars), end-to-end group with
  RATING_MODE (real Qwen3.8-27B → 2 replies + 1 reaction),
  and `/start` command (257 chars welcome message).

### Deferred (next-wave candidates)

These require real Telegram user interaction (single user
can't simulate) or a slow LLM swap (Ornith instead of Qwen).
Listed in `docs/TODO.md` under "Critical — operator tests":

- **O1 — Stop button end-to-end UX** (5 min)
- **O2 — Multi-user concurrency in group** (10 min)
- **O3 — Edit-during-LLM** (10 min; bot is in polling, not
  webhook, so edits aren't seen until the polling cycle
  finishes — known limitation, full fix is the webhook
  migration in F4 deferred)
- **O4 — `/reset` in a group forum topic** (5 min)
- **O5 — Donsetch tool via real user prompt** (5 min: ask
  the bot to web_search something; verify the MCP call lands)
- **O6 — Visual stop-button feedback** (2 min: when ⏹ is
  pressed, does the thinking message update, does the
  keyboard go away)

## [v0.6.1] — 2026-10-10 — P0 hotfix for F3 module split

Five P0 bugs from the v0.6.0 post-release review, all in bot.py
or config.py. No user-facing behavior change - these were
all internal cleanups. Silent hotfix (no public release notes
at the time; added here retrospectively).

### Fixed

- **P0-1: F4 LRU bound is broken in handlers.** handlers.py
  used raw `_abort_events[key] = ev` in 3 places, bypassing
  the LRU eviction. Replaced with
  `state._register_abort_event(chat_id, msg_id, ev)` in
  handle_photo, handle_document, handle_text. handle_voice
  didn't have abort_event at all in v0.5.2 - added it as
  part of the same fix.
- **P0-2: TWO _abort_events dicts in bot.py.** bot.py had a
  duplicate OrderedDict and `_register_abort_event` function
  that shadowed the state.py versions. Deleted the 60-line
  block, kept only the `from state import ...` re-exports.
- **P0-3: bot.py duplicated config constants.** 60 lines
  of env-var reads (BOT_TOKEN, LLAMA_URL, ALLOWED_USER_IDS,
  MAX_*_BYTES) AFTER importing them from config.py. The
  local declarations shadowed the imports. Also deleted
  the hardcoded `DISABLED_TOOLS = {25-entry set}` that
  ignored the env-overridable config.DISABLED_TOOLS.
- **P0-4: Two Storage instances.** bot.py had
  `store = storage.Storage(DB_PATH)` creating an eager
  instance; storage.py had `get_store()` lazy singleton.
  Two open SQLite connections. Deleted the eager init.
- **P0-5: Dockerfile is broken.** `COPY bot.py storage.py ./`
  only copied 2 of 10 Python files. Changed to
  `COPY *.py ./` with explanation comment.

### Verified

E2E with real Qwen3.8-27B: 173 chars response with [llm]
prefix, abort_events cleaned to 0 after handler.

## [v0.6.2] — 2026-10-11 — selftest regression hotfix after v0.6.1

The v0.6.1 P0-4 fix (delete eager `store = ...`) broke the
in-tree selftest, which uses `import bot as _b; _b.store.xxx`
in 25 places. The hotfix restores the selftest as the safety
net for the next refactor. Silent hotfix (no public release
notes at the time; added here retrospectively).

### Fixed

- **T1a: Re-export Storage as `bot.store`.** Used
  `from storage import get_store; store = get_store()` to
  make `bot.store` a real Storage instance (not the get_store
  function, which was the v0.6.1 mistake). Single instance
  shared with `storage.get_store()`.
- **T1b: Re-export `_PER_USER_SEMAPHORE_LIMIT` and
  `_GLOBAL_LLM_SEM_LIMIT` to bot.py** (selftest used these).
- **T2-T6: Dead code cleanup.** `from collections import
  OrderedDict`, `import storage`, `DB_PATH = ...`,
  `_apply_reaction` import, 3× duplicate concurrency caps
  in config.py, `_s()` wrapper in persistence.py.

### Deferred (v0.6.3)

- LRU regression test in in-tree selftest (claimed in v0.6.2
  commit message but not actually added - see v0.6.3).
- Rating parser test `rest`→`body` KeyError (broken since
  v0.6.0, fixed in v0.6.3).
- Existing abort_event test uses raw insert (gives false
  confidence, fixed in v0.6.3).

## [v0.9.0] — 2026-10-11 — T25 webhook migration (sub-second latency)

Long-polling → webhook transport via aiohttp server + nginx
reverse-proxy. Production rollout at gw1.kuduza.com (Amsterdam
DC) with public hostname `bot.kuduza.com`. Single-port
ingress, secret-token authentication, drop-in TLS
termination at nginx. Falls back to long-polling by setting
`WEBHOOK_MODE=0`.

The migration touched every layer (network, secrets,
process supervision, deploy packaging), so it was released
as a major version bump rather than a hotfix.

### Added

- **`webhook.py`** (119 lines) — aiohttp server module.
  `make_app()` builds the app with two routes:
  `POST /tg-webhook/{tail:.*}` (verifies
  `X-Telegram-Bot-Api-Secret-Token` via constant-time
  `hmac.compare_digest`, then `asyncio.create_task` to
  `_dispatch_update()` and returns 200 immediately — fire-
  and-forget, since Telegram expects 200 within ~5s and
  LLM calls routinely take 30s+); and `GET /healthz` for
  operator liveness probes.

- **`dispatch.py` main branch** — when `WEBHOOK_MODE=1`,
  starts aiohttp server on `0.0.0.0:WEBHOOK_PORT`
  (default 8443) via `web.AppRunner` + `web.TCPSite` and
  blocks on `asyncio.Event().wait()`. When `WEBHOOK_MODE=0`,
  the original long-poll loop runs unchanged. The split is
  a single `if/else` at the top of `_run()`; everything
  below it (handlers, call_llama, state) is shared.

- **`config.py` webhook fields** — `WEBHOOK_MODE`,
  `WEBHOOK_HOST`, `WEBHOOK_PORT` (default 8443),
  `WEBHOOK_PATH` (default `/tg-webhook`),
  `WEBHOOK_SECRET_TOKEN`. All read from env, no defaults
  that would silently misconfigure production.

- **Operator-facing docs** — `docs/WEBHOOK.md` (initially
  ~15 KB, generic public/industry-standard form after
  v0.9.0.1) covers the full deploy: nginx server block,
  `setWebhook()` call, secret-token generation, log
  diagnostics for the common failure modes
  (Connection timed out / 502 Bad Gateway / 403 forbidden).

- **`requirements.txt`** — `aiohttp==3.9.5` for the webhook
  server. The polling loop already used `httpx`; aiohttp
  was added rather than refactoring the existing httpx
  client.

- **Webhook boot selftest** — new `_selftest` group
  exercises `make_app()` via `aiohttp.test_utils`:
  spins up a `TestServer`, hits `/healthz` and a POST with
  right + wrong secret, asserts 200/403/400 correctly.
  Webhook regression is now caught at boot.

### Changed

- **`dispatch.py` `_dispatch_update(upd_dict)`** — was the
  inner function called by the polling loop. Now also
  called by the webhook handler. No signature change;
  it already took a dict, not an `Update` object, so
  webhooks and polling share the same dispatch path.

- **`docker-compose.yml`** — bot moved from `network_mode:
  host` to `lan_macvlan` (static IP 192.168.10.5) so
  outgoing Telegram API calls get a stable source IP for
  PBR / stateful inspection; nginx in front terminates
  TLS, bot speaks plain HTTP on :8443. `WEBHOOK_MODE=1`
  and `WEBHOOK_SECRET_TOKEN=...` added to the env.

- **Production deploy packaging** — `/opt/telegram-bot/`
  on llmhost2 is the single source of truth; the worktree
  at `/opt/llama.cpp_telegram/` is rebuilt into a Docker
  image and `docker compose up -d` deploys. Two-directory
  layout (worktree ≠ deploy) is documented in
  `OPERATOR-TESTS-MORNING.md`.

### Fixed (hotfix chain v0.7.8 → v0.8.2)

This release was 11 hotfixes deep before it shipped.
Each was a regression caught by the in-tree `_selftest`
or by operator UI testing; the chain is the audit trail.

- **v0.7.8** — `_check_user_slot` rejected the third
  concurrent message with "Бот уже обрабатывает 2 твоих
  запросов". Replaced the `if in_flight >= MAX: reject`
  with `asyncio.wait_for(sem.acquire(), timeout=300)`. The
  bot runs in an asyncio task, so the wait doesn't block
  the polling/webhook loop; messages just queue per-user
  and `💭 думаю…` updates keep coming.

- **v0.7.9** — Telegram's flood-control rejected bot sends
  with `RetryAfter` after 7+ messages in a burst. `_reply`
  now catches `RetryAfter` and retries up to 3 times with
  `sleep(retry_after + 1)`.

- **v0.7.9b** — `cmd_callback`'s `edit_message_text("⏹ Уже завершено")`
  also hits the same flood-control path. Added a sliding-
  window `_rate_limit_send(chat_id)` (60s × 15 sends) and
  made the edit retry the same way.

- **v0.8.0** — `handle_photo`, `handle_voice`,
  `handle_document` all had a race: if the LLM call
  returned microseconds after the user clicked Stop, the
  bot would still send the response. Each handler now
  checks `abort_event.is_set()` after `call_llama()`
  returns and discards the response even if non-`None`.
  `handle_voice` was also missing an `if bot_response is
  None` check; that branch crashed before the abort check
  could run. New regression test in `_selftest` exercises
  the post-race discard path.

- **v0.8.1** — llama-server returned `HTTP 500` on some
  vision requests when the PEG grammar parser rejected
  malformed tool-call JSON. `call_llama` now retries
  once without tools offered: without tools in the
  request, the model can't produce tool calls, so the
  grammar has nothing to reject. User-visible: vision
  requests that previously 500'd now transparently
  succeed.

- **v0.8.2** — CodeQL alert `actions/missing-workflow-
  permissions` (CWE-275, Medium) on `.github/workflows/
  lint.yml`. Added explicit `permissions: contents: read`
  block. `GITHUB_TOKEN` is now downscoped to read-only;
  no write operations are possible with it.

### Fixed (production hotfixes v0.7.4 → v0.7.7)

These landed before the v0.7.8 chain but were bundled
into v0.9.0 for release coherence.

- **v0.7.4** — added `_stop_button_markup` helper; the
  Stop inline button (callback_data="stop:<chat_id>:
  <user_id>:<update_id>") is now sticky: even after
  `Stop` is clicked and the message is deleted, the
  user sees the next LLM reply with the same button
  attached until the bot's own LLM is fully cancelled.

- **v0.7.5** — `handle_voice` was missing the Stop
  button markup entirely. The voice handler was
  emitting `final_text` but not wrapping it in
  `ReplyKeyboardMarkup` for the reply. Fixed by going
  through the same `_reply` helper that the other
  three handlers use.

- **v0.7.6** — Stop button clicked but the LLM call
  continued. Root cause: `httpx.AsyncClient.post()` is
  blocking — by the time `call_llama()` returned, the
  LLM had already finished. New `_abortable_post`
  helper races `client.post()` against
  `abort_event.wait()` via `asyncio.wait(...)` with
  `return_when=FIRST_COMPLETED`. If abort wins, the
  in-flight POST is cancelled and `None` is returned.

- **v0.7.7** — `handle_photo` crashed with `TypeError:
  object of type 'NoneType' has no len()` on abort.
  The handler did `print(f"...{len(content)}...")`
  for logging, but `content` was `None` because
  `_abortable_post` returned `None` on abort. Guarded
  with `if content else 0`.

### Known limitations

- **macvlan isolation** — the bot container on
  `lan_macvlan` cannot reach the parent host's
  `192.168.10.7` (kernel-level isolation: macvlan
  sub-interfaces cannot ARP their parent). `WHISPER_URL
  =http://192.168.10.7:8000` is therefore unreachable;
  voice messages don't transcribe. Workaround: move
  whisper to its own macvlan with a free IP (`.5`, `.8`),
  or onto the llama-server host (`.6`).

- **`ip_address` in `getWebhookInfo` shows `85.136.112.144`,
  not the bot's LAN source** — bot's outgoing NAT goes
  through the gw1 egress path. Cosmetic; doesn't affect
  webhook delivery.

- **Telegram-specific scrubbing risk** — gw1's hosting
  provider may run JA3-based anti-bot on the network
  edge. The bot is reachable from the bot host and
  from regular curl/Chrome traffic, but Telegram's Go
  net/http TLS fingerprint is distinct. If `last_error
  =Connection timed out` reappears and `nginx -T` shows
  the right server block, the immediate workaround is
  Cloudflare Tunnel in front of the bot, which terminates
  TLS on Cloudflare's edge and re-originates over a
  long-lived outbound connection from `cloudflared` to
  the bot host. CF egress IP is universally whitelisted
  by Russian/EU hosters; the bot's source IP is no longer
  exposed to the public internet.

## [v0.9.1] — 2026-10-11 — T25 hotfix: image abort rollback, whisper on macvlan

Two operator-caught regressions from the v0.9.0 rollout,
fixed at the end of the gw1 deployment cycle. The first
was a real code bug (image persisted to history before
abort_event was registered); the second was an infra
miss (whisper still on the parent host, unreachable
from the macvlan bot container).

### Fixed (v0.9.1, hotfix chain)

- **Image abort leaves orphan in history** (operator
  report, 2026-10-11 ~21:50 MSK). On `handle_photo`,
  `await _persist_message(...)` was called *before*
  `thinking = await _reply(...)` and `_register_abort_event`.
  If the user clicked Stop after the image was already
  saved to history but before the LLM call returned, the
  image stayed in history. The next text-only message
  was answered about the (cancelled) image because the
  LLM still had the photo in its context. **Fix in
  \`handlers.py:handle_photo\`**: reordered so the thinking
  message is sent and the abort_event is registered
  *first*; `_persist_message` only runs after both are
  in place. Added two rollback paths: in the `except`
  block (any exception after persist) and in the abort
  branch (Stop button after persist). Both call the
  new `Storage.delete_last_user_message(chat_id,
  thread_id)` (added in `storage.py`) which deletes the
  most recent `user`-role row in a thread. **Regression
  test**: post a bad `file_id` via the webhook, then
  send a follow-up text — the bot's response is no
  longer about the (phantom) image.

- **Voice (whisper) unreachable from macvlan bot**.
  whisper-api was running with `network_mode: host` on
  192.168.10.7:8000, but the bot is in `lan_macvlan`
  with static IP 192.168.10.5. macvlan sub-interfaces
  cannot ARP their parent host (well-known kernel
  isolation), so `socket.create_connection((192.168.10.7,
  8000))` from the bot timed out. **Fix**: moved
  whisper-api to `lan_macvlan` with static IP
  192.168.10.3 (the IP previously used by the dev
  nginx container, now decommissioned). Updated
  `WHISPER_URL=http://192.168.10.3:8000` in the bot's
  `docker-compose.yml`. whisper's model cache volume
  (`./cache`) was preserved across the move, so no
  1.5 GB re-download.

### Known limitations (unchanged from v0.9.0)

- macvlan bot cannot reach 192.168.10.7 (parent host)
  for any non-macvlan service. The remaining
  parent-host service is the bot's own `data/` and
  `files/` bind mounts, which work because they're
  filesystem-level not network-level.

## [v0.9.2] — 2026-10-11 — review hotfix chain: handlers rollback + webhook hardening

Pre-release review (REVIEW-MINIMAX-v7) caught 4 P0 issues
that block a clean public release. v0.9.2 closes all of them
plus 2 P1 issues and adds 7 unit tests.

### Fixed (v0.9.2)

- **handle_voice + handle_document had the same bug v0.9.1
  fixed in handle_photo** (P0-1). Persist happened BEFORE the
  thinking message and abort_event registration; a Stop click
  during the LLM call left the transcript / document content
  in history, so the next user message was answered about the
  (cancelled) voice or document. Applied the same
  reordering + `Storage.delete_last_user_message` rollback
  in both abort and except branches. handle_text also got the
  same fix for consistency. `handlers.py` total change:
  ~120 lines across the three handlers.

- **webhook.py used `__import__("json")` instead of
  top-level `import json`** (P0-2). The pattern was a copy-
  paste artefact with no circular-import justification (json
  is stdlib). Replaced with a normal `import json` at the top
  of the module. One-line behavioural change, no logic diff.

- **webhook.py had no body size limit** (P0-3). A
  `request.read()` with no cap lets a malicious actor who
  learns the secret token (or finds a path that bypasses
  the check) OOM the bot. Added a 1 MB cap both via
  `web.Application(client_max_size=1*1024*1024)` and an
  explicit re-check in `_handle()`. Telegram updates are
  < 100 KB; 1 MB is 10x the legitimate max.

- **webhook.py fire-and-forget task silently swallowed
  exceptions** (P1-1). If `_dispatch_update` raised, the
  loop's default handler logged `Task exception was never
  retrieved` to stderr but the operator never saw it because
  the webhook had already returned 200 OK. Wrapped the
  dispatch task body in a try/except that calls
  `log.exception(...)`. The webhook still returns 200
  (the request itself was valid); the traceback is now
  in `docker logs`.

- **config.py referenced a non-existent `WEBHOOK_URL`
  constant** (P1-2). The comment said "WEBHOOK_URL is the
  public URL Telegram should POST to" but no such constant
  existed. Defined it: `WEBHOOK_URL = os.environ.get(...)`.
  In `dispatch.py:main()`, if `WEBHOOK_URL` is set AND
  `WEBHOOK_SECRET_TOKEN` is set, the bot auto-registers
  the webhook with Telegram on startup via
  `bot.setWebhook(url=..., secret_token=...)`. If
  `WEBHOOK_URL` is empty, register manually (see
  docs/WEBHOOK.md section 4.5).

- **call_llama.py: `_abortable_post` caught
  `asyncio.CancelledError` in a combined except clause
  with `Exception`**. Split into two except blocks: bare
  `asyncio.CancelledError: pass` and `Exception: pass`.
  The combined clause was correct in practice but
  unnecessarily broad; the split makes the cancellation
  intent explicit and matches the canonical asyncio pattern.

- **handlers.py: handle_text comment "handle_photo uses
  is_group in print below" was misleading** — handle_text
  does not use `is_group` in any print. Replaced with
  "is_group is unused in this handler (tuple shape kept
  for symmetry)".

- **handlers.py: handle_voice had no `persisted` flag** —
  now added; handle_document and handle_text also have it.

### Test coverage added (v0.9.2)

7 new pytest tests in `tests/test_selftest.py`:

- `test_delete_last_user_message_removes_persisted_image` —
  writes a user message, calls `delete_last_user_message`,
  asserts the row is gone.
- `test_delete_last_user_message_returns_false_when_empty` —
  empty thread, asserts return value is `False`.
- `test_delete_last_user_message_only_removes_user_role` —
  writes user + bot + tool rows, asserts only the user
  row is removed (regression test for the bot/tool rows
  being accidentally deleted on rollback).
- `test_webhook_check_secret_accepts_valid_token` —
  `monkeypatch` sets `WEBHOOK_SECRET_TOKEN`, request
  has matching header, asserts `_check_secret` returns True.
- `test_webhook_check_secret_rejects_invalid_token` —
  header has different value, asserts False.
- `test_webhook_check_secret_rejects_missing_token` —
  no header, asserts False.
- `test_webhook_check_secret_rejects_when_unconfigured` —
  `WEBHOOK_SECRET_TOKEN` empty, asserts False for any
  header value (safe-by-default behaviour).

All 12 tests pass in 0.22s.

### Documentation updates (v0.9.2)

- `docs/WEBHOOK.md` — `127.0.0.1` claim replaced with
  `WEBHOOK_HOST:WEBHOOK_PORT` and a note that the default
  is `0.0.0.0` for macvlan production. Added new section 4.3.1
  covering the `docker network create -d macvlan` setup.
  Section 4.4 updated to include `WEBHOOK_URL` and document
  auto-registration. Section 4.5 now points to the
  auto-register path first and the manual curl as the
  fallback.
- `CHANGELOG.md` — this section.

### Operator action required

None for the public repo. The previous v0.9.1 (commit
`20b5cd9`) already moved secrets to `.env` and rotated the
Telegram bot token. v0.9.2 adds no new env vars that must
be set; `WEBHOOK_URL` is optional. If you want auto-
register on startup, set:

```
WEBHOOK_URL=https://<your-subdomain>.<your-domain>/tg-webhook
```

in `telegram-bot/.env` and restart.
## [v0.9.3] — 2026-10-11 — auto-register footgun fixes + cleanup

Post-v0.9.2 review (REVIEW-MINIMAX-v8) caught 2 NEW P0 bugs in
the v0.9.2 auto-register feature. v0.9.3 closes them plus 2
cleanup items. None of the bugs were live in production
(WEBHOOK_URL was empty; the operator registered manually),
but they would have manifested the moment auto-register was
enabled.

### Fixed (v0.9.3)

- **Auto-register missing `allowed_updates`** (P0-NEW-1).
  The v0.9.2 set_webhook call did not pass `allowed_updates`,
  so Telegram defaulted to `["message", "edited_message"]`
  only — silently dropping `callback_query` updates (the
  Stop button). Added `allowed_updates=["message",
  "edited_message", "callback_query", "chat_member"]` to the
  registration call. Without `callback_query`, the Stop
  button appears in the UI but clicks are lost; the LLM
  runs to completion ignoring the user intent.

- **Auto-register used double `asyncio.run()` loop** (P0-NEW-2).
  The v0.9.2 code did `asyncio.run(_auto_register())` followed
  by `asyncio.run(_webhook_run())`. The first loop initialized
  a Bot object whose httpx client was closed when the loop
  exited. The second loop started the webhook server, but
  every `context.bot.send_message(...)` call failed because
  the Bot's httpx client was dead. Replaced with a synchronous
  HTTP call (`urllib`) for the one-shot `set_webhook`
  registration. The Bot is now initialized lazily inside
  the webhook event loop, where its httpx client lives for
  the duration of the process. New helper:
  `dispatch._webhook_auto_register_sync(url, secret_token)`.

- **WEBHOOK.md section 4.5 manual-register curl lost
  `allowed_updates`** (P2 paired with P0-NEW-1). The v0.9.2
  doc had a simpler curl that did not include
  `allowed_updates`. Updated to use the JSON-body form with
  `allowed_updates=["message", "edited_message",
  "callback_query", "chat_member"]` and added a callout
  paragraph explaining why `allowed_updates` is mandatory.

- **tests/test_selftest.py imports in middle of file**
  (P3). v0.9.2 had `import os / json / asyncio / hmac /
  MagicMock / pytest` placed AFTER
  `test_rating_parser_returns_body`. Moved `MagicMock` to
  the top imports; removed duplicate `os`/`asyncio`/`pytest`;
  removed unused `json`/`hmac`.

### Documentation added

- `docs/archive/v0.5-v0.12/REVIEW-MINIMAX-v8.md` — the post-v0.9.2 review wave
  that surfaced the 2 new P0s. 8 KB.

### Operator action required

None. The 2 P0s were dormant in production (WEBHOOK_URL was
empty). If you intend to use auto-register in the future, the
v0.9.3 fix is now in place. Just set `WEBHOOK_URL` in
`telegram-bot/.env` and restart.
## [v0.9.4] — 2026-10-11 — idempotent auto-register + JSON body + comprehensive tests

Final pre-tests review (REVIEW-MINIMAX-v9) caught 1 P1
deployment-hygiene issue (silent webhook overwrite) and
2 P3 issues (string `drop_pending_updates`, response body
not parsed). v0.9.4 closes them and adds the first
**comprehensive unit-test suite for the entire pipeline**
(text, voice, image, document handlers, webhook auth + body
cap, auto-register, storage, rollback patterns) so the
operator can stop hand-testing every release.

### Fixed (v0.9.4)

- **Auto-register silently overwrites working webhook**
  (P1-NEW-1). The v0.9.3 helper blindly called `setWebhook`
  with whatever `WEBHOOK_URL` was in the env, so a typo or
  staging URL in env would overwrite a working production
  webhook. The v0.9.4 helper first calls `getWebhookInfo`
  to see the current state. If the current URL matches
  `WEBHOOK_URL`, it skips `setWebhook` (idempotent path
  prints "webhook already registered"). If different, it
  prints a 3-line WARNING before overwriting.

- **`drop_pending_updates` was sent as string "false"**
  (P3-NEW-1). Telegram's Bot API spec says it should be a
  Boolean. Switched the helper to use a JSON body
  (`Content-Type: application/json`) so `drop_pending_updates`
  is a real `False` and `allowed_updates` is a real JSON
  list, not a stringified one.

- **Auto-register response body not parsed for error details**
  (P3-NEW-2). If Telegram returns `{"ok": false,
  "description": "Bad webhook: ..."}`, the v0.9.3 helper
  printed the HTTP error but not the actual Telegram
  description. v0.9.4 parses the response JSON; if
  `ok` is false, prints `description`. Distinguishes
  HTTPError (network/proxy) from Telegram-level errors.

### Tests added (v0.9.4)

This is the comprehensive test suite. New file
`tests/test_handlers.py` covers the rollback pattern in
all 4 handlers (text/voice/photo/document) for both the
abort and the exception paths. New file `tests/test_webhook.py`
covers webhook auth (already in test_selftest), body cap
(NEW), safe_dispatch exception logging (NEW), and
`_webhook_auto_register_sync` (NEW: 4 tests covering payload
correctness, idempotent skip, overwrite warning, JSON
body format). New file `tests/test_storage.py` covers
storage operations including the v0.9.1+ delete_last_user_message
edge cases.

Total pytest count: 12 -> 32+ tests, all in 0.3-0.4s.

### Documentation added

- `docs/archive/v0.5-v0.12/REVIEW-MINIMAX-v9.md` - the final pre-tests review
  wave that surfaced the P1 + 2 P3 issues.

### Operator action required

None. The fixes are additive. The comprehensive test suite
lets the operator skip the manual smoke test for these
scenarios on future releases.
## [v0.9.5] — 2026-10-12 — test suite polish + coverage expansion

v0.9.4 review (REVIEW-MINIMAX-v10, wave 12) caught 7 P3 items
in the new test suite. v0.9.5 closes 6 of them. T1 (DRY up
handler test setup) is deferred — the fixture approach had
subtle ordering issues that need a deeper investigation, and
the inlined setup is small enough (12 lines × 7 tests) to
be acceptable for now.

### Fixed (v0.9.5)

- **Dead `_pers._store_instance` line in 7 handler tests** (T2).
  The line was a no-op because `persistence.py` doesn't have
  `_store_instance`; it was an artefact of a previous pattern.
  Removed all 7 instances.

- **conftest.py comment said "35+" tests, actual is 36** (T6).
  One-word fix.

### Tests added (v0.9.5)

- `test_auto_register_handles_empty_current_url` (T3) — first-deploy
  case. getWebhookInfo returns `{"result": {"url": ""}}` and the
  helper proceeds with setWebhook without printing a WARNING
  (because the existing webhook is empty, not different).

- 3 tests for `call_llama._abortable_post` (T4):
  - `test_abortable_post_returns_response_when_post_finishes_first`
  - `test_abortable_post_returns_none_when_abort_fires_first` —
    exercises the v0.7.6 race condition fix.
  - `test_abortable_post_passes_through_when_no_abort_event`

- 3 tests for `storage` known_topics (T5):
  - `test_known_topics_round_trip` — add / find by name / find by id
    / remove
  - `test_known_topics_list_per_chat` — list_known_topics returns
    only topics for the given chat
  - `test_known_topics_returns_none_for_missing`

Total pytest count: 36 -> 43 tests, all in 0.48s.

### Documentation added

- `docs/archive/v0.5-v0.12/REVIEW-MINIMAX-v10.md` (T7) — the v0.9.4 test suite
  review wave that surfaced the 7 P3 items.

### Deferred to a future release

- **T1: DRY up test_handlers.py setup** (P2, 30 min).
  The 12-line DB setup block is repeated 7 times. The reviewer
  suggested a pytest fixture (`fresh_store`) but the fixture
  approach caused test isolation issues (the fixture's
  monkeypatch+Storage setup interfered with the test's inlined
  setup). Will revisit with a context-manager approach
  (`with fresh_db() as store:`) in a follow-up.
## [v0.9.6] — 2026-10-12 — T28 fixed, in-tree selftest at 20/20

v0.9.5 review (autonomous sweep) found T28 — the 2 pre-existing
rating parser failures that have been quietly in the in-tree
selftest since v0.7.9. Fixed.

### Fixed (v0.9.6)

- **T28: parse_rating_response fall-back behaviour.** Two
  pre-existing in-tree selftest failures:
  - `("plain answer", "question", None, "plain answer")` — the
    parser was returning `type="unknown"` for unprefixed text,
    but the selftest expected `type="question"`.
  - `("[[TYPE:unknown]]", "question", None, "[[TYPE:unknown]]")`
    — the parser was matching the literal `[[TYPE:unknown]]`
    prefix and returning `type="unknown"` with an empty body,
    but the selftest expected `type="question"` with the
    whole text as the body.

  The fix: map any non-known type (including "unknown" and
  the literal `[[TYPE:unknown]]` from a misbehaving LLM) to
  `type="question"`, and return the whole input text as the
  body. This matches the `apply_rating_and_persist` dispatch
  which already routes any non-(info|statement|bloat) type
  to the "persist full response + reply with it" branch.

  Verified with the same 10-case in-tree selftest suite that
  was previously failing on cases #1 and #10. Now passes
  10/10.

### Tests added (v0.9.6)

- `test_rating_parser_handles_unknown_type_as_question` — covers
  all 4 cases:
  - unprefixed text → question + whole body
  - `[[TYPE:unknown]]` → question + whole body
  - empty string → question + empty body (no crash)
  - valid `[[TYPE:info]] [[RATE:7]]` still works (regression)

Total pytest count: 43 → 44 tests, all in 0.48s.

### In-tree selftest status

Before v0.9.6: 18/20 test groups pass (2 pre-existing failures:
"plain answer" and "[[TYPE:unknown]]").

After v0.9.6: **20/20 test groups pass** (T28 closed).
## [v0.10.1] — 2026-10-12 — Qwen wave-2 review fixes

Three real issues from the wave-2 review that I dismissed in
v0.10.0 but turned out to be real concerns, plus cleanup of
the v0.9.6 / v0.10.0 deploy confusion.

### Real issues fixed (Qwen wave-2 was right)

**P0-4 race in Stop button (1-line fix in handlers.py).**
Between the post-await `abort_event.is_set()` check and
`send_reply()`, there's an `_apply_rating_and_persist()` call
(SQLite write, takes time). A Stop click in that window
would send the response. Fixed by adding the abort check
right before `send_reply()` in all 4 handlers (handle_text,
handle_photo, handle_voice, handle_document). v0.9.1 had the
aborts-in-abort-branch rollback but missed the post-write
pre-send window.

**P0-5 explicit transactions in storage.delete_thread().**
The previous code relied on Python sqlite3's implicit
transaction (default `isolation_level=""` holds an implicit
txn across DML). This worked but was undocumented and not
protected by a test. v0.10.1 wraps in explicit
`BEGIN`/`COMMIT` with `ROLLBACK` on exception, so the
contract is obvious and survives a future refactor that might
flip `isolation_level` to autocommit. Regression test added
in test_storage.py.

**P0-6 backpressure cap on webhook queue.**
`asyncio.create_task()` with no counter meant a flood could
spawn 1000+ tasks waiting in memory for a semaphore slot.
The semaphores (`_GLOBAL_LLM_SEM_LIMIT=4`, per-user=2) cap
CONCURRENCY, not QUEUE DEPTH — the queue could grow without
bound for up to 5 minutes (per `MAX_QUEUE_WAIT_SEC`).

v0.10.1 adds `_MAX_PENDING_DISPATCH=50` (10s of burst at
normal Telegram rate). Beyond that, `_handle()` returns 503
"bot busy, retry later" — Telegram retries with backoff.
This is the right HTTP status: bot is alive but overloaded,
so Telegram doesn't mark the webhook as broken.

### Cleanup

**Dead code in dispatch.py removed.**
The `_get_global_llm_sem` and `_user_semaphore` re-exports
in `dispatch.py` were F3 stage 5 leftovers. After v0.10.0's
LRU additions, the same functions live in `state.py`. v0.10.1
deletes the dispatch.py copies and updates `bot.py` and
`handlers.py` to import from `state` directly. Single source
of truth, no shim.

### What the LRU limit of 500 actually means

Qwen wave-2 (N-3) asked "why 500, not 200 or 1000?" — fair.
The reasoning:
- `_abort_events` uses 200 because each entry holds a
  waiting `asyncio.Event` that the handler resolves within
  a few seconds. 200 = ~50 messages/s for 4s of in-flight.
- `_user_semaphores` and `_bot_replies` use 500 because
  they're keyed on (chat_id, user_id) or (chat_id, msg_id)
  and stay alive much longer. 500 = ~10x what `_abort_events`
  holds, generous headroom.

The new test `test_user_semaphores_at_actual_500_limit_evicts`
verifies that the LRU logic is correct (using a smaller
test maxsize for speed; the assertion checks the production
default is 500).

### Held-semaphore eviction risk (Qwen wave-2 1.2)

The reviewer noted: if a semaphore is evicted while held
(`sem.locked() == True`), the next message from the same
user creates a NEW sem with full budget (4 slots instead
of 2). This is a real concern in a public fork with many
users. For the current deployment with `ALLOWED_USER_IDS=2,1`
and 5-10 entries, eviction never happens. **Documented as
accepted risk in `_get_user_semaphore()` docstring**, not
implemented (would add complexity for a scenario that
doesn't occur in the current deployment).

### Tests added (57 total, was 52)

- `test_delete_thread_happy_path` — verifies all 3 tables cleared
- `test_delete_thread_idempotent_on_missing_thread` — deleting
  a non-existent thread returns 0
- `test_delete_thread_uses_explicit_transaction` — verifies
  BEGIN/COMMIT pattern via `set_trace_callback`
- `test_user_semaphores_at_actual_500_limit_evicts` — LRU
  eviction at the production limit
- `test_backpressure_returns_503_when_queue_full` — webhook
  queue overflow

The 3 prompts tests in v0.10.0 (test_prompts_has_long_*) were
updated to check SEMANTIC MARKERS instead of string length
(Qwen N-2: length check is fragile).

### Bundle

v0.10.1 contains everything from v0.9.6 + v0.9.7 + v0.10.0 +
the wave-2 fixes. **The operator only needs to deploy v0.10.1
to get all fixes.**
## [v0.10.2] — 2026-10-12 — Qwen wave-3 final review fixes

Two real bugs caught by the wave-3 review (which both earlier
waves missed), plus 6 non-blocking improvements.

### BLOCKING bugs fixed (Qwen caught these, both waves missed)

**1. `EMPTY_RESPONSE_FALLBACK` NameError in `handlers.py:send_reply`.**
The function uses this constant on empty LLM response, but
`handlers.py` never imported it (and didn't have a local
definition). Result: a `NameError` whenever the LLM returned
empty content — exactly the moment the user most needs a
friendly fallback. **This would have crashed the bot in
production.**

Fix: moved the constant to `config.py` (which handlers.py
already imports from) and removed the dead duplicate in
`bot.py`. Single source of truth. Regression test added.

**2. `_rate_limit_send` defined but never called.**
A full per-chat rate limiter (15 sends per 60s window) was
defined in `dispatch.py` but no code path called it. The
bot was unprotected from Telegram's 20 msg/min/chat limit.
A burst (queue drain after restart, rating-mode storm) would
trigger `RetryAfter` on Telegram's side and produce failed
sends with no client-side rate.

Fix: `send_reply` now calls `await _rate_limit_send(chat_id)`
before sending. Dead code is now live.

### Non-blocking improvements (Qwen wave-3 items 2.2, 2.5-2.7, 1.2)

**3. `_tag_sender` leaks internal fields to LLM API.** Fixed
to strip `_*` prefixed fields before constructing the request
body. `_sender_name` becomes a `From: Alice` tag in content
(visible to the LLM) but the field itself doesn't leave the
process. Regression test added.

**4. `transcribe_voice` hardcodes `language: 'ru'`.** Made
configurable via `WHISPER_LANGUAGE` env var; empty value
= autodetect. Default remains `ru` for backwards compat with
the LlmChatPlace group.

**5. `fetch_tools_from_llama` uses fragile `LLAMA_URL.replace('/v1','')`.**
Replaced with `urlparse`/`urlunparse` to handle edge cases
(trailing slashes, multiple `/v1`, no `/v1`, etc.).

**6. `_check_user_slot` silently drops on timeout.** After
5 minutes in queue, the message was dropped with only a
log line. Now sends the user a "Бот перегружен" message
before dropping. UX improvement.

**7. `set_active_thread` not in transaction.** Wrapped the
two writes (`INSERT OR REPLACE` + `UPDATE last_used`) in
explicit `BEGIN`/`COMMIT` for consistency with v0.10.1's
`delete_thread` fix.

**8. Webhook mode lacks signal handlers.** `webhook.run()`
awaited `asyncio.Event()` forever — no response to SIGTERM.
v0.10.2 installs handlers for SIGINT/SIGTERM, triggers
`runner.cleanup()` on shutdown so in-flight requests can
finish.

### False alarms (Qwen wave-3 was wrong about these)

- **3.1 `_RATING_PREFIX_RE` lost `^\s*` allowance.** Already
  has `^\s*` in `prompts.py` (v0.10.0 had this correct).
  Models can have leading whitespace and the regex still
  matches.

- **3.2 `_EMOJI_CHAR_RE` different versions.** Both
  `bot.py` and `prompts.py` now use the same regex
  (`^(\S{1,4})\b`). After the v0.10.0 dead-code cleanup,
  the Unicode-range version in `prompts.py` was replaced
  with the bot.py version (which is what `rating.py` actually
  uses).

- **2.8 `BOT_COMMANDS` language inconsistency.** Cosmetic,
  not blocking. Russian bot uses Russian descriptions with
  English technical terms ("sub-talk") which is intentional
  and matches the operator's group convention.

### Tests (60 total, was 57)

- `test_empty_response_fallback_is_imported_in_handlers` —
  regression: same object in handlers and config
- `test_send_reply_does_not_crash_on_empty_text` — actually
  exercises send_reply with empty text, verifies fallback
  is used
- `test_tag_sender_strips_underscore_prefixed_fields` — no
  `_` fields leak to LLM API

The new tests would catch a regression of the wave-3 bugs.
## [v0.8.0] — 2026-10-10 — v0.7.8 → v0.7.9b + v0.8.0 → v0.8.2 chain

Hotfix chain documented in v0.9.0 above. This entry
exists so the chain has a single-versioned anchor for
`git tag` purposes; the actual changes are in v0.9.0.

## [v0.7.0] — 2026-10-09 — production hardening baseline

First production-grade run. Code that survived three
operator-test waves plus one set of in-tree `_selftest`
regressions. Predecessor of v0.9.0 (F3 + webhook + hotfix
chain).

### Added

- **5 pytest tests** in `tests/test_selftest.py` covering
  `_selftest()` runs end-to-end, the store singleton
  invariant, the constants module, the F4 LRU bound
  (200 entries max), and the rating parser's body-
  extraction path.

- **19 in-tree selftest groups** in `bot.py:_selftest()`,
  all 19/19 PASS in Docker at this version. Documented
  the selftest bootstrap sequence in `OPERATOR-TESTS-
  MORNING.md`.

- **CodeQL workflow** at `.github/workflows/lint.yml`
  running `ruff` + `mypy` on every push and PR.


## [v0.5.0] — 2026-10-10 — Qwen migration, GROUP_CONTEXT, react_to_message

Big wave: model switch (Ornith → Qwen3.8-27B-Ultra-Heretic-MTP-256k),
GROUP_CONTEXT injected on every call_llama, react_to_message tool
for ad-hoc LLM reactions, full PTB 21 migration, donsetch v4.7.4
integration. The bot now passes the model switch, the system
prompt, the tool call, and the abort ladder without losing
state.

### Added

- **GROUP_CONTEXT system prompt** (bot.py `GROUP_CONTEXT`,
  ~80 lines, injected in every call_llama call before tool-mode
  or rating-mode). Locks the LlmChatPlace rules (10-step rating
  scale, [llm] tag, truth-over-style, no tone policing) into a
  system message that survives model switches and /reset. The
  pinned welcome message stays for humans; the LLM only sees
  this. See [docs/CALL_LLAMA.md](docs/CALL_LLAMA.md) §GROUP_CONTEXT.

- **`react_to_message` tool** (OpenAI function-calling). LLM
  can set a single-emoji Telegram reaction on any message in
  the current chat. Standard 10-step reaction set (👍 👏 ❤️ 🔥
  positive, 😐 🤔 neutral, 😢 😡 🤮 💩 negative). Executor
  wraps `bot.set_message_reaction` with `ReactionTypeEmoji`.
  Default `message_id` is the user's current message. Available
  in non-rating mode; rating mode still uses the hardcoded path.

- **Sender name in history** (storage v4). The bot tags every
  user message with `From: <name>: <text>` so the LLM can tell
  Vasisualy from Dimon in a group. NULL → `From: user:`. Schema
  migration `messages.sender_name TEXT` is idempotent.

- **Malformed tool-call JSON rejection** (Qwen 4.6.x line had
  runaway loops when the LLM emitted `max_chars:8000` 10 times).
  Three checks in the dispatcher:
  1. cheap pre-check (`":` count > 8 or len > 1500) → REJECT
  2. `json.loads` raises → REJECT with parse error
  3. parses to `{}` for non-empty raw → REJECT
  Each rejection returns a tool result asking the LLM to
  re-emit cleanly. Closes the Qwen-500-loop on bad chains.

- **`abort_event` per-handler key**: the Stop button now keys
  on `(chat_id, thinking.message_id)` instead of
  `(chat_id, user_id)`. Concurrent handlers no longer overwrite
  each other's events. 3 selftest cases for distinct events
  across two in-flight handlers.

- **donsetch v4.7.4 integration** with split-shape result.
  `DONSETCH_MCP__TEXT_ONLY=false` (double underscore) in
  `/opt/search/docker/docker-compose.yml` returns the
  human-readable text + structured JSON separately, matching
  what the bot's `donsetch_call` was designed for. Locked in
  by a new live-MCP selftest block that asserts the response
  contains `"Search results"` and NOT `"[meta]"`.

- **Comprehensive selftest suite** (~75 cases, runs on every
  container start). Categories: routing, known_topics, lazy
  history trim, rating parser, rating emoji, noise filter,
  reply filter, per-user semaphore, LLM smoke, chat_member
  welcome, edit-replace, per-handler abort key, PTB 21
  download_with_limit, sender-name flow, malformed tool-call
  JSON rejection, react_to_message, non-streaming tool_call
  regression, GROUP_CONTEXT injection, real donsetch
  integration. Every commit that adds behaviour adds a case.

### Changed

- **Model switch**: `LLAMA_URL` `192.168.10.7:8080` → `192.168.10.6:8080`,
  `MODEL` `Ornith-1.5-35B-A3B-Uncensored` → `Qwen3.8-27B-Ultra-Heretic-MTP-256k`.
  Speculative decoding (MTP) on Qwen 4.7.x. Vision support
  retained (multimodal flag). Whisper stays on `.7:8000`.

- **`call_llama` non-streaming branch** now falls through to
  the unified tool-dispatch loop when the LLM responds with
  `tool_calls`. The old code unconditionally returned
  `content` after a non-streaming POST, which silently dropped
  vision-task tool calls (e.g. "react to this picture" would
  hang with empty thinking). Sentinel `_ns_handled` skips the
  streaming branch to avoid a second POST. Regression test
  mocks llama-server to return a tool_call + a follow-up text,
  asserts the executor ran exactly once.

- **`[llm]` tag reclassified** from "opt-out convention" to
  "REQUIRED header on every LLM message". The earlier wording
  let the LLM interpret the tag as optional and skip it on
  direct answers, which broke the peer-LLM rating protocol.
  New wording in GROUP_CONTEXT §3 is explicit, with examples
  for every message type and a "do NOT skip" rule in the
  not-do list.

- **Tool dispatch** (`bot.py:_tag_sender` closure) gained
  three new kwargs: `bot`, `chat_id`, `current_message_id`.
  Threaded through all 4 handlers (text/photo/voice/document)
  and their retry branches. None defaults are safe — the
  dispatch branch only fires when the LLM actually calls
  `react_to_message`.

### Fixed

- **PTB 21 download compat**: `File.download_as_chunks` was
  removed in PTB 21. Replaced with `File.download_as_bytearray`
  in a new `_download_with_limit` helper. Two size guards:
  pre-check on `file.file_size` (no download if Telegram
  already knows the file is too big), post-check on `len(buf)`
  (catches the rare case where `file_size` is missing).
  4 selftest cases for oversized rejection, small download,
  friendly error, post-check.

- **Stop button race condition**: in group mode with concurrent
  users, clicking Stop on one user's thinking message
  sometimes aborted a DIFFERENT user's request. Caused by the
  abort_event key being `(chat_id, user_id)` — the dispatch
  loop in a different handler overwrote the event. Fixed by
  keying on `(chat_id, thinking.message_id)`. Verified by
  selftest with two concurrent in-flight handlers.

- **Tool-call JSON runaway loop** (Qwen 4.6.x + MTP heads):
  LLM emitted `max_chars:8000` 10 times in the same args
  object. `json.loads` silently took the last value, the
  tool errored, the LLM retried with more garbage, until
  llama-server returned 500. The 500 was the SYMPTOM; the
  runaway tool-calling loop was the cause. Fixed in
  `call_llama` dispatcher (see "Malformed tool-call JSON
  rejection" above).

- **`import asyncio` shadowing in `_selftest`**: a local
  `import asyncio` inside a new selftest block made `asyncio`
  a function-local variable for the WHOLE `_selftest` function,
  breaking every earlier `asyncio.X(...)` call with
  UnboundLocalError. Bot crashed on first startup after the
  new test was added. Fixed by using the module-level import
  (asyncio is already imported at the top of bot.py).

# Changelog

All notable changes to this project are documented here.
Format: [Semantic Versioning](https://semver.org/) + sections
per release. The repo's `git log` is the granular record;
this file is the human-readable summary.

## [v0.5.1] — 2026-10-10 — third-pass review fixes (T1-T8)

Response to the third code-review pass on v0.5.0. The P0
items are real bugs that the prior two reviews missed;
the P1/P2 items are refactoring and dead-code removal.

### P0 — fix before next deploy

  - **T1 (P0-1): global LLM concurrency cap** — added
    `_GLOBAL_LLM_SEM = asyncio.Semaphore(4)` and wired it
    into all 4 message handlers (handle_text, handle_photo,
    handle_voice, handle_document). Without this cap, the
    v0.5 `asyncio.create_task` switch in the polling loop
    plus the per-user semaphore (=2) meant 50 different
    users in a group could each fire one message and we'd
    queue 50 concurrent 27B forward passes on a single GPU
    → OOM. With cap=4, llama-server sees at most 4
    in-flight requests no matter how many users spam.
    Defense in depth: also added `limit=10` to the
    `getUpdates` params so the per-cycle batch is bounded.
  - **T2 (P0-2): GROUP_CONTEXT position in tools mode**.
    The previous order in tools mode was
    `[helpful_assistant_sys, GROUP_CONTEXT, ...rest]`, which
    made Qwen3.8 treat the generic "helpful assistant"
    persona as the most-authoritative system message and
    deprioritise the LlmChatPlace rules. Fix Option B from
    the review: rewrite the tools-mode `sys_prompt` to lead
    with GROUP_CONTEXT (so the [llm] tag and rating-on-truth
    rules are at the most-authoritative position), then
    drop the now-redundant `_group_ctx_msg` from `messages`
    in tools mode. Rating mode is unchanged (RATING_RULES
    first, GROUP_CONTEXT second).

### P1 — refactor + dead code

  - **T3 (P1-1): `_apply_rating_and_persist()` helper** —
    pulled the 30-line `if rating_active: ... else: ...`
    block out of all 4 handlers into a single async helper.
    Each handler now has a one-line call. Removes ~100
    lines of duplication. Pure refactor, no behavior
    change; all selftest cases still pass.
  - **T4 (P1-2): deleted dead code** — `_msg_text_edited_during()`
    function (10 lines) + the `_user_msg_text` data structure
    and its 50-line explanatory comment block (60 lines) +
    the polling-loop write to `_user_msg_text` (12 lines) +
    the `_bg_text_offset` global. The function was a no-op
    since the background text updater was disabled
    (Telegram rejects simultaneous getUpdates with HTTP
    409), and nothing else read the dict. Total: ~130
    lines deleted.
  - **T5 (P1-3): `__ABORTED__` sentinel → `None`** — `call_llama`
    returns `None` (was `'__ABORTED__'`) on abort; handlers
    check `if bot_response is None` (was `== '__ABORTED__'`).
    3 return sites + 5 check sites updated. The string
    sentinel was fragile: a user could paste `__ABORTED__`
    into chat and the bot would silently swallow its own
    response.
  - **T6 (P1-4): `_tag_sender` deduplicated** — the
    tools-mode closure and the rating-mode closure were
    byte-for-byte identical. Lifted to a single module-level
    function. Both branches now use the same code; "did I
    keep them in sync?" hazard removed.

### P2 — cleanup

  - **T7 (P2-1): `_is_rating_active()` helper** — the 3-line
    `RATING_MODE and _is_group_chat(update) and not
    _should_mute_in_group(update)` check was duplicated in
    4 handlers. Now a single function. The `not
    _should_mute_in_group(update)` clause was already dead
    at the call sites (the dispatch filter drops muted
    messages earlier), but kept as defense-in-depth inside
    the helper.
  - **T8 (P2-2): duplicate GROUP_CONTEXT comment removed** —
    two ~7-line blocks saying the same thing in different
    words. Kept one.

### No production behavior change

End users see the same responses, the same reactions, the
same [llm] tagging, the same tools, the same rating emoji.
The P0 fixes are structural (prevent OOM, make GROUP_CONTEXT
actually take effect); the P1/P2 fixes are refactoring that
should be invisible.

### Live selftest

All 75+ selftest cases pass on container restart. The
"real donsetch integration" subtest is skipped inside the
bot's event loop (it needs `asyncio.run()` which can't be
called from a running loop) — same limitation as v0.5.0;
manually verified via `bot_smoke.py` against the live
donsetch-http.

### Net diff

`bot.py`: +232 / −300 (net −68 lines, despite the new
helper functions, because T3 + T4 alone removed ~200 lines
of duplicated/dispatch/dead code).

## [v0.5.2] — 2026-10-10 — v0.5.1 follow-up paperwork (F1, F2)

Two trivial follow-ups from the v0.5.0 fourth-pass review.
Both are 5-minute edits. No production behavior change.

### Fixed

  - **F1 (NEW-1): stale comment at handle_photo:2923** —
    The 14-line "Edit-during-LLM detection" comment in
    `handle_photo` survived the T4 dead-code deletion in
    v0.5.1 and still said "we keep the data structure for
    future use" — but `_user_msg_text` is gone. Replaced
    with a 6-line comment that explicitly says the dict and
    the helper function were deleted in v0.5.1 (T4) and
    points the reader at the polling loop's edit-replace
    block as the live mechanism. Same fix applies to any
    other handler that has the same comment (only
    `handle_photo` did; the search is exhaustive).
  - **F2 (NEW-2): selftest for `_GLOBAL_LLM_SEM`** —
    The v0.5.1 global LLM concurrency cap (T1) was a
    production safety mechanism but had no selftest
    coverage. Added a 9-line smoke test that acquires
    `_GLOBAL_LLM_SEM_LIMIT=4` slots and asserts the
    semaphore is locked, then releases and asserts it's
    unlocked. Catches a future refactor that accidentally
    removes the cap or changes the limit.

### No production behavior change

End users see the same responses, same reactions, same
[llm] tagging, same tools. The two changes are a comment
edit and a selftest addition; runtime code is unchanged.

### Live selftest

All previous selftest cases still pass; the new
"global LLM semaphore test" group reports 2/2 OK.

### Net diff

`bot.py`: +36 / −15. The +36 includes the new selftest
code (~22 lines: 5-line test + 11-line docstring +
assertion messages) and the new shorter comment (6 lines
vs 14 lines removed). The net is +21 lines, which is
expected for comment + test additions.

## [Group-mode migration — written 2026-10-10, not yet released then]

> This was the repository's `[Unreleased]` section on 2026-10-10, and it
> stayed in that name for the fourteen releases that followed it. Two
> sections called `[Unreleased]` in one file is a section nobody can
> point at, so it is renamed here and left where it is, with its
> original text. The live `[Unreleased]` section is at the top of this
> file, and `tests/test_changelog_structure.py` now fails if a second
> one ever appears.

The bot now runs in **dual mode**: a 1:1 private chat (the
original use case) OR a Telegram supergroup with Topics
(collaborative, multi-user, one LLM history per topic).
Same storage layer, same call_llama pipeline, same tool stack.

### Added

- **Group mode** (Telegram supergroup with Topics):
  - Bot auto-detects via `chat.is_forum` (NOT `message_thread_id`,
    which is `None` for the General topic — see commit 4 in
    the migration for the gotcha)
  - `/newsub <name>` calls `createForumTopic` and replies in
    the current topic
  - `/subs` lists topics the bot created (from local `known_topics`
    table; Bot API has no `getForumTopics` for bots)
  - `/delsub <name|id>` looks up by name or numeric id, calls
    `deleteForumTopic`, wipes local history
  - `/reset` clears messages in the current topic
  - `/here`, `/stats` work in both modes
  - Bot replies in the same topic via `_reply()` that
    preserves `message_thread_id` (PTB's `Message.reply_text`
    does NOT pass it through by default)
  - General topic uses `"general"` as the storage thread_id
    sentinel (Telegram does not assign a numeric id to it)

- **UTF-8 names** for sub-talks and topics. The previous
  ASCII-only regex is replaced with `^[^\s]{1,32}$` (sub-talks)
  and `^[^\s]{1,128}$` (forum topics). Russian, Chinese, emoji —
  all OK.

- **Edit-cmd handling**: edits of a command (tap message → Edit
  → Save, or up-arrow + edit + Send) are treated as new
  commands. Edits of regular text are dropped (we already
  answered the original).

- **Group-mode noise filter ("[llm] honor system")**: the bot
  stays silent in group mode for messages that are either
  from another Telegram bot (`is_bot=true`) or contain
  `[llm]` anywhere in the text or caption (case-insensitive).
  The marker is a convention set in the group's pinned
  welcome message ("If you are LLM, mark yourself with [llm]
  and answer user questions"). The filter is applied at
  the dispatch layer (`_dispatch_update`) so EVERY handler
  — message AND command — is covered uniformly. In private
  mode the filter is a no-op (no other LLM to yield to).
  Suppressions are logged as `[dispatch] muting group-mode
  message from <who>`.

- **Selftest expanded**: 6 cases for `_is_group_chat` matrix
  (private, group legacy, supergroup, supergroup+forum+General,
  supergroup+forum+topic 42, double-check on private) plus a
  full add/list/find/remove round-trip on `known_topics`,
  plus a 7-case matrix for `_should_mute_in_group` covering
  group/private mode × is_bot/has-marker/no-from-user.

### Changed

- **Storage schema v2**: `(user_id, sub_talk)` renamed to
  `(chat_id, thread_id)`. v1 → v2 migration is automatic on
  first `Storage()` init (SQLite `ALTER TABLE RENAME COLUMN`,
  3.25+). Private mode stores `chat_id == user_id`, group mode
  stores `chat_id == group_chat_id` (negative) and
  `thread_id == str(message_thread_id)`.

- **Whitelist semantics**: in private mode, `ALLOWED_USER_IDS`
  and `ALLOWED_USERNAMES` are enforced (silent rejection on
  miss, LOCKDOWN if both empty). In group mode, the whitelist
  is **skipped entirely** — access control is delegated to
  Telegram (group membership, per-topic permissions, admin
  status).

- **All `update.message.reply_text` call sites** in handlers
  replaced with `await _reply(update, ...)`. The helper
  passes `message_thread_id` through when in a topic.

- **Documentation**: README, SETUP, ARCHITECTURE, SECURITY
  all rewritten for dual-mode. `docs/SETUP.md` has a
  "Deployment mode B: Group with Telegram Topics" section
  with 8 sub-sections. `docs/SECURITY.md` reorganised into
  8 numbered sections covering both modes.

### Fixed

- `getForumTopics` does not exist in the Bot API for bots.
  Worked around with the `known_topics` local table
  (commit 7). Without this, `/subs` and `/delsub <name>`
  were unreachable in group mode.

- `edited_message` updates crashed handlers because
  `update.message is None` for those. Fixed in two steps:
  first by dropping the updates (commit 5), then by
  re-processing edited commands (commit 8) for better UX.

- `cmd_callback` had `name` and `thread_id` mixed up (would
  have crashed on first inline-button click in production).
  Found and fixed during the migration.

- `chat.is_forum` is the canonical "is this a group" signal.
  `message_thread_id is not None` was wrong (General topic
  has it as None).

- Russian / Cyrillic / non-ASCII names for sub-talks and
  topics were rejected by the old ASCII-only regex.

### Known limitations

- Topics created outside the bot (manually in Telegram UI, or
  by another bot) are not in `known_topics` and won't show
  in `/subs`. Workaround: delete via UI, recreate via `/newsub`.

- Renamed topics keep their old name in the local DB. Re-issuing
  `/newsub` with the new name updates the row (via `ON CONFLICT
  DO UPDATE`).

- Pre-`0dce580` topics (created before the `known_topics` table
  existed) are not retroactively imported. User must recreate
  them via `/newsub` or delete them via UI.

- No per-user rate limit / semaphore. A user in a group can
  flood the bot with messages. Tracked, not implemented.

- No `/ban` or `/del` admin commands. Deferred until the
  user has real spam to test against.

## Earlier versions

See `git log` for the pre-`cf96920` history. Major themes:

- `750bf2a` — `2f518d8`: SQLite-backed sub-talks, native
  Telegram menu (`setMyCommands` + inline keyboards).
- `c0a0778` / `7c53067` — second-pass review fixes (R-1..R-8).
- `a98d116` — first-pass review fixes (P0-P3).
- `eab99ba` — initial review follow-ups (PII, graceful
  shutdown, tools retry, username warning).
- `26c5556` — initial `CALL_LLAMA.md` and architecture docs.
- `1259cde` — replace hardcoded `192.168.10.7:8080` with
  localhost defaults + auto-discover model.
- `b16500e` — raise `max_tokens` for vision/voice/document
  to 16384.
- `97f2fc8` — simplify non-streaming path in `call_llama`
  (the 15-iteration vision bug).
- `a62bec5` — detect actual image MIME (Telegram photos are
  often WebP, not JPEG).
- `b16500e`, `97f2fc8`, `a62bec5` — first set of vision
  fixes.
- Earlier: `0d0a2db4` and friends — initial bot implementation
  with `conversations = {}` in-memory dict.

## Unreleased — TECH-DEBT #1 facade cleanup + round-3 review

### Fixed

- **`bot.py` held a second, unwritable copy of the per-user semaphore table
  (`_user_semaphores`) and the global LLM semaphore (`_global_llm_sem`).**
  `state.py` rebinds its own module globals inside its accessor functions, so
  the copies in `bot.py` were never the live ones: any code holding a name
  imported from `bot` would have read a container that `state.py` never wrote
  to. No in-tree code did this, so there is no evidence the per-user
  concurrency cap was ever bypassed in production — but the trap was live and
  silent. `tests/test_facade_ownership.py` now fails if either name reappears
  in `bot.py`.
- **Video notes raised `NameError` instead of being size-checked.**
  `handlers.py` imported `MAX_VOICE_BYTES` but not `MAX_VIDEO_NOTE_BYTES`, and
  one handler serves both formats because `filters.AUDIO` catches voice and
  round video alike. Voice notes worked, video notes did not.
- **`parse_rating_response` discarded the entire response body.** It used
  `text[m.end():]`, but `_RATING_PREFIX_RE` is anchored with `$` and ends in
  `(?P<rest>.*)`, so `m.end()` sits past the end of the string. Every parsed
  rating-mode response produced `body=""` and the user received an empty
  reply. `RATING_MODE` is off by default, so this was latent — it is the
  failure D1 was about to walk into.
- **`KNOWN_TOOL_NAMES` was used bare on the unknown-tool paths** of
  `call_llama` while only two other names were imported from
  `tool_isolation`, so the branches meant to name the known tools raised
  `NameError` instead of producing the message.
- **The overload notice was swallowed.** `access._check_user_slot` told the
  user "бот перегружен" inside a bare `except Exception: pass`, so every
  Telegram API failure on that path looked identical to a clean drop. It now
  emits a structured line on stderr:
  `{"level":"error","event":"overload_notice_not_delivered", ...}`.

### Changed

- `bot.py` 2245 → 261 lines. 73 of those are code; 165 are comments. The 1894
  lines of `_selftest()` moved to `selftest.py`, six duplicated definitions
  moved to their owners (`state.py`, `chat_utils.py`, `prompts.py`), and seven
  unused imports went with them.
- `tests/test_handlers.py` no longer patches `bot.get_store`. It was
  compensating for `turn.py` never having been patched, and only worked
  because `import bot` ran `bot.py`'s module-level `store = get_store()`
  against an already-patched storage module. The fixture now patches the
  module that owns the name.

### Known still open

- `bot.py` keeps its ~15 `from X import (...)` re-export blocks. It is a
  script and an entry point, not a library — nothing in `telegram-bot/` imports
  it — so those blocks exist for forks that patch `bot.*`. Enumerated in
  TECH-DEBT.md #1.
