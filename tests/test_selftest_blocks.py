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

BOT = pathlib.Path(__file__).resolve().parent.parent / "telegram-bot"
if str(BOT) not in sys.path:
    sys.path.insert(0, str(BOT))

MODULES = ("selftest_storage", "selftest_handlers")


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


@pytest.mark.parametrize("module_name", MODULES)
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