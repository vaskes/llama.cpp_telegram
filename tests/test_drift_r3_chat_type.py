"""T60 Tripwire #3: call_llama() missing chat_type= kwarg.

Wave-trap class: T53 #R3. The team added `chat_type` parameter
to call_llama() signature but missed the retry call sites in
handlers.py. Bot crashed on every message with TypeError.

This tripwire walks handlers.py, finds every ast.Call to
`call_llama` (bare name, not `x.call_llama`), and asserts
that the call has a `chat_type=` keyword argument.

If a future commit adds a new call site without chat_type=,
this test fails.
"""
from __future__ import annotations

import ast

from test_drift_helpers import (
    CALL_LLAMA_PY,
    HANDLERS_PY,
    find_calls_to,
    parse_source,
)


def test_every_call_llama_in_handlers_passes_chat_type():
    """T60 #3: every call_llama() in handlers.py must pass
    chat_type= as a keyword argument.
    """
    tree = parse_source(HANDLERS_PY)
    calls = find_calls_to(tree, "call_llama")
    assert calls, (
        "No call_llama() calls found in handlers.py. Either "
        "the file was renamed, or call_llama is no longer "
        "the entry point. Update this tripwire if the call "
        "site moved."
    )

    bad: list[tuple[int, list[str]]] = []
    for call in calls:
        kw_names = {kw.arg for kw in call.keywords if kw.arg is not None}
        if "chat_type" not in kw_names:
            bad.append((call.lineno, sorted(kw_names)))

    assert not bad, (
        f"handlers.py has call_llama() calls missing the "
        f"chat_type= kwarg at lines {[b[0] for b in bad]}. "
        f"This is the wave-3 #R3 wave-trap class: adding a "
        f"new param to call_llama but missing it at one or "
        f"more retry sites causes a TypeError on every "
        f"message. The call sites found: {bad}."
    )


def test_chat_type_signature_in_call_llama_is_required():
    """T60 #3: call_llama() signature must require chat_type
    (no default value). T53 #R4 design: chat_type is required,
    not optional, so a future call site cannot accidentally
    skip it.
    """
    tree = parse_source(CALL_LLAMA_PY)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "call_llama":
            args = node.args
            for arg in args.args:
                if arg.arg == "chat_type":
                    # Find the default for chat_type
                    # ast.args.args has positional args; defaults
                    # are on args.defaults (last N match last N args)
                    n_defaults = len(args.defaults)
                    n_positional = len(args.args)
                    default_index = n_positional - n_defaults
                    if args.args.index(arg) >= default_index:
                        assert False, (
                            "call_llama(chat_type=...) has a default "
                            "value. T53 #R4 design: chat_type is "
                            "REQUIRED (no default). If a default is "
                            "needed, the call sites must opt-in "
                            "explicitly, not via signature default."
                        )
                    return
            # chat_type not in positional args; check kwonly.
            # args.kw_defaults is parallel to args.kwonlyargs:
            #   args.kw_defaults[i] is the default for args.kwonlyargs[i],
            #   or None if that kwonly has no default.
            # Do NOT use `d.arg` here - d is an ast.AST, not
            # an ast.keyword (no .arg attribute).
            for i, karg in enumerate(args.kwonlyargs):
                if karg.arg == "chat_type":
                    if i < len(args.kw_defaults) and args.kw_defaults[i] is not None:
                        assert False, (
                            "call_llama(chat_type=...) has a default "
                            "value (kwonly). T53 #R4 design: chat_type "
                            "is REQUIRED (no default). If a default is "
                            "needed, the call sites must opt-in "
                            "explicitly, not via signature default."
                        )
                    # kwonly without default = required, pass
                    return
            assert False, (
                "call_llama() signature does not include chat_type. "
                "T53 #R3 + #R4: chat_type is a required kwarg. "
                "Add `chat_type: str` to the signature."
            )
    assert False, "call_llama() function not found in call_llama.py"
