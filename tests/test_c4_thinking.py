import ast  # noqa: E402
from loop_owner import (call_source, call_tree,  # noqa: F401
                       loop_source, loop_tree, run_one)
"""C4 -- push_thinking and the request-body scrubber, tested directly.

Two functions were the last uncovered ones in call_llama, and both were
uncovered for the same structural reason: they were nested `def`s inside
a 1041-line function, so no test could reach them without running the
whole thing.

scrub_surrogates was worse than merely uncovered.
tests/test_t54_surrogate_sanitisation.py carried its own copy, with the
comment "inline the same logic to test it without calling the whole
call_llama". A duplicate that exists because the real one is unreachable
is worse than no test at all: it goes green while the production copy
drifts, and nothing in the repo would notice. That test file now imports
the real function instead of restating it.

push_thinking is the thing users see on every streamed turn. Its six
branches are pinned one by one below.
"""
import json
import sys
from pathlib import Path

import pytest

BOT_DIR = Path(__file__).resolve().parent.parent / "telegram-bot"
sys.path.insert(0, str(BOT_DIR))

import llm_json  # noqa: E402
import llm_thinking  # noqa: E402
from llm_json import dumps_body, scrub_surrogates  # noqa: E402
from llm_thinking import (FLOOD_BACKOFF_SEC, MAX_PAYLOAD,  # noqa: E402
                          THROTTLE_SEC, ThinkingThrottle, push_thinking)

pytestmark = pytest.mark.tech_debt(
    item=3,
    reason="Pins TECH-DEBT #3 C4: the last two nested closures in "
           "call_llama, hoisted so they can be reached at all.")


class _Msg:
    """Stands in for a Telegram Message."""

    def __init__(self, fail=None):
        self.edits = []
        self._fail = fail

    async def edit_text(self, text, reply_markup=None):
        if self._fail:
            raise self._fail
        self.edits.append((text, reply_markup))


@pytest.fixture
def clock(monkeypatch):
    """Controllable monotonic clock.

    Patched on the module, not globally: `llm_thinking.time` IS the
    stdlib time module, so setting the attribute through it would move
    the clock for the whole process including pytest. Rebinding the
    module's own name avoids that.
    """
    state = {"t": 1000.0}
    monkeypatch.setattr(llm_thinking.time, "monotonic", lambda: state["t"])
    return state


class _NoMessage:
    """Sentinel, so `msg=None` can mean "no message at all".

    The first version used `msg if msg is not None else _Msg()`, which
    turned every no-message test into a test with a message.
    """


_NO_MESSAGE = _NoMessage()


async def push(text="мысль", msg=_NO_MESSAGE, silent=False, throttle=None,
               force=False):
    """Call the real function.

    `throttle` defaults to None, which means "a fresh one". A test that
    needs the throttle to remember anything must pass the SAME object to
    every call -- otherwise the first version of these tests created a new
    throttle per call, nothing was ever throttled, and four of them
    passed for the wrong reason while three failed.
    """
    return await push_thinking(
        text,
        thinking_msg=_Msg() if msg is _NO_MESSAGE else msg,
        silent=silent,
        throttle=throttle if throttle is not None else ThinkingThrottle(),
        force=force,
    )


# ===========================================================================
# 1. the six branches
# ===========================================================================

async def test_ordinary_delta_edits_the_message(clock):
    msg = _Msg()
    assert await push("размышляю", msg) is True
    assert len(msg.edits) == 1
    assert "размышляю" in msg.edits[0][0]
    assert msg.edits[0][0].startswith("💭 thinking…")


async def test_silent_never_touches_telegram(clock):
    """T65.1. The whole point: a message addressed to another human in a
    group still gets reasoned about, but nothing is shown. If reasoning
    ever leaks here, the bot visibly thinks along with a conversation
    it was not part of."""
    msg = _Msg()
    assert await push("секретные размышления", msg, silent=True) is False
    assert msg.edits == []


async def test_no_message_is_a_no_op_not_an_error(clock):
    assert await push("текст", msg=None) is False


async def test_empty_text_is_a_no_op(clock):
    """A vision turn has no reasoning at all; calling with '' must not
    clear the message the user is already looking at."""
    msg = _Msg()
    await push("первая мысль", msg)
    assert await push("", msg, force=True) is False
    assert len(msg.edits) == 1, "an empty push wiped or added to the message"


