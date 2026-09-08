# Setup — для людей

Пошаговая инструкция для голой Ubuntu 24.04 (или похожей).
Предполагается, что вы работаете от пользователя с `sudo` без пароля.

## 0. Предусловия

Проверьте, что у вас есть:

```bash
docker --version          # Docker 24+
docker compose version    # v2 (compose — подкоманда, не бинарь)
git --version
sudo -n true              # sudo без пароля (или замените на sudo -v)
```

Если `docker compose` отдельно не установлен, поставьте плагин:
```bash
sudo apt-get install -y docker-compose-plugin
```

## 1. Клонируем

```bash
sudo mkdir -p /opt
sudo chown $USER:$USER /opt
git clone https://github.com/vaskes/llama.cpp_telegram.git
cd llama.cpp_telegram
```

## 2. Запускаем install.sh

```bash
sudo ./scripts/install.sh
```

Скрипт:
- копирует файлы в `/opt/telegram-bot/` и `/opt/whisper-api/`
- создаёт `.env` из `.env.example` если его нет
- регистрирует systemd-юниты `telegram-bot-compose.service` и `whisper-api-compose.service`
- включает их (но не запускает — сначала настройте `.env`)

## 3. Узнаём свой Telegram user_id

Напишите любому боту, который умеет показывать user_id (например **@userinfobot**).
Скопируйте число.

## 4. Заполняем `.env`

```bash
sudo $EDITOR /opt/telegram-bot/.env
```

Минимум, что нужно поставить:
- `BOT_TOKEN` — от @BotFather (создайте бота командой `/newbot`)
- `ALLOWED_USER_IDS=ВАШЕ_ЧИСЛО` — иначе бот будет в LOCKDOWN и никого не пустит
- `LLAMA_URL=http://localhost:8080/v1` — адрес вашего llama-server

Опционально:
- `MODEL` — должно совпадать с `--alias` на llama-server
- `ALLOWED_USERNAMES` — второй способ (по `@username`, case-insensitive)
- `SEARXNG_URL` — если поднят SearXNG

## 5. Поднимаем зависимости

Этот репозиторий **не** запускает llama-server — это ваша задача.
Минимально нужно:

### Вариант A: у вас уже есть llama-server
Ничего не делайте. Бот подключится на `LLAMA_URL`.

### Вариант B: ставим с нуля на Radeon 780M
Следуйте инструкциям в [vaskes/llama.cpp-rocm-780m](https://github.com/vaskes/llama.cpp-rocm-780m).

### Вариант C: для tool-calling (SearXNG)
Следуйте инструкциям в [vaskes/llama.cpp_search](https://github.com/vaskes/llama.cpp_search)
— там готовый SearXNG + Playwright MCP, и `--mcp-servers-config` для llama-server.

## 6. Стартуем

```bash
sudo systemctl start whisper-api-compose
sudo systemctl start telegram-bot-compose
sudo systemctl status telegram-bot-compose
sudo docker logs telegram-bot --tail 30
```

Если в логах `🤖 LlamaBot v2 (with tool-calling) started...` — бот живой.

## 7. Проверяем

Откройте Telegram, найдите бота, напишите `/start`.
Должен прийти приветственный текст.

Затем:
- «погода в Ялте» — бот вызовет `get_weather` и вернёт реальную температуру
- «что нового в AI» — если есть SearXNG, бот вызовет `searxng_search`
- голосовое — бот расшифрует через Whisper и ответит на текст

## 8. Логи и обновления

```bash
# логи
sudo docker logs -f telegram-bot
sudo docker logs -f whisper-api

# обновить бот до последней версии
cd llama.cpp_telegram
./scripts/update.sh
```

## 9. Бэкап перед обновлением

`scripts/update.sh` НЕ трогает `.env`, но если хотите перестраховаться:

```bash
sudo cp /opt/telegram-bot/.env /opt/telegram-bot/.env.bak
```

## Что делать, если бот молчит

См. [TROUBLESHOOTING.md](TROUBLESHOOTING.md).
