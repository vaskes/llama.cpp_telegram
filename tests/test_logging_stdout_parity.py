"""TECH-DEBT #3 -- logging must be byte-identical to the print() it replaced.

The claim under test is narrow and it is the whole justification for a 429
call-site sweep: the operator's log file does not change. The container runs

    python -u bot.py 2>&1 | stdbuf -oL tee -a /var/log/bot/bot.log

and every runbook greps that file for '[storage]', '[dispatch]',
'[reanchor]', '[telemetry]'. A migration to logging that prepends a
timestamp, or that writes to a stream nobody is capturing, would be
technically successful and operationally a disaster.

So the tests below compare bytes, not properties.

The failure this file also exists to prevent is the one that bit during the
migration: logconf bound sys.stdout once, and pytest's capture teardown
closes it, so every later record raised ValueError inside logging.emit --
uncaught, and it took the whole interpreter down. That is not a capsys
assertion failing, it is the test session dying, and it would take
production down the same way if anything there ever closed stdout.
"""
import ast
import io
import logging
import subprocess
import sys
from pathlib import Path

import pytest

BOT_DIR = Path(__file__).resolve().parent.parent / "telegram-bot"
sys.path.insert(0, str(BOT_DIR))

import logconf  # noqa: E402

pytestmark = pytest.mark.tech_debt(
    item=3, reason="Pins TECH-DEBT #3: print() -> logging is byte-compatible")

# Every module that gained a logger in the sweep, and the levels they use.
CONVERTED = [
    "selftest", "dispatch", "handlers", "call_llama", "reanchor", "summarize",
    "personality", "rating", "config", "access", "turn", "context_budget",
    "storage", "limits", "chat_utils",
]

# Representative shapes: plain, f-string with !r, braces that a %-format
# would eat, unicode, and an embedded newline from a multi-line message.
MESSAGES = [
    "plain message",
    f"with a value {42} and a repr {'x'!r}",
    "literal braces {not_a_field} and a percent %s",
    "кириллица в логе",
    "multi\nline\nmessage",
    "",
]


def _fresh_root():
    """A root logger with nothing on it, so configure() really runs."""
    root = logging.getLogger()
    saved = root.handlers[:], root.level
    for h in root.handlers[:]:
        root.removeHandler(h)
    root.setLevel(logging.NOTSET)
    return root, saved


def _restore(root, saved):
    handlers, level = saved
    for h in root.handlers[:]:
        root.removeHandler(h)
    for h in handlers:
        root.addHandler(h)
    root.setLevel(level)


# --- byte equality ------------------------------------------------------

@pytest.mark.parametrize("msg", MESSAGES)
def test_logging_output_is_byte_identical_to_print(msg, capsys):
    root, saved = _fresh_root()
    try:
        logconf.configure()
        logging.getLogger("t.eq").info("%s", msg)
        logged = capsys.readouterr().out

        print(msg)
        printed = capsys.readouterr().out
    finally:
        _restore(root, saved)

    assert logged == printed, (
        f"logging wrote {logged!r}, print wrote {printed!r}. The log file "
        f"changes shape and every saved runbook grep stops matching."
    )


def test_format_has_no_timestamp_or_level_prefix():
    """The single most likely regression: someone 'improves' the format."""
    root, saved = _fresh_root()
    try:
        handler = logconf.configure()
        fmt = handler.formatter._fmt
    finally:
        _restore(root, saved)

    for forbidden in ("asctime", "levelname", "name", "module", "funcName",
                      "lineno", "threadName", "processName"):
        assert forbidden not in fmt, (
            f"log format {fmt!r} contains %({forbidden})s. print() wrote the "
            f"bare message; adding a prefix rewrites every line in "
            f"bot.log and breaks the greps in the runbooks."
        )
    assert fmt == "%(message)s"


def test_handler_writes_to_stdout_not_stderr(capsys):
    root, saved = _fresh_root()
    try:
        logconf.configure()
        logging.getLogger("t.stream").info("goes to stdout")
        cap = capsys.readouterr()
    finally:
        _restore(root, saved)

    assert "goes to stdout" in cap.out
    assert cap.err == "", f"log records leaked to stderr: {cap.err!r}"


# --- the closed-stdout failure -----------------------------------------

