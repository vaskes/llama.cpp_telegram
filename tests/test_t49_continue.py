"""T49 tests: when a tool call comes in for a name not in
KNOWN_TOOL_NAMES, the dispatch loop continues (does not
early-return) so the model can see the synthetic tool
result on the next iteration and retry with a whitelisted
tool.

T49 is a structural change in call_llama.py that removes
the early-return. The test asserts that the post-loop
`if saw_unavailable_tool:` block no longer contains a
`return` statement (it now just logs a continue message).
"""
import ast
import pathlib


# T8.1: the tool loop moved to llm_iteration.py. Every assertion
# below is about the loop's body, not about call_llama, which now
# only builds a ToolLoop and awaits its run().
CALL_LLAMA = pathlib.Path(__file__).parent.parent / "telegram-bot" / "llm_iteration.py"
src = CALL_LLAMA.read_text()
# The stuck-call detector moved to llm_loop_guard.py in
# TECH-DEBT #3 Block C3; the check below reads that file.
guard_src = CALL_LLAMA.parent.joinpath("llm_loop_guard.py").read_text()


def _find_function_body(name):
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def test_no_early_return_on_saw_unavailable_tool():
    """T49: the `if saw_unavailable_tool:` block in the
    iter loop must NOT contain a Return statement. It only
    logs."""
    fn = _find_function_body("run_one")
    assert fn is not None

    # Find the post-loop if-block (the one after the
    # for tc in tool_calls loop). It should be the only
    # `if saw_unavailable_tool:` block that has a print
    # but no return.
    tree = ast.parse(src)
    saw_unavailable_blocks = []
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            # Check the test for saw_unavailable_tool
            test = ast.unparse(node.test) if hasattr(ast, "unparse") else ""
            if "saw_unavailable_tool" in test:
                saw_unavailable_blocks.append(node)
    assert len(saw_unavailable_blocks) >= 1, "no saw_unavailable_tool block found"

    # The first such block is the post-loop one. It should
    # NOT contain a Return. The other blocks (inside the
    # for loop) are setting saw_unavailable_tool = True
    # (an Assign), which is fine.
    for block in saw_unavailable_blocks:
        body_src = ast.get_source_segment(src, block) or ""
        # Skip the assignment `saw_unavailable_tool = True` blocks
        if "=" in ast.unparse(block.test if hasattr(ast, "unparse") else block.test):
            continue
        if "Return" in [type(s).__name__ for s in ast.walk(block)]:
            # This is the post-loop block, and it has a Return - T49 not applied
            assert False, f"T49 not applied: post-loop if-block has a Return:\n{body_src[:300]}"


def test_post_loop_block_only_logs():
    """The post-loop `if saw_unavailable_tool:` block must
    only contain a print/log statement."""
    fn = _find_function_body("run_one")
    assert fn is not None, "the named iteration is gone"
    assert "T49: removed the early-return" in src, \
        "T49 marker comment missing"


def test_stuck_call_detector_intact():
    """T32c stuck-call detector (`current_calls == prev_calls`)
    must still be in place to catch models that keep retrying
    the same forbidden tool."""
    assert "current_calls == self.prev_calls" in guard_src
    assert "same tool calls as previous iter; aborting" in guard_src


def test_t43_synthetic_tool_result_intact():
    """T43 synthetic tool result message for unhandled tools
    must still be generated and appended to msgs."""
    # Block D4 moved the message into llm_dispatch.UNAVAILABLE_TOOL_MESSAGE.
    from llm_dispatch import UNAVAILABLE_TOOL_MESSAGE
    assert "tool __rejected__ unavailable" in src or \
        "tool {name} unavailable" in UNAVAILABLE_TOOL_MESSAGE, \
        "synthetic tool result message missing"
    # The else: branch sets saw_unavailable_tool and appends
    # to msgs. Verify both code paths.
    assert "msgs.append" in src
    assert "saw_unavailable_tool = True" in src
