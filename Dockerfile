FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY webhook_listener.py store.py load_env.py start.sh ./
RUN chmod +x start.sh

RUN mkdir -p /data

CMD ["./start.sh"]
