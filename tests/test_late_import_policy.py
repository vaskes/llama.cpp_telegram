"""Wave 5: rules that were written broadly and checked narrowly.

Three separate waves shipped the same defect shape. Wave 4 finding #1:
the fix for the `global`-filter was a no-op, and the matrix that was
supposed to pin it encoded the live bug as correct. Wave 4 finding #3:
the `handlers.py` comment claimed a dependency structure that did not
exist, and was "fixed" by grepping for the one module that was visible.
Wave 5 C2: `_GLOBAL_MATRIX[3]` is `dispatch_polling.py` line for line,
with `must_be_flagged=False`.

So this file pins the difference between what a rule says and what a
check looks at. Each test is a claim, and every claim here has a
mutation that must turn it red.
"""
import ast
from pathlib import Path

BOT = Path(__file__).resolve().parent.parent / "telegram-bot"

# Which module's imports may NOT be resolved lazily inside a function.
#
# This is deliberately a dict, not a boolean. Wave 4 wrote the rule as
# "no late imports" and checked exactly one module; the gap between the
# two was invisible until wave 5 found `handlers.py` importing its own
# constant back through `config`. A list forces a second entry to be a
# deliberate decision rather than an omission.
NO_LATE_IMPORTS = {
    "handlers.py": ["dispatch", "bot"],
}


def _tree(name):
    return ast.parse((BOT / name).read_text(encoding="utf-8"))


def _late_imports(tree):
    """(module, lineno) for imports nested inside a function body."""
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.ImportFrom) and inner.module:
                out.append((inner.module, inner.lineno))
            elif isinstance(inner, ast.Import):
                for alias in inner.names:
                    out.append((alias.name, inner.lineno))
    return out


def test_no_module_has_a_late_import_of_a_banned_module():
    """The check matches the rule: every banned module, every function.

    Mutation to watch: add `handlers.py` to the dict with a module the
    code does not import. This stays green -- it only asserts that what
    is listed is absent. The tripwire is the list, and the list is the
    claim, so both have to be extended together.
    """
    offenders = []
    for name, banned in NO_LATE_IMPORTS.items():
        for module, lineno in _late_imports(_tree(name)):
            if module in banned:
                offenders.append(f"{name}:{lineno} imports {module}")
    assert not offenders, (
        f"late imports of banned modules: {offenders}. Module-level "
        "imports are the pattern; a late one re-creates the import "
        "cycle this split was meant to remove.")


def test_the_banned_list_is_not_a_single_entry_by_accident():
    """`handlers.py` had two, deliberately.

    A one-entry dict passes every assertion above while leaving the
    second rule unenforced. This is the shape of the wave-4 comment
    bug in miniature: the policy is what the reader assumes, the check
    is what exists.
    """
    for name, banned in NO_LATE_IMPORTS.items():
        assert len(banned) >= 2, (
            f"{name} bans only {banned}. Either the second rule was "
            "dropped by accident, or the dict should be a set of "
            "one-module policies and belong in two separate tests.")


def test_handlers_header_does_not_name_banned_modules_as_sources():
    """The header is documentation; make it checkable.

    The old header told the reader that `handlers.py` pulls cross-module
    state from `bot.py` by late import. There is no `from bot import` in
    the file. Wave 4 fixed a mid-file comment with the same claim and
    left this one, because the tripwire could not see comments.
    """
    src = (BOT / "handlers.py").read_text(encoding="utf-8")
    header = "\n".join(src.splitlines()[:40]).lower()
    # Matched on the assertive form, not on the module name: the header
    # is allowed -- and should be allowed -- to say "there is no
    # `from bot import` here", and a substring check on the name alone
    # would forbid exactly that. The old header said "is imported from
    # bot.py INSIDE each function", which is the shape being banned.
    claims = [m for m in NO_LATE_IMPORTS["handlers.py"]
              if f"imported from {m}" in header
              or f"imports from {m}" in header
              or f"from {m} inside each function" in header]
    assert not claims, (
        f"the handlers.py header still asserts {claims} as an import "
        "source. A comment describing a structure the code does not "
        "have reads as justification for building it.")


def test_handler_error_paths_are_logged_above_info():
    """M8: every real failure was at INFO, so `journalctl -p warning`
    was empty while the bot dropped work.

    Including C1, which is the reason this file exists in this shape: a
    TypeError on every update, logged at INFO, in the default transport.

    The rule is deliberately narrow -- a handler's error path must not
    report the failure at INFO. It is not a logging-style opinion; it
    is the difference between an operator seeing the outage and not.
    """
    offenders = []
    tree = _tree("handlers.py")
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            func = inner.func
            if not isinstance(func, ast.Attribute):
                continue
            if func.attr not in ("info", "debug"):
                continue
            # an error path that logs at INFO
            if func.attr == "info":
                src = ast.unparse(inner)
                if "[ERR" in src or "error" in src.lower():
                    offenders.append(f"line {inner.lineno}: {src}")
    assert not offenders, (
        "these error paths log at INFO: "
        f"{offenders}. A failure nobody can see with `-p warning` is "
        "indistinguishable from a bot that is working.")


