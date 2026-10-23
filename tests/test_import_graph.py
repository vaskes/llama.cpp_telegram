"""The import graph is verified by an algorithm, not by grep.

Review round 3 asked for this three times in three forms: grep does not prove
acyclicity, `from . import bot` is invisible to it, and a test that only
catches the shape somebody already wrote is theatre.

Each test below is paired with a mutation check, so "it passes" means the
detector fires, not that the file happens to be clean.
"""
import ast
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import import_graph as ig  # noqa: E402

BOT_DIR = ig.BOT_DIR


# --- the real check --------------------------------------------------------

def test_module_level_import_graph_has_no_cycles():
    """Tarjan SCC over every module-level import edge in telegram-bot/.

    An SCC of size > 1 is a cycle. A size-1 SCC with a self-loop is also a
    cycle and is reported separately, because a module importing itself is
    usually a typo that only shows up under a particular import order.
    """
    cycles = ig.cycles()
    assert not cycles, (
        "module-level import cycles:\n"
        + "\n".join(f"  {ig.describe(c)}" for c in cycles)
        + "\nPut the shared helper in a leaf module (chat_utils, access) "
          "rather than letting two modules reach for each other."
    )


def test_no_module_imports_itself():
    edges, _ = ig.build_graph()
    self_loops = {m for m, dsts in edges.items() if m in dsts}
    assert not self_loops, f"modules importing themselves: {sorted(self_loops)}"


def test_bot_is_imported_by_nothing_at_module_level():
    """bot.py is a script. Nothing in telegram-bot/ may import it."""
    edges, _ = ig.build_graph()
    importers = sorted(src for src, dsts in edges.items() if "bot" in dsts)
    assert not importers, (
        f"module-level import of bot from: {importers}. bot.py is the entry "
        f"point (`python -u bot.py`), not a library. Import the owner."
    )


def test_topological_layering_is_complete():
    """Sanity check on the layering itself.

    If the layering cannot consume every module, the graph has a cycle the
    SCC pass missed -- two detectors disagreeing is a stronger signal than
    either alone.
    """
    layers = ig.topological_layers()
    seen = {m for layer in layers for m in layer}
    missing = ig.module_names() - seen
    assert not missing, (
        f"modules absent from the topological layering: {sorted(missing)}. "
        f"Either they are in a cycle, or the layering is wrong."
    )


def test_leaf_modules_stay_leaves():
    """chat_utils and access exist to be leaves. If they gain edges upward,
    the refactor that created them has been undone."""
    edges, _ = ig.build_graph()
    for leaf in ("chat_utils", "access", "turn"):
        up = sorted(d for d in edges.get(leaf, ())
                    if d not in ("storage", "state", "config", "prompts",
                                 "limits", "chat_utils", "access",
                                 # TECH-DEBT #3: every module takes its
                                 # logger from here, so all of them have
                                 # this edge. It is downward-only -- logconf
                                 # imports nothing from the package, which
                                 # test_logging_stdout_parity.py pins.
                                 "logconf"))
        assert not up, (
            f"{leaf}.py is supposed to be a leaf but imports {up}. Every "
            f"edge added here is one more late import waiting to happen."
        )


# --- detector verification (the tests above are only as good as the code) ---

def _graph_from(tmp_path, files):
    for name, body in files.items():
        (tmp_path / f"{name}.py").write_text(textwrap.dedent(body))
    return ig.tarjan_scc(
        {p.stem for p in tmp_path.glob("*.py")},
        ig.build_graph(tmp_path)[0],
    )


def test_detector_finds_a_two_module_cycle(tmp_path):
    comps = _graph_from(tmp_path, {
        "a": "import b\n",
        "b": "import a\n",
    })
    assert any(len(c) == 2 for c in comps), (
        "the detector missed a -> b -> a. Every other test in this file is "
        "then reporting a clean bill of health for a broken tool."
    )


def test_detector_finds_a_three_module_cycle(tmp_path):
    comps = _graph_from(tmp_path, {
        "a": "import b\n",
        "b": "import c\n",
        "c": "import a\n",
    })
    assert any(len(c) == 3 for c in comps), "missed a -> b -> c -> a"


def test_detector_finds_two_independent_cycles(tmp_path):
    """A greedy DFS that stops at the first back edge reports one cycle and
    exits. The whole point of SCC is that it reports all of them, because
    fixing one and re-running must not reveal the next one."""
    comps = _graph_from(tmp_path, {
        "a": "import b\n", "b": "import a\n",
        "c": "import d\n", "d": "import c\n",
        "e": "import a\n",
    })
    two_cycles = [c for c in comps if len(c) == 2]
    assert len(two_cycles) == 2, (
        f"expected two independent cycles, found {two_cycles}"
    )


def test_detector_finds_a_deep_cycle(tmp_path):
    """Guards the iterative Tarjan: a 60-module cycle must not blow the
    recursion limit the recursive version would have hit."""
    n = 60
    files = {f"m{i}": f"import m{(i + 1) % n}\n" for i in range(n)}
    comps = _graph_from(tmp_path, files)
    assert any(len(c) == n for c in comps), "missed a 60-module cycle"