async def test_rate_limit_skips_the_second_delta_within_five_seconds(clock):
    msg = _Msg()
    th = ThinkingThrottle()
    assert await push("первая", msg, throttle=th) is True
    clock["t"] += THROTTLE_SEC - 0.5
    assert await push("вторая", msg, throttle=th) is False
    assert len(msg.edits) == 1
    # ...and the window really does close after 5s
    clock["t"] += 1.0
    assert await push("третья", msg, throttle=th) is True
    assert len(msg.edits) == 2


async def test_a_flood_backs_off_edits_for_thirty_seconds(clock):
    th = ThinkingThrottle()
    msg = _Msg(fail=Exception("Too Many Requests: retry after 12"))
    assert await push("первая", msg, throttle=th) is False

    clock["t"] += FLOOD_BACKOFF_SEC - 1.0
    healthy = _Msg()
    assert await push("вторая", healthy, throttle=th) is False, (
        "still inside the flood blackout -- retrying makes it worse"
    )
    clock["t"] += 2.0
    assert await push("третья", healthy, throttle=th) is True


async def test_force_bypasses_both_timers(clock):
    """The final flush. Without force the last thing the user sees is
    not the last thing the model thought -- the message freezes a few
    seconds before the answer arrives."""
    msg = _Msg()
    th = ThinkingThrottle()
    await push("первая", msg, throttle=th)
    assert await push("вторая", msg, throttle=th) is False        # rate limited
    assert await push("третья", msg, throttle=th, force=True) is True


async def test_force_also_bypasses_the_flood_blackout(clock):
    th = ThinkingThrottle()
    msg = _Msg(fail=Exception("flood"))
    await push("первая", msg, throttle=th)
    healthy = _Msg()
    assert await push("вторая", healthy, throttle=th) is False
    assert await push("третья", healthy, throttle=th, force=True) is True


# ===========================================================================
# 2. truncation
# ===========================================================================

async def test_long_reasoning_is_truncated_to_the_tail(clock):
    """The tail, not the head: the user watches what the model is
    thinking NOW. Head-truncation shows the part it already finished.

    The markers have to be one-off, not repeated filler. Three versions
    of this test failed on their own arithmetic: any marker repeated
    enough times to exceed the window necessarily fills the retained
    tail, so "the head is gone" was false for a reason that had nothing
    to do with the code. A single prefix at position 0 is unambiguously
    outside any 3800-char window of a 6000-char body.
    """
    msg = _Msg()
    start = "СТАРТ-РАЗУМЛЕНИЯ\n"
    text = start + ("промежуточный текст " * 400) + "ФИНАЛ-МАРКЕР"
    assert len(text) > MAX_PAYLOAD
    assert len(start) < MAX_PAYLOAD

    await push(text, msg)
    body = msg.edits[0][0]

    assert "ФИНАЛ-МАРКЕР" in body, "the newest reasoning must survive"
    assert "СТАРТ-РАЗУМЛЕНИЯ" not in body, "the head must be gone"
    assert "earlier reasoning omitted" in body
    assert len(body) <= MAX_PAYLOAD + 200


async def test_the_payload_stays_under_telegrams_limit(clock):
    msg = _Msg()
    await push("x" * 100000, msg)
    assert len(msg.edits[0][0]) <= 4096, (
        f"sent {len(msg.edits[0][0])} chars to a 4096-char API"
    )
    assert MAX_PAYLOAD < 4096


async def test_short_reasoning_is_not_marked_as_truncated(clock):
    msg = _Msg()
    await push("короткая мысль", msg)
    assert "earlier reasoning omitted" not in msg.edits[0][0]


# ===========================================================================
# 3. edit_text failures
# ===========================================================================

async def test_not_modified_is_swallowed(clock):
    """Raised by Telegram when the text is byte-identical. It happens
    on every repeat of the same reasoning prefix and must not be logged
    as an error."""
    assert await push("текст", _Msg(fail=Exception("Bad Request: message is not modified"))) is False


async def test_an_unknown_edit_error_does_not_propagate(clock):
    """A failed thinking edit must never take down the turn. The answer
    is what matters; the progress indicator is decoration."""
    assert await push("текст", _Msg(fail=RuntimeError("boom"))) is False


async def test_the_stop_button_is_re_attached_on_every_edit(clock):
    """editMessageText drops the inline keyboard unless reply_markup is
    passed again. Lose it and the ⏹ Stop button vanishes mid-answer."""
    msg = _Msg()
    await push("мысль", msg)
    assert msg.edits[0][1] is not None, "the Stop button was dropped"


# ===========================================================================
# 4. the throttle is per turn
# ===========================================================================

