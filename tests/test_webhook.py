"""Tests for webhook.py: auth, body cap, safe_dispatch, auto-register.

The webhook module is the entry point for Telegram updates in
webhook mode. It must:
1. Verify the X-Telegram-Bot-Api-Secret-Token header (constant-time)
2. Cap body size to 1 MB (defense in depth)
3. Wrap dispatch in _safe_dispatch so handler exceptions are
   logged, not silently swallowed
4. Parse the JSON body to a Telegram Update object
5. Auto-register itself on startup if WEBHOOK_URL is set
   (idempotent, JSON body, allowed_updates includes callback_query)
"""
import asyncio
import os
import json as _json
import urllib.error
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

# conftest.py should set up sys.path and env


def _make_request(headers, body=b"", content_length=None):
    """Build a minimal aiohttp.web.Request mock for _check_secret."""
    import aiohttp.web
    request = MagicMock(spec=aiohttp.web.Request)
    request.headers.get = lambda name, default="": headers.get(name, default)
    request.remote = "127.0.0.1"
    if content_length is not None:
        request.content_length = content_length
    else:
        request.content_length = len(body)
    # Async read
    async def read():
        return body
    request.read = read
    return request


# === _check_secret tests (already in test_selftest.py but re-stated for completeness) ===

def test_check_secret_accepts_valid_token(monkeypatch):
    import config as _cfg
    monkeypatch.setattr(_cfg, "WEBHOOK_SECRET_TOKEN", "deadbeef")
    import webhook
    req = _make_request({"X-Telegram-Bot-Api-Secret-Token": "deadbeef"})
    assert webhook._check_secret(req) is True


def test_check_secret_rejects_invalid_token(monkeypatch):
    import config as _cfg
    monkeypatch.setattr(_cfg, "WEBHOOK_SECRET_TOKEN", "deadbeef")
    import webhook
    req = _make_request({"X-Telegram-Bot-Api-Secret-Token": "wrong"})
    assert webhook._check_secret(req) is False


def test_check_secret_rejects_missing_token(monkeypatch):
    import config as _cfg
    monkeypatch.setattr(_cfg, "WEBHOOK_SECRET_TOKEN", "deadbeef")
    import webhook
    req = _make_request({})
    assert webhook._check_secret(req) is False


def test_check_secret_rejects_when_unconfigured(monkeypatch):
    import config as _cfg
    monkeypatch.setattr(_cfg, "WEBHOOK_SECRET_TOKEN", "")
    import webhook
    req = _make_request({"X-Telegram-Bot-Api-Secret-Token": "anything"})
    assert webhook._check_secret(req) is False


# === _handle body size cap (413) ===

@pytest.mark.asyncio
async def test_handle_oversized_body_returns_413(monkeypatch):
    """v0.9.2: a body larger than 1 MB should be rejected with 413, not OOM."""
    import config as _cfg
    monkeypatch.setattr(_cfg, "WEBHOOK_SECRET_TOKEN", "secret")
    import webhook
    # 2 MB body
    big = b"x" * (2 * 1024 * 1024)
    req = _make_request(
        {"X-Telegram-Bot-Api-Secret-Token": "secret"},
        body=big,
        content_length=len(big),
    )
    resp = await webhook._handle(req)
    assert resp.status == 413, f"expected 413, got {resp.status}"
    assert b"too large" in resp.body.lower()


@pytest.mark.asyncio
async def test_handle_oversized_via_content_length_returns_413(monkeypatch):
    """v0.9.2: even if the body isn't read yet, a Content-Length header
    > 1 MB should be rejected at the check."""
    import config as _cfg
    monkeypatch.setattr(_cfg, "WEBHOOK_SECRET_TOKEN", "secret")
    import webhook
    req = _make_request(
        {"X-Telegram-Bot-Api-Secret-Token": "secret"},
        body=b"",
        content_length=2 * 1024 * 1024,  # 2 MB declared
    )
    resp = await webhook._handle(req)
    assert resp.status == 413


