---
tech_debt: 12
marker: tests/test_prompts.py
marker_kind: tripwire
state: closed
issue: null
---

## 12. `prompts.py` is 423 lines of module constants (MEDIUM, closed)

**Measured 2026-10-22:** 422 lines, **zero** top-level functions. It is
entirely prompt text.

**What is already right.** `persona.json` exists and is the correct
half of this solution — persona data is data, not code. Finishing it
consistently is the point of the item.

**Why the test comes first, not after.** Moving text into files must not
change what the bot says. Without a byte-exact test, "move the prompts
out" becomes "change the prompts and hope", and 423 lines of prose is
far too much to review by eye. The comparison has to be
byte-for-byte between the assembled prompt before and after.

**The specific trap this has already hit.** `tests/test_t54` tested its
own copy of the code rather than the production module, so it was green
and wrong. Whatever test lands here has to import `prompts` and assert
against production output, not against a fixture that duplicates the
logic.

**Also in scope when this is picked up:** the "never invent" persona
rule currently lives in a docstring. It belongs in the rendered prompt
text, phrased as a directive naming the class of thing not to invent
rather than an enumeration of fields.


---

## Progress, 2026-10-22

The test this item asked for first now exists: `tests/test_prompts.py`,
24 tests, six mutations all caught. Two layers -- sha256 content pins so
every wording change is deliberate and visible, and semantic properties
that have to survive the relocation (advertised commands must be
registered, the five mandatory GROUP_CONTEXT sections, all five bash
safety categories, and the rating prefix regex's anchoring and 1-10
range).

Two of the six mutations changed no pinned constant and were caught only
by the semantic layer, which is the evidence that the second layer is
doing work rather than restating the first.

**Relocation done — `6c6adc1`, Dockerfile `ad9850d`.** The prose is in
`telegram-bot/prompt_text/`, one file per prompt: `group_context.py`,
`rating_rules.py`, `welcome.py`, `commands.py`, `emoji.py`.
`prompts.py` is a facade at 75 lines — four regexes, `RATING_MODE`,
and re-exports, because eight modules import from it. `RATING_MODE`
stays because it reads the environment: a switch, not a sentence.

Every constant is byte-identical, verified by hashing all twelve against
the previous revision. Not read, not eyeballed: 13828 characters of
English and Russian. The sha256 pins now read `prompt_text`, so they
are a contract about the files and not about a re-export.

**9 mutations, 9 caught. Two were INVALID first time and were rewritten
rather than counted** — one broke an import, the other edited a string
that is not in the file. The harness now hashes every candidate file
and refuses to report a no-op as a survivor.

**The Dockerfile was the interesting failure.** The image built and the
bot died on startup with `ModuleNotFoundError: No module named
'prompt_text'`: `COPY *.py ./` matches top-level files only, and the
suite was green the whole time because it runs against the host tree,
which has the package. The same trap already had a comment and a
dedicated COPY line for `personal_tools`, for the same reason.

**Three tripwires read `prompts.py` as text** and had to be pointed at
the package, including `selftest_llm`'s GROUP_CONTEXT check — which had
already been bitten by exactly this once, by reading a file that no
longer held the thing it asserted, and which failed against correct
code for two deploys. #12 walked it into the same trap one step
further, in the other direction.

**The never-invent directive was moved out of a docstring on
2026-10-22 and nothing held it.** Deleting the line that appends it to
the rendered persona leaves every other test green and the bot quietly
starts filling in a biography the operator never wrote.
`test_t69_personality.py` says so now, and checks that the directive is
*last* in the block, so it reads as the thing that settles any question
about identity rather than one rule among many.

**Found while writing the test, not fixed:** `_RATING_PREFIX_RE` accepts
`[[RATE:0]]` and `[[RATE:11]]`. The RATE group is optional, so an
out-of-range score parses as a rating with no rate and the literal
marker lands in `rest`, where the user sees it. Pinned as current
behaviour. Tightening the alternation changes what counts as a rating in
production, so it is a product decision about malformed model output,
not a test-writing one.
