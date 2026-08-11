FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static
COPY templates ./templates
COPY knowledge ./knowledge

# Writable state (SQLite + uploaded knowledge) lives here. Mount a volume on it.
RUN mkdir -p /app/data && \
    useradd --create-home --uid 10001 agent && \
    chown -R agent:agent /app
USER agent

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status==200 else 1)"

# One worker on purpose: session slots, the concurrency cap and SQLite writes
# all live in-process. The work is I/O bound, so async handles the load.
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "1", \
     "--proxy-headers", "--forwarded-allow-ips", "*", \
     "--ws-ping-interval", "20", "--ws-ping-timeout", "20", \
     "--timeout-keep-alive", "65"]
