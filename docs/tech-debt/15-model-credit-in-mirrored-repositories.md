---
tech_debt: 15
marker: tests/test_contributor_panel_audit.py
marker_kind: tripwire
state: open
issue: null
---

## 15. Two mirrored repositories carry a model account on their contributors panel

**Measured 2026-10-31.** The contributors panel of two of the
operator's repositories shows `claude` (uid 81847):

| repository | panel count | model account | why the credit is there |
|---|---|---|---|
| `vaskes/greenboost` | 2 | `claude` | one commit carries `Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>` |
| `vaskes/rtx-5090-strata-nvfp4` | 269 | `claude` | upstream contributors are credited, including by trailer |

Both are **copies of other people's projects** -- `greenboost` is a fork
of Ferran Duarri's work under GPL v2, and `rtx-5090-strata-nvfp4` is a
copy of `sergqwer/strata-nvfp4`. Neither has an upstream sync
workflow, so the credit is frozen: it will not come back on its own,
and nothing in this repository can stop it from coming back either.

**Why this is a ledger item and not a fix.** The obvious fix is a
history rewrite, and for `greenboost` that rewrite would destroy the
attribution the GPL v2 requires the operator to keep. That is a worse
outcome than a name on a panel, and it is not the agent's call to make
silently. The other lever is GitHub's "block user", which needs a
fine-grained token this project does not have and a click in a browser
the operator has to make by hand.

So the credit stays, and it is written down: the exemptions live in
`scripts/contributor-panel-baseline.json`, each with its reason and the
date it was recorded. The audit reports those repositories as **KNOWN**
-- with the account named in the output, not hidden behind a word like
"expected" -- and the run still passes. Withdraw an entry and the same
repository turns red on the next run.

**The scope of the audit is not this list.** The audit asks GitHub
which repositories the owner has and checks every one of them, so a
repository created today is covered today -- a credit in a repository
that nobody remembered to add to a list would be a credit nobody
looked for. The two repositories above are the ones that need a
decision; the rest are clean today.

**The trigger for revisiting this.** The exemption stops being honest
if any of these become true:

* the operator rewrites the mirrored history by hand, or forks the
  upstream under a licence that does not require attribution;
* the mirror gains an upstream sync workflow -- then the credit is not
  frozen any more, and the copy is ours to maintain;
* GitHub's block list becomes reachable from CI, and its effect on a
  contributors panel is measured rather than assumed.

**What is not a fix.** Adding `claude` to a keyword blacklist across
the fleet. That is a rule about a word, and it takes down `devin`
(uid 6137) and `mavis` (uid 1176898), who are people and who are on
real panels. The check matches on the account's uid, which survives a
rename, and it was built from a measurement of which addresses resolve
to an account at all.

## Related

- `tests/test_no_model_attribution.py` guards the history of THIS
  repository. It cannot guard a repository this project does not push
  to, and it cannot see a panel it never reads.
- `scripts/audit_contributor_panels.py` reads the panel itself. The
  two checks fail for different reasons, and neither substitutes for
  the other.
