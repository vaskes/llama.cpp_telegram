"""T45 tests: T41 edit_text fallback pattern applies to all 4
finally blocks."""
import ast
import pathlib


HANDLERS = pathlib.Path("/home/git/llama.cpp_telegram/telegram-bot/handlers.py")
src = HANDLERS.read_text()


def _find_function_body(name):
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _all_finally_blocks(fn):
    """Collect every `finally:` block in this function. There is
    usually only one, but handle_document has two (the file
    cleanup is its own try/finally with the thinking cleanup in
    a wider try/finally)."""
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


def _src_of(node):
    return ast.get_source_segment(src, node) or ""


def _all_finally_text(fn):
    return "\n".join(_src_of(stmt) for block in _all_finally_blocks(fn) for stmt in block)


def test_handle_text_finally_uses_t41_pattern():
    fn = _find_function_body("handle_text")
    assert fn is not None
    text = _all_finally_text(fn)
    assert "thinking.edit_text(bot_response)" in text
    assert "T45 handle_text" in text


def test_handle_voice_finally_uses_t41_pattern():
    fn = _find_function_body("handle_voice")
    assert fn is not None
    text = _all_finally_text(fn)
    assert "thinking.edit_text(bot_response)" in text
    assert "T41" in text or "T45" in text


def test_handle_photo_finally_uses_t41_pattern():
    fn = _find_function_body("handle_photo")
    assert fn is not None
    text = _all_finally_text(fn)
    assert "thinking.edit_text(bot_response)" in text
    assert "T45" in text


def test_handle_document_finally_uses_t41_pattern():
    fn = _find_function_body("handle_document")
    assert fn is not None
    text = _all_finally_text(fn)
    assert "thinking.edit_text(bot_response)" in text
    assert "T45 handle_document" in text


def test_handle_text_no_silent_pass():
    import re
    fn = _find_function_body("handle_text")
    assert fn is not None
    text = _all_finally_text(fn)
    bare_silent = re.search(
        r"try:\s*\n\s*await thinking\.delete\(\)\s*\n\s*except Exception:\s*\n\s*pass",
        text,
    )
    assert bare_silent is None, f"bare silent-pass delete still present: {bare_silent.group(0)}"


def test_handle_photo_no_silent_pass():
    import re
    fn = _find_function_body("handle_photo")
    assert fn is not None
    text = _all_finally_text(fn)
    bare_silent = re.search(
        r"try:\s*\n\s*await thinking\.delete\(\)\s*\n\s*except Exception:\s*\n\s*pass",
        text,
    )
    assert bare_silent is None, f"handle_photo still has bare silent-pass: {bare_silent.group(0)}"
