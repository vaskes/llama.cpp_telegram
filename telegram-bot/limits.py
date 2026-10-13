# limits.py
# Tunable per-deployment limits for the LLM gateway.
#
# Why a dedicated module (not in config.py):
#   - These are *model-facing* limits (tokens, timeouts, tool-loop
#     wall clock), distinct from the *Telegram-facing* limits in
#     config.py (BOT_TOKEN, MAX_PHOTO_BYTES, allowed user IDs).
#   - Operators tune them in tight feedback loops while debugging
#     a particular prompt class. Keeping them in one place lets
#     an operator look at a single file and see "what can I make
#     bigger or smaller to fix this behaviour".
#   - All defaults match the current production llama-server
#     (Qwen3.8-27B-Ultra-Heretic-MTP at 256k context). When the
#     operator changes llama-server (new model, new --ctx-size),
#     this file is the single place to revisit.
#
# Why every value is env-overrideable:
#   - Operator feedback 2026-10-13 ("Ограничением для бота должны
#     быть параметры llama, если лама не смогла бот - не смог.
#     Сам бот не должен ограничивать модель и обрывать
#     выполнение запросов.") means the bot must not have hidden
#     caps. If the operator wants a bigger window, they set
#     LLAMABOT_MAX_TOKENS and restart, no code change.
#   - Defaults are *max* (what the current llama-server can
#     handle), not arbitrary safe-floor values. That way out of
#     the box the bot does the same thing the underlying model
#     would do, which is what the operator asked for.
#
# Why no "right answer" tuning here:
#   - This module is the *tunable surface*. The right values
#     depend on the model and the workload and are discovered
#     by stress-testing (see Track 2 in docs/ROADMAP.md). When
#     Track 2 produces new numbers, they go here as the new
#     defaults.

import os


# === Output token cap ===
# Cap on max_tokens passed to llama-server per turn. This is
# the per-iter budget, NOT the per-conversation budget.
#
# Production default: 32768. The reasoning-heavy Qwen 3.8-27B
# Heretic MTP can spend ~30k tokens on a single thinking block
# for a 4K-char prompt, leaving very little for the actual
# answer. A larger cap lets the model finish a long answer
# without being cut off mid-sentence. A smaller cap forces
# shorter answers and prevents runaway reasoning.
#
# Override: LLAMABOT_MAX_TOKENS
MAX_TOKENS_DEFAULT = int(os.environ.get("LLAMABOT_MAX_TOKENS", "32768"))

# Cap for caption-style handlers (handle_photo, handle_voice,
# handle_document) where the user message is shorter and a
# shorter answer is usually appropriate. Production default:
# 16384. Captions rarely need 32k of output.
#
# Override: LLAMABOT_MAX_TOKENS_CAPTION
MAX_TOKENS_CAPTION = int(os.environ.get("LLAMABOT_MAX_TOKENS_CAPTION", "16384"))

# Default in call_llama's own signature. Not used by any
# current caller (every call site overrides), but kept here
# so a future caller doesn't accidentally trigger the
# 65536-of-the-past which would mask the new lower default.
# Production default: MAX_TOKENS_DEFAULT.
#
# Override: not exposed; if you want a different default,
# change MAX_TOKENS_DEFAULT.
MAX_TOKENS_CALL_LLAMA_DEFAULT = MAX_TOKENS_DEFAULT


# === Wall-clock budgets ===
# Tool-loop wall clock. After this many seconds in a single
# call_llama() invocation, the bot bails with a user-readable
# message even if the model is still iterating. 600 sec = 10
# min, matches Track 2 design: shell-tool sessions are
# expected to last <60s, so 10 min is a generous safety net.
#
# Override: LLAMABOT_LOOP_BUDGET_SEC
LOOP_BUDGET_SEC = int(os.environ.get("LLAMABOT_LOOP_BUDGET_SEC", "600"))

# httpx client timeout for any POST to llama-server. Must be
# greater than LOOP_BUDGET_SEC so the client doesn't fire its
# own timeout mid-loop. Production default: 600.0 sec = 10
# min, same as LOOP_BUDGET_SEC.
#
# Override: LLAMABOT_LLAMA_HTTP_TIMEOUT
LLAMA_HTTP_TIMEOUT_SEC = float(os.environ.get("LLAMABOT_LLAMA_HTTP_TIMEOUT", "600.0"))

# httpx client timeout for GET /v1/models (model discovery,
# called once at first call_llama). 10 sec — this is a local
# call, llama-server answers in <100ms.
#
# Override: LLAMABOT_LLAMA_MODELS_TIMEOUT
LLAMA_MODELS_TIMEOUT_SEC = float(os.environ.get("LLAMABOT_LLAMA_MODELS_TIMEOUT", "10.0"))

# httpx client timeout for /v1/audio/transcriptions (whisper).
# 120 sec — 5-min voice messages take ~20s to transcribe.
#
# Override: LLAMABOT_TRANSCRIBE_TIMEOUT
TRANSCRIBE_TIMEOUT_SEC = float(os.environ.get("LLAMABOT_TRANSCRIBE_TIMEOUT", "120.0"))


# === Reasoning budget ===
# Qwen 3.8-27B-Ultra-Heretic MTP can spend its entire token
# budget on the reasoning_content field, leaving content=""
# and finish_reason="length". 2026-10-13 4K EN prompt in
# production: 94797 reasoning chars, 0 content chars, 0 tool
# calls, finish=length.
#
# REASONING_RATIO_HARD_LIMIT is a client-side cap: if reasoning
# consumed this fraction of max_tokens AND content is empty,
# we surface a clear error rather than letting the user see a
# truncated tail of pure thinking. Default 0.5 — if reasoning
# ate >50% of the budget and there's no content yet, the
# answer is going to be useless.
#
# Override: LLAMABOT_REASONING_RATIO_LIMIT
REASONING_RATIO_HARD_LIMIT = float(os.environ.get("LLAMABOT_REASONING_RATIO_LIMIT", "0.5"))


# === Self-test overrides ===
# The in-tree selftest uses smaller caps to keep the smoke
# test fast. NOT for production tuning. These are constants
# on purpose, not env-driven, because the selftest must be
# deterministic.
SELFTEST_MAX_TOKENS = 2048
SELFTEST_LOOP_BUDGET_SEC = 120
