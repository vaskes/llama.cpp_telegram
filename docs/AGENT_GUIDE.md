# Agent Guide — для AI-агентов

Короткий справочник по эксплуатации этого репо. Если ты — AI-агент и тебя
попросили что-то сделать с ботом, начни с этого файла.

## Где что лежит

```
/opt/telegram-bot/          ← рабочая копия (создаётся install.sh)
├── bot.py                  ← весь код бота
├── Dockerfile              ← python:3.11-slim + pip install
├── docker-compose.yml      ← network_mode: host
├── .env                    ← credentials (НЕ коммитить)
├── requirements.txt        ← python-telegram-bot, httpx
└── files/                  ← статика (сейчас пусто)

/opt/whisper-api/
├── docker-compose.yml      ← fedirz/faster-whisper-server
└── cache/                  ← скачанные модели

/etc/systemd/system/
├── telegram-bot-compose.service
└── whisper-api-compose.service
```

## Частые операции

### Узнать, что бот живой
```bash
sudo docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" | grep -E "(telegram|whisper|llama)"
sudo docker logs telegram-bot --tail 20
sudo docker logs whisper-api --tail 20
```

### Проверить llama-server
```bash
curl -s -m 5 http://localhost:8080/health        # → {"status":"ok"}
curl -s -m 5 http://localhost:8080/v1/models | head -c 400
```

### Проверить SearXNG
```bash
curl -s -m 5 "http://localhost:8888/search?q=test&format=json" | head -c 200
```

### Перезапустить бот (например, после правки bot.py)
```bash
cd /opt/telegram-bot
sudo docker compose build telegram-bot
sudo docker compose up -d --no-deps --force-recreate telegram-bot
sudo docker logs telegram-bot --tail 10
```

### Добавить нового юзера в whitelist
```bash
sudo $EDITOR /opt/telegram-bot/.env
# → добавьте user_id в ALLOWED_USER_IDS=через_запятую
sudo systemctl restart telegram-bot-compose
```

### Узнать Telegram user_id человека
- попросите человека написать `/start` **нашему** боту
- бот его не пустит (LOCKDOWN если user_id не в whitelist), но в логах:
  ```
  [SECURITY] rejected id=XXXXXXXXX @username msg='/start'
  ```
- или пусть человек напишет `/start` боту **@userinfobot** в Telegram

### Посмотреть, кого бот отверг (security log)
```bash
sudo docker logs telegram-bot 2>&1 | grep "SECURITY" | tail -30
```

### Обновить до последней версии
```bash
cd /path/to/llama.cpp_telegram
./scripts/update.sh
```

## Что НЕЛЬЗЯ делать

1. ❌ Коммитить `.env` — там токены. Проверьте `git status` перед `git add`.
2. ❌ Менять `network_mode: "host"` без понимания последствий — это дыра в сеть.
3. ❌ Включать `parallel_tool_calls: true` без тестирования — текущая реализация
   рассчитана на последовательные вызовы.
4. ❌ Прописывать реальные credentials в `docker-compose.yml` — только env vars.
5. ❌ Удалять `DISABLED_TOOLS` из `bot.py` без re-аудита. `read_file`,
   `write_file`, `exec_shell_command` отключены не случайно — это значит
   **любой** авторизованный юзер мог бы через tool-calling выполнить
   произвольный shell на хосте. НЕ включайте их.

## Если юзер жалуется, что бот «не отвечает»

Сначала проверь по чек-листу:

```bash
# 1) бот вообще запущен?
sudo docker ps | grep telegram

# 2) контейнер свежий? что в логах?
sudo docker logs telegram-bot --tail 50 | tail -30

# 3) в LOCKDOWN ли?
sudo docker logs telegram-bot --tail 200 | grep -E "(LOCKDOWN|whitelist)"

# 4) есть ли отказы по этому юзеру?
sudo docker logs telegram-bot --tail 200 | grep "rejected id=ТОТ_ЖЕ_ID"

# 5) llama-server живой?
curl -s -m 5 http://localhost:8080/health
```

Если `LOCKDOWN` — у юзера не в whitelist. Проверь `ALLOWED_USER_IDS` в `.env`.

Если `rejected id=...` — бот его знает, но не пускает. Добавь в whitelist.

Если llama-server не отвечает — это **отдельная задача**, не относится к этому репо.

## Техдолг, который я знаю

- В `bot.py` лежат хардкод-ссылки на `192.168.10.7:8080` в комментариях, но
  в коде используются env vars `LLAMA_URL` и `WHISPER_URL`. Так и задумано.
- SearXNG engines на cloud IP часто CAPTCHA-ятся. `get_weather` через wttr.in
  работает всегда. Если юзер спрашивает «почему гугл не ищет» — это
  [известная проблема](https://github.com/searxng/searxng/issues/...)
  с user-agent detection, не код бага.
- Docker network mode `host` означает, что бот не имеет своего IP —
  `0.0.0.0` биндинги в контейнере занимают порты хоста. Если что-то
  начнёт конфликтовать — переход на bridge-сеть + явные порты.
