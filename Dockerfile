# syntax=docker/dockerfile:1
ARG PYTHON_IMAGE=python:3.11-slim-bookworm
FROM ${PYTHON_IMAGE}

ENV HOME=/home/capa \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    CAPA_ENV=local \
    CAPA_PROJECT_ROOT=/app \
    CAPA_DATA_DIR=/data \
    CAPA_CACHE_DIR=/data/cache \
    MATCH_HOST=0.0.0.0 \
    MATCH_PORT=8102

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 capa \
    && useradd --uid 10001 --gid capa --create-home capa

WORKDIR /app
COPY requirements-lock.txt ./
# Install the existing Torch version as CPU-only before resolving the lock.
# This avoids pulling CUDA libraries into a CPU service image.
RUN python -m pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch==2.15.0 \
    && python -m pip install --no-cache-dir -r requirements-lock.txt

COPY pyproject.toml ./
COPY src/ ./src/
COPY docker/healthcheck.py /opt/capa/healthcheck.py
RUN python -m pip install --no-cache-dir --no-deps --no-build-isolation -e . \
    && python -m pip check \
    && mkdir -p /data \
    && chown -R capa:capa /app /data

USER 10001:10001
EXPOSE 8102
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "/opt/capa/healthcheck.py"]
CMD ["python", "-m", "capa_match.cli"]
