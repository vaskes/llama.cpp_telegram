"""What the bot believes about the donsetch service it talks to. One place.

The bot has two doors to the same server, and the second one is the
interesting one:

* `donsetch.py` -- a hand-written MCP client for the four web tools
  (`web_search`, `web_fetch`, `web_crawl`, `web_screenshot`), written
  before the generic MCP module existed and kept because it wants the
  **split shape**: a human-readable text result instead of the MCP
  `[meta]` envelope;
* `mcp_registry.py` -- the generic module, which asks any MCP server
  what it offers and calls its tools under the name `<server>_<tool>`.

Two doors to one server is the "one machine -- one door" question with
a real answer here, and the answer is written down in
`docs/TODO.md`: the split shape is a *server-side* knob
(`mcp.text_only = false`, or its legacy environment form
`DONSETCH_MCP__TEXT_ONLY=false`), so the generic door can have the
same shape as the native one. Until then the two doors differ in the
result they return, and the difference is a fact, not a preference.

So the facts live here, once:

* `DONSETCH_PINNED_VERSION` -- the version this repo pins as a
  submodule. `tests/test_donsetch_contract.py` reads the pin out of the
  tree and the version out of the submodule's own `Cargo.toml`, and
  compares them with this number, so one of the three cannot move
  without the others.
* `DONSETCH_MIN_VERSION` -- the floor. The split shape exists from
  v4.7.4 on (that is the version the production deployment ran with
  the text result the native client was written for), so a pin below
  the floor is a pin whose shape the bot's client does not expect.
* `DONSETCH_SPLIT_SHAPE_ENV` / `DONSETCH_SPLIT_SHAPE_VALUE` -- the
  server-side knob that produces the split shape, and the value the
  deployment has to set for the bot's native client to get text back.
* `DONSETCH_LICENSE` -- the submodule's license. It is **not** ours:
  the sandbox is MIT and this one is AGPL-3.0, and the operator-facing
  documents have to say which is which.

Everything else that talks about donsetch -- the README, the setup
guide, the architecture map -- points here instead of restating it.
Restating is how "4.7.4" outlived the deployment it described.
"""
from __future__ import annotations

__all__ = [
    "DONSETCH_REPO",
    "DONSETCH_PINNED_VERSION",
    "DONSETCH_MIN_VERSION",
    "DONSETCH_SPLIT_SHAPE_ENV",
    "DONSETCH_SPLIT_SHAPE_VALUE",
    "DONSETCH_LICENSE",
    "DONSETCH_NATIVE_TOOLS",
]

#: Where the submodule comes from. Third-party: the bot does not own it,
#: it consumes it over MCP.
DONSETCH_REPO = "dondai44423/donsetch"

#: The version this repo pins as a submodule. Keep it equal to the pin in
#: the tree and to the version the submodule's own source declares: a bump
#: of one without the others is a red test, not a quiet drift.
#
# v4.10.2 (2026-10-22) is the version the production service runs: the
# tag the submodule points at, and the release that fixed the eight
# compromised intermediate CA keys (2026-10) and hardened the MCP
# response mode behind an explicit `mcp.text_only` default.
DONSETCH_PINNED_VERSION = "4.10.2"

#: The floor. The split shape (a human-readable text result rather than
#: the `[meta]` envelope) is what the bot's native client is written
#: for, and it exists from v4.7.4 on.
DONSETCH_MIN_VERSION = "4.7.4"

#: The server-side knob that produces the split shape, and the value the
#: deployment sets. From v4.10 on, `mcp.text_only` defaults to **true**
#: (the MCP envelope), so the deployment has to say "false" explicitly
#: -- the knob is still supported, and the changelog says so.
DONSETCH_SPLIT_SHAPE_ENV = "DONSETCH_MCP__TEXT_ONLY"
DONSETCH_SPLIT_SHAPE_VALUE = "false"

#: The submodule's license. The sandbox is MIT (Kuduza Ai Lab); this one
#: is AGPL-3.0. A submodule is a separate work with its own terms, and
#: the documents that point at it have to say which is which.
DONSETCH_LICENSE = "AGPL-3.0"

#: The tools the bot's native client calls. The pinned server's own MCP
#: dispatch is the source of truth: `tests/test_donsetch_contract.py`
#: reads the names out of the submodule's source and compares them with
#: this list, so a renamed tool is a red test rather than a tool call
#: that fails in production and nowhere else.
DONSETCH_NATIVE_TOOLS = (
    "web_search",
    "web_fetch",
    "web_crawl",
    "web_screenshot",
)
