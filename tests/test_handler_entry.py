"""Routing and the per-user slot, written once instead of four times.

TECH-DEBT #13, block D. Every handler opened the same way: route the
message to its (chat_id, thread_id), take the per-user slot, and drop
the update if either step says no. Four copies of seven lines.

The one line in there that is not bookkeeping is `user_id`:

    sem = await _check_user_slot(chat_id, user_id, update)

It must be the effective user's id. In private mode Telegram uses the
same value for chat_id and user_id, so the wrong one works there and
breaks only in a group, where chat_id is the group's negative id. The
alias this replaced made the abort_event key (group_id, group_id) while
the callback handler looked up (group_id, real_user_id) -- the Stop
button failed silently, which is the worst way for it to fail. The
tests below do not check that this is the id; they check that the value
comes from `effective_user` and not from the routed chat.

The drop cases are the other half. `_route_to_thread` returns None for
an unauthorized sender in private mode, and `_check_user_slot` returns
None when the user is over their concurrent-turn limit. Both mean the
update goes nowhere, and both used to be a bare `return` in four places
where a slot that had just been taken was the thing at risk.
"""
import ast
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.tech_debt(item=13)

BOT = Path(__file__).resolve().parent.parent / "telegram-bot"
if str(BOT) not in sys.path:
    sys.path.insert(0, str(BOT))

import handlers  # noqa: E402

HANDLERS = ["handle_photo", "handle_voice", "handle_document", "handle_text"]


def _patch(monkeypatch, routed, slot):
    """Stand in for the two collaborators the helper calls.

    Patched on the module the helper reads them from, not injected as
    arguments: the signature is `(update, context, label)` because that
    is what the four callers have, and adding a seam only for tests would
    make the extraction look more tested than it is.
    """
    async def route(update, context):
        return routed
    monkeypatch.setattr(handlers, "_route_to_thread", route)
    monkeypatch.setattr(handlers, "_check_user_slot",
                        lambda chat_id, user_id, update: _ret(slot))
    return route


def _tree():
    return ast.parse((BOT / "handlers.py").read_text(encoding="utf-8"))


def _fn(tree, name):
    for n in ast.walk(tree):
        if isinstance(n, ast.AsyncFunctionDef) and n.name == name:
            return n
    raise AssertionError(f"{name} not found in handlers.py")


# --- what it returns ------------------------------------------------------

async def test_it_returns_the_four_values_a_handler_needs(monkeypatch):
    _patch(monkeypatch, (7, "main", False), _semaphore())
    got = await handlers._acquire_turn_slot(
        _update(chat_id=7, user_id=7, thread_id=None), _ctx(), label="handle_text")
    assert isinstance(got, handlers._TurnSlot)
    assert got.chat_id == 7
    assert got.thread_id == "main"
    assert got.user_id == 7
    assert got.sem is not None


async def test_a_named_tuple_so_a_reordered_field_is_a_compile_error():
    """Four values, unpacked in the same order by four callers.

    A bare tuple would make a swap silent; the named one makes it an
    AttributeError at the point of use instead.
    """
    assert handlers._TurnSlot._fields == ("sem", "chat_id", "thread_id", "user_id")


# --- the two ways it drops the update ------------------------------------

async def test_an_unauthorized_sender_drops_the_update(monkeypatch, caplog):
    _patch(monkeypatch, None, _semaphore())
    got = await handlers._acquire_turn_slot(
        _update(), _ctx(), label="handle_photo")
    assert got is None
    assert "[handle_photo] REJECTED" in caplog.text


async def test_a_user_over_their_slot_limit_drops_the_update(monkeypatch):
    """`_check_user_slot` returns None rather than waiting. The handler
    must not treat that as a slot and go on to release it."""
    _patch(monkeypatch, (5, "main", False), None)
    got = await handlers._acquire_turn_slot(
        _update(), _ctx(), label="handle_text")
    assert got is None


async def test_the_rejection_line_carries_the_handler_label(monkeypatch, caplog):
    """One string, four handlers -- without the label it is not
    attributable in a log."""
    _patch(monkeypatch, None, None)
    await handlers._acquire_turn_slot(
        _update(), _ctx(), label="handle_document")
    assert "[handle_document] REJECTED" in caplog.text
    assert "[handle_photo]" not in caplog.text


# --- the id that is easy to get wrong ------------------------------------

