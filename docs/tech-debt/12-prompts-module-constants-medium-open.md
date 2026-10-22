---
tech_debt: 12
marker: tests/test_prompts.py
marker_kind: tripwire
state: partial
issue: null
---

## 12. `prompts.py` is 423 lines of module constants with no test (MEDIUM, open)

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

**Still open:** the relocation itself. 422 lines of prose becomes
several files, and `persona.json` -- already data rather than code --
needs to be carried to the same shape.

**Found while writing the test, not fixed:** `_RATING_PREFIX_RE` accepts
`[[RATE:0]]` and `[[RATE:11]]`. The RATE group is optional, so an
out-of-range score parses as a rating with no rate and the literal
marker lands in `rest`, where the user sees it. Pinned as current
behaviour. Tightening the alternation changes what counts as a rating in
production, so it is a product decision about malformed model output,
not a test-writing one.
