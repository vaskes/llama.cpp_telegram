"""TECH-DEBT #3 -- the logging configuration.

Why a dedicated module and not logging.basicConfig() at the top of bot.py:
because the failure mode is silent. A module imported directly -- a fork's
script, a test, `python -c "import handlers"` -- never runs bot.py, so no
handler is ever attached, and logging falls back to lastResort: WARNING and
above to stderr, and every INFO vanishes. print() never had that cliff.
get_logger() closes it by configuring on first use.

Why format='%(message)s' and not a timestamp/level format: the container
runs

    python -u bot.py 2>&1 | stdbuf -oL tee -a /var/log/bot/bot.log

and every operator runbook greps that file for '[storage]', '[dispatch]',
'[reanchor]' and friends. A '%(asctime)s %(levelname)s' prefix would not
drop those substrings, but it would change every line's prefix and break
every saved grep. print() wrote the bare message, so logging writes the
bare message. What the migration buys is levels, routing and per-call-site
metadata -- not a reformat of the operator's log file.

stdout, not stderr: print() wrote to stdout. The container merges 2>&1 so
either stream reaches the file, but stdout keeps the byte stream identical
for anyone capturing it without redirection.

Level INFO: print() has no levels, and INFO is the total equivalent --
everything that used to be visible stays visible. Sites that really are
warnings are promoted at their call site.
"""
import logging
import sys

_TAG = "_tb_logconf"


class _LazyStreamHandler(logging.StreamHandler):
    """Writes to whatever the stream is AT EMIT TIME.

    StreamHandler(stream=sys.stdout) binds the object once. pytest's capsys
    and its capture teardown both replace and close sys.stdout, so the bound
    stream is a dead file and every later record raises

        ValueError: I/O operation on closed file

    inside logging.emit. That is not caught by StreamHandler unless the
    write is inside its try -- it is, but handleError then re-raises if
    raiseExceptions is on, and during interpreter shutdown it takes the
    whole process down. This bit during the migration: the pytest session
    died with exit 120 after every test had already passed.

    print() never had the cliff because it looked sys.stdout up on every
    call. Restoring that is most of what byte-compatibility means here.

    handleError is a no-op for the same reason: a broken pipe must cost one
    line, not the process.
    """

    def __init__(self, resolve):
        self._resolve = resolve
        super().__init__(stream=None)

    @property
    def stream(self):
        return self._resolve()

    @stream.setter
    def stream(self, value):
        pass                      # StreamHandler.__init__ assigns; ignore it

    def handleError(self, record):
        pass


def _ensure(resolve, key: str) -> logging.Handler:
    """Attach one handler per stream, exactly once.

    Idempotent by inspection rather than by a module global: pytest installs
    its own handlers on the root logger, and under it this is correctly a
    no-op so caplog sees the records. A blanket "already ran" flag would
    defeat that.
    """
    for h in logging.getLogger().handlers:
        if getattr(h, _TAG, None) == key:
            return h
    handler = _LazyStreamHandler(resolve)
    handler.setFormatter(logging.Formatter("%(message)s"))
    setattr(handler, _TAG, key)
    return handler


def configure() -> logging.Handler:
    """Attach the stdout handler to the root logger. Returns it."""
    handler = _ensure(lambda: sys.stdout, "stdout")
    root = logging.getLogger()
    if handler not in root.handlers:
        root.addHandler(handler)
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)
    return handler


def get_logger(name: str, stream=None) -> logging.Logger:
    """The logger every telegram-bot module uses.

    configure() runs here rather than at logconf's import time because all
    fifteen modules import this module, and doing the work at import would
    fire before the caller had a chance to install its own handlers.

    stream=sys.stderr returns a logger whose records go to stderr and
    nowhere else. Exactly one call site wants that: access.py's structured
    JSON for a message the user lost outright, whose comment explains that
    it stays off stdout so a piped stdout can be parsed on its own.
    """
    if stream is None or stream is sys.stdout:
        configure()
        return logging.getLogger(name)

    target = stream
    handler = _ensure((lambda t=target: t), f"stream:{id(target)}")
    logger = logging.getLogger(name)
    if handler not in logger.handlers:
        logger.addHandler(handler)
        logger.propagate = False        # do not also print it to stdout
    return logger


# logconf must stay a leaf: every module in telegram-bot/ imports it, so an
# import from here into the package is an import cycle by construction.
# Pinned by tests/test_logging_stdout_parity.py::test_logconf_imports_only_stdlib.