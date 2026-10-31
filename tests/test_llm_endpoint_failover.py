"""The bot must keep working when the engine moves to the other port.

The operator runs one engine instance at a time -- the two share the
GPUs -- and keeps the other one standing by. On 2026-10-31 the engine
fork changed, the engine moved to another port, and the bot was left
pointing at a port nobody listened on: it could not reach the engine at
all, and every turn was a "the engine is unavailable" sentence.

So the bot has to move with the engine. The rules are the operator's:

  * it does NOT probe which port is up. A probe per turn would double
    the traffic, and the answer would depend on which port happened to
    answer first -- so one turn could be served by a different model
    than the turn before it;
  * it uses the active endpoint, and only when that one FAILS does it
    try the other one;
  * the endpoint that answered becomes the one the bot keeps using;
  * when neither answers, the user gets the usual "the engine is
    unavailable" sentence.

These tests use REAL servers on 127.0.0.1: one that answers, one that
is dead (a port nobody listens on), and one that answers with an
error. A mock would not see the thing this is about -- which URL the
request actually went to, and how many requests were made.
"""
import asyncio
import contextlib
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

import llm_endpoint
from user_notice import ENGINE_UNREACHABLE_MESSAGE, engine_failure_message


def _answer(text: str = "ok") -> dict:
    return {"choices": [{"message": {"content": text},
                          "finish_reason": "stop"}]}


class _Handler(BaseHTTPRequestHandler):
    """Answers with whatever the server was configured to answer."""

    protocol_version = "HTTP/1.1"

    def _record(self):
        self.server.requests.append((self.command, self.path))
        return self.server.answer

    def _reply(self):
        status, body, delay = self._record()
        if delay:
            # The answer comes after the client gave up: this is the
            # "the engine is there but it does not answer" case.
            import time
            time.sleep(delay)
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        if getattr(self.server, "truncate", False):
            # The headers promise more than the body delivers, and then
            # the connection closes: the answer breaks half-way.
            self.send_header("Content-Length", str(len(payload) + 500))
            self.end_headers()
            self.wfile.write(payload[:5])
            self.close_connection = True
            return
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        self._reply()

    def do_GET(self):
        self._reply()

    def log_message(self, *args):  # keep the test output quiet
        pass


@contextlib.contextmanager
def _engine(status=200, body=None, delay=0.0):
    """A server that answers like an engine, and counts what it was asked."""
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.requests = []
    srv.answer = (status, body if body is not None else _answer(), delay)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}/v1", srv
    finally:
        srv.shutdown()
        srv.server_close()


@contextlib.contextmanager
def _dead_endpoint():
    """An endpoint nobody listens on: the port is taken and given back."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    yield f"http://127.0.0.1:{port}/v1"


@pytest.fixture(autouse=True)
def _restore_endpoints():
    """Every test starts and ends with the endpoints the env names."""
    llm_endpoint.reset_endpoints()
    yield
    llm_endpoint.reset_endpoints()


async def _post(url: str, timeout=5.0):
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
        return await llm_endpoint.engine_post(
            client, "/chat/completions", json={"model": "m", "messages": []})


async def test_the_active_endpoint_answers_and_the_other_is_not_asked():
    """The operator's rule: no probe. The other port is not even touched."""
    with _engine(body=_answer("first")) as (first, first_srv), \
            _engine(body=_answer("second")) as (second, second_srv):
        llm_endpoint.set_endpoints([first, second])
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await llm_endpoint.engine_post(
                client, "/chat/completions", json={"messages": []})
            assert response.json()["choices"][0]["message"]["content"] == "first"
        assert len(first_srv.requests) == 1, first_srv.requests
        assert second_srv.requests == [], (
            "the bot asked the other endpoint while the active one was "
            f"answering: {second_srv.requests}")


async def test_a_dead_endpoint_moves_the_bot_to_the_other_one():
    """The engine moved: the port that is gone must not stop the turn."""
    with _dead_endpoint() as dead, \
            _engine(body=_answer("alive")) as (alive, alive_srv):
        llm_endpoint.set_endpoints([dead, alive])
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await llm_endpoint.engine_post(
                client, "/chat/completions", json={"messages": []})
            assert response.json()["choices"][0]["message"]["content"] == "alive"
        assert len(alive_srv.requests) == 1, alive_srv.requests
        assert llm_endpoint.active_url() == alive


