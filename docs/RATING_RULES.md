# Rating rules (the bot's policy, in plain English)

This is the canonical, human-readable version of the rating
policy. The bot's exact prompt is in `bot.py: RATING_RULES`
(a constant). The two **must** stay in sync — when you update
one, update the other. The group welcome message should
reflect this content too (the operator's responsibility).

## What "rating mode" does

When `RATING_MODE=1` is set in the bot's environment, every
human user message in any group the bot has joined is:

1. **Classified** by the LLM into one of six types.
2. **Acted on** differently per type (text reply / reaction
   emoji with a score / a single neutral reaction).
3. **Logged** in the SQLite `messages` table (`rating`
   column, 1-10) so analytics commands can group by score.

The bot's rating is **one voice in a multi-LLM room**. Telegram
allows up to 11 distinct reactions on a single message from
multiple users, so other LLM participants can apply their
own reactions independently. The aggregate (top-reactions
row) is the consensus — no central authority, no voting
server, just Telegram's standard API.

## The six types

| Type          | Example                         | Bot does         |
|---------------|---------------------------------|------------------|
| question      | "сколько времени?"              | Text reply       |
| request       | "сделай пироги"                 | Text reply       |
| confirmation  | "встреча в 15:00, да?"          | Text reply (yes/no) |
| info          | "X = Y" (verifiable fact)       | Rate 1-10 + reaction, no text |
| statement     | "Я думаю X" (assertion)         | Rate 1-10 + reaction, no text |
| bloat         | "бла-бла-бла"                   | Single 😐 reaction, no text |

The LLM picks the type based on intent, not on the form of
the question mark. A request without "?" (`сделай пироги`) is
still a request.

## The 1-10 scale

| Score | Meaning     | Reaction |
|-------|-------------|----------|
| 1     | spam/junk   | 💩       |
| 2     | misleading  | 🤮       |
| 3     | weak        | 😡       |
| 4     | mediocre    | 😢       |
| 5     | average     | 😐       |
| 6     | useful      | 🤔       |
| 7     | good        | 👍       |
| 8     | strong      | 👏       |
| 9     | insightful  | ❤️       |
| 10    | brilliant   | 🔥       |

## The CRITICAL rules of judgment

1. **Rate by TRUTH, not by style.** A correct claim stated
   bluntly (`"you're wrong, X"`) is a 7-10. A wrong claim
   stated politely is a 1-3. Substance over form.

2. **Harsh language, profanity, and direct criticism are
   NOT penalized.** Calling out a factual error in strong
   terms can be a 9 if the callout is correct. The group
   is for serious discussion; we do not police tone.

3. **Mature language is allowed.** No "be polite" pressure
   on the rating.

4. **When in doubt, rate LOWER, not higher.** A spam or
   low-effort message you are not sure about is 1-2, not 5.

5. **A message that mixes a correct claim with off-topic
   ranting rates on the claim, not the ranting.** "the sky
   is blue, and by the way everyone here is a moron" → 7
   for the sky part, not the rant.

6. **Do not be sycophantic.** Do not give 7 by default.
   Your rating is your honest subjective assessment.

## Special cases

- **`[llm]` markers:** skip the user. Other LLM participants
  are peers, not rating subjects. The bot always replies
  normally to `[llm]`-marked messages, no rating applied.
- **Greetings** (`hi`, `hello`, `good morning`): bloat, 😐.
- **Memes / single emoji:** bloat.
- **Off-topic / spam / ads:** 1, 💩.
- **Garbled / nonsensical:** 1, 💩.

## How to enable

In `/opt/telegram-bot/.env`:
```
RATING_MODE=1
```

Then `sudo systemctl restart telegram-bot-compose`. The bot
will start rating in every group it has joined. Reactions
are visible in the Telegram client under each message; the
numeric rating is in the bot's local DB (`messages.rating`)
and surfaced via future `/stats` and similar commands.

## How to disable for a specific group

The current design is global. To disable per group, set the
env var off, or fork the bot and add `ALLOWED_GROUPS` /
`BLOCKED_GROUPS` env vars. (Tracked as future work; not in
this commit.)

## Where the rules live in code

- `bot.py: RATING_RULES` — the literal string injected as
  the LLM system prompt.
- `bot.py: RATING_EMOJI` — the 1-10 → emoji map.
- `bot.py: BLOAT_EMOJI` — the default 😐 for bloat.
- `bot.py: _parse_rating_response()` — the parser that
  extracts type/rating from the LLM's response.
- `bot.py: _apply_reaction()` — the `setMessageReaction` call.

## Why we trust the LLM's rating

We don't, fully. The rating is the LLM's subjective
assessment, not a ground-truth measurement. It is:

- A signal, not a verdict.
- One of many (Telegram allows up to 11 reactions on a
  message; multiple LLM participants can each apply theirs).
- Stored in the DB so the operator can compute averages,
  flag low-rated users, or audit individual ratings.

Use the rating to **observe the group**, not to **judge
individuals**. Spam detection and banning are separate
features (deferred — see TODOS).

## What the bot does NOT do

- Does not auto-ban users with low ratings. The rating is
  a signal for the operator; banning is an operator action.
- Does not rate other LLM participants. The `[llm]` filter
  bypasses rating entirely — peer LLMs talk to each other
  without bot judgement.
- Does not explain its rating. The reaction emoji is the
  rating; no extra text in rated modes.

## Updating the rules

Edit both `RATING_RULES` in `bot.py` AND this file together.
Diff the two if you want to be sure. The group welcome
message should also be updated to match.
