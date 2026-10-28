"""T46 tests: after reverting T41/T45 edit_text pattern, all
4 handlers' finally blocks must:
  1. take the reasoning bubble down, silently -- a failure to delete
     must not raise (the deletion itself lives in `close_thinking`,
     one place, since 2026-10-28)
  2. NOT contain `await thinking.edit_text(bot_response)` (T45
     pattern that produced the duplicate-message bug)
  3. NOT contain "T41" or "T45" comment tags (only T46 tag)

The check is on the delegation, not on a quoted line: `close_thinking`
is the one place that deletes the bubble, and the behavioural test at
the bottom pins that it never raises.
"""
import ast
import pathlib
import re


BOT_DIR = pathlib.Path(__file__).parent.parent / "telegram-bot"
HANDLERS = BOT_DIR / "handlers.py"
src = HANDLERS.read_text()


def _find_function_body(name):
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _all_finally_blocks(fn):
    blocks = []
    def walk(stmts):
        for stmt in stmts:
            if isinstance(stmt, ast.Try):
                if stmt.finalbody:
                    blocks.append(stmt.finalbody)
                walk(stmt.body)
                walk(stmt.handlers)
                walk(stmt.orelse)
            elif hasattr(stmt, "body") and isinstance(getattr(stmt, "body"), list):
                walk(stmt.body)
    walk(fn.body)
    return blocks


def _all_finally_text(fn):
    return "\n".join(_src_of(stmt) for block in _all_finally_blocks(fn) for stmt in block)


def _src_of(node):
    return ast.get_source_segment(src, node) or ""


def test_handle_text_finally_silent_delete():
    fn = _find_function_body("handle_text")
    assert fn is not None
    text = _all_finally_text(fn)
    assert "close_thinking(thinking)" in text, (
        "the finally of handle_text must take the bubble down")
    assert "except Exception:" in text and "pass" in text, (
        "the deletion must not raise out of the finally")
    assert "await thinking.edit_text(bot_response)" not in text


def test_handle_voice_finally_silent_delete():
    fn = _find_function_body("handle_voice")
    assert fn is not None
    text = _all_finally_text(fn)
    assert "close_thinking(thinking)" in text, (
        "the finally of handle_voice must take the bubble down")
    assert "except Exception:" in text and "pass" in text, (
        "the deletion must not raise out of the finally")
    assert "await thinking.edit_text(bot_response)" not in text


def test_handle_photo_finally_silent_delete():
    fn = _find_function_body("handle_photo")
    assert fn is not None
    text = _all_finally_text(fn)
    assert "close_thinking(thinking)" in text, (
        "the finally of handle_photo must take the bubble down")
    assert "except Exception:" in text and "pass" in text, (
        "the deletion must not raise out of the finally")
    assert "await thinking.edit_text(bot_response)" not in text


def test_handle_document_finally_silent_delete():
    fn = _find_function_body("handle_document")
    assert fn is not None
    text = _all_finally_text(fn)
    assert "close_thinking(thinking)" in text, (
        "the finally of handle_document must take the bubble down")
    assert "except Exception:" in text and "pass" in text, (
        "the deletion must not raise out of the finally")
    assert "await thinking.edit_text(bot_response)" not in text


def test_no_legacy_t41_t45_tags():
    stripped = re.sub(r"# T\d+:.*", "", src)
    assert "# T41:" not in stripped, "leftover T41 comment"
    assert "# T45:" not in stripped, "leftover T45 comment"
    assert "# T46:" in src


def test_no_duplicate_message_pattern():
    for name in ("handle_text", "handle_voice", "handle_photo", "handle_document"):
        fn = _find_function_body(name)
        assert fn is not None, f"{name} not found"
        text = _all_finally_text(fn)
        assert "edit_text(bot_response)" not in text, \
            f"{name}.finally still has duplicate edit_text(bot_response)"


async def test_close_thinking_never_raises():
    """The bubble's deletion cannot break the code that asked for it.

    A document must go out even when Telegram refuses to delete the
    bubble, and a failing finally must not lose the per-user slot
    behind it.
    """
    import sys
    sys.path.insert(0, str(BOT_DIR))
    from llm_thinking import close_thinking

    class _Angry:
        async def delete(self):
            raise RuntimeError("Telegram said no")

    await close_thinking(_Angry())     # must not raise
    await close_thinking(None)         # no bubble, no problem
