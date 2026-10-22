"""A local bound only to None must not have attributes read off it.

Two production defects, one shape.

`vclient`  Block C1 moved `async with httpx.AsyncClient(...) as vclient:`
            into llm_nonstream.py. The tools-less 500 fallback stayed
            behind and still referenced the client whose binding had
            gone. UnboundLocalError on the 500 path.

`r`        The same block left behind `r = None` where the POST used to
            bind it. The 500 check read `r.status_code`, so the
            condition was permanently False and v0.8.1's
            retry-without-tools could not fire. Nothing raised. The
            feature was simply gone, from ac79264 onward.

The second is the dangerous one, and it is why this is a check on *how*
a name is bound rather than on whether it is bound. Every name-resolution
tripwire in this repo passes on `r`: it is assigned, so it resolves.

So: a local whose every assignment is the literal None, and which is then
dereferenced, is either a bug or is about to become one. The `r` case is
exactly that -- a defensive `= None` that outlived the thing it defended.

Deliberately not "must be non-None" everywhere. Some locals legitimately
start at None and are filled later, as long as nothing *reads* them
before the fill. That case passes here, because the rule needs an
attribute read, not just a load.
"""
import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.tech_debt(item=8)

BOT = Path(__file__).resolve().parent.parent / "telegram-bot"


def _functions(tree):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _bound_names(target):
    """Every plain Name a target assigns, unpacking tuples and lists.

    Added because `a, b = thing` stopped counting as an assignment of
    `a` when a handler moved to tuple unpacking, and this check went
    blind to exactly the name it exists to guard: handlers initialise
    `thinking = None` before the try and assign the real message after
    it, so dropping the unpacked form left the only assignment looking
    like the None initialiser, and the check red on correct code while
    losing the ability to catch the bug.
    """
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        return [nm for e in target.elts for nm in _bound_names(e)]
    return []


def _only_bound_to_none(fn, name):
    """True when every assignment of `name` is the literal None."""
    assigns = [n for n in ast.walk(fn)
               if isinstance(n, ast.Assign)
               and any(name in _bound_names(t) for t in n.targets)]
    if not assigns:
        return False
    # an assignment that binds the name among others still counts as a
    # real binding -- `a, b = (None, x)` binds `a` to None, not to the
    # tuple, but `a, b = thing` binds it to something computed
    return all(isinstance(a.value, ast.Constant) and a.value.value is None
               for a in assigns)


def _dereferenced(fn, name):
    """Lines where `name.<attr>` or `name[...]` is read."""
    out = []
    for n in ast.walk(fn):
        if isinstance(n, (ast.Attribute, ast.Subscript)):
            base = n.value
            if isinstance(base, ast.Name) and base.id == name:
                out.append(n.lineno)
    return sorted(out)


@pytest.mark.parametrize("module", sorted(
    p.name for p in BOT.glob("*.py") if not p.name.startswith("test_")))
def test_no_none_only_local_is_dereferenced(module):
    path = BOT / module
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        pytest.skip(f"{module} does not parse on this interpreter")

    offenders = []
    for fn in _functions(tree):
        assigned = {nm for n in ast.walk(fn) if isinstance(n, ast.Assign)
                    for t in n.targets for nm in _bound_names(t)}
        for name in assigned:
            if not _only_bound_to_none(fn, name):
                continue
            lines = _dereferenced(fn, name)
            if lines:
                offenders.append(
                    f"{module}:{lines[0]} reads {name}.<attr>, but {name} is "
                    f"only ever bound to None")

    assert not offenders, (
        "locals bound only to None and then dereferenced -- the shape of "
        "two production defects:\n  " + "\n  ".join(sorted(offenders))
        + "\n\nEither the thing that used to bind it moved, or the "
          "initialiser outlived what it defended.")


def test_a_none_default_that_is_refilled_is_allowed():
    """Control, so the check is not just "any local named x = None".

    This shape is fine and must stay fine: bound to None, filled on some
    path, and the read is guarded by the branch that fills it. The rule
    rejects it only because the fill is missing -- here it is present, so
    the check has nothing to say.
    """
    ok = ast.parse(
        "async def f(client):\n"
        "    helper = None\n"
        "    if client is None:\n"
        "        helper = build()\n"
        "    return helper\n"
    )
    fn = ok.body[0]
    assert _only_bound_to_none(fn, "helper") is False, \
        "a local that is refilled must not be treated as None-only"


def test_the_r_shape_is_detected():
    """The production defect, as a fixture. If this stops being detected
    the check above is not doing what its name says."""
    outage = ast.parse(
        "async def f():\n"
        "    r = None\n"
        "    try:\n"
        "        await post()\n"
        "    except Exception as e:\n"
        "        if r is not None and r.status_code == 500:\n"
        "            return retry()\n"
        "    return None\n"
    )
    fn = outage.body[0]
    assert _only_bound_to_none(fn, "r") is True
    assert _dereferenced(fn, "r") == [6], \
        "the analysis no longer finds the dereference the outage relies on"


def test_the_c1_shape_is_detected():
    """And the other one: a binding that moved out of scope entirely.

    This one is already caught by the name-resolution tripwire, and it is
    listed here because the two are the same mistake -- a symbol that
    stopped being bound here -- arriving by different routes.
    """
    gone = ast.parse(
        "async def f():\n"
        "    try:\n"
        "        await post()\n"
        "    except Exception as e:\n"
        "        return await vclient.post('x')\n"
    )
    fn = gone.body[0]
    assert not _only_bound_to_none(fn, "vclient")
    assert not any(isinstance(n, ast.Assign) for n in ast.walk(fn)
                   if isinstance(n, ast.Name) and n.id == "vclient"), \
        "vclient should have no binding at all here"

# --------------------------------------------------------------------------
# A limitation, recorded rather than papered over
# --------------------------------------------------------------------------

def test_conditional_binding_is_a_third_shape_and_nothing_here_covers_it():
    """Documented gap. Both tripwires miss this, on purpose.

    `test_drift_call_llama_names` treats any name bound anywhere in the
    function as available, because being stricter produced false
    positives. This file only looks at locals bound *exclusively* to None.

    So a name bound inside one branch and read from another passes both.
    Found by mutation: replacing the 500 guard's `_err_response` with
    `r`, which is bound by the streaming branch's `async with ... as r`
    and not in the non-streaming error handler.

    That is a real UnboundLocalError, and it needs branch-aware scope
    analysis to catch -- not another line-count heuristic. Both existing
    checks state their scope so the gap is findable rather than assumed
    away.
    """
    conditional = ast.parse(
        "async def f(stream):\n"
        "    try:\n"
        "        await post()\n"
        "    except Exception as e:\n"
        "        if r is not None and r.status_code == 500:\n"
        "            return retry()\n"
        "    if stream:\n"
        "        async with client.stream() as r:\n"
        "            pass\n"
        "    return None\n"
    )
    fn = conditional.body[0]

    # neither check fires, and that is the point of asserting it here
    bound_in_fn = any(isinstance(n, ast.Name) and n.id == "r"
                      and isinstance(n.ctx, ast.Store)
                      for n in ast.walk(fn))
    assert bound_in_fn, "the fixture lost its conditional binding"
    assert _dereferenced(fn, "r"), "the fixture lost its read"

    # documented rather than fixed: scope analysis is the real answer
    assert not _only_bound_to_none(fn, "r"), \
        "this file only claims the None-only shape"