@pytest.mark.asyncio
async def test_handle_normal_body_passes_auth_check(monkeypatch):
    """v0.9.2: a normal-sized body with valid secret returns 200 and
    triggers dispatch."""
    import config as _cfg
    monkeypatch.setattr(_cfg, "WEBHOOK_SECRET_TOKEN", "secret")
    import webhook
    
    # Mock the dispatch so we don't actually process the update
    captured = []
    async def fake_dispatch(upd_dict):
        captured.append(upd_dict)
    monkeypatch.setattr("webhook._dispatch_update", fake_dispatch, raising=False)
    # The webhook module imports _dispatch_update lazily inside _handle
    # so we need to patch dispatch._dispatch_update as well
    import dispatch
    monkeypatch.setattr(dispatch, "_dispatch_update", fake_dispatch)
    
    body = _json.dumps({
        "update_id": 12345,
        "message": {
            "message_id": 1,
            "date": 1234567890,
            "chat": {"id": 1, "type": "private"},
            "from": {"id": 1, "is_bot": False, "first_name": "test"},
            "text": "hello",
        }
    }).encode()
    req = _make_request(
        {"X-Telegram-Bot-Api-Secret-Token": "secret"},
        body=body,
    )
    resp = await webhook._handle(req)
    assert resp.status == 200
    # Give the create_task a chance to run
    await asyncio.sleep(0.05)
    assert len(captured) == 1


@pytest.mark.asyncio
async def test_handle_bad_secret_returns_403(monkeypatch):
    import config as _cfg
    monkeypatch.setattr(_cfg, "WEBHOOK_SECRET_TOKEN", "secret")
    import webhook
    req = _make_request({"X-Telegram-Bot-Api-Secret-Token": "wrong"})
    resp = await webhook._handle(req)
    assert resp.status == 403


@pytest.mark.asyncio
async def test_handle_malformed_json_returns_400(monkeypatch):
    import config as _cfg
    monkeypatch.setattr(_cfg, "WEBHOOK_SECRET_TOKEN", "secret")
    import webhook
    req = _make_request(
        {"X-Telegram-Bot-Api-Secret-Token": "secret"},
        body=b"not json at all",
    )
    resp = await webhook._handle(req)
    assert resp.status == 400


@pytest.mark.asyncio
async def test_handle_json_without_update_fields_returns_400(monkeypatch):
    """v0.9.2: valid JSON but not a valid Update (e.g. an empty dict) returns 400."""
    import config as _cfg
    monkeypatch.setattr(_cfg, "WEBHOOK_SECRET_TOKEN", "secret")
    import webhook
    req = _make_request(
        {"X-Telegram-Bot-Api-Secret-Token": "secret"},
        body=b'{"not_update": true}',
    )
    resp = await webhook._handle(req)
    assert resp.status == 400


# === _safe_dispatch exception logging ===

@pytest.mark.asyncio
async def test_safe_dispatch_logs_exceptions(monkeypatch, caplog):
    """v0.9.2: exceptions in the dispatch task are caught and logged,
    not raised or silently swallowed.

    _safe_dispatch is nested inside _handle, so we test it via
    _handle with a dispatch that raises. The handler returns 200
    (the request was valid); the exception is logged.
    """
    import logging
    import config as _cfg
    monkeypatch.setattr(_cfg, "WEBHOOK_SECRET_TOKEN", "secret")
    import webhook
    import dispatch
    
    async def failing_dispatch(upd_dict):
        raise RuntimeError("simulated handler bug")
    
    monkeypatch.setattr(dispatch, "_dispatch_update", failing_dispatch)
    
    body = _json.dumps({
        "update_id": 12345,
        "message": {
            "message_id": 1, "date": 1234567890,
            "chat": {"id": 1, "type": "private"},
            "from": {"id": 1, "is_bot": False, "first_name": "t"},
            "text": "hi",
        }
    }).encode()
    req = _make_request(
        {"X-Telegram-Bot-Api-Secret-Token": "secret"},
        body=body,
    )
    with caplog.at_level(logging.ERROR, logger="webhook"):
        resp = await webhook._handle(req)
    # Webhook returns 200 (the request itself was valid)
    assert resp.status == 200
    # Give the create_task a moment to run and log the exception
    await asyncio.sleep(0.1)
    # The exception should be logged via log.exception
    # log.exception includes both the message AND exc_info (traceback).
    # The message itself contains "dispatch failed"; the exception
    # details are in exc_info.
    assert any("dispatch failed" in r.message for r in caplog.records),         f"no dispatch-failed log in: {[r.message for r in caplog.records]}"
    # And at least one record should have the actual RuntimeError
    assert any(r.exc_info is not None and
               r.exc_info[0] is RuntimeError and
               "simulated handler bug" in str(r.exc_info[1])
               for r in caplog.records),         f"no RuntimeError exc_info in: {[(r.exc_info,) for r in caplog.records]}"


