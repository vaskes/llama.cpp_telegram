"""W2-H1 -- the second slot-release case: failure INSIDE the main try.

The wave-1 test injected at `send_action`, which fails in the window
between the slot acquisition and the `try`. That exercised the guard added
in bf3bf09, not the `finally` it was meant to protect.

So the wave-1 defect itself -- an unguarded `thinking.message_id` in the
finally -- was fully reverted and 631 tests still passed. Reviewer-verified:

    defect restored -> 631 passed, 20 skipped

Reaching the finally with `thinking` still None means failing between the
start of the main try and the assignment of the thinking message. In
handle_document that is `ext = doc.file_name.split('.')...`, the first
statement in the try, with `thinking = None` set just above it.

This is the case the previous test could not reach.
"""
import asyncio
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BOT_DIR = REPO_ROOT / "telegram-bot"
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))


class _InjectedFailure(RuntimeError):
    pass


def _returns(value):
    async def _f(*a, **kw):
        return value
    return _f


def _raises(exc):
    async def _f(*a, **kw):
        raise exc
    return _f


def _acquires(sem):
    async def _f(chat_id, user_id, update):
        await sem.acquire()
        return sem
    return _f


class _ExplodingDoc:
    """A document whose `file_name` blows up.

    Reading it is the first statement inside handle_document's main try, so
    raising here lands in the finally with `thinking` still None -- the
    exact shape that used to raise AttributeError instead of releasing.
    """

    file_size = 10
    file_id = "abc123"

    @property
    def file_name(self):
        raise _InjectedFailure("file_name unavailable")


def _update_with_document():
    class Chat:
        id = 777
        type = "private"

        async def send_action(self, *a, **kw):
            return None

    class Bot:
        async def get_file(self, file_id):
            return types.SimpleNamespace(file_id=file_id)

    msg = types.SimpleNamespace(
        chat=Chat(),
        from_user=types.SimpleNamespace(id=1, username="u", first_name="A",
                                        last_name="B"),
        message_id=555,
        message_thread_id=None,
        text=None,
        caption=None,
        caption_entities=[],
        entities=[],
        date=None,
        reply_to_message=None,
        reply_text=None,
        photo=None,
        document=_ExplodingDoc(),
        voice=None,
        audio=None,
        video_note=None,
        sticker=None,
    )
    return types.SimpleNamespace(
        message=msg, effective_user=msg.from_user, effective_chat=msg.chat,
        callback_query=None, message_thread_id=None,
    )


async def _run(monkeypatch, handlers, limit):
    sem = asyncio.Semaphore(limit)
    monkeypatch.setattr(handlers, "_route_to_thread",
                        _returns((777, None, False)), raising=False)
    monkeypatch.setattr(handlers, "_check_user_slot", _acquires(sem),
                        raising=False)
    monkeypatch.setattr(handlers, "_download_with_limit",
                        _returns(b"hello"), raising=False)

    replies = []

    async def _record(update, text, *a, **kw):
        replies.append(text)
        return types.SimpleNamespace(message_id=1)

    monkeypatch.setattr(handlers, "_reply", _record, raising=False)

    escaped = None
    try:
        await handlers.handle_document(
            _update_with_document(),
            types.SimpleNamespace(bot=_Bot()))
    except BaseException as exc:  # noqa: BLE001 - the point is to see it
        escaped = exc
    return sem, escaped, replies


class _Bot:
    async def get_file(self, file_id):
        return types.SimpleNamespace(file_id=file_id)


def test_slot_is_returned_when_the_failure_is_inside_the_main_try(monkeypatch):
    """The finally must release even when it raises doing so."""
    import handlers

    from state import _PER_USER_SEMAPHORE_LIMIT as limit

    sem, escaped, replies = asyncio.run(_run(monkeypatch, handlers, limit))

    assert sem._value == limit, (
        f"slot leaked from the finally: {sem._value}/{limit}. "
        f"The window guard did not fire, so this is the finally itself."
    )

    # handle_document's `except Exception` logs "[ERR doc]" and rolls the
    # Turn back; it does not message the user. So nothing propagates on the
    # fixed path -- and the difference between fixed and defective is not
    # visible in the return value. It shows up in the finally.

    # Defective: the finally dereferences thinking.message_id while thinking
    # is still None. That AttributeError escapes -- nothing catches it,
    # because it is raised after the handler's own except -- and it skips
    # the sem.release() below it.
    assert not isinstance(escaped, AttributeError), (
        f"AttributeError escaped handle_document: {escaped!r}. The finally "
        f"dereferenced thinking while it was still None, which is the "
        f"wave-1 defect and it also skips the release."
    )


def test_thinking_is_bound_before_the_finally_can_read_it():
    """Structural half of the same contract.

    For every releasing finally in the handlers, `thinking` must be
    assigned before the try that owns that finally. Derived from the rule
    test_turn_scope already applies to `turn` -- `thinking` is the
    neighbouring name in the same finally and had no coverage at all.
    """
    import ast

    offenders = []
    tree = ast.parse((BOT_DIR / "handlers.py").read_text(encoding="utf-8"))

    for fn in tree.body:
        if not isinstance(fn, ast.AsyncFunctionDef):
            continue
        if not fn.name.startswith("handle_"):
            continue

        assigns = sorted(
            n.lineno for n in ast.walk(fn)
            if isinstance(n, ast.Assign) and n.targets
            and any(getattr(t, "id", "") == "thinking" for t in n.targets)
        )
        if not assigns:
            continue

        for node in ast.walk(fn):
            if not isinstance(node, ast.Try) or not node.finalbody:
                continue
            reads_thinking = any(
                isinstance(n, ast.Attribute)
                and n.attr == "message_id"
                and isinstance(n.value, ast.Name)
                and n.value.id == "thinking"
                for stmt in node.finalbody
                for n in ast.walk(stmt)
            )
            if not reads_thinking:
                continue
            before = [a for a in assigns if a < node.lineno]
            if not before:
                offenders.append((fn.name, node.lineno))

    assert not offenders, (
        f"finally reads thinking.message_id but nothing assigned thinking "
        f"before the try: {offenders}"
    )