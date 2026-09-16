# Python 3.11 (совпадает с локальной разработкой)
FROM python:3.11-slim

# Рабочая директория
WORKDIR /app

# Копируем только requirements сначала — для кэша слоёв
COPY requirements.txt .

# Устанавливаем зависимости
RUN pip install --no-cache-dir -r requirements.txt

# Копируем код (config.yml и data/ исключены через .dockerignore)
COPY . .

# Healthcheck: контейнер считается живым, если main.py запущен
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD pgrep -f "python main.py" || exit 1

# Запуск бота
CMD ["python", "main.py"]