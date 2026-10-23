FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DB_PATH=/data/app.db

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY data/menu.json ./data/menu.json
COPY static ./static
COPY scripts ./scripts

EXPOSE 8080

# One worker on purpose: SQLite lives on a single volume attached to a single machine.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
