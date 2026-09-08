# llama.cpp_telegram

Telegram-бот-обёртка над [llama.cpp](https://github.com/ggml-org/llama.cpp) OpenAI-совместимым API
+ локальный Whisper для распознавания голосовых.

## Что это

Готовый к развёртыванию набор из двух контейнеров:

| Сервис | Порт | Назначение |
|---|---|---|
| `telegram-bot` | (нет) | Опрашивает Telegram, общается с LLM, поддерживает tool-calling |
| `whisper-api` | 8000 | Распознаёт голосовые через faster-whisper, отдаёт транскрипт боту |

Бот умеет:

- 💬 Текст, контекст диалога (20 последних сообщений на юзера)
- 🖼 Анализ изображений (vision-модели llama.cpp)
- 🎤 Голосовые через Whisper (русский)
- 📄 Чтение документов (TXT, PDF — текстовый слой)
- 🛠 **Tool-calling**: `get_weather` (wttr.in) + `searxng_search` / `searxng_fetch_url` / `searxng_engines`
  если поднят [llama.cpp_search](https://github.com/vaskes/llama.cpp_search) рядом
- 🔒 **Whitelist** по Telegram `user_id` / `@username` (env-переменные, LOCKDOWN по дефолту)

## Архитектура

```
┌──────────┐    HTTP     ┌──────────────┐   chat/completions   ┌─────────────┐
│ Telegram │◀───────────▶│  telegram-   │──────────────────────▶│  llama.cpp  │
│  user    │  Bot API    │  bot (host   │  +tool_calls loop    │  server     │
└──────────┘             │   network)   │                      │  (8080)     │
                         │              │   /v1/audio/         │             │
                         │              │   transcriptions     │             │
                         │              │─────────────────────▶│  whisper-   │
                         │              │                      │  api (8000) │
                         │              │   /search?format=json│             │
                         │              │─────────────────────▶│  searxng    │
                         └──────────────┘                      │  (8888)     │
                                                               └─────────────┘
```

`network_mode: host` — бот видит llama/whisper/searxng как localhost.
Это самое простое для standalone-машины, где все три сервиса на одном хосте.

## Быстрый старт

См. [docs/SETUP.md](docs/SETUP.md) — там пошагово.

Короткая версия:

```bash
git clone https://github.com/vaskes/llama.cpp_telegram.git
cd llama.cpp_telegram
sudo ./scripts/install.sh
sudo $EDITOR /opt/telegram-bot/.env   # BOT_TOKEN, ALLOWED_USER_IDS
sudo systemctl start whisper-api-compose
sudo systemctl start telegram-bot-compose
```

## Документация

- **[docs/SETUP.md](docs/SETUP.md)** — как установить на чистый хост (для людей)
- **[docs/AGENT_GUIDE.md](docs/AGENT_GUIDE.md)** — короткий справочник команд для AI-агентов
- **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** — что внутри bot.py, как устроен tool-calling loop
- **[docs/SECURITY.md](docs/SECURITY.md)** — whitelist, env vars, что НЕ коммитить
- **[docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)** — типовые проблемы

## Требования

- Linux с Docker + Docker Compose v2
- `sudo` без пароля (для systemd)
- llama.cpp-совместимый сервер на порту 8080 (или своё значение в `.env`)
- Опционально: SearXNG (для web-search tool), Whisper (для голосовых)

## Где взять llama-server

Этот репозиторий **не** включает llama.cpp. Подойдёт любой OpenAI-совместимый endpoint.
Рекомендации для Radeon 780M:
**[vaskes/llama.cpp-rocm-780m](https://github.com/vaskes/llama.cpp-rocm-780m)** —
готовая Docker-сборка с нативной поддержкой gfx1103.

Для SearXNG + tool-calling:
**[vaskes/llama.cpp_search](https://github.com/vaskes/llama.cpp_search)** —
SearXNG + Playwright MCP + готовый `--mcp-servers-config` для llama-server.

## Лицензия

MIT
