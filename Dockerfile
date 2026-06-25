# syntax=docker/dockerfile:1
FROM python:3.11-slim

# Install build tools for dlib/face_recognition
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    cmake \
    git \
    libgl1-mesa-glx \
    libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY bot.py .
COPY bot_lock.py .

EXPOSE 8080

CMD ["python", "bot_lock.py"]