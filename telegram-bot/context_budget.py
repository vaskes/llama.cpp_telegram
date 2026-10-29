# context_budget.py
# Decides WHICH already-persisted messages get sent to the engine.
#
# T70. Why this exists
# -------------------
# The bot is published to a public repo, so a deployment may run an engine
# that is not ours — llama.cpp, Strata, vLLM, or anything else that speaks
# OpenAI-compatible /v1/chat/completions. The engine must stay stateless and
# swapping it must not require changing bot code.
#
# That rules out the obvious shortcut: letting the engine hold sessions
# (`previous_response_id`, Conversations API, llama slots). Each engine has a
# different API for it, so building on one means silent breakage on swap.
# vllm-project/agentic-api does exactly that and is the right idea at the
# wrong weight class (a Rust gateway) for forks of a small bot.
#
# So we do what the industry converged on in 2026, client-side:
#
#     protect recent N  ·  summarize older  ·  budget-assemble
#
# This module implements the assemble step. It NEVER talks to an engine. That
# is the whole point: the context policy is ours, the engine is a pipe.
#
# Engine-agnostic by construction
# ------------------------------
# `apply()` is pure — no I/O, no LLM calls, no clock reads. Everything it
# needs is passed in. That is deliberate:
#   * tests run without network, DB or a model;
#   * the summarize strategy needs an LLM round-trip, which the CALLER
#     performs and hands over as a finished summary;
#   * no asyncio in a sync function.
#
# The default strategy
# --------------------
# `window` — keep messages newer than an anchor fixed at bot start
# (operator decision, 2026-10-20):
#
#     «неделя от старта бота назад, исходя из фичи — загрузка из sql lite при
#      старте бота. этого достаточно, если бот долго не рестартовал он начнет
#      галлюцировать при переполнении — это решается рестартом по крону»
#
# Two properties that are easy to get wrong, so they are stated here and
# enforced by tests/test_t70_context_budget.py:
#
#  1. NOT a sliding window. The anchor is computed ONCE at import time. If it
#     were `now - N days` per request, the window would slide forward and the
#     bot would slowly forget the beginning of every live conversation —
#     silently, with no error anywhere. Nine waves of review have been spent
#     on exactly this class of drift (claim vs implementation).
#
#  2. Upper bound: there is none by age. The bot remembers "everything that
#     was within a week when it started, plus everything since". The daily
#     re-anchor (T68) is what moves it forward — via the process restarting,
#     or via reanchor() in `soft` mode. See
#     docs/adr/ADR-0003-context-reanchoring.md for why the engine is not part
#     of this at all. Without a re-anchor, context grows until the engine
#     refuses it — which is why T70.4 makes overflow loud instead of silent.
#
# History is never destroyed
# --------------------------
# Nothing here deletes. SQLite is the source of truth; whatever this module
# returns is a derived, recomputable view. The `TRIM_HIGH` lazy trim that used
# to DELETE rows at 501 is gone (T67) precisely because it destroyed history
# that is destined for LoRA training data.

from __future__ import annotations

import os
import time
from typing import Callable, Iterable, Sequence

from logconf import get_logger
log = get_logger(__name__)

# Fetch ceiling. This is the "how much do we pull out of SQLite at all"
# bound, NOT the context policy — the policy lives in ContextBudget. It
# exists so a runaway thread cannot drag the entire table into memory
# before the strategy has a chance to assemble anything.
# Kept generous: a week of a heated argument is hundreds of messages.
FETCH_LIMIT = 2000

# === Config ===
# Read from env directly rather than from config.py: context_budget must be
# importable without pulling in the whole bot config chain (tests import it
# standalone). config.py imports these back for the bot's own use.

DEFAULT_MAX_MESSAGES = 20
DEFAULT_REMEMBER_DAYS = 7

#: How far back the bot remembers, in days. 0 or a negative value disables
#: the age filter entirely (send everything up to CONTEXT_MAX_MESSAGES).
#:
#: NOTE: this is the LENGTH of the window, not "how many days ago to look".
#: The anchor is derived from it once at start. See the module docstring.
REMEMBER_DAYS = int(
    os.environ.get("BOT_REMEMBER_DAYS", str(DEFAULT_REMEMBER_DAYS))
)
MAX_MESSAGES = int(
    os.environ.get("CONTEXT_MAX_MESSAGES", str(DEFAULT_MAX_MESSAGES))
)
STRATEGY = (
    os.environ.get("CONTEXT_STRATEGY", "window").strip().lower()
)

