"""Every command the bot advertises, in the order it shows
them. A command advertised here but not registered is a
lie the user finds out about; the tests check both ways.


Every byte of the prompt text is preserved from `prompts.py`.
The sha256 pins in tests/test_prompts.py check that.
"""
BOT_COMMANDS = [
    ("start",   "🏁 Начать работу, показать возможности"),
    ("help",    "❓ Помощь по командам"),
    ("here",    "📍 Показать текущий sub-talk"),
    ("subs",    "📚 Список всех sub-talks (с кнопками)"),
    ("newsub",  "➕ Создать новый sub-talk: /newsub <имя>"),
    ("sub",     "↔️ Переключиться на sub-talk: /sub <имя>"),
    ("delsub",  "🗑 Удалить sub-talk: /delsub <имя>"),
    ("reset",   "🔄 Очистить сообщения в текущем sub-talk"),
    ("stats",   "📊 Статистика и список sub-talks"),
]
