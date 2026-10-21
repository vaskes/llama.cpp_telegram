"""The step order in `parse_tool_args` is a contract, not an implementation detail.

Raised in review 2026-10-22: normalisation of non-object parses sits
after the parsed-empty check, and nothing pins that order. It survived
two wrong attempts during Block D4 precisely because the boundary is
subtle enough to move by accident.

**Why the order matters, precisely.** Both orders agree on `[]` and
`null`, because those are in `_EMPTY_IS_INTENTIONAL` and pass the
empty check either way. They disagree on a *truthy* non-object:

    order as written   "42" -> parses to 42, truthy, passes step 3,
                            then normalised to {} at step 4
                            => ArgsOk({})
    normalised first   "42" -> normalised to {} before step 3, so
                            `not args` is True, and "42" is not one of
                            the shapes that genuinely mean no arguments
                            => ArgsRejected("parsed_empty")

So `42`, `3.5`, `true` and `"text"` are the discriminating inputs, and
`[]`/`null` are not. A test written on the empty shapes proves nothing
about the order -- and the review's suggested test used exactly those,
while describing `[]` as truthy, which it is not.

What must not change is the *decision*, which was made in Block D1 and
has tests: an empty shape is accepted, because rejecting it means the
model re-emits the same shape forever. What these tests pin is only
that the type-normalisation sits after that decision, so that a truthy
non-object is not reclassified as an empty one.
"""
import ast
from pathlib import Path

import pytest

from llm_toolargs import ArgsOk, ArgsRejected, parse_tool_args

pytestmark = pytest.mark.tech_debt(item=8)

TOOLARGS = Path(__file__).resolve().parent.parent / "telegram-bot" / "llm_toolargs.py"

#: Parses to something truthy that is not a dict. These are the only
#: inputs where the two orders disagree.
TRUTHY_NON_OBJECT = ["42", "3.5", "true", '"text"', "[1,2]"]


@pytest.mark.parametrize("raw", TRUTHY_NON_OBJECT)
def test_a_truthy_non_object_is_accepted(raw):
    """Passed by step 3 because it is truthy, normalised by step 4.

    Normalised first it would be falsy, and step 3 would reject it --
    which is the regression this pins.
    """
    got = parse_tool_args("f", raw)
    assert isinstance(got, ArgsOk), (
        f"{raw!r} was rejected. It parses to something truthy, so the "
        f"empty-shape check does not apply to it; if this fails, the "
        f"normalisation has moved above the check and is reclassifying a "
        f"non-empty argument set as an empty one.")


@pytest.mark.parametrize("raw", ["[]", "null"])
def test_an_empty_non_object_is_still_accepted(raw):
    """The decision that must NOT change: rejected empty shapes loop the
    model forever, so they are accepted. Block D1's tests; restated here
    because the normalisation now sits next to it and could look
    removable."""
    assert isinstance(parse_tool_args("f", raw), ArgsOk)


def test_the_normalisation_is_after_the_empty_check_in_source():
    """Structural, so the order survives someone "simplifying" the
    function into a different shape.

    Located by the comment the two steps carry rather than by line
    number, so inserting a line above does not break it.
    """
    src = TOOLARGS.read_text(encoding="utf-8")
    empty_check = src.index('return _rejection(\n            "parsed_empty"')
    normalise = src.index("if not isinstance(args, dict):")
    assert normalise > empty_check, (
        "the non-object normalisation now runs BEFORE the parsed-empty "
        "check. Truthy non-objects become {} and are then rejected as "
        "empty. Swap them back.")


def test_the_check_would_notice_the_swap():
    """Control. The comparison above has to be capable of failing."""
    empty_check_at, normalise_at = 100, 40      # deliberately reversed
    assert normalise_at < empty_check_at, \
        "the control does not describe the broken order"


def test_no_step_between_them_invents_a_third_path():
    """The two checks are adjacent on purpose. Anything routed between
    them would see the normalised value instead of the parsed one."""
    tree = ast.parse(TOOLARGS.read_text(encoding="utf-8"))
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
              and n.name == "parse_tool_args")

    empty_returns, normalise_stmt = None, None
    for node in ast.walk(fn):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Call):
            fn_name = getattr(node.value.func, "attr", getattr(node.value.func, "id", ""))
            if fn_name == "_rejection" and "parsed_empty" in ast.unparse(node):
                empty_returns = node
        if isinstance(node, ast.If) and "isinstance(args, dict)" in ast.unparse(node):
            normalise_stmt = node

    assert empty_returns is not None, "the parsed_empty rejection moved"
    assert normalise_stmt is not None, "the normalisation moved"

    # every return between the two would observe the parsed value
    between = [n for n in ast.walk(fn)
               if isinstance(n, ast.Return)
               and empty_returns.lineno < n.lineno < normalise_stmt.lineno]
    assert not between, (
        "a return sits between the empty check and the normalisation: "
        + ", ".join(ast.unparse(n)[:60] for n in between))