"""T44 tests: bash history block from executed_tools."""
import pytest
from handlers import _format_executed_tools


def test_empty():
    assert _format_executed_tools([]) == ""


def test_no_bash():
    assert _format_executed_tools([
        {"name": "react_to_message", "args": {"emoji": "ok"}},
    ]) == ""


def test_single_bash():
    out = _format_executed_tools([
        {"name": "bash_command", "args": {"command": "ls -l"}},
    ])
    assert "```bash" in out
    assert "$ ls -l" in out
    assert out.endswith("```")


def test_multiple_bash():
    out = _format_executed_tools([
        {"name": "bash_command", "args": {"command": "ls -l"}},
        {"name": "bash_command", "args": {"command": "pwd"}},
    ])
    assert "$ ls -l" in out
    assert "$ pwd" in out


def test_mixed_only_bash_rendered():
    out = _format_executed_tools([
        {"name": "bash_command", "args": {"command": "date"}},
        {"name": "donsetch_web_search", "args": {"q": "llama.cpp"}},
        {"name": "bash_command", "args": {"command": "uname -a"}},
    ])
    assert "$ date" in out
    assert "$ uname -a" in out
    assert "donsetch" not in out
    assert "llama.cpp" not in out


def test_empty_command_skipped():
    out = _format_executed_tools([
        {"name": "bash_command", "args": {}},
        {"name": "bash_command", "args": {"command": ""}},
        {"name": "bash_command", "args": {"command": "whoami"}},
    ])
    assert "$ whoami" in out
    assert out.count("\n$ ") == 1


def test_args_not_dict():
    out = _format_executed_tools([
        {"name": "bash_command", "args": "not a dict"},
        {"name": "bash_command", "args": {"command": "echo hi"}},
    ])
    assert "$ echo hi" in out