def test_writes_to_whichever_stdout_is_current(capsys):
    """The handler must resolve sys.stdout per record, not per process.

    This is the precise form of the closed-stdout defect, and it is what
    the subprocess test below cannot see: with the stream bound early, a
    record written after sys.stdout is swapped goes to the ORIGINAL stdout
    -- which in a test is pytest's capture, alive and healthy. Nothing
    raises, the process survives, and the bug is invisible.

    So assert the destination directly. A handler bound at configure time
    leaves `buf` empty and the text lands in capsys instead.
    """
    root, saved = _fresh_root()
    try:
        logconf.configure()
        buf = io.StringIO()
        real = sys.stdout
        sys.stdout = buf
        try:
            logging.getLogger("t.lazy").info("late binding")
        finally:
            sys.stdout = real
    finally:
        _restore(root, saved)

    assert "late binding" in buf.getvalue(), (
        f"the record did not reach the current sys.stdout (buf={buf.getvalue()!r}). "
        f"The handler is bound to a stream object captured at configure time, "
        f"so anything that swaps or closes stdout afterwards -- pytest capsys, "
        f"a daemonisation, a supervisor -- loses the log line silently."
    )


def test_survives_a_closed_stdout():
    """The defect the migration actually shipped with, and the one that
    crashed the interpreter during pytest teardown.

    Reproduced here as a subprocess, because in-process it would poison the
    rest of the session -- which is what it did.
    """
    # flush() deliberately does NOT raise. A real closed file refuses
    # writes; the first version of this fixture raised on flush too, and
    # that killed the interpreter at shutdown (exit 120) rather than
    # exercising the logging path -- the process died before any assertion
    # could mean anything.
    code = f"""
import io, logging, sys
sys.path.insert(0, {str(BOT_DIR)!r})


class Closed(io.StringIO):
    def write(self, s):
        raise ValueError('I/O operation on closed file')

    def flush(self):
        pass


import logconf
logconf.configure()
log = logconf.get_logger('t.dead')
log.info('before close')

real = sys.stdout
sys.stdout = Closed()
log.info('after close')          # must not raise
log.error('after close again')  # must not raise either

sys.stdout = real
log.info('recovered')
print('REACHED_END')
"""
    r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, timeout=60)
    assert r.returncode == 0, (
        f"a logging call on a closed stdout killed the process "
        f"(exit {r.returncode}).\n{r.stderr[-1500:]}"
    )
    assert "REACHED_END" in r.stdout, (
        f"execution stopped at the first closed-stdout log call.\n"
        f"stdout={r.stdout!r}\nstderr={r.stderr[-800:]!r}"
    )
    assert "recovered" in r.stdout, (
        f"the handler did not recover once stdout came back. print() would "
        f"have simply worked; a logging handler that latched onto the dead "
        f"stream never does.\nstdout={r.stdout!r}"
    )


def test_configure_is_idempotent():
    """Two modules importing logconf must not double every line."""
    root, saved = _fresh_root()
    try:
        logconf.configure()
        n1 = len(logging.getLogger().handlers)
        logconf.configure()
        logconf.configure()
        n2 = len(logging.getLogger().handlers)
    finally:
        _restore(root, saved)
    assert n1 == n2 == 1, (
        f"configure() added {n2 - 1} extra handlers; every line would be "
        f"written {n2} times into bot.log"
    )


def test_configure_raises_level_to_info():
    """print() had no levels and everything it wrote was visible. A root
    logger left at WARNING would silently swallow ~400 INFO lines."""
    root, saved = _fresh_root()
    try:
        root.setLevel(logging.WARNING)
        logconf.configure()
        level = logging.getLogger().level
    finally:
        _restore(root, saved)
    assert level <= logging.INFO, (
        f"root level is {level}; the converted INFO lines would vanish"
    )


def test_get_logger_configures_on_first_use_without_bot_py():
    """The silent failure mode the module exists to prevent.

    A fork running `python -c 'import handlers; ...'` never executes
    bot.py. If the handler were attached there, every INFO record would
    vanish into logging's lastResort.
    """
    root, saved = _fresh_root()
    try:
        log = logconf.get_logger("t.direct")
        assert any(getattr(h, "_tb_logconf", None)
                   for h in logging.getLogger().handlers), (
            "get_logger() returned a logger with no handler attached; a "
            "direct import would print nothing at INFO"
        )
        assert log.isEnabledFor(logging.INFO)
    finally:
        _restore(root, saved)


