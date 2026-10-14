"""T59: AST tripwires + smoke test for dispatch.start().

Wave-6 (v16) caught #S40: acbd062 (T58 revert) deleted the
module-level is_dev_mode_chat helper but left the call site
in dispatch.start(). The bot crashed on every /start with
NameError. This test module exists so a future revert /
cleanup cannot leave the same kind of dead reference.

Tests:
  * test_start_does_not_reference_undefined_name — AST
    walk of dispatch.start() pins that every Name id is
    either a module-level def, a known import, a function
    parameter, a local variable, a for/with/except target,
    or a known builtin / stdlib symbol.
  * test_start_does_not_reference_dev_line — drift
    protection: dev mode is invisible by operator design
    (2026-10-14 11:19 MSK), so /start must not assemble a
    "dev_line" string.
  * test_start_does_not_reference_dev_mode_badge — drift
    protection: the loud red badge function was deleted
    in acbd062. dispatch must not call or define it.
  * test_dispatch_registers_only_imported_handlers — /start
    must not register a MessageHandler with a name that
    isn't imported from handlers.
  * test_start_invokable_without_nameerror — real smoke
    test: import dispatch, call start() with a fake
    update, verify no NameError.
"""
from __future__ import annotations

import ast
import pathlib


_SRC = (
    pathlib.Path(__file__).parent.parent
    / "telegram-bot"
    / "dispatch.py"
)

# Standard-library / known-builtin names that AST may pick up
# as Name() references. We subtract these from "undefined"
# when walking dispatch.start().
_KNOWN_BUILTINS = frozenset({
    "True", "False", "None", "self", "cls",
    "abs", "all", "any", "ascii", "bin", "bool", "bytearray",
    "bytes", "callable", "chr", "classmethod", "compile",
    "complex", "delattr", "dict", "dir", "divmod", "enumerate",
    "eval", "filter", "float", "format", "frozenset", "getattr",
    "globals", "hasattr", "hash", "hex", "id", "input", "int",
    "isinstance", "issubclass", "iter", "len", "list", "locals",
    "map", "max", "memoryview", "min", "next", "object", "oct",
    "open", "ord", "pow", "print", "property", "range", "repr",
    "reversed", "round", "set", "setattr", "slice", "sorted",
    "staticmethod", "str", "sum", "super", "tuple", "type",
    "vars", "zip", "__import__", "__name__", "__file__",
    "__doc__", "__all__", "__cached__", "__loader__",
    "__spec__", "__package__", "__builtins__", "Exception",
})


def _all_module_names(src: str) -> set[str]:
    """Return the set of all function / class / module-level
    assignment names defined at module top level in src.
    This is the universe of "what dispatch.start() can call
    without an import"."""
    tree = ast.parse(src)
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    names.add(tgt.id)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                names.add(node.target.id)
    return names


def _start_function(src: str) -> ast.AsyncFunctionDef:
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "start":
            return node
    raise AssertionError("dispatch.start() not found in source")


def _imported_names(tree: ast.Module) -> set[str]:
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                imported.add(alias.asname or alias.name)
    return imported


def _function_locals(func: ast.AST) -> set[str]:
    """Return the set of all names defined locally in func:
      * function parameters (ast.arg.arg)
      * assign / ann-assign / aug-assign targets
      * for-loop variables
      * with-as variables
      * except handler variables
      * comprehension targets (for x in ...)
    """
    locals_: set[str] = set()
    for node in ast.walk(func):
        if isinstance(node, ast.arg):
            locals_.add(node.arg)
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                _add_name_targets(tgt, locals_)
        elif isinstance(node, ast.AnnAssign):
            _add_name_targets(node.target, locals_)
        elif isinstance(node, ast.AugAssign):
            _add_name_targets(node.target, locals_)
        elif isinstance(node, ast.For):
            _add_name_targets(node.target, locals_)
        elif isinstance(node, ast.AsyncFor):
            _add_name_targets(node.target, locals_)
        elif isinstance(node, ast.With):
            for item in node.items:
                if item.optional_vars is not None:
                    _add_name_targets(item.optional_vars, locals_)
        elif isinstance(node, ast.AsyncWith):
            for item in node.items:
                if item.optional_vars is not None:
                    _add_name_targets(item.optional_vars, locals_)
        elif isinstance(node, ast.ExceptHandler):
            if node.name is not None:
                locals_.add(node.name)
        elif isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
            for gen in node.generators:
                _add_name_targets(gen.target, locals_)
        elif isinstance(node, ast.DictComp):
            for gen in node.generators:
                _add_name_targets(gen.target, locals_)
    return locals_


def _add_name_targets(node: ast.AST, out: set[str]) -> None:
    """Recursively add Name() ids from a target. Handles
    unpacking like `a, b = ...` and `a, *b = ...`."""
    if isinstance(node, ast.Name):
        out.add(node.id)
    elif isinstance(node, (ast.Tuple, ast.List)):
        for elt in node.elts:
            _add_name_targets(elt, out)
    elif isinstance(node, ast.Starred):
        _add_name_targets(node.value, out)


# === AST tripwires ===

