"""T65.1 — a human replying to another human in a group.

The operator's design, from the ticket: the message must still be *seen*. The
model reads it, reasons, and leaves a reaction via react_to_message. It must
not answer, and the reasoning must not appear in the chat.

Before this, the branch was an unconditional `return` before process_update —
so the message was never written to the DB at all. That silent data loss is
the actual bug the ticket fixes; the reaction is the visible half.
"""
import ast
import inspect
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BOT_DIR = REPO_ROOT / "telegram-bot"
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))



def _call_name(call_node):
    """Name of the callee for a Call, whether it is Name or Attribute."""
    return (getattr(call_node.func, "id", None)
            or getattr(call_node.func, "attr", None))


# ---------------------------------------------------------------------------
# the two invariants that matter
# ---------------------------------------------------------------------------

def test_absorb_handler_persists_before_the_llm_call():
    """Persist must come first, and must be unconditional.

    Ordering matters: if the LLM call can raise, and persist runs after it, a
    failed reaction silently drops the message — the exact loss this ticket
    exists to prevent. So this asserts on statement order, not on "persist is
    called somewhere".
    """
    src = (BOT_DIR / "dispatch.py").read_text()
    fn = next(
        n for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.AsyncFunctionDef)
        and n.name == "_handle_reply_to_other_user"
    )
    calls = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name:
                calls.append((node.lineno, name))
    persist = [ln for ln, nm in calls if "persist" in nm]
    llama = [ln for ln, nm in calls if nm == "call_llama"]
    assert persist, "the handler must persist the message"
    assert llama, "the handler must call the LLM (the model has to see it)"
    assert min(persist) < min(llama), (
        "persist() must run BEFORE call_llama(). If the LLM call raises and "
        "persist has not happened yet, the message is lost -- which is the "
        "silent data loss T65.1 was written to remove."
    )


def test_handler_never_sends_a_text_reply():
    """The outward signal is a reaction, not a sentence."""
    src = (BOT_DIR / "dispatch.py").read_text()
    fn = next(
        n for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.AsyncFunctionDef)
        and n.name == "_handle_reply_to_other_user"
    )
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            assert name not in ("reply", "send_message", "_reply"), (
                f"{name}() must not be called from the silent-absorb path -- "
                f"this branch exists precisely to NOT answer"
            )


def test_handler_tags_the_reply_target():
    """The model has to know who was being addressed, otherwise 'вот' is
    unattributable context. The ticket specifies [reply to @name]."""
    src = (BOT_DIR / "dispatch.py").read_text()
    assert "[reply to" in src, (
        "reply-to-human messages must record the target, or the model sees "
        "an answer with no question"
    )


# ---------------------------------------------------------------------------
# reasoning must not reach the GUI
# ---------------------------------------------------------------------------

def test_call_llama_accepts_silent():
    import call_llama
    assert "silent" in inspect.signature(call_llama.call_llama).parameters


def test_silent_suppresses_the_thinking_stream():
    """push_thinking is the only thing that writes '💭 думаю…' into the chat.
    `silent` must short-circuit it, before the Telegram call."""
    # push_thinking moved to llm_thinking.py in TECH-DEBT #3 Block C4. It
    # was a nested def, so this could only ever find it by parsing
    # call_llama.py and hunting for a closure -- which is also why no
    # test could actually call it. The assertions below are unchanged;
    # they were always about the function, not the file it lived in.
    src = (BOT_DIR / "llm_thinking.py").read_text()
    fn = next(
        n for n in ast.walk(ast.parse(src))
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == "push_thinking"
    )
    body = ast.dump(fn)
    assert "silent" in body, (
        "push_thinking must check `silent` -- it is the sole path that shows "
        "reasoning in the chat"
    )
    # The check has to come before any Telegram-side work.
    stmts = [s for s in fn.body]
    silent_idx = None
    for i, s in enumerate(stmts):
        if "silent" in ast.dump(s):
            silent_idx = i
            break
    assert silent_idx is not None and silent_idx <= 1, (
        "the `silent` guard must be at the top of push_thinking, before the "
        "throttle and the Telegram edit — otherwise reasoning still leaks"
    )


def test_handler_passes_silent_true():
    src = (BOT_DIR / "dispatch.py").read_text()
    fn = next(
        n for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.AsyncFunctionDef)
        and n.name == "_handle_reply_to_other_user"
    )
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and _call_name(node) == "call_llama":
            kw = {k.arg: k.value for k in node.keywords}
            assert "silent" in kw, "call_llama must be invoked with silent=..."
            assert isinstance(kw["silent"], ast.Constant) and kw["silent"].value is True, (
                "the absorb path must pass silent=True — passing silent=False "
                "shows the group's private chatter as a '💭 думаю…' message"
            )
            # No thinking message to update: nothing was sent to the chat.
            tm = kw.get("thinking_msg")
            assert isinstance(tm, ast.Constant) and tm.value is None


def test_thinking_msg_is_none_not_a_real_message():
    """Regression guard: passing a real Message here would update a message
    the bot never sent."""
    src = (BOT_DIR / "dispatch.py").read_text()
    fn = next(
        n for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.AsyncFunctionDef)
        and n.name == "_handle_reply_to_other_user"
    )
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and _call_name(node) == "call_llama":
            tm = {k.arg: k.value for k in node.keywords}.get("thinking_msg")
            assert isinstance(tm, ast.Constant) and tm.value is None, (
                "thinking_msg must be None in the absorb path"
            )


# ---------------------------------------------------------------------------
# budget + failure containment
# ---------------------------------------------------------------------------

def test_reaction_turn_has_its_own_small_budget():
    import limits
    assert limits.MAX_TOKENS_REACT_ONLY > 0
    assert limits.MAX_TOKENS_REACT_ONLY < limits.MAX_TOKENS_DEFAULT, (
        "a reaction turn must be cheaper than a normal answer — it produces "
        "one emoji, and a runaway here is pure waste"
    )


def test_handler_swallows_llm_failure():
    """A failed reaction must not surface to the group and must not undo the
    persist. Both failures are already durable by then."""
    src = (BOT_DIR / "dispatch.py").read_text()
    fn = next(
        n for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.AsyncFunctionDef)
        and n.name == "_handle_reply_to_other_user"
    )
    handlers = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.ExceptHandler)
    ]
    assert handlers, "the LLM call must be wrapped in try/except"
    # And the exception must not be re-raised.
    for h in handlers:
        for node in ast.walk(h):
            assert not isinstance(node, ast.Raise), (
                "do not re-raise: the group must never see an error for a "
                "message that was not addressed to the bot"
            )


def test_dispatch_branch_calls_the_handler_instead_of_returning():
    """The old code returned early. If someone restores a bare `return` here,
    the message is silently dropped again — the exact regression."""
    src = (BOT_DIR / "dispatch.py").read_text()
    fn = next(
        n for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_dispatch_update"
    )
    found_handler = False
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            # The call may be a bare Name (`_handle_reply_to_other_user(...)`)
            # or an Attribute (`self._handle_...`). Checking only `.attr`
            # silently misses the first form — which is exactly the mistake
            # that made this test fail against correct code.
            name = (
                getattr(node.func, "id", None)
                or getattr(node.func, "attr", None)
            )
            if name == "_handle_reply_to_other_user":
                found_handler = True
    assert found_handler, (
        "the reply-to-other-user branch must delegate to "
        "_handle_reply_to_other_user"
    )
    # The old log line said "skipping". It must not come back.
    assert "skipping reply-to-other-user" not in src, (
        "the branch no longer skips anything; that log line is how someone "
        "would 'restore' the old behaviour by reading it"
    )