VALID_STRATEGIES = ("window", "summarize")

# === T70.5: chunked summaries ===
#
# The operator's decision (2026-10-24), verbatim:
#
#   > сводки надо делать чанками, и помечать. т.е. если ОБЪЕМ данных для
#   > сводки > 200к надо сделав по ней сводку проставить в базе поле СВЕДЕНО,
#   > ссылку на сводку которая сделана и по этому чанку больше сводки не
#   > делать. Если объем данных для сводки <200к не проставлять поле сведено
#   > и не сводить, а грузить как есть в контекст. Ну и при загрузке
#   > соответсвенно грузить только готовые сводики и только сообщение которые
#   > не сведены.
#
# The 200k figure did not survive the arithmetic. 200k of raw history plus
# the fresh window plus llama's 64k thinking block does not fit in a 256k
# engine window -- and the way it fails is the exact failure T70.6 was about
# ("the model thought and did not answer"), so the number had to come down:
#
#     262144 (n_ctx) - 65536 (thinking) - 16384 (answer reserve)
#       = 180224 for the prompt
#     180224 - ~48000 (fresh window, 400 messages) - ~4000 (system + tools)
#       = ~128000 for the OLD part  ->  SUMMARY_CONTEXT_TOKENS = 131072
#
# and the RAW tail inside that old part is capped separately, at 64k. So:
#
#   SUMMARY_TAIL_TOKENS      raw, never-summarised old data in the prompt
#   SUMMARY_CHUNK_TOKENS     old data per summarisation call
#   SUMMARY_CONTEXT_TOKENS   summaries + raw tail together
#
# All three are tokens, estimated (see estimate_tokens below) because the bot
# is engine-agnostic and has no tokenizer. They are budgets, not exact
# counts: an estimate that is slightly wrong must never be the difference
# between "the bot answers" and "the engine refused the prompt".
DEFAULT_SUMMARY_TAIL_TOKENS = 65536
DEFAULT_SUMMARY_CHUNK_TOKENS = 65536
DEFAULT_SUMMARY_CONTEXT_TOKENS = 131072

SUMMARY_TAIL_TOKENS = int(
    os.environ.get("SUMMARY_TAIL_TOKENS", str(DEFAULT_SUMMARY_TAIL_TOKENS))
)
SUMMARY_CHUNK_TOKENS = int(
    os.environ.get("SUMMARY_CHUNK_TOKENS", str(DEFAULT_SUMMARY_CHUNK_TOKENS))
)
SUMMARY_CONTEXT_TOKENS = int(
    os.environ.get("SUMMARY_CONTEXT_TOKENS", str(DEFAULT_SUMMARY_CONTEXT_TOKENS))
)

#: Characters per token, measured on the operator's own chat: 1 MB of text is
#: ~280k tokens (docs/tickets/README.md), i.e. 1048576 / 280000 = 3.75.
#: Measured on mixed Russian/English group chat, which is the content that
#: matters here.
CHARS_PER_TOKEN = 3.75

#: The estimate must OVER-count, never under-count: a budget that is
#: under-estimated is a budget the engine can refuse. 1.15 buys the margin
#: for code blocks, emoji and the JSON envelopes.
TOKEN_ESTIMATE_SAFETY = 1.15


def estimate_tokens(text) -> int:
    """Approximate token count of a string.

    Engine-agnostic by necessity: the bot must not know which engine serves
    it, so it cannot ask for a tokenizer. The number is used for BUDGETS
    (how much old history may be summarised, how much may be loaded raw),
    where being 15% conservative is free and being 15% optimistic is not.

    Empty text costs zero. A non-string (a multimodal content list) is
    measured by its text parts only -- images are not remembered (T67.2).
    """
    if not text:
        return 0
    if not isinstance(text, str):
        if isinstance(text, (list, tuple)):
            total = 0
            for part in text:
                if isinstance(part, dict) and part.get("type") == "text":
                    total += estimate_tokens(part.get("text"))
            return total
        text = str(text)
    return int(len(text) / CHARS_PER_TOKEN * TOKEN_ESTIMATE_SAFETY) + 1


def estimate_message_tokens(message: dict) -> int:
    """Approximate token count of one message dict."""
    return estimate_tokens(message.get("content"))

