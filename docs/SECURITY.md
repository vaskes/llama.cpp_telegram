# Security

## Whitelist (главное)

Бот отвечает **только** тем, кто в `ALLOWED_USER_IDS` или `ALLOWED_USERNAMES`.

```bash
# /opt/telegram-bot/.env
ALLOWED_USER_IDS=123456789,987654321
ALLOWED_USERNAMES=myfriend,myotherfriend
```

**Если обе переменные пустые — LOCKDOWN.** Бот отвергает все сообщения.
Это безопасный дефолт: если вы забыли настроить whitelist, бот не
станет публичным.

### Как узнать свой Telegram user_id

1. Напишите `/start` **@userinfobot** — он ответит вашим ID.
2. Или напишите `/start` **нашему** боту — он не ответит, но в
   `docker logs telegram-bot` появится `[SECURITY] rejected id=XXXXX`.

## Credentials — что ГДЕ лежит

| Что | Где | Как защищено |
|---|---|---|
| `BOT_TOKEN` | `/opt/telegram-bot/.env` | `chmod 600`, **не в git** |
| `API_KEY` (LLM) | `/opt/telegram-bot/.env` | Обычно `sk-no-key` для localhost — не секрет |
| `WHISPER_MODEL` | `/opt/whisper-api/.env` | Публичное имя модели, не секрет |
| SearXNG URL | `/opt/telegram-bot/.env` | Не секрет |

## Что НЕ должно попасть в git

В этом репозитории `.env` файлы **никогда** не коммитятся — `.gitignore`
защищает. Если вы форкаете и пере-используете код, проверьте:

```bash
git status
# → ничего не должно показывать .env или files/ с данными
```

`.env.example` — это шаблон, его коммитить **можно и нужно**.

## Sandbox tools

В `bot.py` есть `DISABLED_TOOLS`. Сейчас в нём:

```python
DISABLED_TOOLS = {
    # Filesystem / shell — SECURITY RISK
    'read_file', 'write_file', 'edit_file', 'exec_shell_command',
    'file_glob_search', 'grep_search', 'get_info',
    # Playwright — НЕ РЕАЛИЗОВАНО в боте
    'playwright_*',
}
```

**Не удаляйте** `read_file` / `write_file` / `exec_shell_command` из
`DISABLED_TOOLS` без re-аудита безопасности. Через `exec_shell_command`
авторизованный юзер мог бы выполнить **любую** команду на хосте от имени
пользователя, под которым работает llama-server. Это эквивалент
`sudo` без пароля.

Если очень нужно — добавьте **whitelist команд**:

```python
ALLOWED_COMMANDS = {'ls', 'cat', 'grep'}
def safe_exec_shell(args):
    cmd = args.get('command', '')
    if cmd.split()[0] not in ALLOWED_COMMANDS:
        return '[error: command not in allowlist]'
    # ... subprocess ...
```

## `network_mode: "host"`

Контейнер бота видит всю сеть хоста. Это удобно, но:

- Бот может просканировать порты хоста (`127.0.0.1`)
- Бот может достучаться до `192.168.x.x` (LAN)
- Любая уязвимость в `python-telegram-bot` = удалённое выполнение на хосте

Если это критично, переходите на `bridge` с явными линками (см.
ARCHITECTURE.md).

## `RestrictToSpecificChats`

Если бот работает в группе, а не в личке, добавьте проверку:

```python
ALLOWED_CHAT_IDS = {int(x) for x in os.environ.get('ALLOWED_CHAT_IDS','').split(',') if x}

def is_authorized(update):
    ...
    if update.effective_chat.id not in ALLOWED_CHAT_IDS:
        return False
    ...
```

Сейчас бот **предполагает** работу в личке. Для групп нужен этот доп. фильтр.

## Ротация BOT_TOKEN

Если токен утёк:
1. `@BotFather` → `/revoke`
2. Обновите `BOT_TOKEN` в `/opt/telegram-bot/.env`
3. `sudo systemctl restart telegram-bot-compose`
