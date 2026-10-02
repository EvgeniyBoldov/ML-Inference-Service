"""Prometheus metrics owned by a single service app instance."""

from __future__ import annotations

from time import monotonic, time
from typing import Any

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest


class ServiceMetrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self._started = monotonic()
        self.http_requests = Counter("http_requests_total", "HTTP requests completed", ["method", "route", "status_code"], registry=self.registry)
        self.http_latency = Histogram("http_request_duration_seconds", "HTTP request duration", ["method", "route"], registry=self.registry)
        self.http_in_progress = Gauge("http_requests_in_progress", "HTTP requests currently being processed", registry=self.registry)
        self.start_time = Gauge("service_start_time_seconds", "Service process start time", registry=self.registry)
        self.start_time.set(time())
        self.uptime = Gauge("service_uptime_seconds", "Time since service process startup", registry=self.registry)
        self.uptime.set_function(lambda: monotonic() - self._started)
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

    def request_statistics(self) -> dict[str, Any]:
        """Read the same counters exported to Prometheus; do not keep a second log."""
        totals = {"total": 0, "successful": 0, "client_errors": 0, "server_errors": 0}
        routes: list[dict[str, Any]] = []
        for family in self.http_requests.collect():
            for sample in family.samples:
                if sample.name != "http_requests_total":
                    continue
                count = int(sample.value)
                status = int(sample.labels["status_code"])
                totals["total"] += count
                if status >= 500:
                    totals["server_errors"] += count
                elif status >= 400:
                    totals["client_errors"] += count
                elif status < 400:
                    totals["successful"] += count
                routes.append({**sample.labels, "count": count})
        in_progress = next(sample.value for family in self.http_in_progress.collect() for sample in family.samples if sample.name == "http_requests_in_progress")
        return {
            "uptime_seconds": round(monotonic() - self._started, 3),
            "requests": {**totals, "in_progress": int(in_progress), "by_route": sorted(routes, key=lambda item: (item["route"], item["method"], item["status_code"]))},
        }
