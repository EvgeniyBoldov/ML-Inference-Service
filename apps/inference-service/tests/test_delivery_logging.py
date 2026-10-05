import json
import logging
from pathlib import Path
import sys
import tempfile
import unittest

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

    def test_model_runtime_uses_release_pinned_image_and_accepts_legacy_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "runtime.env"
            runtime = DockerFleetRuntimeBackend(image_manifest=str(manifest), artifact_cache_root=tmp)
            for key in ("RUNTIME_IMAGE", "RUNTIME_BASE_IMAGE"):
                image = "registry.test/service-runtime@sha256:" + "a" * 64
                manifest.write_text(f"{key}={image}\n")
                self.assertEqual(runtime._read_image(), image)
            manifest.write_text("RUNTIME_IMAGE=registry.test/service-runtime:latest\n")
            with self.assertRaisesRegex(RuntimeError, "pinned"):
                runtime._read_image()

    def test_model_runtime_reads_full_release_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "release.env"
            image = "registry.test/service-runtime@sha256:" + "b" * 64
            manifest.write_text(
                "# Immutable release state\nRELEASE_VERSION=0.1.3\n"
                "BASE_IMAGE=registry.test/service-base@sha256:" + "a" * 64 + "\n"
                f"RUNTIME_IMAGE={image}\nDB_REVISION=0002\n"
            )
            runtime = DockerFleetRuntimeBackend(image_manifest=str(manifest), artifact_cache_root=tmp)
            self.assertEqual(runtime._read_image(), image)