# --- structural completeness -------------------------------------------

def test_no_print_calls_remain_in_the_package():
    """AST, not grep: a docstring mentioning print() is not a call site.

    Using a word here would pass on turn.py's Turn docstring, which
    documents the print(...) contract it replaced.
    """
    offenders = {}
    for p in sorted(BOT_DIR.glob("*.py")):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        hits = [n.lineno for n in ast.walk(tree)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == "print"]
        if hits:
            offenders[p.name] = hits
    assert not offenders, (
        f"print() call sites left behind: {offenders}. Every one of them "
        f"bypasses levels, routing and the handler."
    )


def test_every_converted_module_takes_its_logger_from_logconf():
    for name in CONVERTED:
        p = BOT_DIR / f"{name}.py"
        assert p.exists(), f"{p} vanished"
        tree = ast.parse(p.read_text(encoding="utf-8"))
        assigns = [
            n for n in tree.body
            if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "log"
                    for t in n.targets)
        ]
        assert assigns, (
            f"{name}.py has no module-level `log = get_logger(__name__)`. "
            f"A converted call site there would raise NameError at runtime."
        )
        src = p.read_text(encoding="utf-8")
        assert "from logconf import get_logger" in src, (
            f"{name}.py defines `log` without importing get_logger"
        )


def test_no_print_keyword_reaches_a_logger():
    """flush=True has no meaning for logging and raises TypeError.

    logger._log() takes no `flush` keyword. The first sweep left
    `flush=True,` (trailing comma, so a regex for ", flush=True)" missed
    it) and took 20 tests down with TypeError.
    """
    bad = []
    for p in sorted(BOT_DIR.glob("*.py")):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("debug", "info", "warning",
                                           "error", "exception", "critical")):
                continue
            for k in node.keywords:
                if k.arg in ("flush", "file", "sep", "end"):
                    bad.append(f"{p.name}:{node.lineno} -> {k.arg}=")
    assert not bad, (
        f"print-only keyword passed to a logger: {bad}. logger.info() "
        f"accepts none of them and raises TypeError."
    )


def test_logconf_imports_only_stdlib():
    """Every module in the package imports logconf, so any package import
    here is a cycle waiting to happen. test_import_graph.py allows the
    edge downward; this pins it going that way only."""
    tree = ast.parse((BOT_DIR / "logconf.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            if node.level:                       # relative import
                imported.add("<relative>")
            elif node.module:
                imported.add(node.module.split(".")[0])
    allowed = {"logging", "sys"}
    extra = imported - allowed
    assert not extra, (
        f"logconf.py imports {sorted(extra)}; it must import only "
        f"{sorted(allowed)}. It is imported by every module in the package, "
        f"so a package-level import here is a cycle by construction."
    )


def test_stderr_logger_is_not_also_on_stdout(capsys):
    """access.py's structured JSON deliberately stays off stdout. If the
    handler propagated, a piped stdout would no longer be parseable alone.

    Asserted on the bytes, not on the propagate flag: an earlier version of
    logconf keyed the handler off `stream is sys.stderr` and gave every
    OTHER stream the stdout handler, so this line came out on stdout while
    propagate was correctly False. The flag was true and the claim false.
    """
    err = io.StringIO()
    logger = logconf.get_logger("t.errcheck.stderr", stream=err)
    logger.error("structured line")
    captured = capsys.readouterr()

    assert logger.propagate is False, (
        "the stderr logger propagates to root, so its records also land on "
        "stdout -- which is the thing access.py's comment says it must not do"
    )
    assert "structured line" not in captured.out, (
        f"get_logger(stream=...) wrote to stdout: {captured.out!r}. The "
        f"stream argument is being ignored."
    )
    assert "structured line" in err.getvalue(), (
        f"get_logger(stream=...) did not write to the stream it was given: "
        f"{err.getvalue()!r}"
    )


def test_real_stderr_logger_writes_to_stderr(capsys):
    """The actual call site: access.py passes sys.stderr."""
    err = io.StringIO()
    logger = logconf.get_logger("t.errcheck.real", stream=err)
    logger.error("only stderr")
    captured = capsys.readouterr()
    assert "only stderr" in err.getvalue()
    assert "only stderr" not in captured.out