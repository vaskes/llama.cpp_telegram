"""Every command the bot advertises, in the order it shows
them. A command advertised here but not registered is a
lie the user finds out about; the tests check both ways.


Every byte of the prompt text is preserved from `prompts.py`.
The sha256 pins in tests/test_prompts.py check that.
"""
BOT_COMMANDS = [
    ("start",   "🏁 Start working, show what the bot can do"),
    ("help",    "❓ Help on the commands"),
    ("here",    "📍 Show the current sub-talk"),
    ("subs",    "📚 List all sub-talks (with buttons)"),
    ("newsub",  "➕ Create a new sub-talk: /newsub <name>"),
    ("sub",     "↔️ Switch to a sub-talk: /sub <name>"),
    ("delsub",  "🗑 Delete a sub-talk: /delsub <name>"),
    ("reset",   "🔄 Clear the messages in the current sub-talk"),
    ("stats",   "📊 Stats and the list of sub-talks"),
]
