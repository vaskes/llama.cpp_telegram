"""Which engine the bot talks to, and what happens when it stops answering.

Two engine endpoints, and only one of them can be serving at a time: the
two instances share the GPUs, so the operator runs one and keeps the
other standing by. The bot therefore does NOT probe them. It uses the
active one, and only when that one FAILS does it try the other:

    the active endpoint answers   -> one request, and nothing else;
    the active endpoint fails     -> the other endpoint is tried once;
    the other endpoint answers    -> it becomes the active one, and stays;
    neither endpoint answers      -> the usual "engine unavailable" sentence.

The switch is sticky on purpose. A probe on every request would double
the traffic to the engine and add a failure mode of its own: the answer
would depend on which port happened to answer first, so one turn could
be served by a different model than the turn before it. The operator's
words: "не надо делать все время probe какой из портов доступен каждый
запрос".

What counts as "the endpoint failed" is deliberately narrow, because a
request that is wrong must not be sent twice:

  * a transport failure -- the connection was refused, timed out, or
    broke before the answer (httpx.TransportError covers ConnectError,
    ConnectTimeout, ReadTimeout and the rest);
  * a 5xx answer -- the endpoint is there, but it is not serving.

A 4xx answer is NOT an endpoint failure: the request itself is wrong,
and the other endpoint would say the same thing. Sending it again would
double the damage and hide the cause.

A 5xx is a failure for the failover, but it is still an ANSWER, and
that difference matters downstream. The mark that says "nobody is there"
is set only when NO endpoint answered at all: a request the engine
refused because of the tools it offered is answered when the tools are
gone, and that retry -- the one from v0.8.1 -- has to survive the
failover instead of being swallowed by it.

The body of a streamed answer is read OUTSIDE this module, so a stream
that breaks half-way is never retried: the user has already seen half
of the answer, and a second attempt would put two answers in the chat.

The endpoints come from the config: LLAMA_URL is the first one and
LLAMA_URL_FALLBACK is the second. An empty fallback means one endpoint,
and then this module does nothing but send the request.

A timeout is waited out to the bot's existing HTTP limit before the
second endpoint is tried. That limit is the turn budget (hours, on
purpose -- a long answer takes long), so a HUNG engine is a long wait
and not a quick switch. A stopped engine refuses the connection at once,
and that is the case this exists for.
"""
from __future__ import annotations

import contextlib
from urllib.parse import urlparse, urlunparse

import httpx
from config import LLAMA_URL, LLAMA_URL_FALLBACK
from http_clients import _abortable_post
from logconf import get_logger

log = get_logger(__name__)


def _build(urls) -> tuple[str, ...]:
    """The endpoints, cleaned and without duplicates, in the given order."""
    out: list[str] = []
    for raw in urls:
        url = (raw or "").strip().rstrip("/")
        if url and url not in out:
            out.append(url)
    return tuple(out)


#: The endpoints, in the order the operator wrote them. The first one is
#: the one the bot uses until it fails.
_ENDPOINTS = _build((LLAMA_URL, LLAMA_URL_FALLBACK))

#: Which endpoint is active right now. 0 is the first one.
_ACTIVE = 0


def endpoints() -> tuple[str, ...]:
    """The engine endpoints, in the order the operator wrote them."""
    return _ENDPOINTS


def active_url() -> str:
    """The endpoint the bot is talking to right now."""
    if not _ENDPOINTS:
        return ""
    index = _ACTIVE if 0 <= _ACTIVE < len(_ENDPOINTS) else 0
    return _ENDPOINTS[index]


def set_endpoints(urls) -> None:
    """Replace the endpoint list and go back to the first one.

    The tests use it to point the bot at two local servers. Production
    never calls it: the list comes from the environment.
    """
    global _ENDPOINTS, _ACTIVE
    _ENDPOINTS = _build(urls)
    _ACTIVE = 0


def reset_endpoints() -> None:
    """Back to the endpoints the environment names."""
    set_endpoints((LLAMA_URL, LLAMA_URL_FALLBACK))


def _candidates() -> list[str]:
    """The endpoints to try, in order: the active one, then the other."""
    if len(_ENDPOINTS) < 2:
        return list(_ENDPOINTS)
    index = _ACTIVE if 0 <= _ACTIVE < len(_ENDPOINTS) else 0
    return [_ENDPOINTS[index]] + [
        url for i, url in enumerate(_ENDPOINTS) if i != index]


def _activate(url: str) -> None:
    """Make `url` the active endpoint. No request, no probe."""
    global _ACTIVE
    try:
        index = list(_ENDPOINTS).index(url)
    except ValueError:
        return
    if index != _ACTIVE:
        log.info("[llm_endpoint] the engine moved to %s", url)
        _ACTIVE = index


