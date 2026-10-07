import json
import logging
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock

from app.logging_config import JsonFormatter
from app.runtime import DockerFleetRuntimeBackend


class DeliveryLoggingTests(unittest.TestCase):
    def test_json_logs_preserve_original_exception_traceback(self):
        try:
            raise ModuleNotFoundError("No module named 'boto3'")
        except ModuleNotFoundError:
            record = logging.LogRecord("ml_inference", logging.ERROR, __file__, 1, "deployment_failed", (), sys.exc_info())
        payload = json.loads(JsonFormatter().format(record))
        self.assertIn("Traceback", payload["exception"])
        self.assertIn("ModuleNotFoundError: No module named 'boto3'", payload["exception"])

    def test_producer_image_resolution_uses_local_immutable_digest(self):
        async def check():
            with tempfile.TemporaryDirectory() as tmp:
                runtime = DockerFleetRuntimeBackend(artifact_cache_root=tmp)
                runtime._docker = AsyncMock(return_value=(0, "sha256:imageid [\"registry.test/airflow@sha256:" + "a" * 64 + "\"]"))
                resolved = await runtime.resolve_image("registry.test/airflow:build-17")
                self.assertEqual(resolved, "registry.test/airflow@sha256:" + "a" * 64)
                runtime._docker.assert_awaited_once_with(
                    "image", "inspect", "--format", "{{.Id}} {{json .RepoDigests}}", "registry.test/airflow:build-17", check=False,
                )

        import asyncio
        asyncio.run(check())

    def test_missing_local_producer_image_fails_without_pull(self):
        async def check():
            with tempfile.TemporaryDirectory() as tmp:
                runtime = DockerFleetRuntimeBackend(artifact_cache_root=tmp)
                runtime._docker = AsyncMock(return_value=(1, "No such image"))
                with self.assertRaisesRegex(Exception, "Producer image is not present"):
                    await runtime.resolve_image("registry.test/airflow:build-17")
                runtime._docker.assert_awaited_once()

        import asyncio
        asyncio.run(check())
