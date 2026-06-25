# syntax=docker/dockerfile:1
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py .
COPY bot_lock.py .
# Expose 8080 for webhook (optional)
EXPOSE 8080
# Use bot_lock.py to prevent multiple instances
CMD ["python", "bot_lock.py"]