async def test_a_fresh_throttle_carries_no_history():
    """call_llama builds one per call. A process-wide throttle would let
    one conversation inherit another's flood blackout."""
    t = ThinkingThrottle()
    assert t.last_push == 0.0 and t.last_flood == 0.0


async def test_throttling_records_the_push_not_the_skip(clock):
    """Skipping a delta must not reset the window, or a fast model
    would defeat the limit entirely by never pausing."""
    msg = _Msg()
    th = ThinkingThrottle()
    await push("раз", msg, throttle=th)
    first = th.last_push
    clock["t"] += 1.0
    await push("два", msg, throttle=th)
    assert th.last_push == first, "a skipped delta moved the throttle window"


# ===========================================================================
# 5. the scrubber
# ===========================================================================

def test_lone_surrogates_become_the_replacement_char():
    """T54. Qwen sometimes emits tool_call arguments with emoji as
    \ud83d\udc4d instead of UTF-8 bytes; ensure_ascii=False passes them
    through and llama-server answers 500."""
    got = scrub_surrogates('{"emoji": "\ud83d\udc4d"}')
    assert "\ud83d" not in got
    assert json.dumps(got, ensure_ascii=False)      # would raise otherwise


def test_clean_text_passes_through_unchanged():
    msg = {"role": "user", "content": "Hello 👍"}
    assert scrub_surrogates(msg) == msg


def test_it_recurses_into_dicts_and_lists():
    obj = {"a": "до\ud83dпосле", "b": [{"c": "\ud83d"}, ["\ud83d\udc4d"]]}
    got = scrub_surrogates(obj)
    assert "\ud83d" not in json.dumps(got, ensure_ascii=False)


def test_non_containers_pass_through_by_identity():
    for value in (1, 0, True, None, 3.5):
        assert scrub_surrogates(value) is value


def test_dumps_body_keeps_cyrillic_and_data_urls_intact():
    """ensure_ascii=False is load-bearing and was once the other way
    round: escaped Cyrillic and an escaped `data:` prefix made
    llama-server answer 400 for prompts that worked over the API."""
    raw = dumps_body({"messages": [{"role": "user",
                                    "content": "Проверь математику"}]})
    assert isinstance(raw, bytes)
    assert "Проверь математику" in raw.decode("utf-8")
    assert "\\u" not in raw.decode("utf-8")


def test_dumps_body_scrubs_before_it_serialises():
    """One call, both jobs -- the reason dumps_body exists rather than a
    scrub next to a json.dumps at each call site."""
    raw = dumps_body({"messages": [{"content": "👍"}], "x": "\ud83d"})
    assert "\\ud83d" not in raw.decode("utf-8")


# ===========================================================================
# 6. they stay out of the closures
# ===========================================================================

