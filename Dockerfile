FROM python:3.12-slim

WORKDIR /app

COPY bot.py deadlines.json undated.json ./

ENV PYTHONUNBUFFERED=1
ENV DB_PATH=/app/data/uwed_bot.sqlite3

RUN mkdir -p /app/data

CMD ["python", "bot.py"]
