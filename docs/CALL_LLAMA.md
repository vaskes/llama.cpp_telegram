# `call_llama` and friends — design notes for code review

This document explains **what** is in `bot.py`, **why** each piece is
the way it is, and **what** failure modes we hit that led to the
current shape. It is intentionally opinionated: where a design choice
was forced by a specific incident, the incident is named.

If you are doing a code review, the recommended order is:
1. The **two branches** of `call_llama` (streaming vs non-streaming).
2. The **tool-intent detector** (`_detect_tool_intent` + `_TOOL_KEYWORDS`).
3. The **abort ladder** (`max_iter`, identical-call, wall-clock, hallucination).
4. The **polling loop** at the bottom of the file (PTB 21.0 workaround).
5. **Image MIME detection** in `handle_photo`.
6. **Config block** + auto-discover.
7. The four `handle_*` dispatchers.

Everything else (whitelist, IPv4 patch, disabled tools) is already
documented in [ARCHITECTURE.md](ARCHITECTURE.md) and [SECURITY.md](SECURITY.md).

---

## 1. `call_llama` — two completely different code paths

The function signature is:

```python
async def call_llama(messages, max_tokens=65536, user_text='', thinking_msg=None, use_stream=True)
```

Internally it splits into two **disjoint** code paths. They share the
function signature and the return contract (string), but inside they
have **nothing** in common.

### 1.1 `use_stream=True` (default — text chat, voice, document)

Streaming SSE from llama-server, full tool-calling loop, live
`push_thinking` updates to Telegram. This is the path that does
the real work.

What it does, per iteration:
1. Build `req_body` (system + messages + optional tools).
2. `client.stream("POST", ...)` — `httpx` SSE iterator.
3. Accumulate `reasoning_buf`, `content_buf`, and `tool_calls_buf`
   (the last keyed by `index` from the tool-call delta).
4. Every ~5 s, push the accumulated reasoning tail to a Telegram
   message via `push_thinking(reasoning, force=False)`. The final
   iteration calls `force=True` so the last chunk lands.
5. When the stream ends, build the assistant `msg` object and the
   `tool_calls` list, decide what to do:
   - if `tool_calls` and `req_tools` → execute, append `tool` role
     messages, `continue` to the next iteration.
   - if `tool_calls` and `req_tools is None` → **hallucination abort**
     (see §3.4).
   - if no `tool_calls` → return `content_buf` (or reasoning tail if
     content is empty).
6. Outer guard: `max_iter=15`, `wall_clock_budget=600s`,
   `identical_call_detector`, `last_empty>=3` hard-abort.

### 1.2 `use_stream=False` (vision — `handle_photo`)

A **single** `httpx.post()`, `r.json()`, return content. No tool
loop, no reasoning streaming, no abort ladder.

This is not laziness. The streaming path on a vision payload was
**hanging** in production: `iter=1`, `iter=2`, `iter=3`, ... would
start, but the `finish=...` log line was never printed, the bot
returned "exceeded tool-calling iteration limit" after 15 iterations,
and we could not localize the raise (the inner `except` did not
fire either — the `for iteration in range(max_iter)` loop just kept
spinning past the line that should have logged the result).

**Possible cause (unconfirmed):** `data["choices"][0]["message"]` may
have a non-dict shape when an `mmproj` multimodal payload is in play,
or the accumulated `accumulated_reasoning += reasoning_buf` line
got a non-string somewhere. We did not bisect it because:

- vision tasks do not use the tool-calling loop in practice
- a simpler code path is easier to reason about
- the loss of features (no tool loop, no live reasoning updates) on
  vision tasks is acceptable — the user mostly sends photos with
  short captions and expects a one-shot answer

**When this changes:** if someone wants tool calling on vision
("OCR this screenshot and then search for …"), revisit the streaming
path and add a proper `try/except` around the `data["choices"]` block
with `traceback.print_exc()` and a clean return.

### 1.3 Why the split is at the `use_stream` parameter, not at a higher level

The split is **inside** `call_llama` so that `handle_photo`,
`handle_voice`, `handle_document`, `handle_text` all share the same
return contract and the same conversation-state machinery. If we
ever fix the streaming path for vision, the change is local to
`call_llama` and the callers do not move.

---

## 2. Tool-intent detector — only pass `tools` when the user wants them

```python
req_tools = all_tools if _detect_tool_intent(user_text) else None
```

`all_tools` is the list fetched once from `GET /tools` on llama-server
(filtered through `DISABLED_TOOLS`).

