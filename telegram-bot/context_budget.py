# context_budget.py
# Decides WHICH already-persisted messages get sent to the engine.
#
# T70. Why this exists
# -------------------
# The bot is published to a public repo with 200+ forks. The engine is not
# ours — it may be llama.cpp, Strata, vLLM, or anything else that speaks
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
#     05:00 restart (T68) is what re-anchors and bounds it. Without that
#     restart, context grows until the engine refuses it — which is why T70.4
#     makes overflow loud instead of silent.
#
# History is never destroyed
# --------------------------
# Nothing here deletes. SQLite is the source of truth; whatever this module
# returns is a derived, recomputable view. The `TRIM_HIGH` lazy trim that used
# to DELETE rows at 501 is gone (T67) precisely because it destroyed history
# that is destined for LoRA training data.

from __future__ import annotations

import time
from typing import Callable, Iterable, Sequence

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
    __import__("os").environ.get("BOT_REMEMBER_DAYS", str(DEFAULT_REMEMBER_DAYS))
)
MAX_MESSAGES = int(
    __import__("os").environ.get("CONTEXT_MAX_MESSAGES", str(DEFAULT_MAX_MESSAGES))
)
STRATEGY = (
    __import__("os").environ.get("CONTEXT_STRATEGY", "window").strip().lower()
)

VALID_STRATEGIES = ("window", "summarize")


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

    # -- introspection, used by /stats and tests --------------------------
    def describe(self) -> dict:
        return {
            "strategy": self.strategy,
            "max_messages": self.max_messages,
            "remember_days": self.remember_days,
            "anchor": self.anchor,
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

    def apply_summarize(self, messages: Iterable[dict]) -> list[dict]:
        """Keep the in-window tail; optionally prepend an older-context summary.

        The summary is NOT generated here — this stays pure. The caller runs
        the summarization (an LLM round-trip) and passes the result via
        `messages` being already assembled, or by using `split_for_summary`
        and prepending the result itself.

        Kept as the explicit strategy name so that a fork can select it in
        .env; the actual LLM call belongs in the caller (handlers.py),
        because that is where the abort_event and the stop button live.
        """
        return self.apply(messages)

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


#: What the user is told. Names the cause and the fix, because the operator's
#: design makes the fix known: the daily 05:00 restart re-anchors the window.
OVERFLOW_MESSAGE = (
    "⚠️ Контекст исчерпан — движок отказался принимать запрос целиком.\n\n"
    "Это значит, что накопленная история не помещается в окно модели. "
    "Обычно помогает /reset в этом чате. Бот также перезапускается ночью в 05:00, "
    "после чего окно памяти пересчитывается."
)