from loop_owner import (call_source, loop_source,  # noqa: F401
                       loop_tree)
"""Block A -- characterization ("golden master") for call_llama.

Why this file exists before any refactor code does

call_llama.py's own docstring sets the precondition:

    DO NOT REFACTOR without reading docs/CALL_LLAMA.md end-to-end and
    writing a regression test against a captured llama-server response.

These are that test. The fixtures in tests/fixtures/ are not hand-written
mocks: they were captured from the live engine (Qwen3.8-27B-Ultra-Heretic-
MTP-256k) through the same request call_llama builds. A mock encodes what
I believe the server sends, which is precisely what a refactor invalidates.

What must not change, and why each one bites

* content assembly. The first streaming chunk carries `content: null`, not
  `""`. `buf += delta["content"]` raises TypeError on that one chunk. The
  reasoning_content field is Qwen-specific and has no OpenAI counterpart.

* tool_calls merging. This is the sharp one, recorded from the wire:
  the `id`, `type` and `name` appear ONLY in the first chunk for an index,
  and `arguments` arrive as separate string fragments:

      [{"index":0,"id":"30SW...","type":"function",
        "function":{"name":"get_weather","arguments":"{"}}]
      [{"index":0,"function":{"arguments":"\\"city\\":\\""}}]
      [{"index":0,"function":{"arguments":"Mos"}}]
      [{"index":0,"function":{"arguments":"cow"}}]
      [{"index":0,"function":{"arguments":"\\""}}]
      [{"index":0,"function":{"arguments":"}"}}]

  So `{"city":"Moscow"}` is assembled from six fragments. An accumulator
  that assigns instead of appending produces `{"city":"Mos"}` -- valid
  JSON, wrong city, and no error anywhere. Nothing but a fixture taken
  from the real server catches that.

* the abort ladder. Four independent guards (max_iter, identical-call,
  wall-clock, hallucination, plus last_empty). They are layered, not
  alternatives. A refactor that loses one does not fail -- it spins.

Note on how the fake client is built

call_llama constructs httpx.AsyncClient inline in seven places, so the
transport is patched at httpx.AsyncClient rather than injected. That means
Block C2 will have to move this patch target. The fixtures do not move,
and that is the point: the test is allowed to change shape during the
refactor, the recorded behaviour is not.
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest

BOT_DIR = Path(__file__).resolve().parent.parent / "telegram-bot"
sys.path.insert(0, str(BOT_DIR))

FIXTURES = Path(__file__).resolve().parent / "fixtures"
pytestmark = pytest.mark.tech_debt(
    item=3,
    reason="Pins TECH-DEBT #3: the wire facts and the abort ladder that a "
           "call_llama refactor must preserve. Fixtures captured from the "
           "live engine, not mocked.")


def _load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# ======================================================================
# 1. What the wire actually looks like. These are facts, not behaviour.
# ======================================================================

def test_fixture_first_stream_chunk_has_null_content():
    """The one detail that makes a naive accumulator raise TypeError."""
    lines = _load("llama_stream.json")["raw_lines"]
    first = json.loads(lines[0][len("data: "):])
    delta = first["choices"][0]["delta"]
    assert delta.get("content", "MISSING") is None, (
        f"the engine no longer sends content:null on the opening chunk; got "
        f"{delta!r}. If this changed, re-check the accumulator -- and re-"
        f"capture the fixtures, do not edit this assertion."
    )


def test_fixture_stream_ends_with_finish_reason_then_done():
    lines = _load("llama_stream.json")["raw_lines"]
    assert lines[-1] == "data: [DONE]", f"last SSE line is {lines[-1]!r}"
    final = json.loads(lines[-2][len("data: "):])
    assert final["choices"][0]["finish_reason"] == "stop"
    assert final["choices"][0]["delta"] == {}, (
        "the terminal chunk carries an empty delta object; a fixture that "
        "assumes a content key here would mis-handle the last step"
    )


def test_fixture_reasoning_is_a_separate_field():
    """Qwen extension. There is no OpenAI equivalent, so dropping the key
    silently loses the entire reasoning feed to Telegram."""
    lines = _load("llama_stream.json")["raw_lines"]
    keys = set()
    for line in lines:
        if not line.startswith("data: ") or line == "data: [DONE]":
            continue
        keys |= set(json.loads(line[6:])["choices"][0].get("delta", {}))
    assert "reasoning_content" in keys, (
        f"stream deltas now carry {sorted(keys)}. If reasoning_content is "
        f"gone the push_thinking feed has nothing to push and the T65.1 "
        f"silent mode becomes indistinguishable from a broken model."
    )


def _tool_call_chunks(fixture="llama_stream_toolcalls.json"):
    """Every tool_calls delta on the wire, in order."""
    out = []
    for line in _load(fixture)["raw_lines"]:
        if not line.startswith("data: ") or line == "data: [DONE]":
            continue
        delta = json.loads(line[6:])["choices"][0].get("delta", {})
        for tc in delta.get("tool_calls", []):
            out.append(tc)
    return out


def test_fixture_tool_id_and_name_appear_only_in_the_first_chunk():
    """The fact the whole tool accumulator is built around."""
    chunks = _tool_call_chunks()
    assert len(chunks) > 1, "expected a multi-chunk tool call to test with"

    first = chunks[0]
    assert first["id"] and first["function"]["name"], (
        f"the opening chunk no longer carries id/name: {first!r}"
    )
    for later in chunks[1:]:
        assert "id" not in later, (
            f"a later chunk now carries id: {later!r}. An accumulator that "
            f"takes identity from the last chunk instead of the first will "
            f"silently change tool routing."
        )
        assert "name" not in later.get("function", {}), (
            f"a later chunk now carries name: {later!r} -- same failure mode"
        )


def test_fixture_tool_arguments_arrive_as_fragments():
    chunks = _tool_call_chunks()
    frags = [tc.get("function", {}).get("arguments", "") for tc in chunks]

    assert len(frags) > 1, "expected split arguments"
    whole = "".join(frags)
    assert json.loads(whole) == {"city": "Moscow"}, (
        f"fragments do not reassemble to the expected call: {frags}"
    )

    # The failure this exists to prevent: an accumulator that ASSIGNS the
    # arguments field instead of appending. The result is not "a different
    # city" on this fixture, it is unparseable -- which is louder, and
    # still silent at the point where the tool would have run.
    try:
        naive = json.loads(frags[-1])
    except json.JSONDecodeError:
        naive = None
    assert naive != json.loads(whole), (
        "the last fragment alone happens to be the whole JSON, so an "
        "assign-instead-of-append bug would be invisible on this fixture. "
        "Re-capture with a longer argument value before trusting it."
    )


def test_fixture_nonstream_message_carries_reasoning_content():
    body = _load("llama_nonstream.json")["body"]
    msg = body["choices"][0]["message"]
    assert "reasoning_content" in msg, (
        f"non-streaming message keys are now {sorted(msg)}; the vision path "
        f"reads reasoning_content directly"
    )
    assert body["choices"][0]["finish_reason"] == "stop"


# ======================================================================
# 2. What the merge must produce
# ======================================================================

def _merge_tool_calls(lines):
    """The documented contract: key by index, identity from the first
    chunk, arguments concatenated. Not production code -- the assertion is
    on what the fixture forces any implementation to produce."""
    buf = {}
    for line in lines:
        if not line.startswith("data: ") or line == "data: [DONE]":
            continue
        delta = json.loads(line[6:])["choices"][0].get("delta", {})
        for tc in delta.get("tool_calls", []):
            slot = buf.setdefault(tc["index"], {"id": None, "name": "",
                                                "arguments": ""})
            if tc.get("id"):
                slot["id"] = tc["id"]
            if tc.get("function", {}).get("name"):
                slot["name"] = tc["function"]["name"]
            slot["arguments"] += tc.get("function", {}).get("arguments", "")
    return buf


def test_merged_tool_call_is_complete_and_addressable():
    buf = _merge_tool_calls(_load("llama_stream_toolcalls.json")["raw_lines"])
    assert len(buf) == 1, f"expected a single tool call, got {sorted(buf)}"
    call = buf[0]
    assert call["name"] == "get_weather", f"got {call['name']!r}"
    assert call["id"], "the id from the first chunk was dropped"
    args = json.loads(call["arguments"])
    assert args == {"city": "Moscow"}, (
        f"arguments reassembled to {args!r}, expected the full city name. "
        f"This is the failure an append-vs-assign bug produces and nothing "
        f"else catches."
    )


# ======================================================================
# 3. The abort ladder
# ======================================================================

ABORT_GUARDS = {
    "max_iter": "hard cap on tool-calling iterations",
    "identical-call": "same tool + same args twice in a row",
    "wall-clock": "600s budget on the whole call",
    "hallucination": "tool_calls returned with req_tools=None",
    "last_empty": "three empty iterations in a row",
}


# Which module owns which guard, as of TECH-DEBT #3 Block C3. Measured,
# not assumed: the ledger called these an "abort ladder" and they are not
# one. hallucination fires before any tool runs and returns a dict while
# the rest return strings, so it stayed in call_llama; the three tail
# guards moved to llm_loop_guard.
#
# A single concatenated source answers "does the string appear anywhere",
# not "is the guard in the place that runs it" -- which is how the
# previous version stayed green as the code moved underneath it.
GUARD_OWNERS = {
    "max_iter":       ("limits.py",          "MAX_ITER = _env_or"),
    "hallucination":  ("call_llama.py",      "hallucinat"),
    "identical-call": ("llm_loop_guard.py",  "current_calls == self.prev_calls"),
    "wall-clock":     ("llm_loop_guard.py",  "elapsed > self.budget_sec"),
    "last_empty":     ("llm_loop_guard.py",  "self.last_empty >= 3"),
}


def test_all_five_abort_guards_are_present_in_the_source():
    """Structural, not behavioural: each guard has its own failure shape
    and losing one does not raise -- it spins. So assert they exist."""
    missing = []
    for guard, (filename, needle) in GUARD_OWNERS.items():
        if needle not in (BOT_DIR / filename).read_text(encoding="utf-8"):
            missing.append(f"{guard} (expected in {filename})")
    assert not missing, (
        f"abort guards missing: {missing}. Each one is a distinct way the "
        f"loop stops; losing one is silent -- the call runs to max_iter "
        f"instead."
    )
    assert len(ABORT_GUARDS) == 5, "this test and the guard list have drifted"


def test_max_iter_is_still_fifteen():
    """Pinned by value, not just presence. 15 was chosen; a silent change
    to 50 turns a 10-second hang into a 30-minute one.

    The cap moved out of llm_iteration.py into limits.py on 2026-10-28:
    it is a deployment setting now (`LLAMABOT_MAX_ITER`), and the
    production deployment raises it. What this pins is the value the
    REPOSITORY ships -- the one every other deployment inherits.
    """
    src = (BOT_DIR / "limits.py").read_text(encoding="utf-8")
    assert "MAX_ITER = _env_or(\"LLAMABOT_MAX_ITER\", 15)" in src, (
        "the repository's default iteration cap is no longer 15. If that "
        "was deliberate, say so in the commit -- the number is an incident "
        "response, not a preference"
    )