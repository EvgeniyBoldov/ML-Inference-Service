FROM python:3.12-slim

ARG RUNTIME_BASE_VERSION
ARG RUNTIME_BASE_INPUT_SHA256

LABEL org.opencontainers.image.title="ML Inference Model Runtime Base" \
      org.opencontainers.image.version="${RUNTIME_BASE_VERSION}" \
      com.ml-inference.runtime-input-sha256="${RUNTIME_BASE_INPUT_SHA256}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /runtime
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY runner.py ./

RUN useradd --create-home --uid 10001 runtime
USER runtime
EXPOSE 8080
CMD ["uvicorn", "runner:app", "--host", "0.0.0.0", "--port", "8080"]
