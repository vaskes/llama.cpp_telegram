from loop_owner import (call_source, loop_source,  # noqa: F401
                       loop_tree, run_one)
"""Loop-local flags must be bound before anything reads them.

Production, twice, on the same variable.

Block C1 made `saw_unavailable_tool` a loop local, turning a
function-scope read into an UnboundLocalError on the error path. Block
D2 added a read of it at the tool-call normalisation site, forty lines
above where it was initialised, so every message in production died with

    UnboundLocalError: cannot access local variable 'saw_unavailable_tool'

1211 tests stayed green: none of them runs a loop body far enough to
reach that read.

**The check is narrow on purpose.** The first version flagged every name
the loop stores, and nine modules went red on ordinary code -- `for tc
in tool_calls` stores `tc`, comprehensions store their targets, and none
of that has anything to do with the bug. A tripwire that reddens on
harmless code is worse than none: it teaches you to ignore it, and then
it is gone.

So the rule is only about names bound by a **plain assignment** -- one
Name target, no tuple, no for-target, no comprehension -- and only when
the first read precedes the first such assignment. That is exactly the
shape that took production down.

One clause keeps it honest: a name the enclosing scope already bound
before the loop is exempt, because then Python has a binding and the
read does not raise. The exemption is why the check is a floor and not a
proof. What it cannot see is a read inside a branch that a later
assignment precedes in source order -- that can still be unbound at
runtime.
"""
import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.tech_debt(item=8)

BOT = Path(__file__).resolve().parent.parent / "telegram-bot"


def _plain_assign_targets(loop):
    """Names bound by `name = ...` inside the loop.

    Not for-targets, not tuple targets, not comprehension targets, not
    augmented assignment. Those are updates or bindings with their own
    guarantees; none of them is the shape that broke.

    Since T8.1 the loop state lives on `self`, so `self.flag = ...`
    binds the same property `flag = ...` bound when the code was
    local. Keyed on the bare attribute name, because that is the name
    both the write and the read use.
    """
    found = {}
    for node in ast.walk(loop):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name):
            found.setdefault(target.id, []).append(node.lineno)
    return found


def _reads(loop, name):
    return sorted(n.lineno for n in ast.walk(loop)
                  if isinstance(n, ast.Name) and n.id == name
                  and isinstance(n.ctx, ast.Load))


