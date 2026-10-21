"""selftest.py must not assume a container path, and its gate must be real.

W3-H2 removed three `/app/*.py` hardcodes. Confirmed by running
`_selftest()` against a mock harness, and then the mutation "put
`/app/storage.py` back" survived the suite on both interpreters:

    W2-M1: /app path restored   SURVIVED  SURVIVED

`tests/test_selftest.py` is in CI's --ignore list, and `_selftest()`
returns immediately unless LLAMABOT_SELFTEST=1, so nothing in CI ever
executed the 94 assertion sites behind that gate.

W4-H1. The first version of the gate tripwire was

    assert "LLAMABOT_SELFTEST" in source

which cannot tell the gate from a mention of it. The string appears five
times in the file:

    8    comment
    28   comment
    75   docstring
    76   if os.environ.get('LLAMABOT_SELFTEST') != '1':   <- the gate
    1663 an unrelated donsetch condition

Remove the gate and the other four survive, so the assert passes.

That is not a decorative tripwire failing to fail. `dispatch.py:1020` calls
`await _selftest()` unconditionally before the first poll, and `main()`
catches only KeyboardInterrupt, so with the gate gone an InvalidToken
escapes `_run`, escapes `asyncio.run`, and kills the process at startup:

    SHIPPED   exit=0  [selftest] lines emitted=0     (silent)
    NO GATE   exit=1  [selftest] InvalidToken: The token `1:T` was rejected

Zero polls. The bot does not come up.

The word "unconditionally" is carrying that sentence, and it is the
part that can quietly stop being true. Measured in wave 5: wrapping
that call in a try/except in dispatch.py makes the bot come up fine
with the gate still removed. So the gate is load-bearing **while
nothing in dispatch.py wraps the call** -- not unconditionally, ever.

That is what test_dispatch_calls_selftest_unconditionally below is
for: it pins the half of the contract that carries the claim, instead
of leaving a sentence in a docstring to assert what no code checks.
When someone adds a try/except -- a good change -- this file will say
the paragraph above needs revisiting, rather than the docstring
quietly becoming a lie.

So the check has to look at the guard itself -- an `if` on the env var,
returning early, inside the function -- not at the string's presence.
"""
import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BOT_DIR = REPO_ROOT / "telegram-bot"
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

SELFTEST = BOT_DIR / "selftest.py"
DISPATCH = BOT_DIR / "dispatch.py"


def test_selftest_contains_no_container_paths():
    """`/app/...` is the container's layout. Any occurrence means the smoke
    test aborts on the first one it hits -- a function whose contract is
    that it never raises."""
    source = SELFTEST.read_text(encoding="utf-8")
    offenders = [
        (n, ln.strip())
        for n, ln in enumerate(source.splitlines(), 1)
        if "/app/" in ln and not ln.lstrip().startswith("#")
    ]
    assert not offenders, (
        f"selftest.py hardcodes container paths at {offenders}. The smoke "
        f"test cannot run outside the image, and nothing in CI would notice: "
        f"test_selftest.py is --ignore'd and _selftest() returns "
        f"immediately unless LLAMABOT_SELFTEST=1."
    )


def _selftest_node():
    tree = ast.parse(SELFTEST.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_selftest":
            return node
    raise AssertionError("_selftest not found")


def test_the_env_gate_is_a_guard_not_a_mention():
    """An `if` on LLAMABOT_SELFTEST whose body returns, at the top of the
    function.

    Checking that the string appears in the source cannot do this: it
    occurs in two comments, the docstring, an unrelated donsetch condition,
    and the gate.
    """
    fn = _selftest_node()

    guarded = any(
        isinstance(node, ast.If)
        and "LLAMABOT_SELFTEST" in ast.dump(node.test)
        and any(isinstance(s, ast.Return) for s in node.body)
        for node in fn.body[:3]
    )
    assert guarded, (
        "_selftest() has no early return on LLAMABOT_SELFTEST near the top. "
        f"dispatch.py calls it unconditionally before the first poll, and "
        f"main() catches only KeyboardInterrupt -- so without this gate an "
        f"InvalidToken escapes asyncio.run and the process dies at startup. "
        f"The bot never comes up. Body starts with "
        f"{[type(n).__name__ for n in fn.body[:4]]}"
    )


def test_dispatch_calls_selftest_unconditionally():
    """Pin the half of the contract the gate importance rests on.

    If dispatch puts _selftest() behind its own env check, the gate
    inside selftest stops being load-bearing. If dispatch starts
    wrapping the call in try/except, it stops being load-bearing too
    -- wave 5 measured that the bot comes up either way once such a
    wrapper exists.

    This asserts the call sits at statement level, not under an `if`.
    It does not assert surrounding control flow in general: a call
    under a `with` or a `while` would still satisfy it. That gap is
    deliberate for now (W5-L1) and is on the list if dispatch.py is
    ever refactored around the startup path.
    """
    source = DISPATCH.read_text(encoding="utf-8")
    tree = ast.parse(source)

    callers = []
    # An `if` that lexically contains the call makes it conditional.
    # Range containment is the honest test; the first version tried to
    # walk parent nodes and marked every call conditional.
    if_ranges = [(n.lineno, n.end_lineno or n.lineno)
                 for n in ast.walk(tree) if isinstance(n, ast.If)]

    for node in ast.walk(tree):
        if not isinstance(node, ast.Await):
            continue
        call = node.value
        if not (isinstance(call, ast.Call)
                and getattr(call.func, "id", "") == "_selftest"):
            continue
        conditional = any(lo <= node.lineno <= hi for lo, hi in if_ranges)
        callers.append((node.lineno, conditional))

    assert callers, "dispatch.py no longer calls _selftest() at all"
    conditional = [(ln, c) for ln, c in callers if c]
    assert not conditional, (
        f"dispatch.py calls _selftest() conditionally at {conditional}; "
        f"the gate inside _selftest is the only guard then, and the tripwire "
        f"above exists to protect exactly that"
    )


def test_module_source_helper_exists():
    """If someone deletes the helper without inlining its logic, the next
    test would stop being able to find sibling modules at all."""
    source = SELFTEST.read_text(encoding="utf-8")
    assert "def _module_source(" in source, (
        "selftest.py lost _module_source(); sibling modules are read through "
        "it instead of a hardcoded path"
    )
