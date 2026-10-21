"""Block D2 -- llm_toolcalls.

Two reject sites, one rule. These tests hold the rule and check that
the extraction did not change what either site does.

The differential tests are the ones that matter. Refactors that pass
handfuls of example-based tests routinely differ from the original on
an input nobody thought of; here the original loop is reconstructed
verbatim as an oracle and run against the module on randomised
buffers. If the two ever disagree, one of them is wrong.
"""
import ast
import json
import random
from pathlib import Path

import pytest

from llm_toolcalls import (
    REJECTED_NAME,
    normalize_tool_calls,
    reject_unknown_names,
)

BOT = Path(__file__).resolve().parent.parent / "telegram-bot"
KNOWN = frozenset({"bash_command", "donsetch_web_search"})


def buffered(*entries):
    return dict(enumerate(entries))


def entry(name="bash_command", args='{"command": "ls"}', cid="call_a"):
    return {"id": cid, "name": name, "arguments": args}


# --------------------------------------------------------------------------
# the oracle: the loop as it was, before Block D2
# --------------------------------------------------------------------------

def oracle(buffered_, known=KNOWN):
    """Verbatim transcription of the pre-D2 inline loop.

    Note the mutation of the caller's buffer on the reject path. That
    was the behaviour; the module does not do it, because nothing
    read it afterwards. `_OLD_MUTATED` records what the old one did so
    a test can assert the difference is exactly that and nothing else.
    """
    _OLD_MUTATED = []
    tool_calls = []
    for idx, tc in buffered_.items():
        if tc["name"]:
            if tc["name"] not in known:
                tc["name"] = "__rejected__"
                _OLD_MUTATED.append(tc["name"])
            args_str = tc["arguments"]
            try:
                args_obj = json.loads(args_str) if args_str else {}
            except Exception:
                args_obj = {}
            tool_calls.append({
                "id": tc["id"] or f"call_{idx}",
                "type": "function",
                "function": {"name": tc["name"],
                             "arguments": json.dumps(args_obj) if args_obj else args_str},
            })
    return tool_calls, _OLD_MUTATED


# --------------------------------------------------------------------------
# normalisation
# --------------------------------------------------------------------------

def test_empty_buffer_yields_nothing():
    out = normalize_tool_calls({}, known=KNOWN)
    assert out.calls == []
    assert out.empty
    assert out.rejected == []
    assert out.saw_unavailable_tool is False


def test_known_name_passes_through_unchanged():
    out = normalize_tool_calls(buffered(entry()), known=KNOWN)
    assert len(out.calls) == 1
    assert out.calls[0]["function"]["name"] == "bash_command"
    assert out.rejected == []
    assert out.saw_unavailable_tool is False


def test_unknown_name_becomes_the_sentinel_and_is_reported():
    out = normalize_tool_calls(buffered(entry(name="rm_rf")), known=KNOWN)
    assert out.calls[0]["function"]["name"] == REJECTED_NAME
    assert out.rejected == ["rm_rf"]
    assert out.saw_unavailable_tool is True


def test_a_rejected_call_is_kept_not_dropped():
    """The protocol needs one tool result per call.

    Dropping it would leave the model with a tool call that never gets
    an answer, and the next iteration would be malformed input from the
    server's point of view -- a 400, not a soft rejection.
    """
    out = normalize_tool_calls(buffered(entry(name="nope")), known=KNOWN)
    assert len(out.calls) == 1, "the rejected call was dropped instead of rewritten"


def test_entry_with_no_name_is_dropped():
    """A stream cut mid-call leaves an index opened and never filled."""
    out = normalize_tool_calls(
        buffered(entry(), entry(name="", cid="call_z")), known=KNOWN)
    assert len(out.calls) == 1
    assert out.calls[0]["id"] == "call_a"


def test_missing_id_falls_back_to_the_index():
    out = normalize_tool_calls(
        {3: {"id": None, "name": "bash_command", "arguments": "{}"}}, known=KNOWN)
    assert out.calls[0]["id"] == "call_3"


def test_mixed_names_report_only_the_refused_ones():
    out = normalize_tool_calls(
        buffered(entry(name="bash_command", cid="a"),
                 entry(name="ghost", cid="b"),
                 entry(name="donsetch_web_search", cid="c")),
        known=KNOWN)
    assert out.rejected == ["ghost"]
    assert [c["function"]["name"] for c in out.calls] == [
        "bash_command", REJECTED_NAME, "donsetch_web_search"]


# --------------------------------------------------------------------------
# argument handling -- the fiddly bit
# --------------------------------------------------------------------------

