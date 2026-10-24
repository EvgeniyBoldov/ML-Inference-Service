ARG BASE_IMAGE
FROM node:22-alpine AS dashboard-build
WORKDIR /dashboard
COPY ui/package.json ui/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY ui/ ./
RUN npm run build

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
    && apt-get install --no-install-recommends -y docker-cli \
    && rm -rf /var/lib/apt/lists/* \
    && docker --version

WORKDIR /srv/app
COPY app ./app
COPY --from=dashboard-build /dashboard/dist ./app/static/dashboard
COPY alembic.ini ./
COPY migrations ./migrations
USER runtime
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
