---
tech_debt: 12
marker: none yet
marker_kind: none
state: open
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