`_detect_tool_intent(text)` does a **substring** match against
`_TOOL_KEYWORDS` — Russian and English, weather/web/search vocabulary.

### 2.1 Why this exists

Two reasons, in order of importance:

1. **Without this, the model hallucinates tool calls.** On a prompt
   like "проверь математику" (check the math) with `tools=[]`
   in the request, Ornith-1.5 would emit `tool_calls` to a non-existent
   function and refuse to produce content. The output came back as
   empty `content` and a `tool_calls` field pointing at nothing.
   That was the "exceeded tool-calling iteration limit" symptom from
   §1.2, but for **text**, not vision.

2. **Latency.** Tool definitions in the system prompt are a few
   hundred tokens. Skipping them saves time and, more importantly,
   keeps the model focused on the actual question.

### 2.2 What the keywords look like

```python
_TOOL_KEYWORDS = [
    # weather
    'погод', 'температур', 'осадк',
    # web search
    'найди в интернет', 'найди в сети', 'погугли', 'загугли',
    'поищи в гугл', 'поищи в интернет', 'search the web',
    'web search', 'news about', 'latest on',
    # fetch / screenshot
    'открой сайт', 'скриншот', 'fetch the page', 'screenshot',
]
```

The list is **intentionally narrow**. "проверь" is **not** in it — we
tried, and it caused `req_tools=all_tools` for almost every "проверь
математику" / "проверь что" / "проверь, пожалуйста" message, which
made the model take the wrong branch and never answer.

### 2.3 Failure mode: model still hallucinates tool calls without `tools`

Even with `req_tools=None` and no `tools` field in the body, Ornith
sometimes returns `tool_calls` anyway (it "knows" the format from
training). When that happens, the **hallucination abort** (§3.4)
kicks in: we return `content_buf` if it is non-empty, else the
`reasoning_buf` tail, else a `[bot: empty response, model returned
tool_calls but no tools were sent]` marker.

---

## 3. The abort ladder — four independent guards

The streaming path has **four** ways to stop. They are layered, not
alternatives — any one of them is sufficient.

### 3.1 `max_iter=15`

Hard cap on iterations of the for loop. A model that genuinely needs
15 tool calls to answer a single user message is broken; the cap
prevents infinite spirals.

### 3.2 Identical-call detector

```python
if last_tool_signature == current_tool_signature:
    abort()
```

The "signature" is `name + json(arguments, sort_keys=True)`. If the
model calls the same tool with the same args twice in a row, it is
looping, not thinking. Bail.

### 3.3 Wall-clock budget: 600 s

```python
t0 = time.monotonic()
...
if time.monotonic() - t0 > 600:
    abort()
```

A 10-minute wall-clock cap. Some math problems genuinely need 5-7
minutes of reasoning on Ornith 35B, so this is not a small number.
But past 10 minutes, even a "correct" answer is unusable, and the
GPU is burning for nothing.

### 3.4 Hallucination abort

```python
if tool_calls and req_tools is None:
    print("[abort] model returned tool_calls but req_tools is None")
    return content_buf or reasoning_tail or "[bot: empty response]"
```

This is the **fourth** guard, the newest, and the most specific. It
exists because the keyword detector (§2) is not perfect: there are
prompts that the model decides need tools even when the user did
not ask for any. Without this guard, the loop would run 15 times,
then die at the `max_iter` cap with a confusing error message.

### 3.5 `last_empty>=3` hard abort

```python
if not content_buf and not reasoning_buf:
    last_empty += 1
    if last_empty >= 3:
        abort("three empty iterations in a row")
else:
    last_empty = 0
```

If the model produces nothing useful for three iterations in a row,
something is structurally wrong (template mismatch, image attachment
that the server did not parse, etc.). Bail.

---

## 4. The polling loop at the bottom of the file

**Why this exists:** python-telegram-bot 21.0's `Application.run_polling()`
keeps a second keep-alive connection open across cycles, and Telegram's
Bot API returns `409 Conflict` ("terminated by other getUpdates request")
roughly every other cycle. We saw this in production: 30 % of cycles
were 409s, the rest succeeded, and there was no pattern in timing.

The fix is to **bypass** `Application.run_polling()` entirely and run
a hand-rolled polling loop:

