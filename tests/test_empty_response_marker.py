"""The empty-response marker has exactly one definition and one screen.

The marker used to be a local of `call_llama` named
`EMPTY_FALLBACK_PREFIX`, and its comment claimed it "keeps the marker and
the recovery logic in one place". It kept it nowhere: `llm_finish`
wrote the text out longhand, `config.EMPTY_RESPONSE_FALLBACK` wrote it a
second time, and six sites in `handlers.py` matched on a sixth copy of
the literal. The constant had no readers at all.

That shape is the dangerous one. A duplicated literal drifts silently --
someone lengthens the marker in the producer and the consumer keeps
matching the old prefix, or the reverse -- and nothing goes red. The
production symptom would be a bot that retries an empty answer forever
and then answers the user with the raw fallback string, or one that
accepts a truncated marker as a real answer and ships a
`[model returned an empty response.` fragment to a Telegram chat.

So this file pins the *property* rather than a place:

1. The literal exists in executable code exactly once -- the constant.
2. Both producers build from that constant, so they cannot disagree.
3. Every consumer screens on that constant, never on a literal.

Docstrings are excluded, and deliberately so. Prose that mentions the
marker is not a second definition; a naive substring search would flag
it, and a tripwire that flags correct code teaches people to delete the
tripwire.
"""
import ast
import pathlib
import sys

import pytest

BOT = pathlib.Path(__file__).resolve().parent.parent / "telegram-bot"
if str(BOT) not in sys.path:
    sys.path.insert(0, str(BOT))

import config  # noqa: E402
import llm_finish  # noqa: E402

PREFIX = config.EMPTY_RESPONSE_PREFIX
LITERAL = f'"{PREFIX}"'


def _code_only(path):
    """Parse a module and drop every docstring, returning the tree.

    A mention in prose is not a definition. The T8.1 pass hit this
    with `LoopGuard`: the name appeared in a module docstring, a
    substring search called that a use, and the check went red on a
    correct extraction.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef,
                             ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                node.body = body[1:]
    return tree


def _constants_with_literal(tree):
    """Where the marker text is defined, as `(module, lineno)`."""
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            tgt = node.targets[0]
            if isinstance(tgt, ast.Name) and isinstance(node.value, ast.Constant) \
                    and node.value.value == PREFIX:
                hits.append((tgt.id, node.lineno))
    return hits


def test_the_marker_is_defined_once_and_only_once():
    """Executable code must not restate the marker.

    One definition is the whole point. A second one is a producer or a
    consumer that has not been converted yet, and it will drift.
    """
    found = []
    for path in sorted(BOT.glob("*.py")):
        if path.name.startswith("__"):
            continue
        for name, line in _constants_with_literal(_code_only(path)):
            found.append((path.name, name, line))

    assert len(found) == 1, (
        f"the empty-response marker is defined {len(found)} times: {found} -- "
        f"every producer and consumer must share config.EMPTY_RESPONSE_PREFIX")
    assert found[0][0] == "config.py" and found[0][1] == "EMPTY_RESPONSE_PREFIX", (
        f"the marker is defined as {found[0][1]} in {found[0][0]}, not as "
        f"config.EMPTY_RESPONSE_PREFIX")


def test_no_module_matches_the_marker_as_a_literal():
    """The consumer side. `handlers.py` used to carry six copies."""
    offenders = []
    for path in sorted(BOT.glob("*.py")):
        if path.name in ("config.py", "__init__.py"):
            continue
        tree = _code_only(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and node.value == PREFIX:
                offenders.append((path.name, node.lineno))
    assert not offenders, (
        f"the marker appears as a bare string literal in executable code at "
        f"{offenders} -- import EMPTY_RESPONSE_PREFIX instead. Six of these "
        f"were in handlers.py and they drift independently of the producer.")


def test_both_producers_build_from_the_constant():
    """Producer side. If either restates the text, the two can disagree
    about where the marker ends and the diagnostic tail begins."""
    src = (BOT / "llm_finish.py").read_text(encoding="utf-8")
    assert "EMPTY_RESPONSE_PREFIX" in src, (
        "llm_finish.finish_without_tool_calls writes the marker longhand; "
        "handlers.py screens on the constant and would miss it")

    # and the producer must not contain the literal in executable code
    assert PREFIX not in "\n".join(
        ast.unparse(n) for n in ast.walk(_code_only(BOT / "llm_finish.py"))
        if isinstance(n, ast.Constant) and isinstance(n.value, str)), (
        "llm_finish still spells the marker out")


def test_the_user_facing_message_starts_with_the_marker():
    """`EMPTY_RESPONSE_FALLBACK` is the text the user eventually reads.
    If it does not begin with the marker, a response built from it will
    not be recognised as empty and will never be retried."""
    assert config.EMPTY_RESPONSE_FALLBACK.startswith(PREFIX), (
        "EMPTY_RESPONSE_FALLBACK does not start with EMPTY_RESPONSE_PREFIX; "
        "handlers.py recognises empty responses by the prefix alone")


def test_the_diagnostic_message_starts_with_the_marker():
    built = llm_finish.finish_without_tool_calls(
        content_buf="", reasoning_buf="", finish_reason="length",
        executed_tools=[])
    assert built.startswith(PREFIX), (
        f"the finish_reason diagnostic starts with {built[:40]!r}, which "
        f"handlers.py will not recognise as empty")


def test_the_producer_and_the_screen_agree_on_the_boundary():
    """The two halves, checked against each other.

    This is the assertion that would have caught the original drift: it
    does not care where either string is written, only that what the
    screen looks for is a prefix of what the producer emits.
    """
    built = llm_finish.finish_without_tool_calls(
        content_buf="", reasoning_buf="", finish_reason="stop",
        executed_tools=[])
    assert PREFIX in built
    assert built.startswith(PREFIX)
    assert config.EMPTY_RESPONSE_FALLBACK.startswith(PREFIX)


def test_the_dead_local_is_gone():
    """It had zero readers for the whole life of the repo."""
    src = (BOT / "call_llama.py").read_text(encoding="utf-8")
    names = {n.id for n in ast.walk(_code_only(BOT / "call_llama.py"))
             if isinstance(n, ast.Name)}
    assert "EMPTY_FALLBACK_PREFIX" not in names, (
        "call_llama defines EMPTY_FALLBACK_PREFIX again. It is a local "
        "inside call_llama, so it cannot be imported by handlers and "
        "cannot be reached by the screen.")