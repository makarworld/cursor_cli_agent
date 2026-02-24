# Cursor CLI Agent — развёртывание на сервере

Docker Compose для развёртывания **Cursor CLI** с доступом через:
- **Браузер** — веб-интерфейс CloudCLI
- **Telegram** — бот для управления через мессенджер

**[Полный гайд с нуля](DEPLOYMENT.md)** — установка Docker, настройка, первый запуск на пустом Linux-сервере.

## Архитектура

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│   Браузер       │     │   Telegram      │     │   Cursor CLI    │
│   (CloudCLI)    │────►│   Бот           │────►│  (cursor-agent) │
│   :3001         │     │                 │     │                 │
└─────────────────┘     └─────────────────┘     └─────────────────┘
         │                        │                        │
         └────────────────────────┴────────────────────────┘
                              Docker Compose
                         (общий volume: workspace)
```

## Требования

- Docker и Docker Compose
- Cursor API ключ ([получить](https://cursor.com/dashboard?tab=background-agents))
- Telegram бот ([создать через @BotFather](https://t.me/botfather))

## Быстрый старт

### 1. Клонирование и настройка

```bash
cd cursor_cli_agent
cp .env.example .env
# Важно: создайте .env до запуска docker compose
```

### 2. Редактирование .env

Откройте `.env` и заполните:

| Переменная | Описание |
|------------|----------|
| `TELEGRAM_BOT_TOKEN` | Токен от @BotFather |
| `ALLOWED_USER_IDS` | Ваш Telegram user ID (от @userinfobot) |
| `CURSOR_API_KEY` | Ключ с [cursor.com/dashboard](https://cursor.com/dashboard?tab=background-agents) |

### 3. Запуск

```bash
docker compose up -d
```

### 4. Доступ

- **Веб-интерфейс:** http://localhost:3001 (или http://\<IP-сервера\>:3001)
- **Telegram:** найдите бота по username и отправьте `/start`

## Структура проекта

```
cursor_cli_agent/
├── docker-compose.yml    # Оркестрация сервисов
├── DEPLOYMENT.md         # Полный гайд развёртывания с нуля
├── .env.example          # Шаблон переменных окружения
├── cloudcli/             # Веб-интерфейс CloudCLI
│   └── Dockerfile
├── telegram-bot/        # Telegram бот для Cursor CLI
│   ├── Dockerfile
│   ├── requirements.txt
│   └── bot/
│       └── main.py
└── README.md
```

## Сервисы

### CloudCLI (порт 3001)

- Веб-интерфейс для Cursor CLI
- Чат, терминал, файловый браузер, Git
- Сессии сохраняются в volume `cloudcli-data`

#### Авторизация CloudCLI

CloudCLI защищён встроенной аутентификацией. При первом заходе на сайт:

1. Откроется форма **регистрации** — задайте username и пароль (минимум 6 символов)
2. После регистрации вы войдёте в систему
3. При следующих визитах — форма **логина** (username + пароль)

Рекомендации: используйте надёжный пароль и настройте HTTPS (Let's Encrypt) при публичном доступе к серверу.

### Telegram бот

- Принимает сообщения в Telegram
- Передаёт их в `cursor-agent` (headless)
- **Сохраняет контекст** — cursor-agent помнит предыдущие сообщения в диалоге
- Отправляет ответ обратно в чат

**Команды бота:** `/start`, `/new`, `/status`, `/help`, `/set_prompt`, `/myprompt`, `/clear_prompt`, `/cd`, `/pwd`, `/ls`, `/mkdir`, `/cat`, `/rm`

- `/start` — приветствие и **информация о пользователе** (ID, username) — добавьте свой ID в `ALLOWED_USER_IDS`
- `/new` — сбросить контекст чата и начать новый диалог
- `/set_prompt <текст>` — задать свой промпт (о себе, предпочтениях) — добавляется к каждому запросу
- `/myprompt` — показать свой промпт
- `/clear_prompt` — очистить свой промпт

## Рабочая директория

Оба сервиса используют общий volume `cursor-workspace` (`/workspace`).  
Положите сюда проекты, с которыми будет работать Cursor CLI.

Монтирование своей папки:

```yaml
# В docker-compose.yml, в volumes сервисов:
volumes:
  - /path/to/your/projects:/workspace
```

## Безопасность

- **CloudCLI** — встроенная аутентификация (логин/пароль при первом заходе)
- **Telegram бот** — ограничьте `ALLOWED_USER_IDS` своими ID
- Настройте HTTPS (Let's Encrypt) при публичном доступе

## Устранение неполадок

### CloudCLI не видит Cursor CLI

Убедитесь, что Cursor CLI установлен в контейнере. В Dockerfile CloudCLI используется скрипт `curl https://cursor.com/install | bash`.

### Telegram бот не отвечает

- Проверьте `TELEGRAM_BOT_TOKEN`
- Убедитесь, что ваш user ID есть в `ALLOWED_USER_IDS`
- Проверьте логи: `docker compose logs telegram-bot`

### Cursor API ключ

Для headless (Telegram бот) нужен `CURSOR_API_KEY`. Без него бот вернёт ошибку.

### cursor-agent не найден

После установки Cursor CLI команда может называться `cursor-agent` или `agent`. Задайте в `.env`:

```
CURSOR_CLI_PATH=agent
```

## Полезные ссылки

- [CloudCLI (claudecodeui)](https://github.com/siteboon/claudecodeui)
- [Cursor CLI](https://cursor.com/docs/cli/overview)
- [Cursor Headless](https://cursor.com/docs/cli/headless)
