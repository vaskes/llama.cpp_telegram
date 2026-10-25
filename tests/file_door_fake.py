"""A fake sandbox file door, for the tests that need one.

The bot's half of the sandbox contract (`vaskes/OpenWebUI-bash-sandbox`,
tag `v2.7.1`) is HTTP: `GET /files/meta`, `GET /files`, `DELETE /files`,
all with a bearer token. The tests drive the real httpx stack through
`httpx.MockTransport`, so the code under test does its real streaming,
its real status handling and its real error mapping -- only the socket
is replaced.

What the fake reproduces, because the contract depends on it:

* the proxy's meta route answered **200 with `{"error": ...}`** when the
  agent refused the path -- that was the proxy's own behaviour in v2.7.0
  (see `openapi_proxy.py`, `file_meta`), and a client that only looks at
  the status code read it as a success with no metadata in it. v2.7.1
  relays the status instead, so the fake can answer either way
  (`quirk_meta`).
* a file over the limit comes back as **413**, and its detail names the
  real size -- the number the model needs.
* a file that is not there is a **404** on the download and on the
  delete, and the delete of a file that is not there is not a failure:
  "make sure this is gone" is satisfied.
* `DELETE` answers **204** when it removed something.
"""
from __future__ import annotations

import json
import os
import tempfile
from functools import partial

import httpx


class FakeSandbox:
    """The sandbox's file door, in memory."""

    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.limit = 0            # 0 means "no limit", like AGENT_BINARY_LIMIT
        self.requests: list[tuple[str, str, str]] = []
        self.tokens: list[str | None] = []
        self.delete_status = 204
        self.meta_body = None     # override the meta answer wholesale
        self.version = "2.7.1"    # what /api/config says
        self.has_door = True      # False: an old side-car, no routes
        # v2.7.0 answered 200 with {"error": ...} when the agent refused
        # the path; v2.7.1 relays the agent's status instead. The default
        # is the older shape, because that is the one a client has to
        # survive -- a test that wants the newer one says so.
        self.quirk_meta = True

    # -- what the tests look at ---------------------------------------

    def calls(self, method: str) -> list[tuple[str, str, str]]:
        return [r for r in self.requests if r[0] == method]

    def paths(self, method: str) -> list[str]:
        return [r[2] for r in self.calls(method)]

    # -- the routes ---------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        method = request.method
        route = request.url.path
        target = request.url.params.get("path", "")
        self.requests.append((method, route, target))
        self.tokens.append(request.headers.get("authorization"))

        if method == "DELETE":
            if self.delete_status != 204:
                return httpx.Response(self.delete_status,
                                      json={"detail": "could not delete"})
            if target not in self.files:
                return httpx.Response(404,
                                      json={"detail": f"file not found: {target}"})
            self.files.pop(target)
            return httpx.Response(204)

        if route.endswith("/api/config"):
            return httpx.Response(200, json={
                "name": "sandbox-agent",
                "version": self.version,
                "features": {"system": False},
            })

        if route.endswith("/files/meta"):
            if not self.has_door:
                return httpx.Response(404, json={"detail": "Not Found"})
            if self.meta_body is not None:
                return httpx.Response(200, json=self.meta_body)
            if target not in self.files:
                if not self.quirk_meta:
                    return httpx.Response(
                        404, json={"detail": f"file not found: {target}"})
                # The proxy answers 200 and puts the agent's error
                # document into "error" as a string.
                return httpx.Response(200, json={"error": json.dumps(
                    {"detail": f"file not found: {target}"})})
            data = self.files[target]
            return httpx.Response(200, json={
                "name": os.path.basename(target),
                "path": target,
                "size": len(data),
                "mtime": 0,
                "mime": "application/octet-stream",
                "limit": self.limit,
            })

        if target not in self.files:
            return httpx.Response(404, json={"detail": f"file not found: {target}"})
        data = self.files[target]
        if self.limit and len(data) > self.limit:
            return httpx.Response(413, json={"detail": (
                f"file is {len(data)} bytes, over the binary limit of "
                f"{self.limit} bytes")})
        return httpx.Response(200, content=data,
                              headers={"content-type": "application/octet-stream"})


def install(monkeypatch, sandbox: FakeSandbox, tmp_path) -> FakeSandbox:
    """Point the transport at the fake, and keep its temp files in tmp_path.

    The temp file matters: the courier writes the file it pulled to disk
    before it hands it over, and a test that cannot see that file cannot
    prove it was removed afterwards.
    """
    import personal_tools.file_transport as ft

    real_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(sandbox.handle)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    monkeypatch.setattr(ft, "AGENT_STACK_URL", "http://sandbox.test")
    monkeypatch.setattr(ft, "AGENT_STACK_TOKEN", "test-token")
    monkeypatch.setattr(ft.tempfile, "mkstemp",
                        partial(tempfile.mkstemp, dir=str(tmp_path)))
    return sandbox
