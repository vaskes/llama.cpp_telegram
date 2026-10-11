"""New tests for call_llama._abortable_post and storage known_topics.
File: tests/test_call_llama.py
"""
import asyncio
import pytest

# === _abortable_post ===

@pytest.mark.asyncio
async def test_abortable_post_returns_response_when_post_finishes_first():
    """v0.7.6: when the POST finishes before abort fires, the response is returned."""
    from call_llama import _abortable_post

    abort = asyncio.Event()

    class FakeResponse:
        def __init__(self, payload): self.payload = payload
        def raise_for_status(self): pass
        def json(self): return self.payload

    async def fast_post(*a, **kw):
        return FakeResponse({"choices": [{"message": {"content": "hi"}}]})

    class FakeClient:
        post = staticmethod(fast_post)

    result = await _abortable_post(FakeClient(), "http://test", abort_event=abort)
    assert result is not None
    assert result.json() == {"choices": [{"message": {"content": "hi"}}]}


@pytest.mark.asyncio
async def test_abortable_post_returns_none_when_abort_fires_first():
    """v0.7.6: when abort_event fires before the POST, _abortable_post
    cancels the POST and returns None."""
    from call_llama import _abortable_post

    abort = asyncio.Event()

    async def slow_post(*a, **kw):
        await asyncio.sleep(10)  # never returns in test time
        return None  # unreachable but makes the linter happy

    class FakeClient:
        post = staticmethod(slow_post)

    # Set abort before the POST completes
    asyncio.get_event_loop().call_later(0.05, abort.set)

    result = await _abortable_post(FakeClient(), "http://test", abort_event=abort)
    assert result is None, f"expected None on abort, got {result}"


@pytest.mark.asyncio
async def test_abortable_post_passes_through_when_no_abort_event():
    """When abort_event=None, _abortable_post just calls client.post()."""
    from call_llama import _abortable_post

    class FakeResponse:
        def json(self): return {"ok": True}

    async def fake_post(*a, **kw):
        return FakeResponse()

    class FakeClient:
        post = staticmethod(fake_post)

    result = await _abortable_post(FakeClient(), "http://test", abort_event=None)
    assert result.json() == {"ok": True}


# === storage known_topics ===

def test_known_topics_round_trip(tmp_path):
    """storage.add_known_topic / find_known_topic_by_id / remove_known_topic
    are the public API for the per-chat known topics used by the LLM.

    Note: the second arg is message_thread_id (Telegram's topic id),
    not a generic "id". The dict key is also message_thread_id.
    """
    from storage import Storage
    s = Storage(str(tmp_path / "test.db"))
    s.create_thread(1, "main")
    s.add_known_topic(1, 100, "test-topic")
    found = s.find_known_topic_by_name(1, "test-topic")
    assert found is not None
    assert found["name"] == "test-topic"
    assert found["message_thread_id"] == 100
    by_id = s.find_known_topic_by_id(1, 100)
    assert by_id["name"] == "test-topic"
    s.remove_known_topic(1, 100)
    assert s.find_known_topic_by_name(1, "test-topic") is None


def test_known_topics_list_per_chat(tmp_path):
    """list_known_topics returns only topics for the given chat."""
    from storage import Storage
    s = Storage(str(tmp_path / "test.db"))
    s.create_thread(1, "main")
    s.create_thread(2, "main")
    s.add_known_topic(1, 100, "topic-a")
    s.add_known_topic(1, 101, "topic-b")
    s.add_known_topic(2, 200, "topic-c")
    list_1 = s.list_known_topics(1)
    list_2 = s.list_known_topics(2)
    assert len(list_1) == 2
    assert len(list_2) == 1
    assert {t["name"] for t in list_1} == {"topic-a", "topic-b"}
    assert list_2[0]["name"] == "topic-c"


def test_known_topics_returns_none_for_missing(tmp_path):
    """find_known_topic_by_id / by_name return None for missing topics."""
    from storage import Storage
    s = Storage(str(tmp_path / "test.db"))
    s.create_thread(1, "main")
    assert s.find_known_topic_by_id(1, 999) is None
    assert s.find_known_topic_by_name(1, "nope") is None
    # remove_known_topic on a missing topic should not raise
    s.remove_known_topic(1, 999)  # no-op
