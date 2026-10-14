"""T61 #R11 v2: every async def cmd_X / start / reset / etc.
in handlers.py and dispatch.py is registered via
app.add_handler(<Handler>(X, handler)) in dispatch.py.

Wave-trap pattern (predicted): a future commit adds a
new async def cmd_foo(update, context) function but
forgets to register it in dispatch.py's
app.add_handler(...). The command becomes a no-op
silently. The user types /foo, the bot does nothing,
no error in logs.

Wider than v1: checks for both CommandHandler AND
CallbackQueryHandler, MessageHandler wrappers, etc.
The key invariant is: every command-handler function
must appear as a `Name` reference inside an
`app.add_handler(<Handler>(..., handler))` call.
"""
from __future__ import annotations

import ast
import pathlib


HANDLERS_PY = (
    pathlib.Path(__file__).parent.parent
    / "telegram-bot"
    / "handlers.py"
)
DISPATCH_PY = (
    pathlib.Path(__file__).parent.parent
    / "telegram-bot"
    / "dispatch.py"
)


def _extract_command_handler_functions(src: str) -> set:
    """Find every async def cmd_X(update, context) function.
    Also async def X(update, context) for short command
    names like start, reset, stats.
    """
    tree = ast.parse(src)
    names: set = set()
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if len(node.args.args) < 2:
            continue
        if node.args.args[0].arg != "update" or node.args.args[1].arg != "context":
            continue
        if node.name.startswith("cmd_") or node.name in (
            "start", "reset", "stats", "help", "here",
        ):
            names.add(node.name)
    return names


def _extract_registered_handlers(src: str) -> set:
    """Walk dispatch.py and find every name that appears
    as the second arg (or callback kwarg) of a Handler
    inside an app.add_handler(<Handler>(...)) call.

    Catches CommandHandler, CallbackQueryHandler,
    MessageHandler, and any other Handler subclass.
    """
    tree = ast.parse(src)
    registered: set = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "add_handler":
            continue
        # The first positional arg should be a Handler(...) call
        if not node.args:
            continue
        handler_call = node.args[0]
        if not isinstance(handler_call, ast.Call):
            continue
        # The handler is the LAST positional arg OR the
        # `callback=` kwarg of the Handler(...) call.
        if handler_call.args:
            cb = handler_call.args[-1]
            if isinstance(cb, ast.Name):
                registered.add(cb.id)
        for kw in handler_call.keywords:
            if kw.arg == "callback" and isinstance(kw.value, ast.Name):
                registered.add(kw.value.id)
    return registered


def test_every_command_handler_is_registered():
    """T61 #R11: every async def cmd_X / start / reset / etc.
    in handlers.py and dispatch.py must appear as a
    callback inside an app.add_handler(...) call in
    dispatch.py.
    """
    handler_funcs: set = set()
    for path in [HANDLERS_PY, DISPATCH_PY]:
        src = path.read_text()
        handler_funcs |= _extract_command_handler_functions(src)

    dispatch_src = DISPATCH_PY.read_text()
    registered = _extract_registered_handlers(dispatch_src)

    missing = handler_funcs - registered
    assert not missing, (
        f"Command handler functions in handlers.py / dispatch.py "
        f"that are not registered via app.add_handler: "
        f"{sorted(missing)}. T61 #R11 wave-trap: a future commit "
        f"adds a new async def cmd_foo(update, context) but "
        f"forgets the app.add_handler(<Handler>('foo', cmd_foo)) "
        f"registration. The command becomes a no-op silently. "
        f"Add the registration to dispatch.py's _dispatch_update "
        f"/ Application.builder section."
    )