def test_neither_function_is_defined_inside_call_llama_again():
    """The whole reason this block exists. A nested def is unreachable
    from a test, and the next person who does not know that will
    re-nest it -- or duplicate it, which is what happened to
    scrub_surrogates."""
    import ast

    tree = loop_tree()
    names = {n.name for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for gone in ("push_thinking", "_scrub_surrogates", "scrub_surrogates"):
        assert gone not in names, (
            f"{gone} is defined in call_llama.py again -- nested defs "
            f"cannot be imported, which is what made this block necessary"
        )


def test_the_loop_imports_thinking_and_call_llama_keeps_the_json_scrubber():
    """Two separate claims, two modules.

    The loop owns the push_thinking call now. `dumps_body` never moved --
    it scrubs surrogate pairs out of request bodies, which call_llama
    builds and hands over. Checking both against one file is the mistake
    that makes a test red on a correct extraction.
    """
    loop_src = loop_source()
    assert "from llm_thinking import" in loop_src and "push_thinking" in loop_src
    call_src = call_source()
    assert "from llm_json import" in call_src, (
        "dumps_body scrubs the request body call_llama builds; it did not "
        "move into the loop")


def test_every_call_site_passes_its_context_explicitly():
    """Not just the argument NAMES -- the values.

    The first version of this checked `{k.arg for k in keywords}` and a
    mutation that rewrote `throttle=_throttle` to `throttle=None`
    survived it: the keyword was still called throttle. Checking that an
    argument is named throttle is the same class of mistake as checking
    that a function is named push_thinking.

    A missing `throttle` raises TypeError at runtime; a `None` one
    raises AttributeError inside the first rate check; a missing
    `silent` silently starts showing reasoning for messages addressed to
    other people. None of them show up in a green suite.

    Scoped to the package, not to one file. TECH-DEBT #8 block 3 moved
    the two streaming sites into llm_stream, and a check pinned to
    call_llama.py went red on correct code. "Every site in this file" is a
    location; the property is "every site passes a name that is actually
    bound where it is used".

    So the value check is scope, not spelling. `throttle=_throttle` in
    call_llama and `throttle=throttle` in llm_stream are the same
    property -- a parameter passed straight through -- and a test that
    demanded one spelling would have failed the extraction instead of
    catching it. What is rejected is a kwarg that is a computed
    expression, or a name that does not exist in the enclosing scope:
    both are the `throttle=None` bug wearing a different hat.
    """
    import ast

    required = ("thinking_msg", "silent", "throttle")
    owners = ["llm_iteration.py", "llm_stream.py"]
    calls = []
    for owner in owners:
        src = (BOT_DIR / owner).read_text(encoding="utf-8")
        tree = ast.parse(src)
        parents = {child: parent
                   for parent in ast.walk(tree) for child
                   in ast.iter_child_nodes(parent)}
        for n in ast.walk(tree):
            if not (isinstance(n, ast.Await) and isinstance(n.value, ast.Call)
                    and getattr(n.value.func, "id", None) == "push_thinking"):
                continue
            fn = parents.get(n)
            while fn is not None and not isinstance(
                    fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                fn = parents.get(fn)
            assert fn is not None, (
                f"{owner}: push_thinking called outside any function")
            bound = set()
            a = fn.args
            for arg in (a.posonlyargs + a.args + a.kwonlyargs):
                bound.add(arg.arg)
            if a.vararg:
                bound.add(a.vararg.arg)
            if a.kwarg:
                bound.add(a.kwarg.arg)
            for sub in ast.walk(fn):
                if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
                    bound.add(sub.id)
            calls.append((owner, n, fn.name, bound))

    # The floor is what keeps this from passing vacuously: delete a site
    # and the keyword checks have nothing left to inspect.
    assert len(calls) == 3, (
        f"expected 3 push_thinking call sites across {owners}, "
        f"found {len(calls)}"
    )
    for owner, node, fname, bound in calls:
        call = node.value
        got = {k.arg for k in call.keywords}
        assert set(required) <= got, (
            f"{owner}:{node.lineno} in {fname}() missing "
            f"{set(required) - got}")
        for kw in call.keywords:
            if kw.arg not in required:
                continue
            # `self.thinking_msg` is as bound as a local name: it is an
            # attribute of the instance, resolved at run time and not a
            # computed expression. Since T8.1 the loop's state lives on
            # `self`, and demanding a bare Name would fail every correct
            # call in the module.
            if isinstance(kw.value, ast.Attribute) and \
                    isinstance(kw.value.value, ast.Name) \
                    and kw.value.value.id == "self":
                continue
            assert isinstance(kw.value, ast.Name), (
                f"{owner}:{node.lineno} {kw.arg}= is neither a plain name "
                f"nor a self attribute -- it evaluates to something else "
                f"at runtime")
            assert kw.value.id in bound, (
                f"{owner}:{node.lineno} in {fname}(): {kw.arg}={kw.value.id}, "
                f"and that name is not bound anywhere in the function")


def test_the_throttle_is_constructed_per_call_not_at_module_scope():
    """A module-level throttle would be shared by every conversation in
    the process: one chat's flood silences another chat's reasoning."""
    import ast

    fn = next(n for n in call_tree().body
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "call_llama")
    tree = call_tree()

    def constructs(node):
        """ast.dump renders `ThinkingThrottle()` as
        Call(func=Name(id='ThinkingThrottle', ctx=Load()), args=[], ...)

        so the substring "ThinkingThrottle()" is NOT in it. Third time
        this has bitten: ast.dump does not reproduce source text, it
        reconstructs it. Match on the node, never on the dump.
        """
        return any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id == "ThinkingThrottle"
            for n in ast.walk(node))

    assert constructs(fn), "the throttle is no longer constructed in call_llama"
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        assert not constructs(n), \
            "a ThinkingThrottle at module scope is shared between turns"


# ===========================================================================
# 7. the positive control
# ===========================================================================

async def test_the_control_path_actually_edits():
    """Without this, every assertion above is satisfiable by a
    push_thinking that returns False forever."""
    msg = _Msg()
    assert await push("контроль", msg) is True
    assert len(msg.edits) == 1


def test_the_scrubber_control_is_not_a_no_op():
    """If scrub_surrogates did nothing, the other scrubber tests would
    be trivially true."""
    assert scrub_surrogates("\ud83d") != "\ud83d"