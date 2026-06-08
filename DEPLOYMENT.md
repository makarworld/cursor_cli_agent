# Полный гайд: развёртывание Cursor CLI Agent с нуля на Linux

Пошаговая инструкция для развёртывания на пустом Linux-сервере (Ubuntu 22.04 / Debian 12).

---

## 1. Подготовка сервера

### 1.1 Подключение к серверу

```bash
ssh root@ВАШ_IP_АДРЕС
# или
ssh пользователь@ВАШ_IP_АДРЕС
```

### 1.2 Обновление системы

```bash
sudo apt update && sudo apt upgrade -y
```

### 1.3 Установка Docker

```bash
# Установка зависимостей
sudo apt install -y ca-certificates curl gnupg

# Добавление официального GPG-ключа Docker
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg

# Добавление репозитория
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

# Установка Docker
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# Проверка
docker --version
docker compose version
```

> **Debian 12:** замените `ubuntu` на `debian` в URL репозитория. Полная инструкция: [docs.docker.com/engine/install](https://docs.docker.com/engine/install/)

### 1.4 Открытие порта в firewall (если используется)

```bash
# UFW
sudo ufw allow 3001/tcp
sudo ufw reload

# или firewalld
sudo firewall-cmd --permanent --add-port=3001/tcp
sudo firewall-cmd --reload
```

---

## 2. Получение необходимых ключей и ID

### 2.1 Cursor API ключ

1. Зайдите на [cursor.com/dashboard](https://cursor.com/dashboard?tab=background-agents)
2. Войдите в аккаунт Cursor
3. Раздел **Background Agents** → скопируйте **API Key**

### 2.2 Telegram бот

1. Откройте Telegram и найдите [@BotFather](https://t.me/botfather)
2. Отправьте `/newbot`
3. Введите имя бота (например: `My Cursor Bot`)
4. Введите username (например: `my_cursor_bot`)
5. Скопируйте **токен** вида `1234567890:ABCdefGHIjklMNOpqrsTUVwxyz`

### 2.3 Ваш Telegram ID

1. Найдите в Telegram [@userinfobot](https://t.me/userinfobot)
2. Отправьте ему любое сообщение
3. Бот пришлёт ваш **ID** (число, например `123456789`)

---

## 3. Установка проекта

### 3.1 Клонирование (если проект в Git)

```bash
cd ~
git clone https://github.com/ВАШ_РЕПОЗИТОРИЙ/cursor_cli_agent.git
cd cursor_cli_agent
```

### 3.2 Или загрузка вручную

Если проект у вас локально — загрузите папку на сервер через `scp`:

```bash
# С вашего компьютера
scp -r cursor_cli_agent root@ВАШ_IP:~/
```

Затем на сервере:

```bash
cd ~/cursor_cli_agent
```

### 3.3 Создание .env

```bash
cp .env.example .env
nano .env
```

Заполните минимум:

```env
TELEGRAM_BOT_TOKEN=1234567890:ABCdefGHIjklMNOpqrsTUVwxyz
ALLOWED_USER_IDS=123456789
CURSOR_API_KEY=ваш_cursor_api_ключ
```

Сохраните: `Ctrl+O`, `Enter`, `Ctrl+X`.

---

## 4. Запуск

### 4.1 Сборка и запуск

```bash
docker compose up -d --build
```

Первый запуск может занять 5–10 минут (скачивание образов, установка CloudCLI, Cursor CLI, TaskMaster).

### 4.2 Проверка статуса

```bash
docker compose ps
```

Оба контейнера должны быть в статусе `running`:
- `cursor-cloudcli`
- `cursor-telegram-bot`

### 4.3 Просмотр логов

```bash
# Все сервисы
docker compose logs -f

# Только CloudCLI
docker compose logs -f cloudcli

# Только Telegram бот
docker compose logs -f telegram-bot
```

---

## 5. Первый вход

### 5.1 Веб-интерфейс (CloudCLI)

1. Откройте в браузере: `http://ВАШ_IP:3001`
2. При первом заходе откроется форма **регистрации**
3. Введите username и пароль (минимум 6 символов)
4. Нажмите **Register** — вы войдёте в систему
5. При следующих визитах используйте **логин** (username + пароль)

### 5.2 Telegram бот

1. Найдите бота в Telegram по username (из @BotFather)
2. Отправьте `/start`
3. Бот пришлёт приветствие и ваш ID
4. Отправьте любое сообщение — бот передаст его Cursor Agent и пришлёт ответ

---

## 6. Рабочая директория

Проекты для Cursor CLI лежат в Docker volume `cursor-workspace` (`/workspace`).

### Добавить свой проект

**Вариант A:** через CloudCLI — откройте веб-интерфейс, создайте папку в файловом браузере.

**Вариант B:** смонтировать папку с хоста в `docker-compose.yml`:

```yaml
volumes:
  - /home/user/my-projects:/workspace
```

---

## 7. Полезные команды

```bash
# Остановить
docker compose down

# Запустить снова
docker compose up -d

# Пересобрать после изменений
docker compose up -d --build

# Удалить всё (включая данные)
docker compose down -v
```

---

## 8. Безопасность (рекомендации)

- **Пароль CloudCLI** — используйте надёжный пароль
- **ALLOWED_USER_IDS** — указывайте только свои ID
- **HTTPS** — при публичном доступе настройте nginx + Let's Encrypt
- **Firewall** — открывайте только нужные порты (3001)

---

## 9. Устранение неполадок

### Бот не отвечает

- Проверьте `TELEGRAM_BOT_TOKEN` в `.env`
- Убедитесь, что ваш ID есть в `ALLOWED_USER_IDS`
- Логи: `docker compose logs telegram-bot`

### Telegram заблокирован на сервере

Добавьте в `.env` прокси (формат `host:port:login:password`):

```
TELEGRAM_PROXY=178.171.42.127:9654:login:password
TELEGRAM_PROXY_TYPE=socks5
```

Пересоберите и перезапустите: `docker compose up -d --build telegram-bot`

### Ошибка «CURSOR_API_KEY не настроен»

- Добавьте `CURSOR_API_KEY` в `.env`
- Перезапустите: `docker compose restart telegram-bot`

### CloudCLI не открывается

- Проверьте firewall: порт 3001 открыт
- Логи: `docker compose logs cloudcli`

### Ошибка при сборке (node-pty, Python)

CloudCLI требует Python и build-essential для сборки node-pty. В Dockerfile они уже добавлены. Если ошибка — проверьте, что используется актуальный Dockerfile из репозитория.

### Долгий ответ бота

Cursor Agent может выполнять запрос 1–2 минуты. Статус обновляется в сообщении («Выполняю...», «Читаю...»). При таймауте увеличьте `CURSOR_TIMEOUT_SECONDS` в `.env`.

---

## 10. Самомодификация бота

Бот может **править свой код**, коммитить в git и перезапускаться.

### Как это работает

1. Репозиторий монтируется в контейнер: `.:/workspace/cursor_cli_agent`
2. Код бота — live-mount: `./telegram-bot:/app`
3. При критической ошибке (если `SELF_MODIFY_AUTO_FIX=true`) бот вызывает cursor-agent, чинит `telegram-bot/`, делает `git commit` и перезапускается
4. Вручную: `/self_fix описание бага` в Telegram

### Команды

| Команда | Описание |
|---------|----------|
| `/self_fix [текст]` | Попросить бота исправить свой код |
| `/bot_git_status` | Текущие изменения в git |
| `/bot_git_log` | История коммитов бота |
| `/bot_rollback [N]` | Откатить последние N коммитов (макс. 5) |

### Переменные .env

```
SELF_MODIFY_ENABLED=true           # вкл/выкл самомодификацию
SELF_MODIFY_AUTO_FIX=true          # автофикс при падении
SELF_MODIFY_CODEWORD_REQUIRED=true # кодовое слово обязательно
SELF_MODIFY_CODEWORDS=бурмалда,бурмалди
SELF_MODIFY_MAX_PER_HOUR=5         # лимит автофиксов в час
```

### Кодовое слово

В **обычном сообщении** править код бота можно только если есть **бурмалда** или **бурмалди** — бот сам поймёт, нужна ли правка:

```
бурмалда добавь команду /ping в бота
```

Команда `/self_fix` работает **без** кодового слова (принудительное самоисправление).

Чтобы отключить проверку: `SELF_MODIFY_CODEWORD_REQUIRED=false`

### Важно

- Разворачивайте из **git-клона** на сервере — иначе коммиты некуда писать
- После `docker compose up -d --build` volume `./telegram-bot:/app` перекрывает код из образа — это нормально
- Откат: `/bot_rollback` или `git revert` на хосте

---

## 11. Краткая шпаргалка

| Действие | Команда |
|----------|---------|
| Запуск | `docker compose up -d` |
| Остановка | `docker compose down` |
| Логи | `docker compose logs -f` |
| Перезапуск бота | `docker compose restart telegram-bot` |
| Веб-интерфейс | `http://IP:3001` |
