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
    src = (REPO / "telegram-bot" / "call_llama.py").read_text()
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
    raise AssertionError("bash_command dispatch site not found in call_llama.py")


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
    assert "chat_id" in src, (
        f"T57 #S16: session_id must be derived from chat_id. "
        f"Found dispatch site:\n{src[:500]}"
    )


def test_session_id_is_chat_dash_chat_id_format():
    """The session_id must follow chat-{chat_id} format and must
    not have an args.get fallback."""
    src = (REPO / "telegram-bot" / "call_llama.py").read_text()
    pattern = re.compile(r"sid\s*=\s*[^\n]*chat_id[^\n]*", re.MULTILINE)
    matches = pattern.findall(src)
    assert matches, "no sid = ... chat_id ... assignment found"
    for match in matches:
        assert "args.get" not in match, (
            f"T57 #S16: sid assignment includes args.get fallback: "
            f"{match!r}. The fix: always derive session_id from chat_id."
        )
        assert "f'" in match or 'f"' in match or "{" in match, (
            f"T57 #S16: sid assignment doesn't use chat_id in the value: "
            f"{match!r}"
        )


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
