# syntax=docker/dockerfile:1
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py .
COPY bot_lock.py .
EXPOSE 8080
CMD ["python", "bot_lock.py"]