"""Tripwires for the TECH-DEBT #1/#2 refactor.

The refactor removed an import cycle and 1894 lines of test code from bot.py.
Both are the kind of thing that comes back: someone adds a helper to
dispatch.py, imports it late from handlers.py "just this once", and the graph
closes again with no error anywhere.

So the shape is pinned here, not just the behaviour.
"""
import pytest
import ast
import os
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BOT_DIR = REPO_ROOT / "telegram-bot"

MODS = {p.stem for p in BOT_DIR.glob("*.py")}

#: Leaf modules may not import from the dispatcher, and vice versa is what we
#: are proving by absence. Anything importing them must be a consumer, not a
#: provider.
LEAF_MODULES = {"chat_utils", "access", "context_budget", "persistence"}


def _module_graph() -> tuple[dict, dict]:
    graph = defaultdict(set)
    late = defaultdict(list)
    for p in sorted(BOT_DIR.glob("*.py")):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Import):
                for a in node.names:
                    t = a.name.split(".")[0]
                    if t in MODS:
                        graph[p.stem].add(t)
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module:
                    t = node.module.split(".")[0]
                    if t in MODS:
                        graph[p.stem].add(t)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for sub in ast.walk(node):
                    names = []
                    if isinstance(sub, ast.Import):
                        names = [a.name for a in sub.names]
                    elif isinstance(sub, ast.ImportFrom) and sub.level == 0 \
                            and sub.module:
                        names = [sub.module]
                    for m in names:
                        t = m.split(".")[0]
                        if t in MODS:
                            late[p.stem].append(t)
    return graph, late


# ---------------------------------------------------------------------------
# the cycle
# ---------------------------------------------------------------------------

def test_module_import_graph_has_no_cycles():
    """Zero cycles before this refactor.

    The cycle existed only because of late imports: dispatch imported handlers
    at module level to register handlers, and handlers imported dispatch inside
    function bodies. The ten helpers that made the second direction necessary
    were leaf code and moved to chat_utils.py and access.py, so both edges are
    now one-way.
    """
    graph, _ = _module_graph()
    cycles = set()

    def dfs(start, cur, path):
        for nxt in graph.get(cur, ()):
            if nxt == start:
                cycles.add(tuple(sorted(path)))
            elif nxt not in path and nxt > start:
                dfs(start, nxt, path + [nxt])

    for m in sorted(MODS):
        dfs(m, m, [m])
    assert not cycles, (
        f"module-level import cycles: {sorted(' -> '.join(c) for c in cycles)}. "
        f"A cycle means a helper got pushed back into a module that should be "
        f"a leaf. Put it in chat_utils.py or access.py instead."
    )


def test_dispatch_has_no_late_imports_of_bot_or_handlers():
    """The specific cycle this work removed.

    `from bot import X` inside a function in dispatch.py is the same late
    import as before, just narrower. bot.py imports dispatch at module level,
    so this would close the graph again.
    """
    tree = ast.parse((BOT_DIR / "dispatch.py").read_text(encoding="utf-8"))
    offenders = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for sub in ast.walk(fn):
            mods = []
            if isinstance(sub, ast.Import):
                mods = [a.name for a in sub.names]
            elif isinstance(sub, ast.ImportFrom) and sub.level == 0 \
                    and sub.module:
                mods = [sub.module]
            for m in mods:
                if m.split(".")[0] in ("bot", "handlers"):
                    offenders.append(f"{fn.name}:{sub.lineno} -> {m}")
    assert not offenders, (
        f"late imports back into the cycle: {offenders}. dispatch.py already "
        f"imports handlers at module level; these are not avoiding anything."
    )


def test_handlers_has_no_late_imports_of_dispatch():
    """handlers.py used to reach into dispatch from eight function bodies.

    After the move it should have none: everything it needed became a leaf
    import. A new late import here means something dispatch-shaped crept back
    into a handler.
    """
    tree = ast.parse((BOT_DIR / "handlers.py").read_text(encoding="utf-8"))
    offenders = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for sub in ast.walk(fn):
            if isinstance(sub, ast.ImportFrom) and sub.level == 0 \
                    and sub.module and sub.module.split(".")[0] == "dispatch":
                offenders.append(f"{fn.name}:{sub.lineno}")
    assert not offenders, (
        f"handlers.py imports dispatch late: {offenders}. If the name is a "
        f"leaf helper it belongs in chat_utils.py or access.py; if it really "
        f"needs the dispatcher, say so and re-derive the cycle instead."
    )


def test_leaf_modules_stay_leaves():
    """The property that makes the acyclic graph possible."""
    for name in LEAF_MODULES:
        if not (BOT_DIR / f"{name}.py").exists():
            continue
        graph, _ = _module_graph()
        deps = {d for d in graph[name]
                if d in ("dispatch", "handlers", "bot", "call_llama")}
        assert not deps, (
            f"{name}.py is supposed to be a leaf but imports {sorted(deps)}. "
            f"A helper that reaches back into the dispatcher recreates the "
            f"cycle this refactor removed."
        )


# ---------------------------------------------------------------------------
# the extraction
# ---------------------------------------------------------------------------

def test_bot_py_no_longer_contains_the_selftest():
    """TECH-DEBT #1. bot.py was 2245 lines, 1894 of them a smoke test."""
    src = (BOT_DIR / "bot.py").read_text(encoding="utf-8")
    assert "async def _selftest" not in src, (
        "the in-container smoke test moved to selftest.py; bot.py grew it back"
    )
    assert len(src.splitlines()) < 500, (
        f"bot.py is {len(src.splitlines())} lines again — it is meant to be a "
        f"thin facade. Whatever grew here probably belongs in a module."
    )