def test_detector_sees_relative_imports(tmp_path):
    """The gap that motivated this rewrite: `from . import bot` is an edge,
    and neither grep nor a naive ast.walk finds it.

    bot imports helper, helper reaches back with a RELATIVE import. Written
    with `import bot` the cycle is obvious; written with `from . import bot`
    it is invisible to every text-level check.
    """
    edges, _ = ig.build_graph(tmp_path)
    _graph_from(tmp_path, {
        "bot": "from . import helper\n",
        "helper": "from . import bot\n",
    })
    edges, _ = ig.build_graph(tmp_path)
    assert "bot" in edges.get("helper", set()), (
        f"relative import edge was missed; graph is {dict(edges)}"
    )
    assert "helper" in edges.get("bot", set()), (
        f"relative import edge was missed; graph is {dict(edges)}"
    )


def test_detector_ignores_external_packages(tmp_path):
    comps = _graph_from(tmp_path, {
        "a": "import os\nimport httpx\nfrom telegram import Update\n",
    })
    assert all(len(c) == 1 for c in comps), "external packages treated as local"


def test_dynamic_imports_are_surfaced_not_ignored(tmp_path):
    """__import__("x") cannot be resolved statically. The honest move is to
    report it so the blind spot is visible, not to pretend it is covered."""
    (tmp_path / "handlers.py").write_text(
        'x = __import__("storage")\n'
    )
    (tmp_path / "storage.py").write_text("")
    _, dynamic = ig.build_graph(tmp_path)
    assert dynamic.get("handlers"), (
        "dynamic imports were silently dropped; the tool now claims to see "
        "edges it cannot see"
    )


def test_no_dunder_import_anywhere():
    """__import__ is not lazy loading, it is a blind spot.

    There were ten of them. `__import__("os")` where os is stdlib was not
    avoiding anything -- it was left over from a cycle fixed elsewhere and
    never cleaned up here. The cost was concrete rather than theoretical:
    the import graph above cannot see a string edge, so `test_dynamic_imports_
    are_surfaced_not_ignored` existed to paper over a hole we were choosing
    to keep.

    If a genuine dynamic import is ever needed, it should go through
    importlib.import_module with the name declared somewhere the graph
    checker can see.
    """
    offenders = []
    for path in BOT_DIR.rglob("*.py"):
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), 1
        ):
            if line.lstrip().startswith("#"):
                continue
            if "__import__(" in line:
                offenders.append(f"{path.relative_to(BOT_DIR)}:{lineno}")
    assert not offenders, (
        f"__import__ reintroduced at {offenders}. A string import is "
        f"invisible to the dependency graph, so the edge cannot be checked "
        f"for cycles. Use importlib.import_module and declare the target."
    )


def _late_edges() -> set:
    """(importer, imported) pairs resolved from inside a function body."""
    out: set = set()
    known = ig.module_names()
    for path in ig.BOT_DIR.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for sub in ast.walk(node):
                targets = []
                if isinstance(sub, ast.Import):
                    targets = [a.name for a in sub.names]
                elif isinstance(sub, ast.ImportFrom) and sub.level == 0 \
                        and sub.module:
                    targets = [sub.module]
                for t in targets:
                    top = t.split(".")[0]
                    if top in known:
                        out.add((path.stem, top))
    return out


def test_every_local_module_is_referenced_somewhere():
    """No orphaned module.

    The first version required a MODULE-LEVEL edge and reported summarize.py
    as unreachable. It is imported lazily from persistence.py. That import
    exists so a bot running CONTEXT_STRATEGY=window does not pull in the LLM
    summarisation path at startup, which is the right reason.

    The second version had the opposite bug: it added the late edges'
    IMPORTERS to the reachable set but not their TARGETS, so summarize stayed
    unreachable while persistence -- which references it -- was counted.

    A module referenced by neither kind of edge is genuinely orphaned.
    """
    edges, _ = ig.build_graph()
    late = _late_edges()

    referenced = set(edges)
    for dsts in edges.values():
        referenced.update(dsts)
    for src, dst in late:
        referenced.add(src)
        referenced.add(dst)

    orphans = sorted(ig.module_names() - referenced)
    assert not orphans, (
        f"modules no import references at all: {orphans}. Dead code, or a "
        f"file nobody remembered shipping."
    )