def test_broken_json_is_carried_through_verbatim():
    """The raw string survives so the diagnostic can quote what the model
    actually sent. Screening broken args is parse_tool_args's job."""
    broken = '{"command": "ls", '
    out = normalize_tool_calls(buffered(entry(args=broken)), known=KNOWN)
    assert out.calls[0]["function"]["arguments"] == broken


def test_empty_object_falls_back_to_the_raw_string():
    out = normalize_tool_calls(buffered(entry(args="{}")), known=KNOWN)
    assert out.calls[0]["function"]["arguments"] == "{}"


def test_absent_arguments_become_an_empty_string():
    out = normalize_tool_calls(
        buffered({"id": "x", "name": "bash_command", "arguments": None}),
        known=KNOWN)
    assert out.calls[0]["function"]["arguments"] == ""


def test_valid_args_are_reserialised():
    out = normalize_tool_calls(buffered(entry(args='{"command":"ls"}')),
                               known=KNOWN)
    assert json.loads(out.calls[0]["function"]["arguments"]) == {"command": "ls"}


# --------------------------------------------------------------------------
# the deliberate behaviour change
# --------------------------------------------------------------------------

def test_the_callers_buffer_is_not_mutated():
    """The old loop rewrote tc["name"] in place on the reject path.

    Nothing read tool_calls_buf after that loop -- it is rebuilt at the
    top of every iteration and the dispatch works from the returned
    list -- so the write was invisible. Pinning that it no longer
    happens, because the next reader has to notice.
    """
    buf = buffered(entry(name="ghost"))
    out = normalize_tool_calls(buf, known=KNOWN)
    assert out.calls[0]["function"]["name"] == REJECTED_NAME
    assert buf[0]["name"] == "ghost", "the caller's buffer was rewritten"


def test_the_old_loop_did_mutate_it_so_this_is_a_real_change():
    """Control: the difference above is real, not something I imagined."""
    buf = buffered(entry(name="ghost"))
    oracle(buf)
    assert buf[0]["name"] == "__rejected__"


# --------------------------------------------------------------------------
# differential against the oracle
# --------------------------------------------------------------------------

_NAMES = ["bash_command", "donsetch_web_search", "rm_rf", "", None, "x" * 40]
_ARGS = ['{"command": "ls"}', "{}", "", '{"command": ', "null", "[1,2]",
         '{"a": {"b": 1}}', '"just a string"', "not json at all"]


@pytest.mark.parametrize("seed", range(40))
def test_matches_the_inline_loop_it_replaced(seed):
    rnd = random.Random(seed)
    entries = []
    for i in range(rnd.randint(0, 5)):
        entries.append({
            "id": rnd.choice([None, "", f"call_{i}"]),
            "name": rnd.choice(_NAMES),
            "arguments": rnd.choice(_ARGS),
        })

    expected, _ = oracle(buffered(*[dict(e) for e in entries]))
    got = normalize_tool_calls(buffered(*[dict(e) for e in entries]),
                               known=KNOWN)

    assert got.calls == expected, f"seed {seed}"
    assert got.rejected == [e["name"] for e in entries
                            if e["name"] and e["name"] not in KNOWN], f"seed {seed}"


def test_a_json_scalar_is_reparsed_not_kept_as_text():
    """json.loads('null') is None, which is falsy, so the raw string
    wins -- and the caller gets 'null', not 'None'."""
    out = normalize_tool_calls(buffered(entry(args="null")), known=KNOWN)
    assert out.calls[0]["function"]["arguments"] == "null"


# --------------------------------------------------------------------------
# site 1: the list-shaped reject
# --------------------------------------------------------------------------

def test_site1_rewrites_names_in_place_and_returns_them():
    calls = [
        {"function": {"name": "bash_command", "arguments": '{"c": 1}'}},
        {"function": {"name": "rm_rf", "arguments": '{"path": "/"}'}},
    ]
    bad = reject_unknown_names(calls, known=KNOWN)
    assert bad == ["rm_rf"]
    assert calls[0]["function"]["name"] == "bash_command", "a known name was rewritten"


def test_site1_blanks_arguments_on_the_rejected_call():
    """Unlike the streaming site, this one dispatches immediately, so a
    rejected call must not carry a payload into an executor."""
    calls = [{"function": {"name": "rm_rf", "arguments": '{"path": "/"}'}}]
    reject_unknown_names(calls, known=KNOWN)
    assert calls[0]["function"]["arguments"] == "{}"


def test_site1_leaves_known_names_alone():
    calls = [{"function": {"name": "bash_command", "arguments": '{"c": 1}'}}]
    assert reject_unknown_names(calls, known=KNOWN) == []
    assert calls[0]["function"]["arguments"] == '{"c": 1}'


def test_site1_survives_a_call_without_a_function_key():
    calls = [{"id": "x"}, {"function": None}, {"function": {"name": "rm_rf"}}]
    assert reject_unknown_names(calls, known=KNOWN) == ["rm_rf"]


