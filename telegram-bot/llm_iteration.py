"""One iteration of the tool loop, as a named unit.

TECH-DEBT #8, criterion 1: "an iteration is a named unit". Everything
inside an iteration had a name and a module after blocks 1-4; the
iteration itself was still a `for` statement with 318 lines of body.

The seam, and why it is not a function with 19 parameters

The obvious shape -- `async def run_one(a, b, c, ...)` -- trades a long
body for a long signature and a caller that copies five values back.
That is the same code with worse ergonomics.

A class owns the TURN instead: the inputs `call_llama` was given, plus
the state that crosses iterations (`msgs`, `accumulated_reasoning`,
`final_fallback`, the `LoopGuard`). Everything that is per-iteration
stays a local of `run_one`, which is what it was. So the signature is
one argument plus the iteration number, and there is nothing to copy
back -- `self.msgs` is the same list the caller passed in.

What crossed iterations is now declared in `__init__` instead of being
implied by which locals happen to be assigned before the `for`. That is
the part that was worth doing: `saw_unavailable_tool` was once reset
forty lines below its first set-site, which made a log line unreachable
and then raised UnboundLocalError once something read it.

The terminal value is heterogeneous

Twelve exits, four shapes: `str`, `None` (abort), `dict` (the answer
dict with its ledger) and `None` again for a cancelled turn. `run_one`
returns `IterationResult`, which says whether the turn is over and
carries the value. `is_terminal` is set by the exit site; it is never
inferred from the value being empty, because an aborted turn is over
and has no text. Same rule `NonStreamStep` and `StreamOutcome` carry.
"""
import json
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx

import limits
from config import API_KEY, LLAMA_URL, MODEL, SHUTDOWN_EVENT
from context_budget import OVERFLOW_MESSAGE, is_context_overflow
from donsetch import DONSETCH_TOOLS
from llm_dispatch import (MAX_TOOL_RESULT_CHARS, DispatchDeps,
                          execute_tool)
from llm_finish import (finish_without_tool_calls,
                        refuse_hallucinated_tools)
from llm_loop_guard import Abort, Nudge
from llm_nonstream import (classify_error, non_stream_iteration,
                           non_stream_step, tools_less_fallback)
from llm_request import (build_request_body, log_request_shape,
                         new_stream_accumulators)
from llm_stream import stream_iteration
from llm_thinking import push_thinking
from llm_toolargs import ArgsRejected, parse_tool_args
from llm_toolcalls import normalize_tool_calls
from llm_truncation import condense
from logconf import get_logger
from tool_isolation import known_tool_names

log = get_logger(__name__)

MAX_ITER = 15
ITERATION_LIMIT_MESSAGE = (
    '[bot: exceeded tool-calling iteration limit. '
    'Please try a simpler question, /reset, or a different model.]')


@dataclass(frozen=True)
class IterationResult:
    """Either the turn ends here, or the loop goes round again.

    `is_terminal` is the decision and `terminal` the payload. Two exits
    carry None on purpose -- shutdown and the Stop button -- and a
    caller that asked "is `terminal` set?" would carry on past both and
    ship the previous iteration's buffers to a user who cancelled.
    """
    is_terminal: bool = False
    terminal: Any = None

    @classmethod
    def done(cls, value: Any) -> "IterationResult":
        return cls(is_terminal=True, terminal=value)

    @classmethod
    def again(cls) -> "IterationResult":
        return cls()