# Measured, not imagined. Two strongly connected components exist once the
# nineteen late imports are counted alongside the module-level edges. They are
# real runtime cycles that the module-level check cannot see, because each
# edge is legal precisely because it was deferred.
#
#   config <-> state
#   access -> bot -> call_llama -> dispatch -> handlers -> rating -> bot
#          (with selftest and webhook hanging off the same component)
#
# Not fixed here. Breaking them means re-deriving what state.py and config.py
# actually own, which is TECH-DEBT and not a late-night edit. What this test
# buys is that the list cannot grow: a new lazy import that closes a new
# cycle fails CI, and an existing one cannot be quietly forgotten.
#
# The first version of this asserted the list was empty, which is the same
# mistake as asserting a cycle finder had no cycles -- the test that cannot
# fail is not a test.
KNOWN_RUNTIME_CYCLES = [
    # Was seven modules, is now two.
    #
    # Dropped by TECH-DEBT #1:
    #   bot    -- selftest.py reached its dependencies through the bot
    #            facade. The only edge was six monkeypatches
    #            (`_b._route_to_thread`, `_b._check_user_slot`,
    #            `_b._apply_rating_and_persist`) that wrote attributes
    #            nobody read. The cycle was load-bearing for nothing,
    #            which is exactly why it also hid six broken tests.
    #   rating -- its only back-edge was a lazy `from bot import
    #            _is_group_chat` inside _is_rating_active(). That name
    #            is owned by chat_utils.py, which imports nothing, so
    #            the import is direct now and the edge is gone.
    #
    # Dropped by TECH-DEBT #8 block 3, which is what made them visible:
    #   llm_thinking -- its ONLY back-edge was a lazy `from dispatch
    #     import _stop_button_markup` inside push_thinking(). The
    #     comment there said dispatch owned the function. It did not:
    #     the function lived in access.py and was reached through a
    #     re-export. Moving it to its own leaf (llm_markup) closed the
    #     edge for real. That comment was an unverified claim sitting
    #     next to the code it misdescribed.
    #   call_llama, dispatch, selftest, webhook -- all four were only
    #     reachable *from* llm_thinking through that one import.
    #
    # The honest reading of the failure that prompted this was not "one
    # more module became cyclic". llm_stream inherited a cycle that was
    # already there, and adding it to this list would have been a green
    # tick for a regression.
    ["access", "handlers"],
    # `["config", "state"]` was here for the whole time and TECH-DEBT #2
    # is listed closed. `state._get_user_semaphore` imported
    # `_PER_USER_SEMAPHORE_LIMIT` from `config`, which re-exports it
    # from `state` -- a constant `state.py` defines itself, reached by a
    # round trip. Removed in wave 5; the edge was legal only because it
    # was deferred, and the item said the cycle was gone.
    # Was hidden inside the seven-module component above. `dispatch`
    # imports `webhook` at /main, `webhook` imports `dispatch` in its
    # request handler, and selftest imports dispatch to drive updates.
    # It was always a cycle; call_llama just used to sit in the same
    # component, so the pair was never named on its own.
    #
    # T1.1 added `selftest_state` to this component, and the honest
    # reading is the one llm_stream got: this is not a new cycle, it is
    # an existing one inherited by a module that was cut out of the
    # middle of it. `check_chat_member` calls
    # `_handle_chat_member_update`, which dispatch owns, so the leaf
    # needs dispatch and the lazy import is what keeps the module-level
    # check green.
    #
    # Naming it here without saying that would be a green tick for a
    # regression, which is what this list exists to prevent. The real
    # fix is to move `_handle_chat_member_update` to a leaf the way
    # `_stop_button_markup` moved to `llm_markup` under item 8, and that
    # is item 10's work, not this slice's.
    #
    # T1.1 added `selftest_infra` to the same component on the same
    # grounds: its blocks patch `handlers` module attributes by name,
    # which is where `handle_text` resolves every one of them, and
    # handlers is inside the access/handlers pair that dispatch drives.
    # Nothing about the component changed -- a leaf that calls into the
    # dispatcher inherits the dispatcher.
    ["dispatch", "selftest", "selftest_infra", "selftest_state", "webhook"],
]


def test_late_imports_do_not_add_new_runtime_cycles():
    """A lazy import is fine. A lazy import that closes a cycle is not.

    Module-level cycles are already forbidden above. This catches the subtler
    shape: an edge that is legal precisely because it was deferred, so it
    escapes the module-level check while two modules still need each other at
    runtime. "An import deferred to avoid a cycle has not avoided it."

    The baseline is the measured set, so this fails on a NEW cycle and on any
    change to an existing one -- it does not pretend the two current cycles
    are fine, it says they are known and priced.
    """
    edges, _ = ig.build_graph()
    late = _late_edges()

    combined = {k: set(v) for k, v in edges.items()}
    for src, dst in late:
        combined.setdefault(src, set()).add(dst)

    from import_graph import tarjan_scc
    found = sorted(sorted(c) for c in tarjan_scc(ig.module_names(), combined)
                   if len(c) > 1)

    assert found == KNOWN_RUNTIME_CYCLES, (
        "the set of runtime import cycles changed.\n"
        f"  now:  {found}\n"
        f"  base: {KNOWN_RUNTIME_CYCLES}\n"
        "A new component is a new cycle. A missing one means an edge was "
        "removed -- if that was deliberate, update this list in the same "
        "commit with a line saying why."
    )
