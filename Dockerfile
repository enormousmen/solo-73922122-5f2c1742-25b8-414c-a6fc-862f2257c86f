FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DRILL_SAMPLE_DIR=/var/lib/drill/sample-logs \
    DRILL_ECHO_BIND_HOST=0.0.0.0 \
    DRILL_ECHO_PORT=9000 \
    DRILL_NOFILE_LIMIT=32

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts

RUN useradd --create-home --uid 10001 drill \
    && mkdir -p /var/lib/drill/sample-logs \
    && chown -R drill:drill /app /var/lib/drill

USER drill

EXPOSE 8000 9000

HEALTHCHECK --interval=5s --timeout=3s --start-period=5s --retries=10 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).read()"

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