def test_start_does_not_reference_undefined_name():
    """T59 #S40: dispatch.start() must not reference any name
    that is not defined at module level, not imported, not a
    function parameter, not a local variable, and not a
    known builtin.

    acbd062 regression: the is_dev_mode_chat call site was
    left in place but the function definition was deleted,
    producing a NameError on every /start. This test catches
    that class of regression.
    """
    src = _SRC.read_text()
    tree = ast.parse(src)
    module_names = _all_module_names(src)
    imported = _imported_names(tree)
    func = _start_function(src)
    locals_ = _function_locals(func)
    ref_names = {
        n.id
        for n in ast.walk(func)
        if isinstance(n, ast.Name)
    }
    unknown = ref_names - module_names - imported - locals_ - _KNOWN_BUILTINS
    # Drop known common patterns
    unknown -= {"_bc", "_is_group_chat", "chat"}
    # Drop any name explicitly matching the wave-6 #S40 dead
    # reference. The test FAILS if this name is in `unknown`.
    assert "is_dev_mode_chat" not in unknown, (
        "dispatch.start() references 'is_dev_mode_chat' but it is "
        "not defined at module level, not imported, not a function "
        "parameter, and not a local variable. acbd062 regression "
        "(T58 revert). Either remove the call or restore the helper."
    )
    # General check: any truly unknown name should trip
    assert not unknown, (
        f"dispatch.start() references names that are not defined "
        f"anywhere reachable: {sorted(unknown)}. This is the "
        f"wave-6 #S40 regression class. Either define the names, "
        f"add them to the import block, or update _KNOWN_BUILTINS "
        f"if they are stdlib."
    )


def test_start_does_not_reference_dev_line():
    """T59 drift protection: dev mode is invisible in the
    user-facing chat by operator design (2026-10-14 11:19 MSK).
    The dev_line variable was deleted in acbd062; this test
    pins the absence of any "dev_line" reference in start().
    """
    src = _SRC.read_text()
    assert "dev_line" not in src, (
        "dispatch.start() references 'dev_line' but dev mode is "
        "invisible in the user-facing chat. acbd062 (T58 revert) "
        "deleted dev_line. Restore only with explicit operator "
        "approval and a clear threat-model justification."
    )


def test_start_does_not_reference_dev_mode_badge():
    """T59 drift protection: the dev_mode_badge function was
    deleted in acbd062. dispatch must not call or define it.
    """
    src = _SRC.read_text()
    assert "dev_mode_badge" not in src, (
        "dispatch.py references 'dev_mode_badge' but the function "
        "was deleted in acbd062 (T58 revert). Operator feedback "
        "2026-10-14 11:19 MSK: dev mode = invisible in user-facing "
        "chat. No badge prefix on bot responses. If you need a "
        "badge, ask the operator first."
    )


def test_dispatch_registers_only_imported_handlers():
    """T59 drift protection: the lazy Application builder
    registers MessageHandlers for photo / voice / document / text.
    Each handler function must be imported at the top of
    dispatch.py. Otherwise PTB raises NameError when
    _safe_dispatch is called.
    """
    src = _SRC.read_text()
    tree = ast.parse(src)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "handlers":
            for alias in node.names:
                imported.add(alias.asname or alias.name)
    handler_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id.startswith("handle_"):
            handler_names.add(node.id)
    expected = {"handle_photo", "handle_voice", "handle_text", "handle_document"}
    missing = handler_names - imported
    assert not missing, (
        f"dispatch.py references handler names {sorted(missing)} "
        f"that are not imported from handlers.py. acbd062 / 327b7b0 "
        f"regression class. Either import the names or remove the "
        f"references."
    )
    missing_expected = expected - handler_names
    assert not missing_expected, (
        f"dispatch.py should register handlers for "
        f"{sorted(missing_expected)} but they are not referenced. "
        f"PTB will not call the handler."
    )


# === Real smoke test ===

def test_start_invokable_without_nameerror(monkeypatch, tmp_path):
    """T59 #S40 smoke test: call dispatch.start() with a fake
    Update + Context and verify it does not raise NameError.
    """
    monkeypatch.setenv("BOT_TOKEN", "TEST:TOKEN")
    monkeypatch.setenv("LLAMABOT_DEPLOYMENT_PROFILE", "personal")
    monkeypatch.setenv("AGENT_STACK_TOKEN", "test-token")
    monkeypatch.setenv("LLAMABOT_ALLOWED_USER_IDS", "286293081")

    class _FakeChat:
        id = 286293081
        type = "private"
        title = None
        username = None

    class _FakeUser:
        id = 286293081
        is_bot = False
        first_name = "V"
        last_name = None
        username = None

    class _FakeMessage:
        message_id = 1
        chat = _FakeChat()
        from_user = _FakeUser()
        text = "/start"
        async def get_bot(self): return None
        async def reply_text(self, *args, **kw): return None

    class _FakeUpdate:
        def __init__(self):
            self.effective_chat = _FakeChat()
            self.effective_user = _FakeUser()
            self.effective_message = _FakeMessage()
            self.update_id = 1
        async def get_bot(self): return None

    class _FakeContext:
        args = []
        bot_data = {}
        user_data = {}
        chat_data = {}

    import importlib
    dispatch_mod = importlib.import_module("dispatch")
    orig_reply = dispatch_mod._reply
    called_reply: list = []
    async def fake_reply(update, text):
        called_reply.append(text)
    dispatch_mod._reply = fake_reply
    try:
        import asyncio
        async def run():
            try:
                await dispatch_mod.start(_FakeUpdate(), _FakeContext())
                return "ok"
            except NameError as e:
                return f"NameError: {e}"
            except Exception as e:
                return f"other: {type(e).__name__}: {e}"
        outcome = asyncio.run(run())
        assert not outcome.startswith("NameError"), (
            f"dispatch.start() raised NameError: {outcome}. "
            f"This is the wave-6 #S40 regression class."
        )
        assert called_reply, (
            "dispatch.start() returned without error but did not "
            "call _reply. /start path may be silently skipping "
            "the response."
        )
    finally:
        dispatch_mod._reply = orig_reply
