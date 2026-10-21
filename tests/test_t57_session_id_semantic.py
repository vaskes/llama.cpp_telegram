"""T57 #S16: permanent tripwire for session_id semantic validation.

Mavis wave-5 found that the previous code:
    sid = args.get('session_id') or f'chat-{chat_id}'

let the LLM emit `session_id = "chat-{other_chat_id}"` and
forward a command into another chat's persistent tmux session.
Combined with a prompt-injection vector, that was a direct
PII / secret leak between private chats.

The fix: ALWAYS derive session_id from chat_id, never trust
the LLM. These AST-level tests pin that contract.

Why AST, not behavioural: the bash_command integration tests
already exercise the sandbox. The thing we want to catch is
a CODE-LEVEL regression where someone reintroduces
`args.get('session_id')` in the bash_command dispatch site.
A grep would work but is fragile; AST is robust to comments,
whitespace, and dict iteration order.
"""
import ast
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(REPO / "telegram-bot"))


def _parse_call_llama_bash_dispatch():
    """Find the bash_command dispatch site in call_llama.py and
    return the AST node where session_id is derived."""
    # Block D4 moved the bash dispatch into llm_dispatch.py. What this
    # protects did not move: the session id is never taken from the model.
    # One edit rather than two -- shifting the loop left costs a level of
    # indent in its body, and the intermediate state does not parse.
    from dispatch_location import iter_modules

    for _name, src in iter_modules():
        tree = ast.parse(src)
        for node in ast.walk(tree):
            # The dispatch site is a chain of `if/elif fn_name == "..."`
            # blocks. The compare is `Name == Constant`, so the string
            # is in test.comparators[0], not test.left.
            if isinstance(node, ast.If):
                test = node.test
                if (isinstance(test, ast.Compare)
                    and len(test.comparators) == 1
                    and isinstance(test.comparators[0], ast.Constant)
                    and test.comparators[0].value == "bash_command"):
                    return node
    raise AssertionError("bash_command dispatch site not found in "
                         "call_llama.py or llm_dispatch.py")


def test_session_id_never_uses_args_get():
    """The session_id derivation must NOT call args.get('session_id').
    Catches the wave-5 #S16 regression."""
    dispatch = _parse_call_llama_bash_dispatch()
    src = ast.unparse(dispatch)
    forbidden_patterns = [
        "args.get('session_id')",
        'args.get("session_id")',
        "args['session_id']",
        'args["session_id"]',
        "session_id=args.get(",
    ]
    for pattern in forbidden_patterns:
        assert pattern not in src, (
            f"T57 #S16: bash_command dispatch site must not use "
            f"LLM-supplied session_id. Found: {pattern!r}. "
            f"The fix: always derive session_id from chat_id."
        )


def test_session_id_uses_chat_id():
    """The session_id must reference chat_id."""
    dispatch = _parse_call_llama_bash_dispatch()
    src = ast.unparse(dispatch)
    # Block D4: the branch delegates to _execute_bash and chat_id is read
    # there. Followed through the module *source*, not by importing the
    # function -- an imported function is a function object, and
    # ast.unparse of one is a TypeError, not a result.
    if "_execute_bash" in src:
        from dispatch_location import source_of
        tree = ast.parse(source_of("llm_dispatch.py"))
        for node in tree.body:
            if isinstance(node, ast.AsyncFunctionDef) \
                    and node.name == "_execute_bash":
                src += "\n" + ast.unparse(node)
                break
        else:
            raise AssertionError(
                "the bash branch calls _execute_bash but the function "
                "is not defined in llm_dispatch.py")
    assert "chat_id" in src, (
        f"T57 #S16: session_id must be derived from chat_id. "
        f"Found dispatch site:\n{src[:500]}"
    )


def test_session_id_is_chat_dash_chat_id_format():
    """The session_id must follow chat-{chat_id} format and must
    not have an args.get fallback."""
    # Block D4: the derivation is a named constant plus a .format()
    # call now. Two facts instead of one adjacency -- the constant holds
    # the chat-scoped pattern, and something formats it from chat_id.
    # Asserting adjacency would have failed here because they are sixty
    # lines apart, which says nothing about whether either is correct.
    from dispatch_location import dispatch_source
    joined_src = dispatch_source()
    src = joined_src

    inline = re.compile(r"sid\s*=\s*[^\n]*chat_id[^\n]*", re.MULTILINE)
    for match in inline.findall(src):
        assert "args.get" not in match, (
            f"T57 #S16: sid assignment includes args.get fallback: "
            f"{match!r}. The fix: always derive session_id from chat_id."
        )

    derived = ('SESSION_ID_TEMPLATE = "chat-{chat_id}"' in src
               and ".format(chat_id=" in src)
    assert inline.search(src) or derived, (
        "no chat-bound session_id derivation found -- neither an inline "
        "assignment naming chat_id nor the constant formatted from it")

    if derived:
        assert not re.search(
            r"SESSION_ID[^\n]*args\.get\(['\"]session_id", src), (
            "the session id is derived from the template but the template "
            "is filled from the model's own arguments")
        # The original check here was `assert "f'" in match ...`, reading
        # the last regex hit: the value has to interpolate something. The
        # constant form interpolates chat_id inside the template instead,
        # which is asserted above, so there is no `match` left to read.
        assert "{chat_id}" in joined_src, (
            "the session id template does not interpolate chat_id")


def test_call_bash_command_validates_session_id_format():
    """Defence in depth: call_bash_command's _validate_session_id
    still rejects shell metacharacters in case the calling code
    is ever refactored. This is the T52 #S3 backstop."""
    from personal_tools.bash_command import _validate_session_id
    ok, _, _ = _validate_session_id("chat-12345")
    assert ok is True
    ok, _, _ = _validate_session_id("chat-12345; ls")
    assert ok is False
    ok, _, _ = _validate_session_id("../../etc/passwd")
    assert ok is False
    ok, _, _ = _validate_session_id(None)
    assert ok is True