class ToolLoop:
    """One turn of the tool loop: its inputs, and what it has built.

    One instance per `call_llama` call. `run()` drives the iterations;
    `run_one()` is one of them. Constructing this object and calling
    `run()` is the whole of what `call_llama` does with the loop.
    """

    def __init__(self, msgs: list, *, max_tokens: int, req_tools,
                 bot, chat_id, current_message_id, chat_type,
                 shutdown_event, abort_event, thinking_msg, silent: bool,
                 use_stream: bool, throttle, guard, loop_start: float,
                 custom_tools: dict, reply_to_message_id=None):
        # --- the turn's inputs ---
        self.msgs = msgs
        self.max_tokens = max_tokens
        self.req_tools = req_tools
        self.bot = bot
        self.chat_id = chat_id
        self.current_message_id = current_message_id
        self.chat_type = chat_type
        self.shutdown_event = shutdown_event
        self.abort_event = abort_event
        self.thinking_msg = thinking_msg
        self.silent = silent
        self.use_stream = use_stream
        self.throttle = throttle
        self.guard = guard
        self.loop_start = loop_start
        self.custom_tools = custom_tools
        # The id of the message the user replied to. The delete tool
        # needs it at EXECUTION time, not only in the tool description:
        # it is the one id of another person's message the bot may
        # touch, and it arrives with the turn.
        self.reply_to_message_id = reply_to_message_id

        # --- what crosses iterations, and nothing else ---
        self.accumulated_reasoning = ""
        self.final_fallback: Optional[str] = None
        # The tools this TURN ran, with what they printed.
        #
        # It belongs here, not in `run_one`. As a local of the iteration
        # it was thrown away every time the iteration returned, so the
        # tools of iterations 0..n-1 never reached the answer -- and
        # the transcript with the console output never reached the chat.
        # Measured on production, 2026-10-25: the file arrived as a
        # document, and the commands that made it were nowhere to be
        # seen. The unit tests did not catch it, because they handed
        # `executed_tools` to the formatter by hand -- a shape the
        # production loop never produces.
        self.executed_tools: list[dict] = []

    async def run(self) -> Any:
        """Drive the iterations and return the turn's answer."""
        for iteration in range(MAX_ITER):
            result = await self.run_one(iteration)
            if result.is_terminal:
                return result.terminal
        if self.final_fallback:
            return self.final_fallback
        return ITERATION_LIMIT_MESSAGE

    async def run_one(self, iteration: int) -> IterationResult:
        """One iteration: ask the engine, then act on what it said.

        Everything per-iteration is a local of this method on purpose.
        It was a local of the loop body before, and the only difference
        is that the loop body is now reachable by name.
        """
        # T44: per-turn accumulator for executed tools. It lives on the
        # loop (see `__init__`), because a local of this method dies
        # with the iteration -- and the iteration is not the turn.
        _executed_tools_in_turn = self.executed_tools
        # Per-iteration flags. These live at the top of the body and not
        # next to the code that happens to read them: the set-sites are
        # spread across the request, the response normalisation and the
        # dispatch, and a reset after the first one silently discards it.
        # saw_unavailable_tool was reset forty lines below its first
        # set-site, which made the T49 log line unreachable and, once D2
        # added a read, raised UnboundLocalError on every turn. See
        # docs/tech-debt/08 for the measurement.
        iter_had_real_result = False
        saw_unavailable_tool = False
        current_calls = []
        # Honour graceful shutdown between iterations. Without this, a
        # SIGTERM mid-call_llama is ignored until the current
        # llama-server request finishes (could be 5-7 min for a hard
        # problem). docker stop SIGKILLs after 10s, so we MUST bail
        # faster than that.
        _sd = (self.shutdown_event if self.shutdown_event is not None
               else SHUTDOWN_EVENT)
        if _sd is not None and _sd.is_set():
            log.info(f"[call_llama] shutdown_event set, aborting tool "
                     f"loop at iteration {iteration}")
            return IterationResult.done(
                '[bot: shutdown requested, aborting tool loop]')
        # Honour per-task abort (Stop button on the thinking message).
        # Distinct from shutdown_event: shutdown is global ("kill the
        # process"), abort is per-message ("skip this one, move on").
        if self.abort_event is not None and self.abort_event.is_set():
            log.info(f"[call_llama] abort_event set, aborting tool loop "
                     f"at iteration {iteration}")
            return IterationResult.done(None)
        # --- request body ---
        async with httpx.AsyncClient(
                timeout=limits.LLAMA_HTTP_TIMEOUT_SEC) as client:
            req_body = build_request_body(
                model=MODEL, msgs=self.msgs, max_tokens=self.max_tokens,
                use_stream=self.use_stream, req_tools=self.req_tools,
            )
            log_request_shape(req_body, iteration=iteration,
                              req_tools=self.req_tools,
                              use_stream=self.use_stream)
            _acc = new_stream_accumulators()
            reasoning_buf = _acc.reasoning_buf
            content_buf = _acc.content_buf
            tool_calls_buf = _acc.tool_calls_buf
            finish_reason = _acc.finish_reason

            # TECH-DEBT #8 block 2: what to DO with a non-streaming
            # response lives in llm_nonstream.non_stream_step, a pure
            # function. The await stays here, inside this try, and so
            # does push_thinking -- C1 and C4 both require it and both
            # are right.
            # THE BRANCH SELECTION. Removed by 0673c5c (T3.1 block 2)
            # and never restored: the non-streaming POST ran first and
            # unconditionally, with a body that still carried
            # `"stream": use_stream`. For handle_text use_stream is True
            # -- that is the default -- so the request asked for SSE and
            # the parse expected JSON. llama-server complied:
            #
            #   [call_llama] non-stream parse failed iter=0: JSONDecodeError
            #     ... status=200 bytes=30330 transfer-encoding='chunked'
            #     body='data: {"choices":[{"finish_reason":null,...
            #
            # i.e. the server answered exactly what was asked for. It was
            # never malformed. handle_photo / handle_document pass
            # use_stream=False and were unaffected, which is why the
            # failures lined up with text turns and read as a flaky
            # engine rather than a request-building bug. 11 of 35
            # requests on 2026-10-22.
            #
            # ac79264 and a079e3c both had this condition; b8d8c11 lost it
            # again while moving the body into ToolLoop.run_one.
            # ns_handled = False, and it goes HERE -- before the branch
            # that may set it True, and before the branch that reads
            # it. Placed next to the read it is a bug, not a safety
            # net: the non-streaming branch has already assigned True
            # by then, this resets it to False, and the streaming
            # branch runs on every turn -- including the
            # use_stream=False ones that just took the non-streaming
            # path. The mutation run on 2026-10-22 caught that:
            # selftest_llm's check_nonstream_tool_call reached
            # stream_iteration with a fake client that has no .stream,
            # and the only thing the log said was an AttributeError
            # about a missing method.
            ns_handled = False
            if not self.use_stream:
                try:
                    result = await non_stream_iteration(
                        req_body, iteration=iteration,
                        abort_event=self.abort_event)
                    if result.outcome == "final":
                        return IterationResult.done(result.final_text)
                    log.info(f"[LLAMA] iter={iteration} non-stream "
                             f"finish={result.finish_reason} "
                             f"content_chars={len(result.content)} "
                             f"reasoning_chars={len(result.reasoning)} "
                             f"tool_calls={len(result.tool_calls)}")
                    if result.reasoning and self.thinking_msg is not None:
                        await push_thinking(
                            self.accumulated_reasoning + result.reasoning,
                            thinking_msg=self.thinking_msg, silent=self.silent,
                            throttle=self.throttle, force=True)
                    self.accumulated_reasoning += result.reasoning
                    _ns_step = non_stream_step(
                        result,
                        accumulated_reasoning=self.accumulated_reasoning,
                        saw_unavailable_tool=saw_unavailable_tool,
                    )
                    # is_terminal, not `terminal is not None`: the two
                    # disagree on the abort path, where the turn ends with a
                    # legitimate None.
                    if _ns_step.is_terminal:
                        if result.outcome == "final":
                            # A diagnostic, not an answer: the engine
                            # said something that is not the model's
                            # reply (an overflow, an abort, no choices).
                            # There are no tools to show with it.
                            return IterationResult.done(
                                _ns_step.assert_terminal())
                        # TEXT: the model's answer. It carries the tools
                        # the turn ran, exactly like the streaming path
                        # does -- the answer is what the chat gets, and
                        # the transcript with the console output is
                        # part of it.
                        return IterationResult.done(
                            finish_without_tool_calls(
                                content_buf=result.content,
                                reasoning_buf=result.reasoning,
                                finish_reason=result.finish_reason,
                                executed_tools=self.executed_tools))
                    saw_unavailable_tool = _ns_step.saw_unavailable_tool
                    content_buf = _ns_step.content_buf
                    reasoning_buf = _ns_step.reasoning_buf
                    tool_calls_buf = _ns_step.tool_calls_buf
                    ns_handled = True
                except Exception as e:
                    # TECH-DEBT #8 block 4: the DECISION moved out; the await
                    # stays, because C1 is right that a failure in the retry
                    # belongs to the handler that knows which iteration it is.
                    _err = classify_error(e, req_body)
                    log.info(f"[non-stream err iter={iteration}] "
                             f"{type(e).__name__}: {e}; body={_err.body!r}")
                    if _err.retry_without_tools:
                        return IterationResult.done(await tools_less_fallback(
                            req_body, iteration=iteration,
                            abort_event=self.abort_event, primary_error=e))
                    return IterationResult.done(_err.message)
            if not ns_handled:
                # TECH-DEBT #8 block 3: the streaming branch and the
                # non-streaming fallback it owns live in llm_stream. The
                # outcome carries both meanings the branch used to mix.
                _stream = await stream_iteration(
                    client, req_body,
                    iteration=iteration,
                    abort_event=self.abort_event,
                    accumulated_reasoning=self.accumulated_reasoning,
                    thinking_msg=self.thinking_msg, silent=self.silent,
                    throttle=self.throttle,
                    saw_unavailable_tool=saw_unavailable_tool,
                    final_fallback=self.final_fallback,
                    finish_reason=finish_reason,
                    tool_calls_buf=tool_calls_buf)
                # Branch on is_terminal, never on `terminal is not None`.
                if _stream.is_terminal:
                    return IterationResult.done(_stream.assert_terminal())
                if _stream.append_msg is not None:
                    self.msgs.append(_stream.append_msg)
                saw_unavailable_tool = _stream.saw_unavailable_tool
                self.final_fallback = _stream.final_fallback
                reasoning_buf = _stream.reasoning_buf
                content_buf = _stream.content_buf
                tool_calls_buf = _stream.tool_calls_buf
                finish_reason = _stream.finish_reason
        # commit accumulated reasoning for next-iteration display
        self.accumulated_reasoning += reasoning_buf

        # assemble final message
        msg = {
            "role": "assistant",
            "content": content_buf or None,
            "reasoning_content": reasoning_buf or None,
        }
        normalized = normalize_tool_calls(tool_calls_buf,
                                          known=known_tool_names())
        saw_unavailable_tool = (saw_unavailable_tool
                                or normalized.saw_unavailable_tool)
        for bad_name in normalized.rejected:
            log.info(f"[TOOL-ISOLATION] iter={iteration} soft-reject "
                     f"streamed tool name {bad_name!r}; injecting tool "
                     f"result feedback")
        tool_calls = normalized.calls
        if tool_calls:
            msg["tool_calls"] = tool_calls
        tool_calls = msg.get("tool_calls") or []
        log.info(f"[LLAMA] iter={iteration} finish={finish_reason} "
                 f"content_chars={len(content_buf)} "
                 f"reasoning_chars={len(reasoning_buf)} "
                 f"tool_calls={len(tool_calls)} "
                 f"req_tools={'yes' if self.req_tools else 'no'}")
        for tc in tool_calls:
            log.info(f"[LLAMA]   call: "
                     f"{tc.get('function',{}).get('name','')}"
                     f"({tc.get('function',{}).get('arguments','')[:200]})")

        if not tool_calls:
            return IterationResult.done(finish_without_tool_calls(
                content_buf=content_buf, reasoning_buf=reasoning_buf,
                finish_reason=finish_reason,
                executed_tools=_executed_tools_in_turn))
        # If we asked for tools and the model produced some, run them.
        # If we did NOT ask for tools (vision/math turn) and the model
        # still hallucinated a tool_call, return whatever content we have
        # rather than enter a loop the user can't escape.
        if self.req_tools is None:
            log.warning(f"[LLAMA] iter={iteration} WARNING: model "
                        f"hallucinated tool_calls={len(tool_calls)} with "
                        f"req_tools=None; aborting tool loop")
            return IterationResult.done(refuse_hallucinated_tools(
                content_buf=content_buf, reasoning_buf=reasoning_buf,
                finish_reason=finish_reason,
                executed_tools=_executed_tools_in_turn))
        if content_buf:
            self.final_fallback = content_buf
        self.msgs.append(msg)

        # --- execute tool calls ---
        #
        # Executing one call is not a loop concern and lives in
        # llm_dispatch.py; screening one is not either and lives in
        # llm_toolargs.py. What stays here is what only the loop knows --
        # the log line, the tool result, and the empty-call budget.
        deps = DispatchDeps(bot=self.bot, chat_id=self.chat_id,
                            current_message_id=self.current_message_id,
                            chat_type=self.chat_type,
                            iteration=iteration,
                            custom_tools=self.custom_tools,
                            donsetch_tools=DONSETCH_TOOLS,
                            reply_to_message_id=self.reply_to_message_id)
        log.info(f"[loop] iter={iteration} tool_calls={len(tool_calls)} "
                 f"finish={finish_reason} "
                 f"content_chars={len(content_buf)} "
                 f"reasoning_chars={len(reasoning_buf)}")
        for tc in tool_calls:
            fn_name = tc.get('function', {}).get('name', '')
            raw_args = tc.get('function', {}).get('arguments', '{}')
            # --- Defensive parse of tool call args ---
            # Qwen3.8-27B-Ultra-Heretic (and a few other recent models
            # with speculative decoding / MTP heads) sometimes emit
            # malformed tool-call JSON during long chains. Detect up
            # front: suspiciously long, unparseable-but-non-empty,
            # parsed-to-{}-from-non-empty, or the same key more than
            # three times. This costs one iteration but prevents the
            # runaway loop that ends in a 500 from llama-server. The
            # screening lives in llm_toolargs.py.
            parsed_args = parse_tool_args(fn_name, raw_args)
            if isinstance(parsed_args, ArgsRejected):
                log.info(f"[tool] iter={iteration} REJECTED {fn_name} args "
                         f"{parsed_args.detail}")
                self.msgs.append({
                    'role': 'tool',
                    'tool_call_id': tc.get('id', ''),
                    'content': parsed_args.message,
                })
                self.guard.note_rejected_call()
                continue
            args = parsed_args.args
            tc_id = tc.get('id', '')
            current_calls.append((fn_name,
                                  json.dumps(args, sort_keys=True)
                                  if isinstance(args, dict) else str(args)))
            log.info(f"[tool] iter={iteration} call {fn_name}({args})")

            outcome = await execute_tool(fn_name, args, deps)

            if outcome.unavailable:
                saw_unavailable_tool = True
            if not outcome.empty:
                iter_had_real_result = True
            if outcome.ledger is not None:
                _executed_tools_in_turn.append(outcome.ledger)
            self.msgs.append({
                'role': 'tool',
                'tool_call_id': tc_id,
                # The cut is marked, not silent. A tool result that is
                # shorter than the text says nothing about what is
                # missing, and a model that cannot see that it is
                # looking at a fragment answers as if it had seen the
                # whole thing.
                'content': condense(outcome.text, MAX_TOOL_RESULT_CHARS),
            })

        # T49: removed the early-return on saw_unavailable_tool. The
        # synthetic tool result is already in msgs. We let the loop
        # continue so the model can see the feedback on the next
        # iteration and either retry with a whitelisted tool or fall
        # back to plain text. The T32c stuck-call detector handles the
        # model asking for the same forbidden tool repeatedly.
        if saw_unavailable_tool:
            log.info(f"[loop] iter={iteration} unhandled tool(s) called; "
                     f"continuing so model can see the synthetic tool "
                     f"result on the next iter and retry with a "
                     f"whitelisted tool")

        # If the model is asking for the same tool+args as in the previous
        # iteration, it has looped -- return whatever we have and stop.
        # T32c (2026-10-13): guard against current_calls being empty (e.g.
        # all tool calls in this iter were REJECTED for malformed args, so
        # current_calls was never appended to). An empty list equals
        # prev_calls=[] on the first iter, which would trigger a spurious
        # "stuck" detection and IndexError.
        elapsed = time.monotonic() - self.loop_start
        outcome = self.guard.tick(
            iteration=iteration,
            current_calls=current_calls,
            elapsed=elapsed,
            iter_had_real_result=iter_had_real_result,
            final_fallback=self.final_fallback,
            reasoning_tail=(self.accumulated_reasoning
                            or reasoning_buf)[-3500:],
            finish_reason=finish_reason,
            last_tool_text=self._last_tool_text(),
        )
        if isinstance(outcome, Abort):
            return IterationResult.done(outcome.text)
        if outcome is not None:
            self.msgs.append({'role': 'user', 'content': outcome.text})

        if isinstance(outcome, Nudge):
            return IterationResult.done(await self._summarize())

        # Neither Abort nor Nudge: the turn carries on. Returning this
        # explicitly is the point of the type -- a `run_one` that fell off
        # its end would hand the loop None, and `None.is_terminal` is an
        # AttributeError inside the loop rather than a state it can read.
        return IterationResult.again()

    def _last_tool_text(self) -> Optional[str]:
        """What the last tool said, in the tool's own words.

        The abort path puts this in the user's answer. The model is
        not asked to summarise it: the model already had its chance and
        used it to repeat the same call, and a paraphrase of a refusal
        is how a refusal turns into a story (2026-10-26).
        """
        for message in reversed(self.msgs):
            if message.get("role") == "tool":
                return condense(str(message.get("content") or ""),
                                MAX_TOOL_RESULT_CHARS)
        return None

    # --- the Nudge path ------------------------------------------------
    #
    # The summarize call is a fourth POST, so it lives in the loop's
    # module and not in llm_stream -- it has nothing to do with SSE.

    async def _summarize(self) -> Any:
        """Final non-streamed summarize call (no tools, plain answer)."""
        async with httpx.AsyncClient(
                timeout=limits.LLAMA_HTTP_TIMEOUT_SEC) as client3:
            r3 = await client3.post(
                f"{LLAMA_URL}/chat/completions",
                headers={"Authorization": f"Bearer {API_KEY}",
                         "Content-Type": "application/json"},
                json={
                    "model": MODEL,
                    "messages": self.msgs,
                    "tool_choice": "none",
                    "max_tokens": self.max_tokens,
                    "stream": False,
                }
            )
            r3.raise_for_status()
            data3 = r3.json()
        # T32b (2026-10-13): guard. Without this, a 200 OK with choices:[]
        # (rate limit / content filter / context overflow) would produce
        # 'list index out of range' in the bot's reply.
        _choices3b = data3.get("choices") or []
        if not _choices3b:
            # Same classification as every other empty-choices site. The
            # guard was missing here once and the test claiming to cover
            # it passed anyway: it asked whether the MODULE called
            # is_context_overflow anywhere, and the answer came from a
            # guard belonging to a different branch.
            if is_context_overflow(
                    json.dumps(data3.get("error") or data3)[:500]):
                log.info("[call_llama] CONTEXT OVERFLOW on summarize "
                         "fallback")
                return OVERFLOW_MESSAGE
            log.info("[call_llama] summarize fallback: empty choices, "
                     "returning explanatory error")
            return ("[bot: the model returned no choices on the "
                    "summarize call — the prompt may have exceeded the "
                    "context window, hit a rate limit, or been blocked "
                    "by a content filter. Try a shorter prompt or try "
                    "again in a minute.")
        msg3 = _choices3b[0].get("message") or {}
        # The empty answer here is not a search that found nothing: it
        # is the model returning no text at all after the tool loop
        # ended in a nudge. The old wording talked about flight prices
        # and Google Flights, which is what a user asked for on some
        # other day and what this user got on 2026-10-24 while asking
        # for a zip file. A diagnostic that describes a different
        # situation is worse than no diagnostic: it sends the user
        # looking for a search that never happened.
        return msg3.get("content") or (
            '[bot: the model returned an empty answer, so there is '
            'nothing to send. Ask again, or ask for something simpler.]'
        )
