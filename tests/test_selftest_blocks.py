"""Each extracted selftest block is callable on its own.

The golden trace proves the blocks run *in sequence, inside `_selftest`*.
It cannot tell whether any one of them is still a function you can call
by name -- and that is the property this whole extraction is for. A
block inlined back into a 1789-line function, or renamed, or turned
into a closure, would leave the golden completely green.

So each block is driven directly here. Three things are checked per
block, and each is a failure mode that the golden misses:

  * it is a module-level coroutine function, so it is addressable;
  * it takes no arguments and returns a bool -- the shape the driver in
    `run_all` relies on, and the one that stops an `all_ok` flag from
    quietly coming back;
  * awaiting it returns True against the real local stubs, which is
    the part that would fail if an extraction dropped an import.

`run_all` is checked separately for the aggregate: it is the seam
between the blocks and the function, and it is four lines.
"""
import asyncio
import inspect
import pathlib
import sys

import pytest

# The ledger names this file as item 9's marker too, and
# `test_tech_debt_ledger` holds the two to naming each other. The
# runtime golden pins the sequence; this file pins the property the
# golden cannot see -- that each extracted block is a module-level
# coroutine, callable on its own, and awaited by `_selftest`.
pytestmark = pytest.mark.tech_debt(
    item=9,
    reason="Pins TECH-DEBT #9: every selftest leaf block is a "
           "zero-argument `check_*() -> bool`, runnable in isolation, "
           "and awaited by `_selftest`.")

BOT = pathlib.Path(__file__).resolve().parent.parent / "telegram-bot"
if str(BOT) not in sys.path:
    sys.path.insert(0, str(BOT))

MODULES = ("selftest_storage", "selftest_handlers", "selftest_access",
            "selftest_state", "selftest_media", "selftest_llm",
            "selftest_infra")


# `selftest_llm` is driven one block at a time rather than through
# `run_all`, because the `selftest_handlers` and `selftest_storage`
# summaries sit between its first three blocks and the golden pins the
# *sequence*. Folding them into one call would reorder the golden.
#
# That arrangement is not something the golden can check, and it is not
# something `test_run_all_reports_its_own_name` covers either -- for this
# module `run_all` is never called, so demanding its summary appear would
# be demanding something the driver deliberately does not do. So the
# claim that matters is stronger and checked separately below: every
# `check_*` in the module is awaited from `_selftest`.
POSITIONAL_MODULES = ("selftest_llm",)
DRIVEN_BY_RUN_ALL = tuple(m for m in MODULES if m not in POSITIONAL_MODULES)


def _blocks(module):
    return [(name, obj) for name, obj in vars(module).items()
            if name.startswith("check_") and inspect.iscoroutinefunction(obj)]


@pytest.mark.parametrize("module_name", MODULES)
def test_the_module_exists_and_has_blocks(module_name):
    mod = __import__(module_name)

    blocks = _blocks(mod)
    assert len(blocks) >= 2, (
        f"{module_name} has {len(blocks)} check_* blocks. A slice that "
        f"moved one section and lost the rest would still leave one, so "
        f"the floor is deliberately above 1.")
    assert inspect.iscoroutinefunction(mod.run_all), (
        f"{module_name}.run_all must be awaitable -- it is what "
        f"_selftest awaits")


@pytest.mark.parametrize("module_name", MODULES)
def test_every_block_takes_nothing_and_returns_a_verdict(module_name):
    """The shape the driver depends on.

    A block that grew a parameter stops being callable by `run_all`,
    and a block that returns something other than a bool stops the
    `all_ok &=` from meaning what it says.
    """
    mod = __import__(module_name)
    for name, fn in _blocks(mod):
        sig = inspect.signature(fn)
        assert not sig.parameters, (
            f"{module_name}.{name} takes {list(sig.parameters)}; `run_all` "
            f"calls it with none, so either the driver or the block is "
            f"wrong")
        assert sig.return_annotation in (bool, "bool"), (
            f"{module_name}.{name} returns {sig.return_annotation!r}, not a "
            f"verdict")


@pytest.mark.parametrize("module_name", MODULES)
def test_every_block_passes_when_run_alone(module_name):
    """The block, driven directly, with nothing else around it.

    This is the direction the golden cannot reach. The golden watches
    `_selftest` run all the blocks in order; if one depends on
    something an earlier block left behind, the golden is green and
    this is red.

    Each block runs in its own subprocess with its own database. The
    first version of this test set `os.environ` in-process, which
    passed when the file was run alone and failed inside the suite --
    `config` had already been imported with another suite's
    `CONVERSATIONS_DB`, so the environment change reached nothing and
    the block quietly ran against the wrong file. A test that is green
    alone and red in the suite is worse than no test.
    """
    import selftest_harness as H

    mod = __import__(module_name)
    for name, _fn in _blocks(mod):
        verdict, err = H.run_block(module_name, name)
        assert verdict is True, (
            f"{module_name}.{name} returned {verdict!r} when run on its "
            f"own, in a fresh process with its own database. If it is "
            f"order-dependent, that is the seam this extraction had to "
            f"get right.\n{err or ''}")


@pytest.mark.parametrize("module_name", DRIVEN_BY_RUN_ALL)
def test_run_all_reports_its_own_name(module_name):
    """The label, not the behaviour.

    The storage summary used to be labelled `[selftest] group-mode
    tests`, which was false, and nothing caught it. `run_all` now names
    its own scope; this holds it to that.
    """
    import selftest_harness as H

    mod = __import__(module_name)
    lines, _trace, rc, raw, _n = H.run_selftest()
    tail = [ln for ln in lines if "all pass" in ln or "FAILED" in ln]
    assert tail, "the selftest emitted no section summaries at all"
    want = mod.SUMMARY_NAME
    assert any(f"[selftest] {want} tests:" in ln for ln in tail), (
        f"no summary line names {module_name}.{want}. The label is a "
        f"constant in the module and `run_all` formats its log line from "
        f"it, so this failing means the line was built by hand instead -- "
        f"which is how the storage summary ended up claiming to be the "
        f"group-mode tests.\nThe summaries are:\n" + "\n".join(tail))

@pytest.mark.parametrize("module_name", POSITIONAL_MODULES)
def test_every_block_is_awaited_by_selftest(module_name):
    """The seam the golden cannot see, for a module with no `run_all` call.

    Removing `.run()` from `call_llama` returned a `ToolLoop` instead of
    an answer and left 333 tests green, because every test of the new
    unit drove it directly and none could see whether its caller called
    it. The same shape is available here: a `check_*` that nothing
    awaits runs nowhere, reports nothing, and takes its markers out of
    the golden -- which is a diff, but one a reader has to notice.

    So this reads `_selftest` and checks every block in the module is
    awaited by name. It is the caller-side direction for a module whose
    blocks are driven individually.
    """
    import ast

    mod = __import__(module_name)
    src = (BOT / "selftest.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "_selftest")

    awaited = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            f = node.func
            if (isinstance(f, ast.Attribute)
                    and isinstance(f.value, ast.Name)
                    and f.value.id == module_name
                    and f.attr.startswith("check_")):
                awaited.add(f.attr)

    missing = sorted(name for name, _fn in _blocks(mod)
                     if name not in awaited)
    assert not missing, (
        f"{module_name} defines {missing} but `_selftest` never awaits "
        f"{missing}. A block nothing calls runs nowhere -- its markers "
        f"disappear from the golden, which is a diff rather than a "
        f"failure. Modules driven by run_all are covered by "
        f"`test_the_module_exists_and_has_blocks`; this is the "
        f"caller-side direction for the ones that are not.")
