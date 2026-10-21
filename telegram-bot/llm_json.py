r"""Request-body encoding helpers.

TECH-DEBT #3, Block C4. `scrub_surrogates` was a nested function inside
the iteration loop in call_llama, which is why
tests/test_t54_surrogate_sanitisation.py carried its own copy of the
logic with the comment "inline the same logic to test it without calling
the whole call_llama".

A duplicated implementation that exists only because the real one is
unreachable is worse than no test: it passes while the production copy
drifts, and nothing detects the drift. Both copies were identical today.
Neither of them had to be.

The bug it defends against: Qwen3.8-27B sometimes emits tool_call
arguments containing Python-style lone surrogates (emoji U+1F44D as
\ud83d\udc4d rather than UTF-8 bytes). `ensure_ascii=False` passes them
through, and llama-server's parser rejects the request with a 500.
"""
import json


def scrub_surrogates(obj):
    """Replace lone surrogates with U+FFFD, recursively.

    Returns a new structure; the input is not mutated. Anything that is
    not a str, dict or list is passed through by identity -- numbers,
    bools, None, and anything the engine adds later.
    """
    if isinstance(obj, str):
        return obj.encode("utf-8", errors="surrogatepass").decode(
            "utf-8", errors="replace")
    if isinstance(obj, dict):
        return {k: scrub_surrogates(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub_surrogates(v) for v in obj]
    return obj


def dumps_body(req_body: dict) -> bytes:
    r"""Serialise the request the way llama-server must receive it.

    `ensure_ascii=False` is load-bearing and was once the other way
    round: dumping with the default escaped Cyrillic as \uXXXX and the
    `data:` prefix of an inline image as \\u002f, and llama-server
    answered 400 for identical prompts that worked over the API.

    Called only from the streaming path; the non-streaming branch builds
    its own body in llm_nonstream.py. Two call sites, two dumps, one
    rule -- if this is ever made the single implementation, check that
    the non-streaming path does not start sending escaped bytes.
    """
    return json.dumps(scrub_surrogates(req_body),
                      ensure_ascii=False).encode("utf-8")