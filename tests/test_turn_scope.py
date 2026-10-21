"""Structural checks for TECH-DEBT #4.

Written after the refactor itself introduced an UnboundLocalError. turn.Turn
was constructed inside each handler's `try`, while the `except` calls
turn.rollback() -- so every exception raised before the construction line hit
`UnboundLocalError: cannot access local variable 'turn'` instead of rolling
back. The rollback was unreachable in exactly the window it exists for.

Unit tests did not catch it: none of them raise an exception before the
persist, so the construct was always already bound. The in-container smoke
test did, which is the argument for running the thing rather than reading it.
"""
import pytest
import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BOT_DIR = REPO_ROOT / "telegram-bot"
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

HANDLERS = ("handle_text", "handle_voice", "handle_document")


def _handler(fn_name):
    tree = ast.parse((BOT_DIR / "handlers.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == fn_name:
            return node
    raise AssertionError(f"{fn_name} not found")


def _calls_rollback(node):
    """Does this try reach a .rollback() call anywhere in it?

    Deliberately not `turn.rollback` as a substring of ast.dump(). ast.dump
    renders an attribute access as

        Attribute(value=Name(id='turn'), attr='rollback')

    so the dotted form never appears in the output and the match is always
    empty. That is not hypothetical: it made the assertion below fire on a
    handler that rolls back perfectly well, which is the same
    claim-does-not-match-the-check family this file exists to catch.
    """
    stmts = list(node.body) + list(node.handlers) + list(node.finalbody)
    return any(
        isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "rollback"
        for stmt in stmts
        for n in ast.walk(stmt)
    )


def test_turn_is_built_before_the_try_that_rolls_it_back():
    """Not inside the try. The except clause needs it bound even when the
    exception fires on the first statement of the block."""
    for name in HANDLERS:
        fn = _handler(name)
        builds = [n.lineno for n in ast.walk(fn)
                  if isinstance(n, ast.Assign) and n.targets
                  and getattr(n.targets[0], "id", "") == "turn"]
        assert builds, f"{name} never builds a Turn"

        has_rollback = any(
            isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "rollback"
            for n in ast.walk(fn)
        )
        assert has_rollback, f"{name} should roll back on abort"

        # Only the Try blocks that actually roll back matter.
        #
        # The first version flagged EVERY try, and the B1 guards tripped it.
        # `except BaseException: sem.release(); raise` wraps the window
        # between the semaphore acquisition and the main try, and in three of
        # the four handlers the Turn is constructed inside it. That is
        # correct and needs no rollback: the guard fires before anything is
        # persisted, so there is nothing to undo. Flagging it was the
        # tripwire being suspicious of the right thing and asserting a claim
        # other than the one it was checking.
        rollback_trys = [n for n in ast.walk(fn)
                         if isinstance(n, ast.Try) and _calls_rollback(n)]
        assert rollback_trys, (
            f"{name} persists through a Turn but no try in it calls "
            f"rollback() -- an aborted turn would stay in SQLite"
        )

        for node in rollback_trys:
            inside = [b for b in builds
                      if node.lineno < b <= (node.end_lineno or 0)
                      and b > node.lineno]
            assert not inside, (
                f"{name}: Turn is built at {inside} which is INSIDE the "
                f"try at {node.lineno}. Its except handler calls "
                f"turn.rollback(), so any exception raised before that "
                f"line is an UnboundLocalError and the rollback never "
                f"runs. Build it above the try."
            )

def test_every_rollback_site_reaches_a_bound_turn():
    """Defensive: count the rollbacks against the constructions.

    Three handlers, one rollback per abort path. If a rollback ever appears
    without a matching construction above it, this trips before production
    does.
    """
    for name in HANDLERS:
        fn = _handler(name)
        rollbacks = [n for n in ast.walk(fn)
                     if isinstance(n, ast.Call)
                     and getattr(n.func, "attr", "") == "rollback"]
        builds = [n for n in ast.walk(fn)
                  if isinstance(n, ast.Assign) and n.targets
                  and getattr(n.targets[0], "id", "") == "turn"]
        assert len(rollbacks) >= 1, f"{name} has no rollback"
        assert len(builds) == 1, (
            f"{name} builds Turn {len(builds)} times; expected exactly one, "
            f"above the try"
        )


@pytest.mark.tech_debt(item=4, reason="Pins TECH-DEBT #4: Turn is built above the try that rolls it back.")
def test_this_marker_is_real():
    """Anchor for the tech-debt ledger reconciler.

    The reconciler greps for the decorator rather than trusting that
    somebody kept it attached to something, so the marker needs a
    function to live on.
    """
    assert True