def test_both_sites_share_one_sentinel():
    """If these ever diverge, a rejected call dispatched from one path
    will not be recognised by the unhandled-tool branch, and the model
    gets no explanation."""
    src = (BOT / "call_llama.py").read_text(encoding="utf-8")
    assert '"__rejected__"' not in src, "a sentinel literal is back inline"
    assert "REJECTED_NAME" in Path(BOT / "llm_toolcalls.py").read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# call sites are wired
# --------------------------------------------------------------------------

def test_call_llama_calls_the_module_rather_than_reimplementing_it():
    tree = ast.parse((BOT / "call_llama.py").read_text(encoding="utf-8"))
    fn = next(n for n in tree.body
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "call_llama")
    called = {c.func.id for c in ast.walk(fn)
              if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
    assert {"normalize_tool_calls", "reject_unknown_names"} <= called


def test_no_indexed_iteration_over_the_buffer_remains():
    src = (BOT / "call_llama.py").read_text(encoding="utf-8")
    assert "tool_calls_buf.items()" not in src

# --------------------------------------------------------------------------
# the call site
#
# Two mutations survived the first harness pass, and both were here
# rather than in the module: the tests drove normalize_tool_calls
# directly, so nothing was watching what call_llama does around the
# call. A function can be correct and its caller still wrong.
# --------------------------------------------------------------------------

def _call_llama() -> ast.AsyncFunctionDef:
    tree = ast.parse((BOT / "call_llama.py").read_text(encoding="utf-8"))
    return next(n for n in tree.body
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "call_llama")


def test_the_caller_does_not_rewrite_the_buffer():
    """Survived mutation 1: the caller re-introducing the in-place
    rewrite of tool_calls_buf.

    Scoped to a write THROUGH the buffer -- `tool_calls_buf[i]["name"]
    = ...` -- because the buffer itself is legitimately written when the
    SSE accumulator fills it, and msg["tool_calls"] / req_body["tools"]
    are not this concern at all. An earlier version of this test banned
    every indexed assignment in call_llama and failed on five legitimate
    ones.
    """
    fn = _call_llama()

    def through_the_buffer(node):
        base = node.value
        if not isinstance(base, ast.Subscript):
            return False
        return isinstance(base.value, ast.Name) and base.value.id == "tool_calls_buf"

    offenders = [n for n in ast.walk(fn)
                 if isinstance(n, ast.Subscript) and isinstance(n.ctx, ast.Store)
                 and through_the_buffer(n)]

    # Two shapes, not one. A mutation that wrote
    # `for t in tool_calls_buf.values(): t["name"] = ...` has no
    # `tool_calls_buf[...]` Subscript in it at all, and survived the
    # narrower check. call_llama has no legitimate reason to assign to
    # a "name" key anywhere -- the reject logic is the only thing that
    # ever did -- so the rule is the key, not the path.
    offenders += [n for n in ast.walk(fn)
                  if isinstance(n, ast.Subscript) and isinstance(n.ctx, ast.Store)
                  and isinstance(n.slice, ast.Constant) and n.slice.value == "name"]

    assert not offenders, (
        "call_llama writes through the streaming buffer: "
        + ", ".join(ast.unparse(o) for o in offenders)
        + "\nnormalize_tool_calls does the rewriting on its output. Editing the"
          " caller's buffer here would be the old dead write coming back."
    )


def test_the_rejection_is_still_logged_from_the_caller():
    """Survived mutation 2: `for bad_name in []:`.

    The log line is a runbook grep target -- operators search for
    TOOL-ISOLATION to see refusals -- so losing it is a support
    regression, not a cosmetic one. Checked structurally: a loop over
    the rejected names whose body logs with that marker.
    """
    fn = _call_llama()
    found = False
    for node in ast.walk(fn):
        if not isinstance(node, ast.For):
            continue
        if ast.unparse(node.iter) != "normalized.rejected":
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Call):
                text = ast.unparse(inner)
                if "log.info" in text and "TOOL-ISOLATION" in text \
                        and "soft-reject" in text:
                    found = True
    assert found, (
        "no loop over normalized.rejected that logs TOOL-ISOLATION. "
        "Refusals would still happen but would be invisible to the "
        "runbook greps."
    )


def test_both_reject_sites_are_logged():
    """Two sites, two log lines, and both must survive -- they are the
    only record that tool isolation fired."""
    src = (BOT / "call_llama.py").read_text(encoding="utf-8")
    assert src.count("TOOL-ISOLATION") == 2, (
        f"expected two TOOL-ISOLATION log sites, found "
        f"{src.count('TOOL-ISOLATION')}"
    )
