FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY webhook_listener.py store.py load_env.py ./

RUN mkdir -p /data

CMD ["sh", "-c", "uvicorn webhook_listener:app --host 0.0.0.0 --port ${PORT:-8000}"]