# === T68: how the window gets re-anchored ===
#
# Three modes, because deployments of this repo have wildly different
# privileges. See docs/adr/ADR-0003-context-reanchoring.md for the reasoning.
#
#   soft            re-anchor inside this process on a timer. No systemd,
#                   no root, no access to the engine required. Works for
#                   everyone, including an engine on someone else's machine.
#   process         this process is restarted (systemd timer / cron / any
#                   supervisor). The module is re-imported, so the anchor is
#                   recomputed for free.
#   process+engine  as above, AND run BOT_ENGINE_RESET_CMD afterwards to
#                   drop the engine's KV cache. Pure VRAM hygiene — see the
#                   ADR for why it is not required for context correctness.
#
# Default is process+engine because that is what the operator runs and asked
# for. Fork operators with no privileges should set `soft`, which needs none.
REANCHOR_MODES = ("soft", "process", "process+engine")
DEFAULT_REANCHOR_MODE = "process+engine"

#: Local time of day for the daily re-anchor, "HH:MM". 24h format.
REANCHOR_AT = (
    os.environ.get("BOT_REANCHOR_AT", "05:00").strip()
)

#: Arbitrary shell command run after a `process+engine` re-anchor, to drop the
#: engine's KV cache. Empty = do not touch the engine. This is where an
#: operator puts `systemctl restart llama-server`, or
#: `ssh -o BatchMode=yes llama-host 'systemctl restart llama-server'`, or an
#: HTTP call to an engine admin endpoint — or nothing at all.
#:
#: It is deliberately a free-form command rather than a typed topology. The
#: set of ways to restart something is open-ended, and a typed enum would
#: need a new case for every host manager, ssh wrapper and API in the wild.
ENGINE_RESET_CMD = (
    os.environ.get("BOT_ENGINE_RESET_CMD", "").strip()
)

REANCHOR_MODE = (
    os.environ.get("BOT_REANCHOR_MODE", DEFAULT_REANCHOR_MODE)
    .strip().lower()
)
if REANCHOR_MODE not in REANCHOR_MODES:
    # Same rule as CONTEXT_STRATEGY: a typo in .env must not stop the bot
    # from starting. Fall back, and shout about it.
    log.info(f"[context_budget] BOT_REANCHOR_MODE={REANCHOR_MODE!r} is not one of "
        f"{REANCHOR_MODES}; falling back to {DEFAULT_REANCHOR_MODE!r}")
    REANCHOR_MODE = DEFAULT_REANCHOR_MODE

#: Only `soft` runs the in-process scheduler. In `process`/`process+engine`
#: the scheduler must NOT also fire, or the anchor would move twice and a
#: self-restarting deployment would restart in a loop.
SOFT_REANCHOR_ENABLED = (REANCHOR_MODE == "soft")


def _default_instance():
    """The shared ContextBudget, or None if the module is still initialising.

    reanchor() runs from the scheduler, long after import, but _DEFAULT is
    assigned further down this file -- so resolving it lazily keeps this
    function safe if anything ever calls reanchor() during import.
    """
    return globals().get("_DEFAULT")


def _anchor_for_days(days: int, now: float | None = None) -> float:
    """Return the start-of-window timestamp for `days`.

    `now` is injectable for tests. Production calls it once, at import.
    """
    if days <= 0:
        return 0.0  # no age filter
    return (time.time() if now is None else now) - days * 86400.0


# Computed ONCE. This assignment is the whole ballgame — see point 1 in the
# module docstring. Do not move this into a function, and do not recompute
# it per request.
CONTEXT_ANCHOR = _anchor_for_days(REMEMBER_DAYS)

#: Bumped by every reanchor() call. /stats reports it so the operator can
#: tell "the window moved 3 hours ago" from "the window moved at 05:00".
ANCHOR_GENERATION = 0


