FROM python:3.12-slim

ARG RUNTIME_BASE_VERSION
ARG RUNTIME_BASE_INPUT_SHA256

LABEL org.opencontainers.image.title="ML Inference Dependency Base" \
      org.opencontainers.image.version="${RUNTIME_BASE_VERSION}" \
      com.ml-inference.runtime-input-sha256="${RUNTIME_BASE_INPUT_SHA256}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /runtime
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
RUN python -c "import boto3; import mlflow; import asyncpg; import alembic"

RUN useradd --create-home --uid 10001 runtime
USER runtime
