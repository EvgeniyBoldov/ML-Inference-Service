"""Prometheus metrics owned by a single service app instance."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest


class ServiceMetrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.prediction_requests = Counter("prediction_requests_total", "Prediction requests", ["model", "version", "status"], registry=self.registry)
        self.prediction_errors = Counter("prediction_errors_total", "Prediction errors", ["model", "version", "code"], registry=self.registry)
        self.prediction_latency = Histogram("prediction_latency_seconds", "Prediction latency", ["model", "version", "kind"], registry=self.registry)
        self.deployments = Counter("deployment_total", "Deployments", ["model", "version", "status"], registry=self.registry)
        self.deployment_failures = Counter("deployment_failed_total", "Failed deployments", ["model", "version", "code"], registry=self.registry)
        self.model_load_duration = Histogram("model_load_duration_seconds", "Model deployment duration", ["model", "version"], registry=self.registry)
        self.runtime_status = Gauge("model_runtime_status", "Runtime state", ["model", "version", "status"], registry=self.registry)
        self.active_version = Gauge("active_model_version", "Whether a model version is active", ["model", "version"], registry=self.registry)

    def exposition(self) -> bytes:
        return generate_latest(self.registry)
