FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY webhook_listener.py store.py load_env.py email_sender.py ./

RUN mkdir -p /app/data

CMD ["python3", "-c", "import os,uvicorn; uvicorn.run('webhook_listener:app', host='0.0.0.0', port=int(os.environ.get('PORT', 8000)))"]