async def test_the_move_is_sticky():
    """After the move the bot keeps using the endpoint that answered."""
    with _dead_endpoint() as dead, \
            _engine(body=_answer("alive")) as (alive, alive_srv):
        llm_endpoint.set_endpoints([dead, alive])
        async with httpx.AsyncClient(timeout=5.0) as client:
            await llm_endpoint.engine_post(
                client, "/chat/completions", json={"messages": []})
            # The dead endpoint is not asked a second time.
            await llm_endpoint.engine_post(
                client, "/chat/completions", json={"messages": []})
        assert len(alive_srv.requests) == 2, alive_srv.requests
        assert llm_endpoint.active_url() == alive


async def test_the_bot_goes_back_when_the_new_endpoint_dies():
    """The other endpoint failed too: the bot returns to the first one.

    Three steps, on two real servers: the first one starts failing, so
    the bot moves to the second; the first one comes back; the second
    one starts failing -- and the bot goes back to the first.
    """
    with _engine(body=_answer("first")) as (first, first_srv), \
            _engine(body=_answer("second")) as (second, second_srv):
        llm_endpoint.set_endpoints([first, second])
        async with httpx.AsyncClient(timeout=5.0) as client:
            # 1. The first endpoint stops serving: the bot moves.
            first_srv.answer = (500, {"error": "boom"}, 0)
            response = await llm_endpoint.engine_post(
                client, "/chat/completions", json={"messages": []})
            assert response.json()["choices"][0]["message"]["content"] == "second"
            assert llm_endpoint.active_url() == second

            # 2. The first one serves again, the second one stops.
            first_srv.answer = (200, _answer("first"), 0)
            second_srv.answer = (500, {"error": "boom"}, 0)

            # 3. The bot goes back to the endpoint it came from.
            response = await llm_endpoint.engine_post(
                client, "/chat/completions", json={"messages": []})
            assert response.json()["choices"][0]["message"]["content"] == "first"
            assert llm_endpoint.active_url() == first
        assert len(first_srv.requests) == 2, first_srv.requests
        assert len(second_srv.requests) == 2, second_srv.requests


async def test_neither_endpoint_answers_gives_the_usual_sentence():
    """Both are down: the user gets the sentence, not a stack trace."""
    with _dead_endpoint() as first, _dead_endpoint() as second:
        llm_endpoint.set_endpoints([first, second])
        async with httpx.AsyncClient(timeout=5.0) as client:
            with pytest.raises(httpx.HTTPError) as caught:
                await llm_endpoint.engine_post(
                    client, "/chat/completions", json={"messages": []})
    assert llm_endpoint.endpoints_exhausted(caught.value) is True
    assert engine_failure_message(caught.value) == ENGINE_UNREACHABLE_MESSAGE


async def test_a_wrong_request_is_not_sent_twice():
    """A 4xx is the request's fault. The other endpoint would say the same."""
    with _engine(status=400, body={"error": "bad request"}) as (first, first_srv), \
            _engine(body=_answer("second")) as (second, second_srv):
        llm_endpoint.set_endpoints([first, second])
        async with httpx.AsyncClient(timeout=5.0) as client:
            with pytest.raises(httpx.HTTPStatusError):
                await llm_endpoint.engine_post(
                    client, "/chat/completions", json={"messages": []})
        assert len(first_srv.requests) == 1, first_srv.requests
        assert second_srv.requests == [], (
            "a request the engine refused was sent to the other endpoint "
            f"as well: {second_srv.requests}")
        assert llm_endpoint.endpoints_exhausted is not None


async def test_a_server_error_moves_the_bot():
    """A 5xx is the endpoint's fault: the other one is tried."""
    with _engine(status=500, body={"error": "boom"}) as (first, first_srv), \
            _engine(body=_answer("second")) as (second, second_srv):
        llm_endpoint.set_endpoints([first, second])
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await llm_endpoint.engine_post(
                client, "/chat/completions", json={"messages": []})
            assert response.json()["choices"][0]["message"]["content"] == "second"
        assert len(first_srv.requests) == 1, first_srv.requests
        assert len(second_srv.requests) == 1, second_srv.requests


