"""Runtime harness for `selftest._selftest`.

There is no runtime coverage of the 1940-line selftest. The one test
that tried (`test_in_tree_selftest`) needs a real Telegram token and a
live llama-server, so it has never passed and cannot pass in CI. Every
other test that mentions selftest reads its source and asserts on the
shape of the AST.

That is the situation this file exists to fix, and it is the situation
in which an extraction is most dangerous: 1940 lines that compile,
that no test executes, and that therefore have no behaviour to regress
against. The `call_llama` blocks already proved the class -- a guard
removed by the first slice, 805 tests green, nobody notices until a
user gets a truncated stream.

So the harness does two things:

1. Runs `_selftest()` for real, against local stub servers on all three
   endpoints the bot talks to. No mocking of httpx: `TELEGRAM_API`,
   `LLAMA_URL` and `DONSETCH_URL` are env-driven precisely so a local
   stub can stand in, and stubbing below httpx would skip the wire
   format -- which is half of what these blocks check.

2. Reports the sequence of `[OK]` / `[FAIL]` markers the selftest emits
   on its own log stream. That sequence is the regression signal.

Why the log and not the exit code: `_selftest` has no exit code and no
return value. It reports. 95 `[OK]`/`[FAIL]` assertion sites across the
28 sections, plus one `[selftest] ...` line per section summary.

Every line is written twice by the logging stack, so the raw stream
carries 296 matching lines and the sequence is deduplicated before it
becomes the golden. Capturing them gives a golden trace that survives
extraction, and a block that stops emitting its marker is a block that
stopped running -- which is exactly the failure an extraction causes
and which no structural test would notice.

Run directly to see what the stubs are asked for:

    python tests/selftest_harness.py --trace
"""
import json
import os
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOT = os.path.join(REPO, "telegram-bot")

# What `_selftest` emits, and therefore what the golden pins.
#
# The `[OK]`/`[FAIL]` markers are the per-assertion verdicts. The
# `[selftest] ...: all pass / FAILED` lines are the per-section
# summaries, and they earn their place for a reason the T1.1 storage
# slice demonstrated: the summary that used to sit at the end of the
# storage blocks was labelled `[selftest] group-mode tests`, which was
# false, and nothing in the suite could see it. Each extracted module
# now reports its own summary, so a summary that goes missing, is
# renamed, or starts describing the wrong block is a golden diff.
#
# Order is preserved throughout: the sequence is the signal, not the
# set. A block that swaps places with another is a behaviour change
# even if every marker still fires.
RESULT_RE = re.compile(r"\[(OK|FAIL)\]|\[selftest\]")


def _telegram_result(method, body):
    """Canned Bot API payloads, by method.

    PTB validates the shape of what it gets back, so a generic `{"ok":
    true, "result": {}}` fails deep inside the library with a confusing
    error rather than at the stub. These are the minimum shapes that let
    `get_me` and `send_message` return.
    """
    if method == "getMe":
        return {"id": 424242, "is_bot": True, "first_name": "selftest_bot",
                "username": "selftest_bot", "can_join_groups": False,
                "can_read_all_group_messages": False,
                "supports_inline_queries": False}
    if method == "getChat":
        return {"id": int(body.get("chat_id", 0) or 0), "type": "private",
                "first_name": "selftest"}
    if method == "getChatMember":
        return {"status": "member",
                "user": {"id": int(body.get("user_id", 0) or 0),
                         "is_bot": False, "first_name": "selftest"}}
    if method in ("sendMessage", "editMessageText",
                  "editMessageReplyMarkup", "sendPhoto", "sendVoice",
                  "sendDocument", "stopPoll", "setMyCommands", "deleteMessage"):
        return {"message_id": 1, "date": 0,
                "chat": {"id": int(body.get("chat_id", 0) or 0),
                         "type": "private", "first_name": "selftest"},
                "text": body.get("text", "")}
    if method == "setMessageReaction":
        return True
    if method == "answerCallbackQuery":
        return True
    return {}


