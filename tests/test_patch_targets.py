"""Every mock.patch target must name a symbol its target module really has.

Found after the third review round, over a class of bug that has now bitten
three separate times in this repo:

  - tests patched `dispatch.is_authorized`; the function had moved to `access`
  - selftest patched `bot._register_abort_event`; handlers imports it from
    `state`
  - selftest patched `bot.httpx.AsyncClient`

The third one is the instructive case, and it cuts the other way from the
other two. Patching `<mod>.httpx.X` mutates the single global httpx module
object, so it *worked* even though bot.py never called httpx -- and it broke
the moment bot.py stopped importing httpx for no reason of its own. The import
was load-bearing purely for the test to resolve.

So the failure is not always "the test asserted nothing". It is that the patch
target and the patched code are coupled through a path nobody wrote down, and
the coupling only holds while some unrelated line keeps existing.

Worth recording that the first version of THIS tripwire was itself the bug it
exists to catch: it matched `mock.patch(...)` by attribute access and missed
the bare `patch(...)` form that selftest.py actually uses, so it reported
three passed while a stale target sat in the suite. Verified by injection, not
by reading.
"""
import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BOT_DIR = REPO_ROOT / "telegram-bot"
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

TESTS = REPO_ROOT / "tests"
LOCAL_MODULES = {p.stem for p in BOT_DIR.glob("*.py")}

PATCH_NAMES = {"patch", "patch.object", "setattr", "setattr_"}


def _is_patch_call(node):
    """True for mock.patch(...), bare patch(...), patch.object(...)."""
    fn = node.func
    if isinstance(fn, ast.Name):
        return fn.id in PATCH_NAMES
    if isinstance(fn, ast.Attribute):
        return fn.attr in PATCH_NAMES
    return False


# W6-H3: filled by patch_targets() when a file will not parse. Deliberately
# module-level and deliberately never cleared -- a scan that silently drops
# unreadable files is the failure mode this replaces.
UNPARSEABLE: list = []


def patch_targets(path):
    """Yield (module, attribute, lineno) for every string patch target.

    Covers both `patch("mod.attr")` / `monkeypatch.setattr("mod.attr", ...)`
    and `patch.object(mod, "attr")` where the module is given by name.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError as exc:
        # W6-H3. One unparseable file used to take the entire audit down
        # before it checked anything. Degrade to a reported skip -- the
        # caller surfaces which file was unreadable and the rest of the
        # scan still runs.
        UNPARSEABLE.append((path.name, exc.lineno, exc.msg))
        return
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _is_patch_call(node):
            continue

        strings = [a.value for a in node.args
                   if isinstance(a, ast.Constant) and isinstance(a.value, str)]

        # patch("mod.attr") / setattr("mod.attr", ...)
        dotted = [s for s in strings if s.count(".") >= 1
                  and s.split(".")[0] in LOCAL_MODULES
                  and not s.startswith("/")]
        for s in dotted:
            module, _, attr = s.rpartition(".")
            yield module, attr, node.lineno

        # patch.object(mod, "attr") where mod is a bare Name
        fn = node.func
        if isinstance(fn, ast.Attribute) and fn.attr == "object" and node.args:
            first = node.args[0]
            if isinstance(first, ast.Name) and first.id in LOCAL_MODULES:
                attr = next((s for s in strings if "." not in s), None)
                if attr:
                    yield first.id, attr, node.lineno


def _collect():
    rows = []
    for test_file in sorted(TESTS.glob("*.py")):
        if test_file.name == Path(__file__).name:
            continue
        for module, attr, lineno in patch_targets(test_file):
            rows.append((test_file, module, attr, lineno))
    return rows


def test_no_test_file_was_skipped_as_unparseable():
    """W2-H3. UNPARSEABLE exists so one unreadable file does not take the
    whole audit down before it checks anything. But the list was never
    asserted anywhere, so the scan silently swallowed files -- reproducing
    the exact failure mode it was added to replace.

    Injected by a reviewer: a file that does not parse AND carries a stale
    `dispatch.totally_missing_symbol` target. UNPARSEABLE filled up, the
    audit stayed green, one test file went entirely unaudited.

    This runs first in collection order and fails loudly, naming the files.
    """
    # Scan first. Asserting before anything has populated the list passes
    # vacuously -- which is how this guard looked like it worked while
    # catching a colleague's identical injection on the *next* test down.
    _collect()
    assert not UNPARSEABLE, (
        f"these test files could not be parsed and were NOT audited: "
        f"{UNPARSEABLE}. A patch target in any of them is unchecked -- "
        f"which is the failure this guard was added to prevent."
    )


def test_unparseable_does_not_accumulate_between_runs():
    """UNPARSEABLE is module-level and never cleared, so once anything lands
    in it every later run in the same process stays red even after the file
    is fixed. It must hold exactly the current scan's findings."""
    before = len(UNPARSEABLE)
    _collect()
    assert len(UNPARSEABLE) == before, (
        f"a clean scan added {len(UNPARSEABLE) - before} entries; the list is "
        f"meant to describe this run, not every run that ever ran"
    )


def test_no_patch_target_names_a_missing_symbol():
    """The load-time contract: every patch target must resolve.

    mock.patch raises AttributeError on a module that lacks the attribute, so
    a stale target is a loud failure -- but only in whichever test happens to
    run it first, which is the worst place to find out.
    """
    import importlib

    problems = []
    for test_file, module, attr, lineno in _collect():
        if module not in LOCAL_MODULES:
            continue
        mod = importlib.import_module(module)
        if not hasattr(mod, attr):
            problems.append(
                f"{test_file.name}:{lineno} patches "
                f"{module}.{attr}, which does not exist"
            )
    assert not problems, (
        "patch targets naming symbols that are gone:\n  "
        + "\n  ".join(problems)
        + "\nThese pass silently if the symbol moved, because the patch lands "
        "on nothing. Point them at the module that owns the symbol."
    )


def test_bot_is_not_used_as_a_patch_target_for_moved_symbols():
    """bot.py is a script, not a library -- nothing in telegram-bot/ imports it.

    Its only consumers are selftest.py and this suite. So `patch("bot.X")`
    works by accident, through whatever re-export happens to be present, and
    breaks when that re-export goes away. Which is the normal direction of
    travel for this repo: every refactor moves symbols out of the facade.
    """
    rows = [r for r in _collect() if r[1] == "bot"]
    assert not rows, (
        "tests patch through the bot facade:\n  "
        + "\n  ".join(f"{f.name}:{ln}: bot.{a}" for f, _, a, ln in rows)
        + "\nPatch the owning module instead."
    )


# A third test here asserted "patch the module that DEFINES the symbol".
# It was inverted, and verifying the inversion cost a full round trip:
#
#   monkeypatch.setattr("handlers.get_store", ...)   <- CORRECT
#   monkeypatch.setattr("storage.get_store", ...)   <- DOES NOT WORK
#
# handlers does `from storage import get_store`, so it resolves the name in
# its own globals. Patching the owning module leaves handlers untouched. The
# rule is the opposite of the one that test encoded: patch the module that
# USES the symbol. It flagged the only correct patch in the file and would
# have blessed the one that cannot work.
