"""T47 tests: prompts.py must not contain the T42 over-broad
'burn the turn' wording that caused the model to skip
whitelisted tool calls. The reframed version must:
  - say that whitelisted tools work normally
  - mention soft-reject (T43) instead of "refuse and burn"
  - keep the closed list of 6 whitelisted tools
"""
import pathlib

PROMPTS = pathlib.Path("/home/git/llama.cpp_telegram/telegram-bot/prompts.py")
src = PROMPTS.read_text()


def test_no_burn_the_turn():
    """T42 phrasing caused models to skip whitelisted tools."""
    assert "burn the turn" not in src, \
        "T42 over-broad 'burn the turn' phrasing still present"


def test_no_refuse_and_burn():
    assert "refuse it and burn" not in src


def test_whitelisted_tools_normal():
    """New T47 wording: 'USE THE TOOLS ABOVE NORMALLY.'"""
    assert "USE THE TOOLS ABOVE NORMALLY" in src or \
        "use the tools above normally" in src.lower(), \
        "T47 emphasis on whitelisted tools missing"


def test_soft_reject_mentioned():
    """T47 must reference the soft-reject mechanism so the
    model knows the turn is not aborted on forbidden calls."""
    assert "soft-reject" in src, "soft-reject mention missing"


def test_six_whitelisted_tools_listed():
    """All 6 tools from KNOWN_TOOL_NAMES must still be listed."""
    for tool in [
        "donsetch_web_search",
        "donsetch_web_fetch",
        "donsetch_web_crawl",
        "donsetch_web_screenshot",
        "react_to_message",
        "bash_command",
    ]:
        assert tool in src, f"whitelisted tool {tool} not in prompts"


def test_forbidden_tools_still_listed():
    """For reference, the model must know which names are
    forbidden (so it doesn't try them)."""
    for forbidden in ["exec_shell_command", "agent", "read_file", "write_file"]:
        assert forbidden in src, f"forbidden name {forbidden} not in prompts"


def test_no_dual_message_wording():
    """Sanity: the T42 phrase 'refuse it and burn the turn' must
    be gone. Check both lowercase variants since prompts.py is
    mixed-case."""
    lowered = src.lower()
    assert "burn the turn" not in lowered
    assert "refuse it and burn" not in lowered
