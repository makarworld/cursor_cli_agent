# CloudCLI (Claude Code UI) - веб-интерфейс для Cursor CLI
# Основа: Node.js 22
FROM node:22-bookworm-slim

# Установка Cursor CLI + зависимости для node-pty (node-gyp требует Python и build-essential)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    python3 \
    python3-pip \
    build-essential \
    && curl -fsSL https://cursor.com/install | bash \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Добавляем Cursor CLI в PATH (устанавливается в ~/.local/bin)
ENV PATH="/root/.local/bin:${PATH}"

WORKDIR /app

# Установка CloudCLI и TaskMaster AI через npm
RUN npm install -g @siteboon/claude-code-ui task-master-ai

# Директория для данных
RUN mkdir -p /app/data

# Переменные окружения
ENV PORT=3001
ENV HOST=0.0.0.0
ENV DATABASE_PATH=/app/data/auth.db

EXPOSE 3001

# Запуск CloudCLI
# cloudcli автоматически обнаруживает Cursor CLI при наличии в PATH
CMD ["cloudcli", "start"]