async def test_the_slot_is_taken_for_the_user_not_the_chat(monkeypatch):
    """Group mode: chat_id is the group's negative id, user_id is not."""
    group_id, real_user = -1001234, 555
    _patch(monkeypatch, (group_id, "42", True), _semaphore())
    got = await handlers._acquire_turn_slot(
        _update(chat_id=group_id, user_id=real_user), _ctx(), label="handle_text")
    assert got.chat_id == group_id
    assert got.user_id == real_user
    assert got.user_id != got.chat_id


def test_the_user_id_comes_from_effective_user_not_from_the_route():
    """Static, because the failure is invisible in a passing test: with
    a private chat the two values are equal, so a wrong one works."""
    src = (BOT / "handlers.py").read_text(encoding="utf-8")
    fn = _fn(ast.parse(src), "_acquire_turn_slot")
    sources = {
        ast.unparse(st.value)
        for st in fn.body if isinstance(st, ast.Assign)
        for t in ast.walk(st.targets[0])
        if isinstance(t, ast.Name) and t.id == "user_id"
    }
    assert sources == {"update.effective_user.id"}, (
        f"user_id is bound from {sources}, not from the user"
    )


# --- the callers ---------------------------------------------------------

@pytest.mark.parametrize("handler", HANDLERS)
def test_every_handler_delegates_the_whole_preamble(handler):
    """And nothing else: no handler routes or takes a slot itself."""
    fn = _fn(_tree(), handler)
    own = [n.lineno for n in ast.walk(fn)
           if isinstance(n, ast.Call)
           and ((isinstance(n.func, ast.Name)
                 and n.func.id in ("_route_to_thread", "_check_user_slot"))
                or (isinstance(n.func, ast.Attribute)
                    and n.func.attr == "_check_user_slot"))]
    assert not own, (
        f"{handler} still does its own routing/slot at {own}. Four "
        f"copies is what this block removed; the point is not that one of "
        f"them was fixed"
    )


@pytest.mark.parametrize("handler", HANDLERS)
def test_every_handler_calls_it_exactly_once_and_unpacks_all_four(handler):
    fn = _fn(_tree(), handler)
    calls = [n for n in ast.walk(fn)
             if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name)
             and n.func.id == "_acquire_turn_slot"]
    assert len(calls) == 1, f"{handler}: {len(calls)} calls, expected 1"

    unpacks = [n for n in ast.walk(fn)
               if isinstance(n, ast.Assign)
               and isinstance(n.value, ast.Name)
               and n.value.id == "slot"
               and isinstance(n.targets[0], ast.Tuple)]
    assert len(unpacks) == 1, f"{handler}: {len(unpacks)} unpacks of `slot`"
    assert [e.id for e in unpacks[0].targets[0].elts] == list(
        handlers._TurnSlot._fields), (
        f"{handler} unpacks {[e.id for e in unpacks[0].targets[0].elts]}, "
        f"which is not the field order -- the values are assigned to the "
        f"wrong names and nothing catches it"
    )


@pytest.mark.parametrize("handler", HANDLERS)
def test_the_drop_is_still_a_drop(handler):
    """`slot is None` must return, and must be inside the handler -- the
    helper cannot return for it."""
    fn = _fn(_tree(), handler)
    guards = [n for n in ast.walk(fn)
              if isinstance(n, ast.If)
              and ast.unparse(n.test) in ("slot is None", "not slot")]
    assert guards, f"{handler}: no `if slot is None` guard"
    assert all(isinstance(n.body[0], ast.Return) for n in guards)


def test_a_slot_is_never_taken_without_being_released_somewhere():
    """The one failure mode with a real cost.

    `_PER_USER_SEMAPHORE_LIMIT` is 2: two lost tokens lock a user out until the
    process restarts. The handler still owns the release in its finally;
    this checks the acquire and the release are in the same function.
    """
    tree = _tree()
    for handler in HANDLERS:
        fn = _fn(tree, handler)
        acquires = [n for n in ast.walk(fn)
                    if isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Name)
                    and n.func.id == "_acquire_turn_slot"]
        releases = [n for n in ast.walk(fn)
                    if isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "release"]
        assert acquires and releases, (
            f"{handler}: acquires={len(acquires)} releases={len(releases)}"
        )


# --- helpers -------------------------------------------------------------

async def _ret(value):
    return value


def _update(chat_id=7, user_id=7, thread_id=None):
    from types import SimpleNamespace
    return SimpleNamespace(
        message=SimpleNamespace(message_thread_id=thread_id, caption=None),
        effective_chat=SimpleNamespace(id=chat_id),
        effective_user=SimpleNamespace(id=user_id),
    )


def _ctx():
    from types import SimpleNamespace
    return SimpleNamespace(bot=None)


def _semaphore():
    import asyncio
    return asyncio.Semaphore(2)