# === _webhook_auto_register_sync ===

def test_auto_register_includes_allowed_updates(monkeypatch, capsys):
    """v0.9.3: auto-register must include allowed_updates so callback_query
    (Stop button) and chat_member are delivered. v0.9.2 bug was missing this."""
    import config as _cfg
    monkeypatch.setattr(_cfg, "BOT_TOKEN", "test:token")
    import dispatch
    
    # Mock urllib.request.urlopen to capture the request
    captured = []
    class FakeResponse:
        status = 200
        def read(self): return b'{"ok": true, "result": true}'
        def __enter__(self): return self
        def __exit__(self, *a): pass
    
    def fake_urlopen(req, timeout=10):
        captured.append({
            "url": req.full_url,
            "data": req.data,
            "headers": dict(req.headers),
            "method": req.get_method(),
        })
        return FakeResponse()
    
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    
    dispatch._webhook_auto_register_sync(
        "https://example.com/tg-webhook", "secret-abc"
    )
    
    # Step 1: getWebhookInfo should be called
    assert len(captured) >= 1
    info_call = captured[0]
    assert "getWebhookInfo" in info_call["url"]
    # BOT_TOKEN is set by conftest; just check the URL contains the bot API host
    assert "api.telegram.org" in info_call["url"]
    
    # Step 2: setWebhook should be called (since current URL is empty/different)
    if len(captured) >= 2:
        set_call = captured[1]
        # Body should be JSON with allowed_updates as a list (not a string)
        body_text = set_call["data"].decode("utf-8")
        body = _json.loads(body_text)
        assert "allowed_updates" in body, f"allowed_updates missing from: {body}"
        # The list should include callback_query and chat_member
        assert "callback_query" in body["allowed_updates"]
        assert "chat_member" in body["allowed_updates"]
        # drop_pending_updates should be a Boolean False, not a string
        assert body["drop_pending_updates"] is False, f"drop_pending_updates should be False, got {body['drop_pending_updates']}"
        # Content-Type should be application/json (case-insensitive, urllib lowercases "Content-Type" to "Content-type")
        headers_lower = {k.lower(): v for k, v in set_call["headers"].items()}
        assert "application/json" in headers_lower.get("content-type", ""), f"got headers: {set_call['headers']}"
        # url and secret_token should be present
        assert body["url"] == "https://example.com/tg-webhook"
        assert body["secret_token"] == "secret-abc"


def test_auto_register_skips_when_already_correct(monkeypatch, capsys):
    """v0.9.4: idempotent — if getWebhookInfo shows the same URL, skip setWebhook."""
    import config as _cfg
    monkeypatch.setattr(_cfg, "BOT_TOKEN", "test:token")
    import dispatch
    
    call_count = [0]
    class FakeResponseSame:
        status = 200
        def read(self):
            return _json.dumps({
                "ok": True,
                "result": {"url": "https://example.com/tg-webhook"},
            }).encode()
        def __enter__(self): return self
        def __exit__(self, *a): pass
    
    def fake_urlopen(req, timeout=10):
        call_count[0] += 1
        return FakeResponseSame()
    
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    
    dispatch._webhook_auto_register_sync(
        "https://example.com/tg-webhook", "secret"
    )
    
    # Only getWebhookInfo was called; no setWebhook
    assert call_count[0] == 1, f"expected 1 call (only getWebhookInfo), got {call_count[0]}"
    # Stdout should mention "already registered"
    out = capsys.readouterr().out
    assert "already registered" in out, f"expected 'already registered' in: {out}"


def test_auto_register_warns_on_overwrite(monkeypatch, capsys):
    """v0.9.4: warn loudly if a different webhook is currently registered."""
    import config as _cfg
    monkeypatch.setattr(_cfg, "BOT_TOKEN", "test:token")
    import dispatch
    
    class FakeResponseDifferent:
        status = 200
        def read(self):
            return _json.dumps({
                "ok": True,
                "result": {"url": "https://old.example.com/tg-webhook"},
            }).encode()
        def __enter__(self): return self
        def __exit__(self, *a): pass
    
    set_called = [False]
    class FakeSetResponse:
        status = 200
        def read(self): return b'{"ok": true, "result": true}'
        def __enter__(self): return self
        def __exit__(self, *a): pass
    
    def fake_urlopen(req, timeout=10):
        if "setWebhook" in req.full_url:
            set_called[0] = True
            return FakeSetResponse()
        return FakeResponseDifferent()
    
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    
    dispatch._webhook_auto_register_sync(
        "https://new.example.com/tg-webhook", "secret"
    )
    
    assert set_called[0], "setWebhook should have been called"
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "overwriting" in out
    assert "old.example.com" in out
    assert "new.example.com" in out


