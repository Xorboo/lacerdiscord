FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY poller.py .

# State lives on a volume so restarts don't replay old events as "new".
RUN mkdir -p /data && useradd -m -u 1000 watcher && chown -R watcher /data /app
USER watcher

ENV STATE_PATH=/data/state.json

CMD ["python", "-u", "poller.py"]
