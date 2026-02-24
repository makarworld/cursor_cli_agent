# Telegram бот для Cursor CLI
# Python 3.11 + Cursor CLI
FROM python:3.11-slim

# Установка Cursor CLI
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && curl -fsSL https://cursor.com/install | bash \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

ENV PATH="/root/.local/bin:${PATH}"

WORKDIR /app

# Зависимости Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Запуск бота
CMD ["python", "-m", "bot"]