```python
async with httpx.AsyncClient(
    timeout=httpx.Timeout(35.0, read=35.0),
    limits=httpx.Limits(max_keepalive_connections=0, max_connections=1),
) as client:
    while not shutdown_event.is_set():
        try:
            r = await client.get(f"{TELEGRAM_API}/getUpdates",
                                 params={"timeout": 30, "offset": next_offset})
            for upd in r.json()["result"]:
                next_offset = max(next_offset, upd["update_id"] + 1)
                await _dispatch_update(upd, ...)
        except httpx.HTTPError as e:
            print(f"[poll] {type(e).__name__}: {e}")
            await asyncio.sleep(2)
```

**Key points:**

- `max_keepalive_connections=0` — no pool. Every request opens a new
  TCP connection, sends, and closes. This is the actual fix for the
  409 Conflict: Telegram's Bot API is fine with that, and the bot
  never accumulates an extra idle connection.
- `timeout=35` (5 s over Telegram's `long_polling_timeout=30`) so a
  hung connection surfaces as an exception, not a silent block.
- `offset` is tracked **locally** and bumped past every seen
  `update_id`. We do not rely on PTB's persistence.
- `drop_pending=True` on first iteration drops everything that piled
  up while the bot was down. This is intentional: catching up on
  1000 messages from 3 days ago is a worse user experience than
  starting fresh.

**What we lose:** PTB's built-in retry, exponential backoff, and
webhook support. We do not need any of those for this deployment.

**What we keep:** `Application.process_update(update)` for handlers.
We still build a real `Application` lazily and dispatch into it, so
all the handler code (`handle_text`, `handle_photo`, etc.) is
unmodified PTB 21.0 idioms.

---

## 5. Image MIME detection in `handle_photo`

**The problem:** Telegram photos from Android are **WebP**, not JPEG.
Sending `data:image/jpeg;base64,...` to llama-server triggers:

```
400 invalid_request_error: Failed to load image or audio file
```

**The fix:** sniff the first 8 bytes of the downloaded file and pick
the right MIME:

| Magic bytes           | MIME     |
|-----------------------|----------|
| `\x89PNG\r\n\x1a\n`   | `image/png`    |
| `\xff\xd8\xff`        | `image/jpeg`   |
| `RIFF....WEBP`        | `image/webp`   |
| `GIF87a` / `GIF89a`   | `image/gif`    |
| anything else         | `image/jpeg` (fallback) |

The fallback is JPEG because the older PTB versions sent JPEG for
"photo" messages; if the magic-byte check fails for some reason,
JPEG is the lowest-risk bet (llama-server's mmproj is more permissive
with JPEG than with WebP).

**Why not trust `file.mime_type`?** Telegram's `file.mime_type` field
on the `PhotoSize` object is often missing or wrong. Magic bytes are
authoritative.

---

## 6. The `httpx ensure_ascii=True` mutation bug

**The bug:** `r = await client.post(url, json=req_body, ...)` — but
inside `call_llama` we sometimes call `client.post(url, content=payload_bytes)`
where `payload_bytes = json.dumps(req_body, ensure_ascii=False).encode()`.
A debug `print(f"[debug] req={json.dumps(req_body)}")` would then
**mutate** `req_body` because `ensure_ascii=True` (the default in
`json.dumps`) replaces non-ASCII characters with `\uXXXX` escapes
**in the dict itself**, not in the string representation.

We hit this on Russian captions: the first debug log printed
`"text": "\u043f\u0440\u043e\u0432\u0435\u0440\u044c"`, and the
**same** dict then went into the POST body. The image_url field was
also corrupted in the same call (the `data:` URL ended up with
`\u002f` instead of `/` in places, llama-server rejected it as 400).

**The fix:** never `json.dumps` the live `req_body` for logging. Use
`copy.deepcopy(req_body)` first, or write the bytes to disk **before**
the POST and log from disk.

```python
safe_body = copy.deepcopy(req_body)
print(f"[debug] {json.dumps(safe_body, ensure_ascii=False)[:300]}")
# req_body is untouched
```

---

## 7. Config block + auto-discover

```python
BOT_TOKEN = os.environ['BOT_TOKEN']  # required, no default
LLAMA_URL = os.environ.get('LLAMA_URL', 'http://localhost:8080/v1')
WHISPER_URL = os.environ.get('WHISPER_URL', 'http://localhost:8000')
API_KEY = os.environ.get('API_KEY', 'sk-no-key')  # convention
MODEL = os.environ.get('MODEL', '').strip()
if not MODEL:
    MODEL = _discover_default_model(LLAMA_URL, API_KEY)
```

**Why each default:**

| Variable     | Default                | Why                                                                 |
|--------------|------------------------|---------------------------------------------------------------------|
| `BOT_TOKEN`  | (none, required)        | No token = no bot. Fail fast.                                       |
| `LLAMA_URL`  | `http://localhost:8080/v1` | Standard llama-server port. `localhost`, not a private IP.         |
| `WHISPER_URL`| `http://localhost:8000` | Standard faster-whisper-server port.                                |
| `API_KEY`    | `sk-no-key`             | Open-source convention. llama-server without `--api-key` accepts any non-empty bearer; this is a placeholder, not a real token. |
| `MODEL`      | (empty → auto-discover) | If the user does not set it, the bot `GET`s `/v1/models` and uses the first `id`. Removes any hardcoded model name from the repo. |

**Why auto-discover for MODEL:** the previous default was
`'Qwen3.6-35B-A3B-Heretic'`, which is a model name that exists
on someone else's server. Hardcoding it was a footgun: a fresh
deployer who did not override `MODEL` would see the bot try to
call a non-existent model and get cryptic 404s. Auto-discover
is one network round-trip at startup, but it removes a class
of misconfiguration.

**`_discover_default_model` details:**

- Hits `LLAMA_URL.removesuffix('/v1') + '/v1/models'`
- Looks at `data[0]` if the response is OpenAI-shaped, else `models[0]`
- Raises `RuntimeError` with the URL if the list is empty — better
  than silently using `""` and getting a 400 from the chat endpoint

---

## 8. The four dispatchers

`handle_text`, `handle_photo`, `handle_voice`, `handle_document` all
follow the same pattern:

1. Whitelist check (`reject_if_unauthorized`).
2. Persist the user message into `conversations[user_id]`.
3. Trim to last 20 messages.
4. Send a "💭 думаю…" placeholder.
5. Call `call_llama(...)` with appropriate `use_stream` and
   `max_tokens`.
6. If `call_llama` returns empty, send a "🤷 empty" placeholder.
7. Delete the placeholder, send the real answer.

Differences:

| Handler         | `use_stream` | `max_tokens` | Special preprocessing                       |
|-----------------|--------------|--------------|---------------------------------------------|
| `handle_text`   | `True`       | 32768        | —                                           |
| `handle_photo`  | `False`      | 16384        | MIME detection, base64 encode               |
| `handle_voice`  | `True`       | 16384        | Whisper transcription first                 |
| `handle_document`| `True`      | 16384        | File → text (first 16 KB)                   |

`max_tokens=16384` for non-text handlers is **deliberate**: voice and
document tasks tend to produce long reasoning on the model side
(transcribing/reading + answering), and 4096 was a real production
ceiling. We hit it on a math photo: `finish=length` after 227 content
chars + 5098 reasoning chars, the answer was cut off mid-sentence.

---

## 9. What we still do not have

For a future reviewer / contributor:

- **No backpressure on the polling loop.** If a single update takes
  10 minutes to process, all subsequent updates queue up.
  Acceptable for our 2-user whitelist; would be a problem for
  higher traffic.
- **No conversation persistence across restarts.** `conversations`
  is an in-memory dict. Restart = lose context. For our use case
  (single user, daily driver) this is fine; for a multi-user bot
  it would need a database.
- **No image caching.** Every photo is downloaded fresh, re-encoded
  to base64, and sent. Telegram's `getFile` returns a URL valid
  for an hour, so caching by `file_id` would be cheap and friendly.
- **Tool execution is sequential.** A `parallel_tool_calls=true`
  model that emits 4 calls at once will wait for each one in turn.
  Fine for HTTP-bound tools; would matter for slow tools.

---

## 10. What was deleted and why

Some code that used to be in `bot.py` is gone. Worth recording so a
reviewer does not "fix" it back:

- **SearXNG executor.** Replaced with donsetch-http MCP. SearXNG
  engines CAPTCHAd Russian IPs reliably; donsetch routes through
  real Chromium and is not captcha-blocked.
- **`-n -1` / `--keep -1` in llama-server command line.** This
  allows the model to self-loop forever on complex tasks. Use
  `-n 8192` and `--keep 0` instead. See
  [vaskes/llama.cpp-rocm-780m](https://github.com/vaskes/llama.cpp-rocm-780m)
  for the start script.
- **Streaming path's tool_calls_buf from `data["choices"][0]["message"]`**
  in the non-streaming branch. Replaced with a single POST + return.
  See §1.2.
- **Old PTB 21.0 `Application.run_polling()` call.** Replaced with
  the direct-httpx loop in §4.
