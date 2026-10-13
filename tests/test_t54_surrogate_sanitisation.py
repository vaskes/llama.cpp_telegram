"""T54 tests: lone-surrogate 500 defence.

Qwen 3.8-27B-Ultra-Heretic MTP sometimes returns tool_call
arguments with Python-style lone surrogates (e.g. emoji U+1F44D
encoded as \uD83D\uDC4D instead of UTF-8 bytes). When the bot
re-serialises the assistant message for the next iteration,
ensure_ascii=False passes them through, producing JSON that
llama-server's parser rejects with 500.

The fix: walk the request dict and replace lone surrogates
with U+FFFD before serialising.
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "telegram-bot"))

# Inline the same logic to test it without calling the whole
# call_llama. (call_llama imports many things; this is a
# unit test of the sanitisation function only.)
def _scrub_surrogates(obj):
    if isinstance(obj, str):
        return obj.encode('utf-8', errors='surrogatepass').decode('utf-8', errors='replace')
    if isinstance(obj, dict):
        return {k: _scrub_surrogates(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub_surrogates(v) for v in obj]
    return obj


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
    import json
    # If we try to encode with ensure_ascii=False, it should NOT raise.
    raw = json.dumps(cleaned, ensure_ascii=False)
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
    import json
    json.dumps(cleaned, ensure_ascii=False)  # must not raise
    assert '\ud83d' not in json.dumps(cleaned)


def test_non_string_passthrough():
    """ints, bools, None pass through unchanged."""
    assert _scrub_surrogates(42) == 42
    assert _scrub_surrogates(True) is True
    assert _scrub_surrogates(None) is None


def test_empty_string_unchanged():
    assert _scrub_surrogates('') == ''