def test_auto_register_handles_telegram_error(monkeypatch, capsys):
    """v0.9.4: parse Telegram's error response and print the description."""
    import config as _cfg
    monkeypatch.setattr(_cfg, "BOT_TOKEN", "test:token")
    import dispatch
    
    class FakeResponseError:
        status = 200  # HTTP 200, but Telegram returns ok=false
        def read(self):
            return _json.dumps({
                "ok": False,
                "description": "Bad webhook: bad protocol",
            }).encode()
        def __enter__(self): return self
        def __exit__(self, *a): pass
    
    def fake_urlopen(req, timeout=10):
        return FakeResponseError()
    
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    
    dispatch._webhook_auto_register_sync(
        "https://example.com/tg-webhook", "secret"
    )
    
    out = capsys.readouterr().out
    assert "REJECTED" in out
    assert "Bad webhook" in out


def test_auto_register_handles_network_error(monkeypatch, capsys):
    """v0.9.4: network error (e.g. DNS failure) is reported but doesn't crash."""
    import config as _cfg
    monkeypatch.setattr(_cfg, "BOT_TOKEN", "test:token")
    import dispatch
    
    def fake_urlopen(req, timeout=10):
        raise urllib.error.URLError("DNS failure")
    
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    
    # Should not raise
    dispatch._webhook_auto_register_sync(
        "https://example.com/tg-webhook", "secret"
    )
    
    out = capsys.readouterr().out
    assert "FAILED" in out or "failed" in out


def test_auto_register_uses_json_body_not_form(monkeypatch):
    """v0.9.4: payload must be JSON-encoded, not form-encoded (cleaner types)."""
    import config as _cfg
    monkeypatch.setattr(_cfg, "BOT_TOKEN", "test:token")
    import dispatch
    
    captured = []
    class FakeResponse:
        status = 200
        def read(self): return b'{"ok": true, "result": true}'
        def __enter__(self): return self
        def __exit__(self, *a): pass
    
    def fake_urlopen(req, timeout=10):
        captured.append(req)
        return FakeResponse()
    
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    
    dispatch._webhook_auto_register_sync(
        "https://example.com/tg-webhook", "secret"
    )
    
    # Find the setWebhook call
    set_call = next((c for c in captured if "setWebhook" in c.full_url), None)
    assert set_call is not None, "setWebhook call not found"
    # Content-Type must be application/json (case-insensitive)
    headers_lower = {k.lower(): v for k, v in set_call.headers.items()}
    assert "application/json" in headers_lower.get("content-type", "")
    # Body must parse as JSON
    body = _json.loads(set_call.data.decode("utf-8"))
    assert isinstance(body["allowed_updates"], list)
    assert isinstance(body["drop_pending_updates"], bool)


def test_auto_register_handles_empty_current_url(monkeypatch, capsys):
    """v0.9.4: first-deploy case. getWebhookInfo returns empty url.
    No WARNING, just register. (P3 review note from wave 12.)"""
    import config as _cfg
    monkeypatch.setattr(_cfg, "BOT_TOKEN", "test:token")
    import dispatch

    class FakeResponseEmpty:
        status = 200
        def read(self):
            return _json.dumps({"ok": True, "result": {"url": ""}}).encode()
        def __enter__(self): return self
        def __exit__(self, *a): pass

    set_called = [False]
    class FakeSetResponse:
        status = 200
        def read(self): return b'{"ok": true, "result": true}'
        def __enter__(self): return self
        def __exit__(self, *a): pass

    def fake_urlopen(req, timeout=10):
        if "setWebhook" in req.full_url:
            set_called[0] = True
            return FakeSetResponse()
        return FakeResponseEmpty()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    dispatch._webhook_auto_register_sync(
        "https://example.com/tg-webhook", "secret"
    )

    assert set_called[0], "setWebhook should have been called (first deploy)"
    out = capsys.readouterr().out
    # No warning, no "already registered" — just a clean register
    assert "WARNING" not in out, f"unexpected WARNING on first deploy: {out}"
    assert "already registered" not in out
    assert "auto-registered" in out
