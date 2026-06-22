FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY webhook_listener.py store.py load_env.py ./

RUN mkdir -p /data

EXPOSE 8000
CMD ["uvicorn", "webhook_listener:app", "--host", "0.0.0.0", "--port", "8000"]
