"""What happens before the first poll, and in what order.

TECH-DEBT #10 block B. Splitting `main` into `_install_signal_handlers`,
`_prepare_runtime` and `_poll_loop` left seven mutations completely
unobserved: seven passed, on a file where every one of them is a real
defect. Nothing in the suite reached into the startup path at all.

That is the honest gap. The split made the code better-shaped and
untested at the same time, and the suite did not notice the difference --
a green suite here was measuring nothing.

So: the startup sequence is pinned by AST, as an order, because that is
the property. Each step is a real failure with a real symptom:

- signals after startup -- Ctrl+C during `_register_bot_menu()` aborts
  the process instead of unwinding through `shutdown_event`;
- prepare after signals but before the loop -- this is the correct
  order, and asserting it is what makes the other two meaningful;
- `SHUTDOWN_EVENT` never published -- `call_llama` reads it between
  iterations to bail out, and reads a stale `None`;
- one signal handled -- `docker stop` sends SIGTERM, so a SIGINT-only
  handler means `docker restart` is the graceful path and `docker stop`
  is not.
"""
import ast
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.tech_debt(item=10)

ROOT = Path(__file__).resolve().parent.parent
DISPATCH = ROOT / "telegram-bot" / "dispatch.py"


def _fn(name: str):
    tree = ast.parse(DISPATCH.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == name:
            return node
    raise AssertionError(f"no top-level function {name}")


def _calls_in_order(fn, wanted):
    """Line numbers of calls to `wanted`, in source order."""
    out = []
    for n in ast.walk(fn):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) \
                and n.func.id in wanted:
            out.append((n.func.id, n.lineno))
    out.sort(key=lambda pair: pair[1])
    return out


def test_the_startup_sequence_runs_signals_then_prepare_then_poll():
    """Order, not presence -- and all three, because the run proved that
    a suite which cannot see this one passes on every variant."""
    loop = _fn("_poll_loop")
    names = [name for name, _ in
             _calls_in_order(loop, {"_install_signal_handlers",
                                    "_prepare_runtime"})]
    assert names == ["_install_signal_handlers", "_prepare_runtime"], (
        "the poll loop must install signal handlers before it prepares the "
        f"runtime, and both before it polls; got {names}")

    src = DISPATCH.read_text(encoding="utf-8")
    # and the poll loop is what runs them, not main
    main = _fn("main")
    main_calls = {name for name, _ in
                  _calls_in_order(main, {"_install_signal_handlers",
                                         "_prepare_runtime"})}
    assert not main_calls, (
        f"main also calls {sorted(main_calls)}; there are now two places "
        f"that decide what starting the bot means")


def test_the_signal_handler_actually_sets_the_event():
    """Mutation w5 lived here for one run: delete the `.set()` and the
    suite stayed green, 24 passed, on a bot that logs a clean shutdown
    and then keeps polling forever.

    Every other piece of this file checks that something exists. This one
    checks that the handler does its only job -- the signal is received,
    logged, and otherwise ignored, which looks exactly like working."""
    fn = _fn("_install_signal_handlers")
    handler = next((n for n in ast.walk(fn)
                    if isinstance(n, ast.FunctionDef)
                    and n.name == "_on_signal"), None)
    assert handler is not None, (
        "_install_signal_handlers no longer defines _on_signal; the "
        "signal handlers and the loop they interrupt are one concern")
    sets = [n for n in ast.walk(handler)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "set"]
    assert sets, (
        "_on_signal logs the signal but never sets shutdown_event, so the "
        "poll loop runs until docker kills the container outright")


def test_the_shutdown_event_is_published_for_call_llama():
    """It is a module global read between iterations, with no context arg."""
    fn = _fn("_install_signal_handlers")
    assigns = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "SHUTDOWN_EVENT"
                for t in n.targets)
    ]
    assert assigns, (
        "SHUTDOWN_EVENT is never assigned here; call_llama reads it between "
        "iterations to bail out and would keep seeing the old value")


def test_both_signals_are_handled():
    """`docker stop` sends SIGTERM. Handling only SIGINT means the
    container path is the abrupt one."""
    fn = _fn("_install_signal_handlers")
    sigs = None
    for n in ast.walk(fn):
        if isinstance(n, ast.For):
            try:
                src = ast.unparse(n.iter)
            except Exception:
                continue
            if "SIGINT" in src:
                sigs = src
                break
    assert sigs is not None, "no loop registering signal handlers"
    assert "SIGINT" in sigs and "SIGTERM" in sigs, (
        f"the signal loop registers {sigs}; SIGTERM is what `docker stop` "
        f"and systemd send")


def test_main_runs_the_loop_and_nothing_else_long():
    """`main` was 269 lines and is a scheduling decision now.

    The bound is deliberately loose -- it is a tripwire against the file
    growing back, not a style rule. It caught nothing by itself; the
    order assertions above are what caught the mutations.
    """
    fn = _fn("main")
    size = fn.end_lineno - fn.lineno + 1
    assert size <= 120, (
        f"main is {size} lines again. It decides which delivery mode runs "
        f"and wires shutdown to it; anything else belongs in a named "
        f"function that can be called on its own.")


def test_the_webhook_branch_returns_before_the_poll_loop():
    """The whole reason the poll loop used to be dead in production."""
    fn = _fn("main")
    returns = [n.lineno for n in ast.walk(fn)
               if isinstance(n, ast.Return)]
    poll_call = next((ln for name, ln in _calls_in_order(fn, {"_poll_loop"})),
                     None)
    assert returns and poll_call, "main has no early return or no poll call"
    assert min(returns) < poll_call, (
        "the webhook branch must return before the polling loop is "
        "reached, or the two modes are not exclusive")


def test_the_poll_loop_still_carries_the_allowed_updates():
    """The comment above it explains why: Telegram defaults to
    `["message", "edited_message"]` and silently drops the Stop button
    and the new-member join."""
    lines = DISPATCH.read_text(encoding="utf-8").splitlines(keepends=True)
    loop = _fn("_poll_loop")
    # slice LINES. `src[a:b]` would slice characters and hand back the
    # head of the file, which is the other way this assertion silently
    # stops meaning anything.
    body = "".join(lines[loop.lineno - 1:loop.end_lineno])
    for token in ("callback_query", "chat_member", "edited_message"):
        assert token in body, (
            f"{token} is missing from the poll loop's allowed_updates; "
            f"without it Telegram drops those updates silently")