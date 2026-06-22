#!/bin/sh
exec uvicorn webhook_listener:app --host 0.0.0.0 --port ${PORT:-8000}
