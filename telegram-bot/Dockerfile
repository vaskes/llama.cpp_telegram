FROM python:3.11-slim

WORKDIR /app

# Copy requirements.txt first for better Docker layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Then copy bot.py
COPY bot.py .

CMD ["python", "bot.py"]
