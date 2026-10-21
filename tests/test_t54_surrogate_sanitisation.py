r"""T54 tests: lone-surrogate 500 defence.

Qwen 3.8-27B-Ultra-Heretic MTP sometimes returns tool_call
arguments with Python-style lone surrogates (e.g. emoji U+1F44D
encoded as \uD83D\uDC4D instead of UTF-8 bytes). When the bot
re-serialises the assistant message for the next iteration,
ensure_ascii=False passes them through, producing JSON that
llama-server's parser rejects with 500.

The fix: walk the request dict and replace lone surrogates
with U+FFFD before serialising.
"""
import json
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "telegram-bot"))

# This file used to define its own copy of the scrubber with the comment
# "inline the same logic to test it without calling the whole call_llama",
# because _scrub_surrogates was a nested def inside call_llama's
# iteration loop and could not be imported.
#
# That made this test decorative. It passed, and the production copy it
# was meant to describe was never executed by anything -- a duplicate
# that exists because the real one is unreachable is worse than no test,
# because it reports confidence nobody earned.
#
# TECH-DEBT #3 Block C4 hoisted the scrubber into llm_json.py. This test
# now imports it. If you are reading this after a future refactor and
# find a copy here again: that is the bug, not the workaround.
from llm_json import scrub_surrogates as _scrub_surrogates  # noqa: E402
from llm_json import dumps_body  # noqa: E402,F401


def test_surrogate_in_arguments_replaced():
    """The exact scenario: assistant tool_call with emoji as surrogate."""
    bad_args = '{"emoji": "\ud83d\udc4d"}'  # lone surrogate
    msg = {
        'role': 'assistant',
        'content': None,
        'tool_calls': [
            {'id': 'call_1', 'type': 'function', 'function': {'name': 'react_to_message', 'arguments': bad_args}},
        ],
    }
    cleaned = _scrub_surrogates(msg)
    # The arguments string now has a valid UTF-8 character.
    # If we try to encode with ensure_ascii=False, it should NOT raise.
    json.dumps(cleaned, ensure_ascii=False)
    # And it should contain the actual emoji, not the surrogate.
    assert '\ud83d' not in cleaned['tool_calls'][0]['function']['arguments']
    assert '\\ud83d' not in cleaned['tool_calls'][0]['function']['arguments']


def test_no_surrogate_unchanged():
    """Clean UTF-8 strings pass through unchanged."""
    msg = {'role': 'user', 'content': 'Hello 👍'}
    cleaned = _scrub_surrogates(msg)
    assert cleaned == msg


def test_nested_dict_and_list():
    """The scrubber recurses into dicts and lists."""
    obj = {
        'a': 'before\ud83dafter',
        'b': [{'c': '\ud83d'}, ['\ud83d\udc4d']],
        'd': 42,
    }
    cleaned = _scrub_surrogates(obj)
    json.dumps(cleaned, ensure_ascii=False)  # must not raise
    assert '\ud83d' not in json.dumps(cleaned)


def test_non_string_passthrough():
    """ints, bools, None pass through unchanged."""
    assert _scrub_surrogates(42) == 42
    assert _scrub_surrogates(True) is True
    assert _scrub_surrogates(None) is None


def test_empty_string_unchanged():
    assert _scrub_surrogates('') == ''


def test_this_file_tests_the_production_copy():
    """The failure mode this file used to have, now pinned.

    A test that imports its own reimplementation of the thing it tests
    reports the health of the copy, not the code.
    """
    import inspect
    import llm_json

    assert inspect.getsourcefile(_scrub_surrogates) is not None
    assert _scrub_surrogates is llm_json.scrub_surrogates, (
        "this file has drifted back to testing a local copy; the "
        "production scrubber lives in llm_json.py"
    )
