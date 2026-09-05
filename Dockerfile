FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py schema.sql README.md bubbles-telegram.js .
ENV PYTHONUNBUFFERED=1
CMD ["sh", "-c", "uvicorn bot:app --host 0.0.0.0 --port ${PORT:-8080}"]
