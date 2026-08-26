FROM python:3.12-slim AS runtime

ARG RELEASE_VERSION
ARG SOURCE_COMMIT

LABEL org.opencontainers.image.title="ML Inference Service" \
      org.opencontainers.image.version="${RELEASE_VERSION}" \
      org.opencontainers.image.revision="${SOURCE_COMMIT}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv/app
COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install --no-cache-dir ".[mlflow,postgres]"

RUN useradd --create-home --uid 10001 appuser
USER appuser
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
