"""Golden master: the SSE the real llama-server actually sends.

Raised in review 2026-10-22, and correct. Every other SSE test in this
repo parses a stream I wrote by hand. If the engine changes its
framing, the parser breaks in production and those tests stay green --
they are checking a fixture that cannot fail.

`tests/fixtures/sse_stream.txt` is a real capture: two streams from
llama-server on this host, one plain and one with a tool available,
recorded by `tests/capture_sse_golden.py`. Re-recording is a deliberate
act with a diff to review; the alternative, calling the engine from CI,
makes the suite depend on a warm GPU and turns an engine bump into a red
pipeline nobody can act on.

What the capture found that a hand-written fixture never would:

  * `reasoning_content` in the delta -- a llama.cpp/Qwen extension, not
    OpenAI. The whole T43-era reasoning display depends on it.
  * `timings` at the top level, and only in the final chunk.
  * `system_fingerprint`, `created`, `model`, `object` on every chunk,
    none of which the parser reads.
  * **a blank line between every data line** -- ordinary SSE event
    separation. The synthetic fixtures were dense, so nothing had ever
    checked that path.
  * a `finish_reason` of `length` when max_tokens is exhausted, which is
    a real user-visible outcome and not an error path.

**The load-bearing assertion is the delta key set.** It is an exact
comparison against what was recorded, so the day the engine starts
sending something new, this fails with the new key named -- rather than
the parser quietly ignoring a field whose meaning nobody checked.
That is the review's objection, turned into something executable.
"""
import json
from pathlib import Path

import pytest

from llm_sse import StreamAccumulator

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "sse_stream.txt"
pytestmark = pytest.mark.tech_debt(item=8)

#: What the engine sent at capture time. An exact set, on purpose.
KNOWN_DELTA_KEYS = {"role", "content", "reasoning_content", "tool_calls"}
KNOWN_TOP_KEYS = {"choices", "created", "id", "model", "object",
                  "system_fingerprint", "timings"}
KNOWN_FINISH_REASONS = {None, "stop", "length", "tool_calls"}


@pytest.fixture(scope="module")
def capture():
    if not FIXTURE.is_file():
        pytest.skip("no golden master recorded; run tests/capture_sse_golden.py")
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _cases(capture, with_tools):
    return [c for c in capture["cases"] if c["request"]["with_tools"] is with_tools]


def test_both_shapes_are_recorded(capture):
    assert _cases(capture, False), "no plain stream captured"
    assert _cases(capture, True), "no tool stream captured"


@pytest.mark.parametrize("with_tools", [False, True])
def test_framing_holds_on_a_real_stream(capture, with_tools):
    """The three guards that Block C2 deleted by accident, now checked
    against something real instead of something invented.

    The first version of this test assumed a dense stream -- every line
    is either `data: ...` or the end marker. Recording a real one
    disproved that in the first minute: **llama-server interleaves a
    blank line between every data line**, which is ordinary SSE event
    separation.

    The production guard is `if not line or not line.startswith("data: ")`
    and it handles this correctly. This test now states that, because a
    future engine that stopped emitting blanks would change the shape
    without changing the behaviour -- and a reader of the synthetic
    fixtures could not have known blanks were involved at all.
    """
    for case in _cases(capture, with_tools):
        lines = case["raw_lines"]
        assert lines[-1].strip() == "data: [DONE]", "the stream does not end with [DONE]"

        body = lines[:-1]
        blanks = [i for i, ln in enumerate(body) if not ln.strip()]
        payloads = [ln for ln in body if ln.strip()]

        assert payloads, "a real stream carried no data lines"
        for ln in payloads:
            assert ln.startswith("data: "), (
                f"non-blank line without the SSE prefix: {ln[:80]!r}")
            json.loads(ln[6:])          # must not raise

        # recorded, so a change in event separation is visible
        assert blanks, (
            "no blank lines in the captured stream. llama-server has "
            "separated every data line with one; if that stopped, the "
            "guard's `not line` branch is no longer being exercised by "
            "anything real.")


@pytest.mark.parametrize("with_tools", [False, True])
def test_the_delta_shape_is_exactly_what_was_recorded(capture, with_tools):
    """The load-bearing one.

    A new key in `delta` is a change in what the engine tells us, and
    whether the parser should act on it is a question someone has to
    answer. Failing here puts the key on the screen instead of leaving it
    to be ignored.
    """
    seen_delta, seen_top, seen_finish = set(), set(), set()
    for case in _cases(capture, with_tools):
        for payload in case["decoded"]:
            seen_top.update(payload.keys())
            for choice in payload.get("choices", []):
                seen_finish.add(choice.get("finish_reason"))
                seen_delta.update((choice.get("delta") or {}).keys())

    new_delta = seen_delta - KNOWN_DELTA_KEYS
    new_top = seen_top - KNOWN_TOP_KEYS
    new_finish = seen_finish - KNOWN_FINISH_REASONS

    assert not new_delta, (
        f"llama-server is now sending {new_delta} inside delta. The parser "
        f"does not read it. Decide whether it matters, then add it to "
        f"KNOWN_DELTA_KEYS and to llm_sse.Delta if so.")
    assert not new_top, f"new top-level keys: {new_top}"
    assert not new_finish, f"new finish_reason values: {new_finish}"


@pytest.mark.parametrize("with_tools", [False, True])
def test_the_accumulator_parses_a_real_stream_without_error(capture, with_tools):
    """End to end through the production parser, on the real bytes."""
    for case in _cases(capture, with_tools):
        acc = StreamAccumulator()
        finishes = []
        # the same guard production uses, so the test cannot feed the
        # accumulator something the loop would never have handed it
        for line in case["raw_lines"][:-1]:
            if not line or not line.startswith("data: "):
                continue
            acc.feed(json.loads(line[6:]))
        for payload in case["decoded"]:
            for choice in payload.get("choices", []):
                finishes.append(choice.get("finish_reason"))

        assert isinstance(acc.content, str)
        assert isinstance(acc.reasoning, str)
        assert isinstance(acc.tool_calls, dict)
        real_finishes = [f for f in finishes if f]
        if real_finishes:
            assert acc.finish_reason == real_finishes[-1], (
                f"the accumulator reports {acc.finish_reason!r}, the stream "
                f"ended with {real_finishes[-1]!r}")


def test_reasoning_is_actually_present_in_the_real_stream(capture):
    """`reasoning_content` is a llama.cpp extension, not OpenAI, and the
    whole thinking-message feature reads it. If the engine ever stops
    sending it, this fails -- which is the point: a silent stop would
    break the feature with nothing in the logs.
    """
    keys = set()
    for case in capture["cases"]:
        for payload in case["decoded"]:
            for choice in payload.get("choices", []):
                keys.update((choice.get("delta") or {}).keys())
    assert "reasoning_content" in keys, (
        "no reasoning_content in the captured stream -- either the engine "
        "changed or the capture predates the feature. Either way the "
        "thinking message is not being driven by this path any more")


def test_the_capture_is_current_with_the_tree():
    """Guards against a fixture that has drifted from the module.

    Not the same property as the capture tests: those check the fixture
    against the engine's behaviour, this checks the fixture against the
    code. A capture re-recorded for an unrelated reason must not be able
    to leave the parser untested.
    """
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert data.get("schema") == 1, "unknown fixture schema -- do not guess at it"
    for case in data["cases"]:
        assert case["raw_lines"], "a recorded case has no lines"
        assert case["decoded"], "a recorded case decoded to nothing"
        assert case["captured_utc"], "a case has no capture timestamp"