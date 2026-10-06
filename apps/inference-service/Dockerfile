ARG BASE_IMAGE
FROM ${BASE_IMAGE} AS runtime

ARG RELEASE_VERSION
ARG SOURCE_COMMIT

LABEL org.opencontainers.image.title="ML Inference Service" \
      org.opencontainers.image.version="${RELEASE_VERSION}" \
      org.opencontainers.image.revision="${SOURCE_COMMIT}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

USER root
# The control plane uses the host Docker Engine through its mounted socket.
RUN apt-get update \
    && apt-get install --no-install-recommends -y docker.io \
    && rm -rf /var/lib/apt/lists/* \
    && docker --version

WORKDIR /srv/app
COPY app ./app
COPY alembic.ini ./
COPY migrations ./migrations
USER runtime
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