def reanchor() -> dict:
    """Move the window forward by re-deriving CONTEXT_ANCHOR. T68 / ADR-0003.

    This is the ONLY sanctioned way to move the anchor. It exists so `soft`
    mode can re-anchor without restarting the process — see ADR-0003 for why
    the process restart and the engine restart are separate concerns and
    neither is required for context correctness.

    Deliberately NOT called per request. One call per day is the contract;
    per-request would be the sliding-window bug.

    Returns a small dict for the log line. Never raises: a failed re-anchor
    must degrade to the previous (older) anchor rather than take the bot
    down, and an older anchor is the safe direction — the window only grows.
    """
    global CONTEXT_ANCHOR, ANCHOR_GENERATION
    try:
        new_anchor = _anchor_for_days(REMEMBER_DAYS)
    except Exception as exc:  # pragma: no cover - defensive
        log.info(f"[context_budget] reanchor failed, keeping old anchor: {exc}")
        return {"ok": False, "error": str(exc)}
    CONTEXT_ANCHOR = new_anchor
    ANCHOR_GENERATION += 1
    # CRITICAL: the bot does not read CONTEXT_ANCHOR. get_budget() returns the
    # shared _DEFAULT instance, and apply() filters on ITS OWN .anchor, set
    # once in __init__. Updating only the module global leaves the window
    # exactly where it was -- so in `soft` mode the daily re-anchor did
    # nothing at all, while the log cheerfully reported success. Found by
    # Qwen review; the original test asserted only on CONTEXT_ANCHOR, which is
    # why it passed against broken behaviour.
    _live = _default_instance()
    if _live is not None:
        _live.anchor = new_anchor
    log.info(f"[context_budget] reanchored: gen={ANCHOR_GENERATION} "
        f"anchor={int(new_anchor)} remember_days={REMEMBER_DAYS}")
    return {
        "ok": True,
        "anchor": new_anchor,
        "generation": ANCHOR_GENERATION,
        "remember_days": REMEMBER_DAYS,
    }