def _bound_before_loop(tree, loop, name):
    """True if `name` already has a binding when the loop starts.

    Any assignment to the name earlier in the module, outside the loop,
    counts. A function-scope assignment above the loop binds it; a
    module-scope one makes it a global, and a missing global raises
    NameError rather than UnboundLocalError -- which is not the failure
    this check is about.

    The first version walked only the direct parent's body, and missed
    both kinds: `offset = 0` sits beside a `while` that contains the
    loop, not beside the loop itself, and `_donsetch_session_id` is a
    module global. Both were reported as bugs. Neither is.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.Name) or node.id != name:
            continue
        if not isinstance(node.ctx, (ast.Store, ast.Del)):
            continue
        if node.lineno < loop.lineno and not (
                loop.lineno <= node.lineno <= loop.end_lineno):
            return True
    return False


@pytest.mark.parametrize("module", sorted(
    p.name for p in BOT.glob("*.py") if not p.name.startswith("test_")))
def test_no_loop_reads_a_plain_flag_before_binding_it(module):
    path = BOT / module
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        pytest.skip(f"{module} does not parse on this interpreter")

    offenders = []
    for loop in ast.walk(tree):
        if not isinstance(loop, (ast.For, ast.AsyncFor, ast.While)):
            continue
        for name, assigns in _plain_assign_targets(loop).items():
            reads = _reads(loop, name)
            if not reads:
                continue
            if min(assigns) < min(reads):
                continue
            if _bound_before_loop(tree, loop, name):
                continue
            offenders.append(
                f"{module}:{reads[0]} reads '{name}' but it is first "
                f"assigned at {min(assigns)}")

    assert not offenders, (
        "loop flags read before they are bound -- UnboundLocalError at "
        "runtime:\n  " + "\n  ".join(sorted(offenders))
        + "\n\nThis shape took production down twice."
    )


def _tool_loop():
    """The iteration itself.

    Before T8.1 this returned the `for tc in tool_calls` node, and the
    loop flags were initialised inside it. They are initialised at the
    top of the iteration method now -- which is the point of the
    extraction -- so the method is the unit that owns both the
    initialisation and the first thing that can consume it.
    """
    return run_one()


def test_the_outage_variable_is_pinned_by_name():
    """`saw_unavailable_tool` has broken production twice -- once when C1
    made it a local, once when D2 read it above its initialisation. A
    variable with that history gets an assertion that says so."""
    loop = _tool_loop()
    assigns = _plain_assign_targets(loop).get("saw_unavailable_tool")
    reads = _reads(loop, "saw_unavailable_tool")
    assert assigns, "saw_unavailable_tool is no longer a loop flag"
    assert reads, "and it is no longer read either -- check this test"
    assert min(assigns) < min(reads), (
        f"read at {min(reads)}, first assigned at {min(assigns)} -- "
        f"this is the production outage")


def test_it_is_initialised_before_the_iteration_can_do_anything():
    """Not just before the read -- before the iteration can leave.

    The bug was not that the assignment was a little late. It sat in the
    wrong section, so a reset ran *after* three sites that set the flag
    and silently discarded them -- which is how T49's log line became
    unreachable. Pinning the position stops the obvious repair, "move it
    next to the reader", from reintroducing the clobber.

    The bound is the first statement that can leave the iteration or
    perform IO -- an await, an async with, or a try. A line-count
    tolerance was the first attempt and a mutation that inserted a single
    `await` above the initialisation survived it, which says a magic
    number is not a property. Anything before that line is guaranteed to
    run on every iteration, which is the thing being asserted.
    """
    loop = _tool_loop()

    def can_leave(node):
        """True for a statement that can leave the iteration or do IO.

        `await x` as a statement is an ast.Expr wrapping an ast.Await --
        checking isinstance(node, ast.Await) on the statement list finds
        nothing, and a mutation that inserts one above the initialisation
        survived for exactly that reason.
        """
        if isinstance(node, (ast.AsyncWith, ast.Try, ast.AsyncFor)):
            return True
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Await):
            return True
        # A bare `if ...: return` is an exit too, and the shutdown
        # guard is exactly that shape. Walking for the Return or the
        # Await anywhere inside is the general form.
        return any(isinstance(d, (ast.Return, ast.Await))
                   for d in ast.walk(node))

    escape_at = None
    for node in loop.body:
        if can_leave(node):
            escape_at = node.lineno
            break
    assert escape_at is not None, (
        "the tool loop has no await, async with or try -- this test is "
        "checking a shape the loop no longer has")

    for name in ("saw_unavailable_tool", "iter_had_real_result", "current_calls"):
        lines = _plain_assign_targets(loop).get(name)
        assert lines, f"{name} is not plain-assigned in the tool loop"
        assert min(lines) < escape_at, (
            f"{name} is first assigned at {min(lines)}, but the iteration "
            f"can await at line {escape_at}. It has to be bound before the "
            f"request, not beside its reader -- that is the production "
            f"outage, twice.")


def test_the_check_rejects_the_outage_shape_itself():
    """Control. The analysis is only worth having if it fails on the
    shape it claims to guard, so this runs the same functions over a
    synthetic function written to reproduce the bug -- rather than
    trusting that the report above is real.
    """
    outage = ast.parse(
        "async def f():\n"
        "    for iteration in range(3):\n"
        "        first = saw + normalized.count\n"
        "        saw = 0\n"
        "    return saw\n"
    )
    loop = next(n for n in outage.body[0].body if isinstance(n, ast.For))
    assigns = _plain_assign_targets(loop)
    assert "saw" in assigns, "the control lost the flag"
    reads = _reads(loop, "saw")
    assert reads, "the control lost the read"
    assert min(assigns["saw"]) > min(reads), (
        "the control no longer reproduces the outage ordering")


def test_a_for_target_is_not_treated_as_a_flag():
    """The other control. `for tc in ...` binds `tc` every iteration; if
    that counted as a flag the check would red on ordinary code, which is
    how the first version flagged nine modules."""
    ordinary = ast.parse(
        "async def f():\n"
        "    for tc in tool_calls:\n"
        "        result = tc.get('id')\n"
        "        print(result)\n"
    )
    loop = next(n for n in ordinary.body[0].body if isinstance(n, ast.For))
    assert "tc" not in _plain_assign_targets(loop)
    assert "result" in _plain_assign_targets(loop)