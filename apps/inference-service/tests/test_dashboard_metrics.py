import unittest

from app.metrics import ServiceMetrics


class DashboardMetricsTests(unittest.TestCase):
    def test_monitoring_traffic_does_not_count_as_prediction(self):
        metrics = ServiceMetrics()
        metrics.http_requests.labels("GET", "/internal/v1/status", "200").inc(10)
        metrics.http_requests.labels("GET", "/health/ready", "200").inc(4)
        self.assertEqual(metrics.prediction_statistics(), {
            "total": 0, "completed": 0, "failed": 0, "by_model": [],
        })

    def test_versions_errors_and_total_latency_are_reported_separately(self):
        metrics = ServiceMetrics()
        metrics.prediction_requests.labels("credit", "1", "completed").inc(2)
        metrics.prediction_requests.labels("credit", "2", "error").inc()
        metrics.prediction_latency.labels("credit", "1", "total").observe(.1)
        metrics.prediction_latency.labels("credit", "1", "total").observe(.3)
        metrics.prediction_latency.labels("credit", "1", "model").observe(.01)
        result = metrics.prediction_statistics()
        self.assertEqual((result["total"], result["completed"], result["failed"]), (3, 2, 1))
        self.assertEqual(result["by_model"][0]["average_latency_ms"], 200)
        self.assertEqual(result["by_model"][1]["failed"], 1)
        self.assertIsNone(result["by_model"][1]["average_latency_ms"])


if __name__ == "__main__":
    unittest.main()