class ContextBudget:
    """Assembles the message list sent to the engine.

    Strategy is pluggable so that a fork can choose different behaviour with
    one env var, without forking the bot. Unknown strategy names fall back
    to `window` rather than raising — a typo in .env must not stop the bot
    from starting.
    """

    def __init__(
        self,
        strategy: str = STRATEGY,
        max_messages: int = MAX_MESSAGES,
        anchor: float | None = None,
        remember_days: int = REMEMBER_DAYS,
        summarizer: Callable[[Sequence[dict]], str] | None = None,
        summary_tail_tokens: int = SUMMARY_TAIL_TOKENS,
        summary_context_tokens: int = SUMMARY_CONTEXT_TOKENS,
        summary_chunk_tokens: int = SUMMARY_CHUNK_TOKENS,
    ):
        self.strategy = strategy if strategy in VALID_STRATEGIES else "window"
        self.max_messages = max(0, int(max_messages))
        self.remember_days = remember_days
        # anchor=None means "derive from remember_days". Passing an explicit
        # anchor is how tests pin time without touching the module constant.
        self.anchor = _anchor_for_days(remember_days) if anchor is None else anchor
        #: Only used by the `summarize` strategy, and only if the caller
        #: supplies one. Kept as data, not called here — apply() is pure.
        self.summarizer = summarizer
        # T70.5 budgets. See the T70.5 block above for the derivation.
        self.summary_tail_tokens = max(0, int(summary_tail_tokens))
        self.summary_context_tokens = max(0, int(summary_context_tokens))
        self.summary_chunk_tokens = max(0, int(summary_chunk_tokens))

    # -- introspection, used by /stats and tests --------------------------
    def describe(self) -> dict:
        return {
            "strategy": self.strategy,
            "max_messages": self.max_messages,
            "remember_days": self.remember_days,
            "anchor": self.anchor,
            "summary_tail_tokens": self.summary_tail_tokens,
            "summary_context_tokens": self.summary_context_tokens,
            "summary_chunk_tokens": self.summary_chunk_tokens,
        }

    def age_cutoff_active(self) -> bool:
        return self.anchor > 0.0

    # -- the one method that matters --------------------------------------
    def apply(self, messages: Iterable[dict]) -> list[dict]:
        """Return the subset of `messages` to send.

        `messages` must be in chronological order. Each item is the dict
        shape produced by persistence._decode: `role`, `content` and
        `_sender_name`, plus `_created_at` (float) when the caller supplied
        it.

        Two guarantees, both load-bearing:

        1. **The last message is never dropped.** The incoming user message
           reaches the engine ONLY through this list — call_llama builds
           `msgs = [system] + messages` and its `user_text` parameter is
           dead code. If the age filter removed the last message, the model
           would answer a prompt with nothing to answer. So when the window
           would leave nothing, the most recent message is kept anyway.

        2. **Messages missing `_created_at` are KEPT, not dropped.** A missing
           timestamp means "we don't know how old this is", and silently
           discarding it would lose history — the opposite of this feature's
           purpose. Only the message-count cap may remove anything here.
        """
        items = list(messages)
        if not items:
            return []

        selected = items
        if self.anchor > 0.0:
            selected = [m for m in items if self._keep(m)]
            # Guarantee 1: the newest message is the whole point of the turn.
            #
            # Only when the filter removed it AND nothing else survived.
            # Testing `selected[-1] is not items[-1]` alone is wrong: when the
            # newest message is simply older than the window while older ones
            # are inside (a message arriving after a long idle period — exactly
            # what a restart cycle produces), that condition is true, and
            # returning items[-1:] would DISCARD the whole in-window history
            # and keep one stale message. So the real question is whether the
            # window came back empty.
            if not selected and not self._keep(items[-1]):
                selected = items[-1:]

        if self.max_messages and len(selected) > self.max_messages:
            selected = selected[-self.max_messages:]
        return selected

    def _keep(self, message: dict) -> bool:
        created = message.get("_created_at")
        if created is None:
            return True  # unknown age -> keep, see apply() docstring
        try:
            return float(created) >= self.anchor
        except (TypeError, ValueError):
            return True  # unparseable -> keep

    # -- strategies --------------------------------------------------------
    def apply_window(self, messages: Iterable[dict]) -> list[dict]:
        """Default. Age filter + message cap. See apply()."""
        return self.apply(messages)

    def apply_summaries(self, messages: Iterable[dict],
                        summaries: Iterable[dict] | None = None) -> list[dict]:
        """Assemble the context for the `summarize` strategy (T70.5).

        PURE. The LLM round-trips happen in summarize.py, ahead of time, and
        their output arrives here as data. That split is not stylistic:
        apply() is the thing a dozen tests hold pure, and a network call in
        here would make every context test need an engine.

        The operator's rule, verbatim:

          > при загрузке соответсвенно грузить только готовые сводики и
          > только сообщения которые не сведены

        So the old part of the history is made of two kinds of thing:

          * a chunk summary -- one per chunk, standing IN THE PLACE of the
            chunk it replaces, so the model reads the timeline in order;
          * a raw message -- old, never summarised, small enough that the
            operator's rule says "load it as is".

        `messages` must be in chronological order and carries the out-of-band
            `_created_at`, `_id` and `_summary_id` fields. `summaries` is a
            list of dicts with `id`, `text`, `first_msg_id` and
            `last_created_at`; `first_msg_id` is what places the summary on
            the timeline, and `last_created_at` is what says whether its
            chunk is still outside the window.

        A message that carries `_summary_id` pointing at a KNOWN summary is
        NOT loaded raw -- the summary stands for it. That is the operator's
        rule, and it is enforced here as well as in the query, so a caller
        that hands over more than it meant to cannot produce a duplicated
        chunk.

        Two budgets, both applied from the NEWEST end, because the recent
        past is the part worth keeping when something does not fit:

          * the raw tail is capped at `summary_tail_tokens`;
          * summaries + raw tail together at `summary_context_tokens`.

        The fresh window keeps the message cap of the `window` strategy --
        the same newest-N rule, so the two strategies do not disagree about
        what "recent" means.
        """
        items = list(messages)
        summaries = list(summaries or [])
        if not items and not summaries:
            return []

        inside, older = self.split_for_summary(items)
        summary_by_id = {}
        for s in summaries:
            sid = s.get("id")
            if sid is not None:
                summary_by_id[sid] = s

        # --- the old part, as a chronological list of units ---------------
        # A unit is a chunk summary or a raw message. `order` is the id of
        # the first message the unit stands for -- that is what places it on
        # the timeline, and it is why a summary sits where its chunk was
        # rather than in a pile at the head.
        units: list[dict] = []
        for s in summary_by_id.values():
            text = s.get("text") or ""
            if not text.strip():
                continue
            # A summary whose chunk is INSIDE the window is not used: the
            # messages themselves are in the context, and adding the summary
            # would say the same thing twice.
            last_at = s.get("last_created_at")
            if last_at is not None and self.anchor > 0.0 \
                    and float(last_at) >= self.anchor:
                continue
            units.append({
                "kind": "summary",
                "id": s.get("id"),
                "text": text,
                "tokens": estimate_tokens(text),
                "order": s.get("first_msg_id") or 0,
            })
        for m in older:
            sid = m.get("_summary_id")
            if sid is not None and sid in summary_by_id:
                # The summary stands for this message. Loading both would
                # tell the model the same stretch of history twice.
                continue
            units.append({
                "kind": "message",
                "message": m,
                "tokens": estimate_message_tokens(m),
                "order": m.get("_id") or 0,
            })
        units.sort(key=lambda u: u["order"])

        # --- budgets, applied from the newest end -------------------------
        kept: list[dict] = []
        raw_tokens = 0
        total_tokens = 0
        for unit in reversed(units):
            if unit["kind"] == "message":
                if raw_tokens + unit["tokens"] > self.summary_tail_tokens:
                    # Older than the tail budget. The summaries that come
                    # after it in the walk are OLDER still, but they are
                    # compressed -- they are worth more per token, so the
                    # walk continues instead of stopping here.
                    continue
                raw_tokens += unit["tokens"]
            if total_tokens + unit["tokens"] > self.summary_context_tokens:
                break
            total_tokens += unit["tokens"]
            kept.append(unit)
        kept.reverse()

        # --- the fresh window ---------------------------------------------
        fresh = list(inside)
        if self.max_messages and len(fresh) > self.max_messages:
            fresh = fresh[-self.max_messages:]
        if not fresh and items:
            # Same guarantee as apply(): the newest message is the whole
            # point of the turn, so it is never dropped. Unless the old part
            # already carries it -- then adding it again would say the same
            # message twice.
            newest = items[-1]
            already = any(u["kind"] == "message"
                          and u["message"] is newest for u in kept)
            if not already:
                fresh = [newest]

        # --- render --------------------------------------------------------
        out: list[dict] = []
        n_summaries = 0
        for unit in kept:
            if unit["kind"] == "summary":
                n_summaries += 1
                out.append({
                    "role": "user",
                    "content": "Сводка разговора вне окна памяти "
                                f"(часть {n_summaries}):",
                })
                out.append({"role": "assistant", "content": unit["text"]})
            else:
                out.append(unit["message"])
        out.extend(fresh)
        return out

    # -- helper for the summarize strategy's caller ------------------------
    def split_for_summary(self, messages: Iterable[dict]) -> tuple[list[dict], list[dict]]:
        """Return (in_window, older_than_window), both chronological.

        The caller summarises `older`, then prepends the result to `in_window`
        as a synthetic user->assistant pair (the shape OpenAI's cookbook uses:
        the model reads it as a finished conversation, not as an instruction).
        """
        items = list(messages)
        if self.anchor <= 0.0:
            return items, []
        inside, older = [], []
        for m in items:
            (inside if self._keep(m) else older).append(m)
        return inside, older


