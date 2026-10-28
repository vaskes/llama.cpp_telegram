"""The reasoning stream the user sees while the model thinks.

TECH-DEBT #3, Block C4. This was a nested `async def` inside call_llama,
closing over four things, which made it the only uncovered function on
the streaming path that fires on every turn. Not reachable from a test
without running all 1041 lines of the function around it.

Moving it out also removed the reason a test had to duplicate it.

Six branches, all of them load-bearing:

  * silent       -- T65.1. A message addressed to another human in a
                    group. The model still reasons (that is the point),
                    but "💭 thinking…" for a question nobody asked is noise.
  * no message   -- thinking_msg is None; there is nothing to edit.
  * empty text   -- no reasoning to show yet.
  * flood        -- Telegram refused an edit within the last 30s. Lay
                    off entirely; retrying makes it worse.
  * rate limit   -- a normal delta within 5s of the last push. Skipped
                    without being recorded as a flood.
  * force        -- the final flush and the tool-dispatch path pass
                    force=True to bypass both timers. Without it the
                    last thing the user sees is not the last thing the
                    model thought.

The flood backoff and the rate limit are different on purpose. A flood is
a 30s blackout that only `force` overrides; the 5s limit is ordinary
politeness. Conflating them makes the bot silent for half a minute after
one rejected edit.
"""
import time

from llm_markup import _stop_button_markup
from logconf import get_logger

log = get_logger(__name__)

# Tuned against Telegram's own limits. 5s is the rate limit for edits to
# the same message; 30s is the flood backoff we impose on ourselves.
THROTTLE_SEC = 5.0
FLOOD_BACKOFF_SEC = 30.0
# Telegram's body limit is 4096; leave room for the prefix and marker.
MAX_PAYLOAD = 3800
PREFIX = "💭 thinking…\n"


class ThinkingThrottle:
    """The two clocks push_thinking needs, owned explicitly.

    A class rather than two floats passed by value because both are
    updated inside push_thinking and read by the caller between calls.
    The alternative -- returning them and reassigning at every call site
    -- is how a throttle silently stops throttling.

    Per turn, not per process: call_llama builds one per call, so a
    conversation cannot inherit the previous turn's flood blackout.
    """

    __slots__ = ("last_push", "last_flood")

    def __init__(self, last_push: float = 0.0, last_flood: float = 0.0):
        self.last_push = last_push
        self.last_flood = last_flood

    def in_flood_backoff(self, now: float) -> bool:
        return now - self.last_flood < FLOOD_BACKOFF_SEC

    def throttled(self, now: float) -> bool:
        return now - self.last_push < THROTTLE_SEC


async def close_thinking(thinking_msg) -> None:
    """Take the reasoning bubble down.

    The bubble is the user's window into the model's thinking, and it
    has to be gone before the answer's files go out: a document that
    lands under a live "thinking..." message reads as part of the
    thought (the operator's rule of 2026-10-28).

    A bubble that is already gone, or that Telegram refuses to delete,
    is not an error: the answer still has to go out.
    """
    if thinking_msg is None:
        return
    try:
        await thinking_msg.delete()
    except Exception as e:
        log.info(f"[thinking delete] {type(e).__name__}: {e}")


async def push_thinking(reasoning_text: str, *, thinking_msg, silent: bool,
                        throttle: ThinkingThrottle,
                        force: bool = False) -> bool:
    """Show the tail of the model's reasoning. Returns True if edited.

    `force` bypasses both timers and is used for the final flush, where
    showing a stale "thinking" message is worse than one extra edit.

    No-op when silent, when there is no message, or when there is no
    text. None of those are errors: a vision turn has no reasoning_msg at
    all and a content-only turn has no reasoning.
    """
    if silent:
        if reasoning_text:
            log.info(f"[LLAMA] silent reasoning ({len(reasoning_text)} chars, "
                     f"not shown to GUI)")
        return False

    if thinking_msg is None or not reasoning_text:
        return False

    now = time.monotonic()
    if not force:
        if throttle.in_flood_backoff(now):
            return False
        if throttle.throttled(now):
            return False
    throttle.last_push = now

    body = reasoning_text
    truncated_marker = ""
    if len(body) > MAX_PAYLOAD:
        # The tail, not the head: the user wants to see what the model is
        # thinking NOW. Reasoning from earlier iterations is preserved in
        # the final answer.
        body = body[-MAX_PAYLOAD:]
        truncated_marker = "\n[…earlier reasoning omitted…]\n"

    try:

        # editMessageText drops the inline keyboard unless reply_markup
        # is passed again. The ⏹ Stop button has to survive every edit.
        await thinking_msg.edit_text(
            f"{PREFIX}{truncated_marker}{body}",
            reply_markup=_stop_button_markup(),
        )
        return True
    except Exception as e:
        err = str(e).lower()
        if "not modified" in err:
            return False          # the message is already up to date
        if "flood" in err or "too many requests" in err:
            throttle.last_flood = now
            log.info("[thinking edit] flood; backing off edits for 30s")
            return False
        log.info(f"[thinking edit err] {type(e).__name__}: {e}")
        return False