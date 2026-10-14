"""T60 Tripwire #5: dispatch.py MessageHandler without import.

Wave-trap class: 327b7b0. The team fixed a NameError on
handle_photo / handle_voice / handle_text / handle_document
by restoring the `from handlers import (...)` block. This
tripwire generalises the check: every `app.add_handler(...)`
call in dispatch.py must reference a name that is imported
at the top of the file (or defined at module level).

If a future commit adds a new MessageHandler with an
unimported name, this test fails.
"""
from __future__ import annotations

import ast

from test_drift_helpers import (
    DISPATCH_PY,
    imported_names,
    module_level_names,
    parse_source,
)


def test_every_app_add_handler_uses_imported_name():
    """T60 #5: every `app.add_handler(<Handler>, callback)`
    call in dispatch.py must reference a callback that is
    either imported at module level or defined at module
    level. The classic wave-trap is when `from handlers
    import (...)` is deleted but the MessageHandler
    registrations remain, causing NameError on every
    webhook.
    """
    tree = parse_source(DISPATCH_PY)
    acceptable = module_level_names(tree) | imported_names(tree)

    bad: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # Match `app.add_handler(<Handler>, <callback>)` or
        # `_dispatcher.add_handler(<Handler>, <callback>)`
        if not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "add_handler":
            continue
        if not isinstance(node.func.value, ast.Name):
            continue
        if node.func.value.id not in ("app", "_dispatcher", "application"):
            continue
        # The second positional arg is the callback. For
        # `MessageHandler(filters.X, handle_X)` it's wrapped
        # in a Call node, so callback is `node.args[1]` only
        # for direct add_handler(<callback>) — we need the
        # common case: add_handler(<Handler>(...filters..., cb))
        # The callback is the LAST positional arg of the
        # Handler(...) call. Find it.
        if len(node.args) == 2:
            # Direct: add_handler(filter, callback)
            cb = node.args[1]
            if isinstance(cb, ast.Name) and cb.id not in acceptable:
                bad.append((node.lineno, cb.id, "positional"))
        elif len(node.args) == 1:
            # Wrapped: add_handler(Handler(filters.X, callback))
            handler = node.args[0]
            if isinstance(handler, ast.Call) and handler.args:
                cb = handler.args[-1]
                if isinstance(cb, ast.Name) and cb.id not in acceptable:
                    bad.append((handler.lineno, cb.id, "wrapped"))
        # Also check keyword arg "callback" if present
        for kw in node.keywords:
            if kw.arg == "callback" and isinstance(kw.value, ast.Name):
                if kw.value.id not in acceptable:
                    bad.append((node.lineno, kw.value.id, "kwarg"))

    assert not bad, (
        f"dispatch.py has app.add_handler() calls whose "
        f"callback names are not imported at module level: "
        f"{bad}. This is the wave-6 327b7b0 regression class: "
        f"`from handlers import (...)` block was deleted but "
        f"the MessageHandler registrations remained, causing "
        f"NameError on every webhook. Either import the names "
        f"or remove the add_handler calls."
    )


def test_handlers_import_block_is_intact():
    """T60 #5: dispatch.py must have a `from handlers import`
    block at module level. The 327b7b0 fix restored this
    block; this test pins its presence so a future cleanup
    cannot silently delete it.
    """
    tree = parse_source(DISPATCH_PY)
    found = False
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "handlers":
            found = True
            break
    assert found, (
        "dispatch.py is missing a `from handlers import (...)` "
        "block at module level. The 327b7b0 fix restored this "
        "block; the wave-6 327b7b0 regression was caused by "
        "deleting it. If you are deliberately removing the "
        "dependency on handlers.py, you must also remove every "
        "app.add_handler(MessageHandler(filters.X, handle_X)) "
        "call in this file."
    )
