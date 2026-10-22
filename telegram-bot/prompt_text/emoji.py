"""Rating reactions. BLOAT_EMOJI is the one used when a message
is rated as contentless.


Every byte of the prompt text is preserved from `prompts.py`.
The sha256 pins in tests/test_prompts.py check that.
"""
RATING_EMOJI = {
    1: "💩", 2: "🤮", 3: "😡", 4: "😢", 5: "😐",
    6: "🤔", 7: "👍", 8: "👏", 9: "❤️", 10: "🔥",
}

BLOAT_EMOJI = "😐"