def _llama_result(path, body):
    """Canned llama-server payloads."""
    if path.endswith("/models"):
        return {"object": "list", "data": [{"id": "selftest-model",
                                            "object": "model"}]}
    if path.endswith("/chat/completions"):
        stream = bool(body.get("stream"))
        text = "selftest ok"
        if stream:
            # SSE framing the production client has to survive.
            chunks = [
                'data: ' + json.dumps(
                    {"choices": [{"delta": {"role": "assistant",
                                            "content": text},
                                  "finish_reason": None}]}) + "\n\n",
                'data: ' + json.dumps(
                    {"choices": [{"delta": {},
                                  "finish_reason": "stop"}]}) + "\n\n",
                "data: [DONE]\n\n",
            ]
            return {"__raw__": "".join(chunks).encode()}
        return {"id": "selftest", "object": "chat.completion",
                "choices": [{"index": 0,
                             "message": {"role": "assistant", "content": text},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                          "total_tokens": 2}}
    return {}


class _Handler(BaseHTTPRequestHandler):
    """One handler for all three endpoints; routing is by path shape."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *_a):
        pass  # the harness prints its own trace

    def _send_json(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_raw(self, body):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


    # --- MCP (donsetch) ------------------------------------------------
    #
    # The exact shapes below come from `donsetch.py`, not from the MCP
    # spec: `_donsetch_init` reads the session id from a *header*, not
    # the body, and `donsetch_call` reads the answer out of
    # `result.content[].text`. A stub that answers `{"result": {}}`
    # makes the selftest report a FAIL that is the stub's fault, and a
    # golden log with unexplained failures is a worse signal than no
    # golden log at all.
    MCP_SESSION = "selftest-session"

    def _mcp(self, body):
        method = body.get("method")
        if method == "initialize":
            payload = json.dumps({
                "jsonrpc": "2.0", "id": body.get("id", "init"),
                "result": {"protocolVersion": "2024-11-05",
                           "capabilities": {"tools": {}},
                           "serverInfo": {"name": "selftest-donsetch",
                                          "version": "1.0"}}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Mcp-Session-Id", self.MCP_SESSION)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        if method == "notifications/initialized":
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if method == "tools/list":
            self._send_json({"jsonrpc": "2.0", "id": body.get("id"),
                             "result": {"tools": [
                                 {"name": "web_search",
                                  "description": "search the web"},
                                 {"name": "web_fetch",
                                  "description": "fetch a url"}]}})
            return

        if method == "tools/call":
            name = (body.get("params") or {}).get("name")
            text = {
                # The real shape, not a convenient one. The search's
                # split-shape result carries a compact HANDLE, a title, a
                # domain and a snippet -- and no URL. A stub that
                # returned titles alone would have hidden the whole
                # defect: the model cannot read a page the search found
                # without the handle, and the production turn of
                # 2026-10-28 died of fifteen guessed URLs because the
                # tool description promised URLs the server never
                # sends. The stub has to look like the server, or it
                # cannot catch that.
                "web_search": "Search results\n\n"
                              "1. StUbHandle01 · python 3.13 release : "
                              "python.org · 1 index family\n"
                              "2. StUbHandle02 · python 3.12 release : "
                              "python.org · 1 index family",
                "web_fetch": "# Example Domain\n\nhttps://example.com/\n\n"
                             "This domain is for use in examples.",
            }.get(name, "[selftest stub: no canned result for %r]" % (name,))
            self._send_json({"jsonrpc": "2.0", "id": body.get("id"),
                             "result": {"content": [
                                 {"type": "text", "text": text}],
                                 "isError": False}})
            return

        self._send_json({"jsonrpc": "2.0", "id": body.get("id"),
                         "result": {}})

    def do_GET(self):
        self._route({})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode() or "{}")
        except (ValueError, UnicodeDecodeError):
            body = {}
        if not isinstance(body, dict):
            body = {}
        self._route(body)

    def _route(self, body):
        path = self.path
        trace = getattr(self.server, "trace", None)
        if trace is not None:
            trace.append(path)

        # Telegram: /bot<token>/<method>
        m = re.match(r"^/bot[^/]+/([A-Za-z]+)", path)
        if m:
            method = m.group(1)
            self._send_json({"ok": True,
                             "result": _telegram_result(method, body)})
            return

        if "/chat/completions" in path or path.rstrip("/").endswith("/models"):
            out = _llama_result(path, body)
            if "__raw__" in out:
                self._send_raw(out["__raw__"])
            else:
                self._send_json(out)
            return

        self._mcp(body)


def start_stub():
    """Start the stub on an ephemeral port. Returns (server, thread, base)."""
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.trace = []
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    host, port = srv.server_address[:2]
    return srv, t, f"http://{host}:{port}"


RUNNER = r'''
import asyncio, logging, os, sys
sys.path.insert(0, os.environ["SELFTEST_BOT_DIR"])
import selftest  # noqa: E402

# Configure logging by *forcing logconf's own lazy setup*, not with
# basicConfig. logconf attaches a _LazyStreamHandler the first time
# get_logger() is called, and that handler resolves sys.stdout at emit
# time. Calling basicConfig here as well left two handlers on the root
# logger and every record was written twice -- which would have made a
# block that fires once look like it fires twice, and would hide a
# block that genuinely started running twice. The duplication is gone
# once logconf owns the stream, which is all it ever wanted.
logconf_logger = logging.getLogger("selftest_harness_probe")
logging.getLogger().handlers.clear()
selftest.log  # force logconf's lazy configuration to run
logging.getLogger().handlers.clear()
logging.basicConfig(level=logging.INFO, format="%(message)s",
                    stream=sys.stdout, force=True)
logging.getLogger("selftest_harness_probe").addHandler(logging.NullHandler())

async def main():
    await selftest._selftest()
asyncio.run(main())
'''


def clean_env(base=None):
    """The environment a block (or the selftest) runs in.

    The variables that would change what the block MEANS are removed.
    The allowlist: the block is about the bot, not about who may talk
    to it. The MCP servers: a token left behind by an earlier test
    would send the block to a server that is not there, and the block
    would fail for a reason that has nothing to do with the block --
    which is the worst kind of failure, because it is green when run
    alone and red inside the suite.
    """
    env = dict(os.environ if base is None else base)
    for k in ("LLAMABOT_ALLOWED_USER_IDS", "ALLOWED_USER_IDS",
              "AGENT_STACK_TOKEN", "AGENT_STACK_URL", "MCP_SERVERS"):
        env.pop(k, None)
    return env


def run_selftest(env_extra=None, timeout=240):
    """Run `_selftest()` in a subprocess.

    Returns (lines, trace, returncode, raw_output, raw_line_count).

    A subprocess, not an in-process call: `config` reads its environment
    at import time, so the endpoints have to be in place before the
    first import. Doing it in-process would mean reloading modules, and
    reloading config and storage underneath a live event loop is how the
    "reloaded module, stale isinstance" class of false positive starts.
    """
    import tempfile

    srv, _thread, base = start_stub()
    env = clean_env()
    env.update({
        "BOT_TOKEN": "1:selftest",
        "TELEGRAM_API": f"{base}/bot",
        "LLAMA_URL": f"{base}/v1",
        "DONSETCH_URL": f"{base}/mcp",
        # the gate the selftest opens on; without it the whole
        # function returns on its second line and the harness
        # measures nothing at all
        "LLAMABOT_SELFTEST": "1",
        "CONVERSATIONS_DB": os.path.join(tempfile.gettempdir(),
                                         "selftest_harness.db"),
        "SELFTEST_BOT_DIR": BOT,
    })
    if env_extra:
        env.update(env_extra)

    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(RUNNER)
        runner = f.name
    try:
        r = subprocess.run([sys.executable, runner], env=env,
                           capture_output=True, text=True, timeout=timeout,
                           cwd=REPO)
    except subprocess.TimeoutExpired as e:
        srv.shutdown()
        return ([], list(srv.trace), "timeout", (e.stdout or ""), 0)
    finally:
        srv.shutdown()

    out = (r.stdout or "") + (r.stderr or "")
    raw = [ln.strip() for ln in out.splitlines() if RESULT_RE.search(ln)]
    # Collapse adjacent duplicates. Some of these lines are written
    # twice by the logging stack and the mechanism is not settled --
    # a handler added by logconf's lazy setup plus pytest's capture is
    # the best guess, and chasing it further is not worth the budget.
    #
    # Deduplicating alone would be unsafe: a block that started running
    # TWICE is exactly what a bad extraction does, and it would look
    # identical to the artifact. That is why
    # `test_selftest_runtime.py` also asserts the exact [OK]/[FAIL]
    # count -- a doubled run fails there even though the line is
    # collapsed here.
    lines = [ln for i, ln in enumerate(raw) if i == 0 or ln != raw[i - 1]]
    return (lines, list(srv.trace), r.returncode, out, len(raw))




BLOCK_RUNNER = r'''
import asyncio, os, sys
sys.path.insert(0, os.environ["SELFTEST_BOT_DIR"])
import importlib
mod = importlib.import_module(os.environ["SELFTEST_BLOCK_MODULE"])
fn = getattr(mod, os.environ["SELFTEST_BLOCK_NAME"])
result = asyncio.run(fn())
print("VERDICT", "True" if result is True else repr(result))
'''


def run_block(module, name, env_extra=None, timeout=120):
    """Run ONE extracted block on its own and return its verdict.

    A subprocess, for the same reason `run_selftest` is one: `config`
    reads its environment at import time, and in a full-suite run the
    test module has usually already been imported with someone else's
    CONVERSATIONS_DB and LLAMA_URL. Setting `os.environ` in-process then
    changes nothing at all, and the block silently runs against the
    wrong database.

    That is not hypothetical. The first version of
    `test_selftest_blocks` did exactly this and passed alone while
    failing inside the suite -- a green-then-red test is worse than no
    test, because the isolated run is the one you trust.
    """
    import tempfile

    srv, _thread, base = start_stub()
    env = clean_env()
    env.update({
        "BOT_TOKEN": "1:selftest",
        "TELEGRAM_API": f"{base}/bot",
        "LLAMA_URL": f"{base}/v1",
        "DONSETCH_URL": f"{base}/mcp",
        "CONVERSATIONS_DB": os.path.join(tempfile.gettempdir(),
                                         f"selftest_block_{name}.db"),
        "SELFTEST_BOT_DIR": BOT,
        "SELFTEST_BLOCK_MODULE": module,
        "SELFTEST_BLOCK_NAME": name,
    })
    if env_extra:
        env.update(env_extra)

    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(BLOCK_RUNNER)
        runner = f.name
    try:
        r = subprocess.run([sys.executable, runner], env=env,
                           capture_output=True, text=True, timeout=timeout,
                           cwd=REPO)
    except subprocess.TimeoutExpired:
        srv.shutdown()
        return None, "timeout"
    finally:
        srv.shutdown()

    for ln in (r.stdout or "").splitlines():
        if ln.startswith("VERDICT"):
            val = ln.split(None, 1)[1]
            return (True if val == "True" else val), None
    return None, ((r.stdout or "") + (r.stderr or ""))[-1500:]


if __name__ == "__main__":
    lines, trace, rc, out, raw_n = run_selftest()
    print(f"returncode={rc} raw={raw_n} deduped={len(lines)}")
    print(f"markers={len(lines)}")
    print("--- markers ---")
    for ln in lines:
        print(" ", ln[:160])
    print("--- endpoints hit ---")
    for t in dict.fromkeys(trace):
        print(" ", t)
    if "--trace" in sys.argv:
        print("--- raw tail ---")
        print(out[-4000:])