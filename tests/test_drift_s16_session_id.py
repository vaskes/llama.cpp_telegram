"""T60 Tripwire #4: LLM-controlled session_id regression.

Wave-trap class: T57 #S16. The team added session_id format
validation (T52 #S3 regex [A-Za-z0-9._-]{1,128}) but the
session_id was still controlled by the LLM via
`args.get('session_id') or f'chat-{chat_id}'`. The LLM could
emit `session_id="chat-{other_chat_id}"` and forward to
another chat's persistent tmux session. Direct cross-chat
PII / secret leak.

This tripwire asserts:
  1. call_llama.py does NOT reference `args.get('session_id')`
     anywhere (the LLM's session_id must be ignored).
  2. call_llama.py has exactly one
     `sid = f'chat-{chat_id}'` (or `f"chat-{chat_id}"`) line,
     which is the canonical assignment.

If a future commit re-introduces `args.get('session_id')` or
removes the sid line, this test fails.
"""
from __future__ import annotations

import ast
import re

from test_drift_helpers import (
    CALL_LLAMA_PY,
    parse_source,
)


def test_no_args_get_session_id_in_call_llama():
    """T60 #4: call_llama.py must NOT reference
    `args.get('session_id')`. The LLM's session_id is
    untrusted; the bot owns the session_id as
    `f'chat-{chat_id}'`.
    """
    # Block D4 moved the bash dispatch into llm_dispatch.py. The rule
    # is unchanged, so the scan covers both modules.
    from dispatch_location import dispatch_source
    src = dispatch_source()
    # Match either single or double quotes
    patterns = [
        "args.get('session_id')",
        'args.get("session_id")',
    ]
    for pat in patterns:
        assert pat not in src, (
            f"call_llama.py contains '{pat}'. The LLM's "
            f"session_id is untrusted. T57 #S16 fix: always "
            f"use `sid = f'chat-{{chat_id}}'`, never pull "
            f"session_id from the LLM's tool args."
        )


def test_sid_is_chat_dash_chat_id():
    """T60 #4: call_llama.py must have exactly one
    assignment matching `sid = f'chat-{chat_id}'` (or
    double-quoted variant). This is the canonical session_id
    construction.
    """
    tree = parse_source(CALL_LLAMA_PY)
    # Find every assignment where target is `sid` and value
    # is a JoinedStr (f-string).
    matches: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            tgt = node.targets[0]
            if isinstance(tgt, ast.Name) and tgt.id == "sid":
                if isinstance(node.value, ast.JoinedStr):
                    # Reconstruct the f-string source
                    src_line = ast.unparse(node.value)
                    matches.append((node.lineno, src_line))
    # Filter to only the chat-bound form. Block D4 moved the derivation
    # to SESSION_ID_TEMPLATE, formatted from chat_id at the call site.
    # Both halves have to be present: a constant nothing formats, or a
    # format of a constant that never mentions chat_id, each pass alone.
    canonical = [
        (lineno, src)
        for lineno, src in matches
        if "chat-" in src and "chat_id" in src
    ]
    from dispatch_location import dispatch_source
    joined = dispatch_source()
    constant_bound = ('SESSION_ID_TEMPLATE = "chat-{chat_id}"' in joined
                      and ".format(chat_id=" in joined)
    if constant_bound:
        assert not canonical or len(canonical) == 1, (
            f"the chat-scoped constant is formatted more than once: "
            f"{canonical}")
        if not canonical:
            canonical = [(0, "SESSION_ID_TEMPLATE.format(chat_id=...)")]
    assert len(canonical) == 1, (
        f"call_llama.py must have exactly one "
        f"`sid = f'chat-{{chat_id}}'` assignment. "
        f"Found {len(canonical)}: {canonical}. Wave-5 #S16 "
        f"fix: the LLM cannot choose the session_id; the "
        f"bot owns it via chat_id."
    )
    # And the comment must still say "never trust the LLM"
    lineno, _ = canonical[0]
    # Block D4 moved the bash dispatch into llm_dispatch.py. The rule
    # is unchanged, so the scan covers both modules.
    from dispatch_location import dispatch_source
    src = dispatch_source()
    # Look at the same line / next line for the S16 comment. With the
    # constant form there is no single line to blame, so the marker is
    # required somewhere in the module that derives it -- the intent of
    # the original check, which was never about line numbers.
    lines = src.splitlines()
    if lineno == 0:
        assert any("S16" in ln for ln in lines), (
            "the session id derivation has lost its "
            "# T57 #S16: never trust the LLM's session_id marker, so a "
            "future contributor has nothing warning them off 'improving' it")
    elif lineno - 1 < len(lines):
        line = lines[lineno - 1]
        if "S16" not in line and (
            lineno < len(lines) and "S16" not in lines[lineno]
        ):
            assert False, (
                f"call_llama.py:{lineno}: sid = f'chat-{{chat_id}}' "
                f"is missing the S16 comment marker. Add "
                f"`# T57 #S16: never trust the LLM's session_id` "
                f"so a future contributor does not 'improve' it."
            )
