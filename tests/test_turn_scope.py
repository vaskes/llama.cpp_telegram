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

        # The construction must not live inside any Try body.
        for node in ast.walk(fn):
            if isinstance(node, ast.Try):
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