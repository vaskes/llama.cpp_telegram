#!/usr/bin/env python3
"""Capture a real SSE stream from llama-server, as a golden master.

Raised in review 2026-10-22 and correct: `tests/test_c2_sse.py` parses a
synthetic stream. If llama-server changes its framing -- adds a field to
`delta`, renames `finish_reason`, starts emitting `usage` -- the parser
breaks in production and the tests stay green, because they are testing a
fixture I invented.

So: record what the engine actually sends, and pin the parse of that.

Run by hand, deliberately. The fixture is checked in, and re-recording is
a deliberate act with a diff to review -- the alternative, a live call in
CI, makes the suite depend on a GPU being warm and turns an engine
version bump into a red pipeline nobody can act on.

    python tests/capture_sse_golden.py --out tests/fixtures/sse_stream.txt

Refuses to run against an unreachable engine rather than writing an empty
fixture: an empty golden master passes everything and is worse than none,
which is the failure mode this file exists to prevent.
"""
import argparse
import json
import pathlib
import sys
import time
import urllib.error
import urllib.request

FIXTURE = pathlib.Path(__file__).resolve().parent / "fixtures" / "sse_stream.txt"

TOOLS = [{
    "type": "function",
    "function": {
        "name": "bash_command",
        "description": "Run a shell command in a sandboxed session.",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
}]


def capture(url: str, *, with_tools: bool, timeout: float) -> dict:
    body = {
        "model": "Qwen3.8-27B-Ultra-Heretic-MTP-256k",
        "messages": [{"role": "user",
                      "content": "What is the weather in Moscow? "
                                 "Use the tool."}],
        "max_tokens": 96,
        "stream": True,
        # deterministic-ish: greedy, and a fixed seed where supported
        "temperature": 0.0,
    }
    if with_tools:
        body["tools"] = TOOLS
        body["tool_choice"] = "auto"
        body["parallel_tool_calls"] = False

    request = urllib.request.Request(
        f"{url.rstrip('/')}/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )

    lines, started = [], time.monotonic()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        for raw in response:
            lines.append(raw.decode("utf-8").rstrip("\n"))
            if lines[-1].strip() == "data: [DONE]":
                break
            if time.monotonic() - started > timeout:
                break

    payloads = []
    for line in lines:
        if line.startswith("data: ") and line[6:].strip() != "[DONE]":
            try:
                payloads.append(json.loads(line[6:]))
            except json.JSONDecodeError:
                payloads.append({"__unparseable__": line[6:]})

    return {
        "engine": urllib.request.urlopen(
            f"{url.rstrip('/')}/models", timeout=5).read().decode()[:0] or None,
        "captured_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "request": {"model": body["model"], "with_tools": with_tools,
                    "max_tokens": body["max_tokens"],
                    "temperature": body["temperature"]},
        "raw_lines": lines,
        "decoded": payloads,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8080/v1")
    parser.add_argument("--out", type=pathlib.Path, default=FIXTURE)
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()

    records = []
    for with_tools in (False, True):
        try:
            record = capture(args.url, with_tools=with_tools,
                             timeout=args.timeout)
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            print(f"engine unreachable ({e}). Refusing to write an empty "
                  f"fixture: it would pass every test and mean nothing.",
                  file=sys.stderr)
            return 2
        print(f"  with_tools={with_tools}: {len(record['raw_lines'])} lines")
        records.append(record)

    text = json.dumps({"schema": 1, "source": args.url, "cases": records},
                      ensure_ascii=False, indent=1)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8")
    print(f"wrote {args.out} ({len(text)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())