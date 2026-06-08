# syntax=docker/dockerfile:1
FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    API_HOST=0.0.0.0 \
    API_PORT=8000 \
    IFIND_CONFIG_PATH=/app/config/ifind_config.docker.yaml

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-docker.txt /app/requirements-docker.txt
RUN pip install --upgrade pip \
    && pip install -r /app/requirements-docker.txt

COPY api /app/api
COPY multi_factor /app/multi_factor
COPY config/ifind_config.docker.yaml /app/config/ifind_config.docker.yaml
COPY config/ifind_config.blader.example.yaml /app/config/ifind_config.blader.example.yaml
COPY config/ifind_config.example.yaml /app/config/ifind_config.example.yaml
COPY run_api.py /app/run_api.py
COPY scripts/sql /app/scripts/sql
COPY deploy/docker/entrypoint.sh /app/entrypoint.sh

RUN chmod +x /app/entrypoint.sh \
    && mkdir -p /app/data /app/output

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health/live || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