async def test_a_timeout_moves_the_bot():
    """The engine is there but does not answer: that is a failure too."""
    with _engine(delay=5.0) as (slow, slow_srv), \
            _engine(body=_answer("second")) as (second, second_srv):
        llm_endpoint.set_endpoints([slow, second])
        async with httpx.AsyncClient(timeout=httpx.Timeout(0.5)) as client:
            response = await llm_endpoint.engine_post(
                client, "/chat/completions", json={"messages": []})
            assert response.json()["choices"][0]["message"]["content"] == "second"
        assert len(slow_srv.requests) == 1, slow_srv.requests
        assert len(second_srv.requests) == 1, second_srv.requests


async def test_a_single_endpoint_means_no_second_try():
    """Without a fallback there is nothing to try, and nothing is asked twice."""
    with _dead_endpoint() as dead:
        llm_endpoint.set_endpoints([dead])
        async with httpx.AsyncClient(timeout=5.0) as client:
            with pytest.raises(httpx.HTTPError):
                await llm_endpoint.engine_post(
                    client, "/chat/completions", json={"messages": []})


async def test_two_server_errors_are_not_the_same_as_two_dead_endpoints():
    """A 5xx is an answer. The engine is there and refused the request.

    That is a failure for the failover -- the other endpoint is tried --
    but it is NOT "nobody is there": the caller's own retry may still
    work, because a request the engine refused because of the tools it
    offered is answered when the tools are gone. The mark that skips
    that retry is only set when no endpoint answered at all.
    """
    with _engine(status=500, body={"error": "boom"}) as (first, first_srv), \
            _engine(status=500, body={"error": "boom"}) as (second, second_srv):
        llm_endpoint.set_endpoints([first, second])
        async with httpx.AsyncClient(timeout=5.0) as client:
            with pytest.raises(httpx.HTTPStatusError) as caught:
                await llm_endpoint.engine_post(
                    client, "/chat/completions", json={"messages": []})
        assert llm_endpoint.endpoints_exhausted(caught.value) is False, (
            "a 5xx is an answer; marking it as 'nobody is there' would "
            "swallow the tools-less retry that this failure can be "
            "answered by")
        assert len(first_srv.requests) == 1, first_srv.requests
        assert len(second_srv.requests) == 1, second_srv.requests


async def test_a_stream_that_breaks_is_not_retried():
    """The user has seen half the answer: a retry would give two answers.

    The failover lives around the REQUEST, not around the answer. A
    stream that breaks while it is being read is not an endpoint that
    stopped serving -- it is an answer that died on the way, and the
    other endpoint would be asked for a second half of a turn the user
    has already seen the first half of.
    """
    with _engine(body=_answer("half")) as (broken, broken_srv), \
            _engine(body=_answer("second")) as (second, second_srv):
        broken_srv.truncate = True
        llm_endpoint.set_endpoints([broken, second])
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await llm_endpoint.engine_post(
                client, "/chat/completions", json={"messages": []},
                stream=True)
            try:
                with pytest.raises(httpx.RemoteProtocolError):
                    async for _ in response.aiter_lines():
                        pass
            finally:
                await response.aclose()
        assert len(broken_srv.requests) == 1, broken_srv.requests
        assert second_srv.requests == [], (
            "a broken answer was asked of the other endpoint as well: "
            f"{second_srv.requests}")


async def test_the_endpoints_are_the_ones_the_environment_names():
    """The two knobs in the config are the two the bot uses."""
    import config
    llm_endpoint.reset_endpoints()
    expected = [u for u in (config.LLAMA_URL, config.LLAMA_URL_FALLBACK)
                if u]
    assert list(llm_endpoint.endpoints()) == [
        u.rstrip("/") for u in expected]


async def test_the_tools_are_asked_of_the_endpoint_that_answers():
    """The tool list is an engine call too: it must follow the failover."""
    with _dead_endpoint() as dead, \
            _engine(body=[{"tool": "read_file", "definition": {}}]) as (alive, alive_srv):
        llm_endpoint.set_endpoints([dead, alive])
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await llm_endpoint.engine_get(
                client, "/tools", at_server_root=True)
            assert response.json() == [{"tool": "read_file",
                                        "definition": {}}]
        assert alive_srv.requests == [("GET", "/tools")], alive_srv.requests