#: Module-level instance used by persistence.load_history. Built once at
#: import, so it captures CONTEXT_ANCHOR as it is right now.
_DEFAULT = ContextBudget()


def get_budget() -> ContextBudget:
    """The budget used in production. Tests build their own instead."""
    return _DEFAULT


# === T70.4: overflow detection ===
#
# The age window bounds history by TIME, not by capacity. A single very large
# message, a week of uninterrupted activity, or a burst of images will still
# exceed the engine's context window. That is expected — so it has to be
# VISIBLE, not swallowed.
#
# Until T70.4 the bot answered a context-overflow with a confident paragraph
# of hallucination, because the failure was indistinguishable from a normal
# answer at the call site.

#: Substrings engines use for "your prompt is too large". Checked lowercase
#: against the engine's own error text, so this works for llama.cpp, vLLM and
#: anything else that speaks HTTP — no engine-specific parsing.
_OVERFLOW_MARKERS = (
    "exceed",
    "exceeds",
    "too large",
    "too long",
    "context length",
    "context window",
    "n_ctx",
    "kv cache",
    "maximum context",
    "prompt is too",
    "reduce the length",
)


def is_context_overflow(error_text: str | None) -> bool:
    """True when an engine error looks like a context-window overflow.

    Deliberately text-based rather than status-code-based: engines disagree
    on whether overflow is a 400, a 422 or a 200 with empty choices, and we
    do not want a per-engine exception table in a bot that must survive an
    engine swap. A false positive means the user sees "context exhausted"
    when they really hit a rate limit — annoying, not harmful.
    """
    if not error_text:
        return False
    lowered = error_text.lower()
    return any(marker in lowered for marker in _OVERFLOW_MARKERS)


#: What the user is told. Names the cause and the fix.
#:
#: Do not promise a specific restart time here. Whether a re-anchor happens at
#: all, and when, depends on BOT_REANCHOR_MODE — and in `soft` mode the window
#: moves without any restart, so telling the user to wait for a restart would
#: be wrong for exactly the deployments where re-anchoring works best. See
#: docs/adr/ADR-0003-context-reanchoring.md.
OVERFLOW_MESSAGE = (
    "⚠️ Context exhausted — the engine refused the request as a whole.\n\n"
    "This means the accumulated history no longer fits in the model's "
    "window. Usually /reset in this chat helps — it clears the history of "
    "this sub-talk, leaving the others alone."
)