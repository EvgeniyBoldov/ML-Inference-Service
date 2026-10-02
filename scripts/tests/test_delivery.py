"""Delivery regressions with a local Git remote and simulated Docker/proxy."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


def run(args, *, cwd=None, env=None):
    return subprocess.run(args, cwd=cwd, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


@unittest.skipUnless(shutil.which("git"), "git is required")
class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        for name in ("scripts/release.sh", "scripts/release-common.sh", "projects/model-runtime-base/Dockerfile", "projects/model-runtime-base/Dockerfile.runtime", "projects/model-runtime-base/requirements.txt", "projects/model-runtime-base/runner.py", "apps/inference-service/Dockerfile", "release.env"):
            target = self.repo / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(ROOT / name, target)
        (self.repo / "release.env").write_text("RELEASE_VERSION=0.1.0\nRELEASE_COMMIT=\nREGISTRY=registry.test\nIMAGE=service\nBASE_VERSION=0.1.0\nBASE_INPUT_SHA256=\nBASE_IMAGE=\nRUNTIME_IMAGE=\nDB_REVISION=\nBLUE_PORT=18001\nGREEN_PORT=18002\n")
        self.git("init", "-b", "main")
        self.git("config", "user.email", "delivery@test.invalid")
        self.git("config", "user.name", "Delivery Test")
        self.commit("initial")
        self.remote = self.root / "remote.git"
        result = run(["git", "init", "--bare", str(self.remote)])
        self.assertEqual(result.returncode, 0, result.stdout)
        self.git("remote", "add", "origin", str(self.remote))
        self.git("push", "-u", "origin", "main")
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        docker = bin_dir / "docker"
        docker.write_text('''#!/usr/bin/env python3
import hashlib, os, sys
args = sys.argv[1:]
with open(os.environ["DOCKER_CALLS"], "a") as f:
    f.write(" ".join(args) + "\\n")
if args[:2] == ["manifest", "inspect"]:
    if os.environ.get("EXISTING_BASE") == "1" and "-base:" in args[-1]:
        sys.exit(0)
    sys.exit(1)
if args[0] == os.environ.get("FAIL_DOCKER"):
    sys.exit(17)
if args[:2] == ["image", "inspect"]:
    ref = args[-1]
    if "runtime-input-sha256" in args[3]:
        print(os.environ["BASE_LABEL"])
    elif "image.version" in args[3]:
        print("0.1.1")
    else:
        print(ref.rsplit(":", 1)[0] + "@sha256:" + hashlib.sha256(ref.encode()).hexdigest())
if args[0] == "run" and args[-1] == "heads":
    print("0002_fleet_runtime_image (head)")
''')
        docker.chmod(0o755)
        self.calls = self.root / "docker.calls"
        self.env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", DOCKER_CALLS=str(self.calls))

    def git(self, *args):
        result = run(["git", *args], cwd=self.repo)
        self.assertEqual(result.returncode, 0, result.stdout)
        return result.stdout.strip()

    def commit(self, message):
        self.git("add", ".")
        self.git("commit", "-m", message)

    def publish(self, **env):
        return run(["bash", "scripts/release.sh", "publish"], cwd=self.repo, env=dict(self.env, **env))

    def values(self):
        return dict(line.split("=", 1) for line in (self.repo / "release.env").read_text().splitlines() if "=" in line)

    def base_builds(self):
        return [line for line in self.calls.read_text().splitlines() if line.startswith("build ") and "RUNTIME_BASE_INPUT_SHA256=" in line]

    def test_code_reuses_base_requirements_rebuild_base_and_manifest_is_pushed(self):
        source_sha = self.git("rev-parse", "HEAD")
        result = self.publish()
        self.assertEqual(result.returncode, 0, result.stdout)
        first = self.values()
        self.assertEqual(first["RELEASE_COMMIT"], source_sha)
        self.assertEqual(len(self.base_builds()), 1)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.git("rev-parse", "origin/main"))
        self.assertEqual(self.git("status", "--porcelain"), "")
        runner = self.repo / "projects/model-runtime-base/runner.py"
        runner.write_text(runner.read_text() + "\n# code change\n")
        self.commit("runner update")
        result = self.publish()
        self.assertEqual(result.returncode, 0, result.stdout)
        second = self.values()
        self.assertEqual(len(self.base_builds()), 1)
        self.assertEqual(first["BASE_IMAGE"], second["BASE_IMAGE"])
        self.assertEqual(first["BASE_INPUT_SHA256"], second["BASE_INPUT_SHA256"])
        self.assertNotEqual(first["RUNTIME_IMAGE"], second["RUNTIME_IMAGE"])
        req = self.repo / "projects/model-runtime-base/requirements.txt"
        req.write_text(req.read_text() + "\nnew-model-library==1.0\n")
        self.commit("new model dependency")
        result = self.publish()
        self.assertEqual(result.returncode, 0, result.stdout)
        third = self.values()
        self.assertEqual(len(self.base_builds()), 2)
        self.assertNotEqual(second["BASE_INPUT_SHA256"], third["BASE_INPUT_SHA256"])
        self.assertNotEqual(second["BASE_VERSION"], third["BASE_VERSION"])
        self.assertEqual(self.git("rev-parse", "HEAD"), self.git("rev-parse", "origin/main"))
        result = self.publish()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No changes", result.stdout)
        self.assertEqual(len(self.base_builds()), 2)

    def test_failed_build_or_push_keeps_manifest_and_git_unchanged(self):
        original = (self.repo / "release.env").read_text()
        sha = self.git("rev-parse", "HEAD")
        for operation in ("build", "push"):
            with self.subTest(operation=operation):
                result = self.publish(FAIL_DOCKER=operation)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertEqual((self.repo / "release.env").read_text(), original)
                self.assertEqual(self.git("rev-parse", "HEAD"), sha)
                self.assertEqual(self.git("status", "--porcelain"), "")

    def test_previously_published_base_is_reused_only_for_matching_inputs(self):
        result = run(["bash", "-c", "source scripts/release-common.sh; base_input_sha"], cwd=self.repo)
        self.assertEqual(result.returncode, 0, result.stdout)
        sha = result.stdout.strip()
        original = (self.repo / "release.env").read_text()
        result = self.publish(EXISTING_BASE="1", BASE_LABEL="b" * 64)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("different inputs", result.stdout)
        self.assertEqual((self.repo / "release.env").read_text(), original)
        result = self.publish(EXISTING_BASE="1", BASE_LABEL=sha)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(len(self.base_builds()), 0)

    def test_base_dockerfile_change_rebuilds_dependency_base(self):
        result = self.publish()
        self.assertEqual(result.returncode, 0, result.stdout)
        dockerfile = self.repo / "projects/model-runtime-base/Dockerfile"
        dockerfile.write_text(dockerfile.read_text() + "\n# system dependency change\n")
        self.commit("base Dockerfile update")
        result = self.publish()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(len(self.base_builds()), 2)


class DeployTests(unittest.TestCase):
    def test_actual_nginx_validation_failure_restores_previous_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            upstream = Path(tmp) / "upstream.conf"
            upstream.write_text("previous upstream\n")
            script = 'source "$DEPLOY_SCRIPT"; nginx() { return 1; }; systemctl() { return 1; }; switch_upstream 18002'
            result = run(["bash", "-c", script], env=dict(os.environ, DEPLOY_SCRIPT=str(ROOT / "scripts/deploy.sh"), ML_INFERENCE_NGINX_UPSTREAM=str(upstream)))
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(upstream.read_text(), "previous upstream\n")

    def simulate(self, action="deploy", *, unhealthy=False, same=False, switch_fails=False):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("new", "old", "standby"):
                (root / name).mkdir()
                (root / name / "compose.yaml").touch()
            script = r'''
source "$DEPLOY_SCRIPT"
declare -A state=( [ACTIVE_SLOT]=blue [ACTIVE_PORT]=18001 [ACTIVE_PROJECT]=ml-inference-0-1-1 [ACTIVE_DIR]="$FIXTURE/old" [STANDBY_PROJECT]=ml-inference-0-1-0 [STANDBY_DIR]="$FIXTURE/standby" )
[[ "$SAME_RELEASE" == 1 ]] && state[ACTIVE_DIR]="$FIXTURE/new"
current_value() { printf '%s' "${state[$1]:-}"; }
load_bundle() {
  RELEASE_DIR="$1"; REGISTRY=registry.test; IMAGE=service; BLUE_PORT=18001; GREEN_PORT=18002
  RELEASE_VERSION=0.1.2; RELEASE_COMMIT=abc; DB_REVISION=revision; RUNTIME_IMAGE=runtime@sha256:abc
  COMPOSE_PROJECT=ml-inference-0-1-2
}
compose() { echo "compose:$COMPOSE_PROJECT:$*" >> "$CALLS"; }
docker() { echo "docker:$*" >> "$CALLS"; }
verify_release_image() { :; }
verify_migrations() { :; }
wait_ready() { echo "ready:$1" >> "$CALLS"; [[ "$UNHEALTHY" == 0 ]]; }
switch_upstream() { echo "switch:$1" >> "$CALLS"; [[ "$SWITCH_FAILS" == 0 ]]; }
write_state() { echo "state:$*" >> "$CALLS"; }
RELEASE_DIR="$FIXTURE/new"
"$ACTION"
'''
            env = dict(os.environ, DEPLOY_SCRIPT=str(ROOT / "scripts/deploy.sh"), FIXTURE=tmp, CALLS=str(root / "calls"), ACTION=action, UNHEALTHY=str(int(unhealthy)), SAME_RELEASE=str(int(same)), SWITCH_FAILS=str(int(switch_fails)))
            result = run(["bash", "-c", script], env=env)
            return result, (root / "calls").read_text().splitlines()

    def test_promotion_then_old_api_stop(self):
        result, calls = self.simulate()
        self.assertEqual(result.returncode, 0, result.stdout)
        switch = calls.index("switch:18002")
        state = next(i for i, line in enumerate(calls) if line.startswith("state:"))
        stop = calls.index("compose:ml-inference-0-1-1:stop inference-service")
        self.assertLess(switch, state)
        self.assertLess(state, stop)
        self.assertIn("compose:ml-inference-0-1-2:up -d --force-recreate --remove-orphans --no-deps inference-service", calls)

    def test_unhealthy_candidate_stops_without_promotion(self):
        result, calls = self.simulate(unhealthy=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("compose:ml-inference-0-1-2:stop inference-service", calls)
        self.assertFalse(any(line.startswith(("switch:", "state:")) for line in calls))
        self.assertNotIn("compose:ml-inference-0-1-1:stop inference-service", calls)

    def test_switch_failure_preserves_old_api(self):
        result, calls = self.simulate(switch_fails=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(line.startswith("state:") for line in calls))
        self.assertNotIn("compose:ml-inference-0-1-1:stop inference-service", calls)

    def test_repeated_deployment_does_not_stop_or_restart_active(self):
        result, calls = self.simulate(same=True)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(calls, ["ready:18001", "compose:ml-inference-0-1-0:stop inference-service"])

    def test_rollback_starts_standby_then_stops_replaced_api(self):
        result, calls = self.simulate(action="rollback")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("compose:ml-inference-0-1-0:up -d --force-recreate --remove-orphans --no-deps inference-service", calls)
        self.assertLess(calls.index("switch:18002"), calls.index("compose:ml-inference-0-1-1:stop inference-service"))


if __name__ == "__main__":
    unittest.main()
