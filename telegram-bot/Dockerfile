FROM python:3.11-slim

WORKDIR /app

# Сначала копируем requirements.txt
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Потом копируем bot.py
COPY bot.py .

CMD ["python", "bot.py"]