def is_endpoint_failure(exc: BaseException) -> bool:
    """True when the failure means "this endpoint is not serving".

    A 4xx is not an endpoint failure: the request is wrong, and the
    other endpoint would refuse it the same way.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return isinstance(exc, httpx.TransportError)


def endpoints_exhausted(exc: BaseException) -> bool:
    """True when NO endpoint answered at all.

    The callers use it to skip their own retries: asking a dead engine
    again in another mode only makes the user wait twice as long.

    "Did not answer" is the whole meaning here. An endpoint that
    ANSWERED with a 5xx is serving -- it refused this request -- and the
    caller's own retry may still work (a request the engine refused
    because of the tools it offered is answered when the tools are
    gone). So a 5xx does not set this mark: the mark says "nobody is
    there", not "nobody liked this request".
    """
    return bool(getattr(exc, "engine_endpoints_failed", False))


def _server_root(url: str) -> str:
    """The endpoint without its /v1 -- where /tools and /models live."""
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    if path.endswith("/v1"):
        path = path.removesuffix("/v1")
    return urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


async def _request(client, method: str, path: str, *, abort_event=None,
                   stream: bool = False, at_server_root: bool = False,
                   **kwargs):
    """One request against the engine, with the failover.

    Returns the response, or None when the user pressed Stop (the
    non-streaming path only). Raises the failure that decided it when no
    endpoint answered -- and marks that exception, so the caller can
    tell "the engine is down" from "this one request was wrong".
    """
    candidates = _candidates()
    if not candidates:
        raise RuntimeError(
            "no engine endpoint is configured: LLAMA_URL is empty")
    last_exc: BaseException | None = None
    # Did any endpoint ANSWER? A 5xx is an answer: the engine is there
    # and refused this request. It is a failure for the failover (the
    # other endpoint is tried) but NOT the same thing as an endpoint
    # that is not there -- the caller's own retry may still work.
    answered = False
    for url in candidates:
        target = _server_root(url) if at_server_root else url
        try:
            if method == "GET":
                response = await client.get(f"{target}{path}", **kwargs)
            elif stream:
                request = client.build_request(
                    method, f"{target}{path}", **kwargs)
                response = await client.send(request, stream=True)
            else:
                response = await _abortable_post(
                    client, f"{target}{path}", abort_event=abort_event,
                    **kwargs)
                if response is None:
                    return None
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                if not is_endpoint_failure(exc):
                    # The request is wrong. The other endpoint would say
                    # the same, and the body of the answer is what says
                    # what was wrong -- so it is closed here and the
                    # error goes to the caller unchanged.
                    await response.aclose()
                    raise
                last_exc = exc
                answered = True
                await response.aclose()
                log.warning("[llm_endpoint] %s answered %s", url,
                            exc.response.status_code)
                continue
        except httpx.TransportError as exc:
            last_exc = exc
            log.warning("[llm_endpoint] %s did not answer: %s: %s", url,
                        type(exc).__name__, exc)
            continue
        _activate(url)
        return response
    # Nothing answered. The failure that decided it is the one the user
    # is told about, and it carries the mark that says every endpoint
    # was tried -- so nobody asks a dead engine again.
    if last_exc is None:  # pragma: no cover - the loop always sets it
        raise RuntimeError("no engine endpoint is configured")
    if not answered:
        try:
            last_exc.engine_endpoints_failed = True
        except AttributeError:  # pragma: no cover - no __dict__ to mark
            log.warning("[llm_endpoint] could not mark the failure: %r",
                        last_exc)
    raise last_exc


async def engine_post(client, path: str, *, abort_event=None,
                      stream: bool = False, **kwargs):
    """POST to the engine, failing over to the other endpoint.

    `path` is relative to the endpoint, e.g. "/chat/completions" for an
    endpoint of "http://127.0.0.1:8083/v1".
    """
    return await _request(client, "POST", path, abort_event=abort_event,
                          stream=stream, **kwargs)


async def engine_get(client, path: str, *, at_server_root: bool = False,
                     **kwargs):
    """GET from the engine, failing over to the other endpoint.

    `at_server_root` asks for the path next to the endpoint rather than
    under it: the engine serves /tools and /models beside /v1, not
    inside it.
    """
    return await _request(client, "GET", path,
                          at_server_root=at_server_root, **kwargs)


@contextlib.asynccontextmanager
async def engine_stream(client, path: str, **kwargs):
    """The streaming request, as an async context manager.

    The body is read INSIDE the block, so a stream that breaks while it
    is being read is not an endpoint failure: the user has already seen
    part of the answer, and asking the other endpoint would give them a
    second answer to the same turn.

    httpx's Response is not an async context manager itself (it has
    close()/aclose()), so the closing is done here.
    """
    response = await _request(client, "POST", path, stream=True, **kwargs)
    try:
        yield response
    finally:
        await response.aclose()