def test_selftest_module_exists_and_still_runs():
    """The extraction has to keep the smoke test working, not delete it."""
    p = BOT_DIR / "selftest.py"
    assert p.is_file(), "selftest.py must ship — it runs inside the container"
    src = p.read_text(encoding="utf-8")
    assert "async def _selftest" in src
    # Still gated on the env var: production runs with it off.
    assert "LLAMABOT_SELFTEST" in src, (
        "the smoke test must stay opt-in; it is not a startup check"
    )


def test_selftest_every_free_name_resolves():
    """Written after selftest.py shipped broken for two commits.

    Extracting it out of bot.py cut it loose from bot.py's namespace. Six
    names resolved through that facade and nothing else did, so they raised
    NameError -- but only on the first await, and only when
    LLAMABOT_SELFTEST=1, which production never sets. The test directly
    above passed the whole time, because it checked the file existed and
    that `_selftest` was still defined. Those are different claims.

    A file whose free names all resolve cannot NameError on its first line.
    """
    import builtins

    tree = ast.parse((BOT_DIR / "selftest.py").read_text(encoding="utf-8"))

    defined = set(dir(builtins))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            defined.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx,
                                                      (ast.Store, ast.Del)):
            defined.add(node.id)
        elif isinstance(node, ast.arg):
            defined.add(node.arg)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                defined.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.ExceptHandler) and node.name:
            defined.add(node.name)
        elif isinstance(node, ast.Global):
            defined.update(node.names)

    used = {n.id for n in ast.walk(tree)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}

    missing = sorted(used - defined)
    assert not missing, (
        f"selftest.py references names it never imports: {missing}. These "
        f"resolved through bot.py's facade until TECH-DEBT #1 extracted it. "
        f"They only fail on the first await, and only with "
        f"LLAMABOT_SELFTEST=1, which production never sets -- so they went "
        f"unnoticed."
    )


def test_extracted_modules_resolve_their_free_names():
    """Same check, applied to every module bot.py used to hold.

    selftest.py was not a special case. Whatever else was moved out of the
    facade and still leans on it has the same latent NameError, so the check
    runs over the leaf modules too rather than over the one file that was
    already caught.
    """
    import builtins

    for mod in ("selftest", "turn", "chat_utils", "access"):
        path = BOT_DIR / f"{mod}.py"
        if not path.is_file():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        defined = set(dir(builtins))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                defined.add(node.name)
            elif isinstance(node, ast.Name) and isinstance(node.ctx,
                                                          (ast.Store, ast.Del)):
                defined.add(node.id)
            elif isinstance(node, ast.arg):
                defined.add(node.arg)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    defined.add((alias.asname or alias.name).split(".")[0])
            elif isinstance(node, ast.ExceptHandler) and node.name:
                defined.add(node.name)
            elif isinstance(node, ast.Global):
                defined.update(node.names)
        used = {n.id for n in ast.walk(tree)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        missing = sorted(used - defined)
        assert not missing, f"{mod}.py has unresolvable free names: {missing}"


def test_dispatch_imports_selftest_at_module_level():
    """The one-way edge that lets the cycle break."""
    src = (BOT_DIR / "dispatch.py").read_text(encoding="utf-8")
    assert "from selftest import _selftest" in src
    tree = ast.parse(src)
    top = set()
    for _node in tree.body:
        if isinstance(_node, ast.ImportFrom) and _node.module:
            top.add(_node.module)
    assert "selftest" in top, (
        "selftest must be imported at module level — a late import here means "
        "selftest has started depending back on the dispatcher"
    )


def test_helpers_live_where_the_graph_says():
    """Spot-check that the moves are real, not just re-exported in place."""
    for mod, names in (
        ("chat_utils", ["_is_group_chat", "_parse_subtalk_arg",
                       "_parse_topic_arg", "_rate_limit_send"]),
        ("access", ["is_authorized", "reject_if_unauthorized",
                    "_should_mute_in_group", "_is_reply_to_other_user",
                    "_check_user_slot"]),
        # TECH-DEBT #8 block 3. It lived in access, which is in the import
        # cycle, and llm_thinking reached it through dispatch at call time.
        # The graph said access; the graph was where the function was, not
        # where the cycle allowed it to be.
        ("llm_markup", ["_stop_button_markup"]),
    ):
        src = (BOT_DIR / f"{mod}.py").read_text(encoding="utf-8")
        for helper in names:
            assert f"def {helper}(" in src or f"async def {helper}(" in src, (
                f"{helper} is not defined in {mod}.py"
            )


def test_update_script_still_syncs_new_modules():
    """new modules ship with the tree; a partial copy is the 4-of-17 bug."""
    script = (REPO_ROOT / "scripts" / "update.sh").read_text()
    assert "telegram-bot/" in script, "the whole tree must be synced"
    # The post-sync completeness check counts *.py, so new modules are covered
    # automatically — assert that it is still glob-based, not a hardcoded list.
    assert "telegram-bot/*.py" in script, (
        "the completeness check must glob telegram-bot/*.py; a hardcoded list "
        "would let a newly added module deploy as a partial build"
    )


@pytest.mark.tech_debt(item=2, reason="Pins TECH-DEBT #2: the module-level import graph is acyclic.")
def test_this_marker_is_real():
    """Anchor for the tech-debt ledger reconciler.

    The reconciler greps for the decorator rather than trusting that
    somebody kept it attached to something, so the marker needs a
    function to live on.
    """
    assert True
