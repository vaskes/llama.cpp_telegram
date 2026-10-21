"""The streaming accumulator.

TECH-DEBT #3, Block C2. A pure object: chunks go in, buffers grow, and
feed() reports what changed so the caller can react.

The wire facts this exists to preserve, captured from a live
Qwen3.8-27B-Ultra-Heretic run in Block A:

  * The opening chunk carries `content: null`, not `""`. A truthiness
    check treats them the same, so this looks free -- until something
    normalises null to "None" and writes that to Telegram.
  * `reasoning_content` is Qwen-specific. OpenAI-shaped servers omit it,
    so reasoning legitimately stays empty and push_thinking is never
    called. That is not a bug and does not deserve a capability flag.
  * A tool call's id and name arrive ONLY in the chunk that opens that
    index; later deltas for the same index carry arguments alone. That
    is why `name` is `+=` and not `=`: a server that split the name
    mid-token across chunks would otherwise lose the tail. `id` is
    replaced outright -- a second id for the same index is a server
    fault, and keeping the first is the only sane reading.
  * Arguments arrive in fragments of one or two characters --
    ["{", '"city":"', "Mos", "cow", '"', "}"] on a six-fragment tool call.
    Joining is mandatory; never parse per delta.

Deliberately absent: push_thinking and abort_event. The first is an async
callback invoked on every reasoning delta, which would force every test
to mock async I/O; the second is request lifecycle, checked between
chunks in the caller. Both stay in call_llama.
"""


class Delta:
    """What one chunk changed.

    A class rather than a tuple so the caller cannot read `d[2]` and
    silently get finish_reason the day a field is inserted.
    """

    __slots__ = ("reasoning", "content", "finish_reason")

    def __init__(self, reasoning, content, finish_reason):
        self.reasoning = reasoning
        self.content = content
        self.finish_reason = finish_reason

    @property
    def grew_reasoning(self) -> bool:
        return bool(self.reasoning)


class StreamAccumulator:
    """Grows the four buffers a llama-server SSE response fills.

    Not thread-safe and does not need to be: one accumulator per request,
    driven by a single stream.
    """

    __slots__ = ("content", "reasoning", "tool_calls", "finish_reason",
                 "chunk_count")

    def __init__(self):
        self.content = ""
        self.reasoning = ""
        self.tool_calls = {}
        self.finish_reason = None
        self.chunk_count = 0

    def feed(self, chunk) -> Delta:
        """Fold one parsed chunk in.

        A chunk with no choices is not an error: llama-server emits a
        usage-only object at the end of some responses, and raising on it
        would discard the whole reply.
        """
        self.chunk_count += 1
        fr = None
        new_reasoning = ""

        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}

            fr = choice.get("finish_reason") or fr

            rc = delta.get("reasoning_content")
            if rc:
                self.reasoning += rc
                new_reasoning = rc

            cc = delta.get("content")
            # `if cc:` and `if cc is not None:` are indistinguishable for
            # the opening null chunk -- += "" changes nothing either way.
            # The comment here used to claim otherwise. Measured instead:
            # a server that sends a non-string content (0, [], {}) makes
            # `is not None` raise TypeError mid-stream and lose the whole
            # reply, while truthiness skips it. That is the real reason.
            if cc:
                self.content += cc

            for tc_delta in delta.get("tool_calls") or []:
                idx = tc_delta.get("index", 0)
                entry = self.tool_calls.get(idx)
                if entry is None:
                    entry = self.tool_calls[idx] = {"id": "", "name": "",
                                                    "arguments": ""}
                if tc_delta.get("id"):
                    entry["id"] = tc_delta["id"]
                fn = tc_delta.get("function") or {}
                if fn.get("name"):
                    entry["name"] += fn["name"]
                if fn.get("arguments"):
                    entry["arguments"] += fn["arguments"]

        # finish_reason arrives on its own trailing chunk and nowhere
        # else. It used to be returned in the Delta but never stored, so
        # acc.finish_reason was None forever -- the attribute existed,
        # was documented, and was dead. The caller reads it off the Delta
        # and was unaffected, which is exactly why nothing went red.
        self.finish_reason = fr

        return Delta(new_reasoning, self.content, fr)

    def tool_calls_list(self) -> list:
        """Indexed dict -> list, ordered by index.

        The caller iterates this to build tool messages, so the ordering
        is part of the contract, not an implementation detail.
        """
        return [self.tool_calls[i] for i in sorted(self.tool_calls)]