def test_the_dispatch_polling_module_does_not_rebind_imported_names():
    """C2's class, as a property rather than as one instance.

    `global X` where X came from `from config import X` writes this
    module's binding and nobody else's. Every reader holding its own
    alias keeps the old object. In `dispatch_polling` that meant
    `llm_iteration`'s shutdown guard read a `None` forever.
    """
    for name in ("dispatch_polling.py", "dispatch.py", "webhook.py"):
        src = (BOT / name).read_text(encoding="utf-8")
        tree = ast.parse(src)
        imported = set()
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    imported.add(alias.asname or alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add((alias.asname or alias.name).split(".")[0])
        globals_declared = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Global):
                globals_declared.update(node.names)
        rebinds = imported & globals_declared
        assert not rebinds, (
            f"{name} rebinds imported name(s) {sorted(rebinds)} via "
            "`global`. That writes this module's binding only; every "
            "other holder of the name keeps the old object, and the "
            "reader that matters usually lives elsewhere.")


def test_the_global_matrix_has_a_declared_size_and_polarity_split():
    """H2: the commit message described eight cases, the code had seven.

    Nothing pinned the count, so the description and the artifact drifted
    while both looked fine. This test reads the count out of the matrix
    itself, so the commit message can be checked against it rather than
    trusted.
    """
    sys_path_tests = Path(__file__).resolve().parent
    matrix_mod = None
    for candidate in (sys_path_tests / "test_module_imports_resolve.py",):
        if candidate.exists():
            matrix_mod = candidate
            break
    assert matrix_mod is not None, "the matrix file moved; update this"

    ns = {"__file__": str(matrix_mod), "__name__": "matrix_probe"}
    exec(compile(matrix_mod.read_text(encoding="utf-8"),
                 str(matrix_mod), "exec"), ns)
    matrix = ns.get("_GLOBAL_MATRIX")
    assert matrix, "_GLOBAL_MATRIX is gone or renamed"
    flags = [bool(row[-1]) for row in matrix]
    from test_module_imports_resolve import EXPECTED_MATRIX_SIZE
    assert len(matrix) == EXPECTED_MATRIX_SIZE, (
        f"_GLOBAL_MATRIX has {len(matrix)} cases, expected "
        f"{EXPECTED_MATRIX_SIZE}. This used to be `>= 7`, which could not "
        "distinguish a lost case from an unchanged one -- and the commit "
        "that introduced the matrix described eight while shipping seven. "
        "If you added a case, raise EXPECTED_MATRIX_SIZE with it.")
    # A matrix of only positive cases is indistinguishable from a broken
    # checker -- every input red, no matter what the code is.
    assert flags.count(False) >= 3, (
        f"only {flags.count(False)} quiet cases in the matrix. Correct "
        "code must be able to stay quiet, or the checker is broken.")
    assert flags.count(True) >= 2, (
        f"only {flags.count(True)} cases that must be flagged; the "
        "checker is not being asked to catch much.")


def test_both_transports_subscribe_to_the_same_update_types():
    """M7: one declaration, two encodings, two consumers.

    `allowed_updates` was written twice -- a JSON string in
    `dispatch_polling._poll_loop`, a real list in
    `dispatch_webhook._webhook_auto_register_sync` -- with the same four
    types in a different order and a different wire format. Nothing
    compared them, so `callback_query` could vanish from one side
    unnoticed -- which is precisely how the Stop button stayed dead in
    production for years while a test said it worked.

    The check is on the SET, not on the text: the two transports must
    serialise differently because the Telegram API requires it, but they
    cannot disagree about which types they want.
    """
    import json as _json

    import config

    expected = {"message", "callback_query", "chat_member"}

    assert set(config.ALLOWED_UPDATES) == expected, (
        f"ALLOWED_UPDATES is {set(config.ALLOWED_UPDATES)}, expected "
        f"{expected}. Every update type the bot handles has to be here or "
        "the handler for it is dead in whichever transport omits it.")

    # The two serialisations must decode to the same set.
    assert set(_json.loads(config.ALLOWED_UPDATES_JSON)) == expected, (
        "ALLOWED_UPDATES_JSON does not round-trip to ALLOWED_UPDATES")
    assert set(config.ALLOWED_UPDATES_LIST) == expected, (
        "ALLOWED_UPDATES_LIST does not match ALLOWED_UPDATES")

    # And neither transport may carry a literal list of its own again.
    for name, const in (("dispatch_polling.py", "ALLOWED_UPDATES_JSON"),
                        ("dispatch_webhook.py", "ALLOWED_UPDATES_LIST")):
        src = (BOT / name).read_text(encoding="utf-8")
        assert f'"allowed_updates": {const}' in src, (
            f"{name} no longer sends {const} for allowed_updates. If the "
            "subscription really has to differ per transport, say so "
            "here -- otherwise this is a second declaration by another "
            "route.")
        # Scoped to the subscription field, not the whole file: the
        # loop legitimately reads `upd_dict.get("message")` at runtime
        # and the comments discuss update types at length, so a
        # file-wide literal scan reports a mention as a declaration.
        # What must not come back is a literal list bound to the
        # `allowed_updates` key.
        tree = ast.parse(src)
        found_key = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            for key, value in zip(node.keys, node.values):
                if not (isinstance(key, ast.Constant)
                        and key.value == "allowed_updates"):
                    continue
                found_key = True
                ok = isinstance(value, ast.Name) and value.id == const
                assert ok, (
                    f"{name} binds something other than {const} to "
                    f"allowed_updates: {ast.unparse(value)}. Two literal "
                    "lists in two transports is exactly how they drifted.")
        assert found_key, (
            f"{name} sends no allowed_updates field at all. That is a "
            "behaviour change, not a refactor, and it stays invisible "
            "until a handler stops firing.")
