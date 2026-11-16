"""Tests for the sandbox limits and the explainability run type (Docker mocked)."""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests
from django.test import SimpleTestCase

from data import sandbox

EXPLAIN_ENV = {
    "SWARMMEDHUB_EXPLAIN_MEM_LIMIT": "3g",
    "SWARMMEDHUB_EXPLAIN_CPUS": "1.5",
    "SWARMMEDHUB_EXPLAIN_TIMEOUT": "600",
}


class SandboxLimitTests(SimpleTestCase):
    def test_other_run_types_keep_their_limits(self):
        with patch.dict(os.environ, EXPLAIN_ENV):
            for run_type in ("validation", "visualization", "results_visualization"):
                with self.subTest(run_type=run_type):
                    self.assertEqual(
                        sandbox.sandbox_limits(run_type),
                        {"mem_limit": "1g", "nano_cpus": 1_000_000_000, "timeout": 300},
                    )

    def test_explainability_defaults(self):
        with patch.dict(os.environ, {}, clear=False):
            for name in EXPLAIN_ENV:
                os.environ.pop(name, None)
            self.assertEqual(
                sandbox.sandbox_limits("explainability"),
                {"mem_limit": "2g", "nano_cpus": 2_000_000_000, "timeout": 900},
            )

    def test_explainability_reads_the_environment(self):
        with patch.dict(os.environ, EXPLAIN_ENV):
            self.assertEqual(
                sandbox.sandbox_limits("explainability"),
                {"mem_limit": "3g", "nano_cpus": 1_500_000_000, "timeout": 600},
            )

    def test_invalid_values_fall_back_to_the_defaults(self):
        bad = {
            "SWARMMEDHUB_EXPLAIN_MEM_LIMIT": "lots",
            "SWARMMEDHUB_EXPLAIN_CPUS": "200",
            "SWARMMEDHUB_EXPLAIN_TIMEOUT": "86400",
        }
        with patch.dict(os.environ, bad):
            self.assertEqual(
                sandbox.sandbox_limits("explainability"),
                {"mem_limit": "2g", "nano_cpus": 2_000_000_000, "timeout": 900},
            )
        with patch.dict(os.environ, {"SWARMMEDHUB_EXPLAIN_MEM_LIMIT": "100m"}):
            self.assertEqual(
                sandbox.sandbox_limits("explainability")["mem_limit"], "2g"
            )
        with patch.dict(os.environ, {"SWARMMEDHUB_EXPLAIN_MEM_LIMIT": "4096M"}):
            self.assertEqual(
                sandbox.sandbox_limits("explainability")["mem_limit"], "4096m"
            )


class _FakeContainer:
    def __init__(self, run_dirs, behaviour, logs=b"script output\n"):
        self.run_dirs = run_dirs
        self.behaviour = behaviour
        self.log_bytes = logs
        self.removed = False
        self.killed = False
        self.wait_timeout = None

    def wait(self, timeout=None):
        self.wait_timeout = timeout
        return self.behaviour(self.run_dirs[-1], self)

    def logs(self, **kwargs):
        self.logs_kwargs = kwargs
        return self.log_bytes

    def kill(self):
        self.killed = True

    def remove(self, force=False):
        self.removed = force


class ExplainSandboxRunTests(SimpleTestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="swm-sandbox-test-")
        self.addCleanup(shutil.rmtree, self.temp_dir, True)
        override = self.settings(PROJECT_TEMP_DIR=self.temp_dir)
        override.enable()
        self.addCleanup(override.disable)
        self.run_dirs = []
        self.run_kwargs = {}
        self.data_dir = tempfile.mkdtemp(dir=self.temp_dir)

    def _run(self, behaviour, run_type="explainability", logs=b"out\n", **kwargs):
        container = _FakeContainer(self.run_dirs, behaviour, logs)
        client = MagicMock()

        def fake_run(**run_kwargs):
            self.run_kwargs = run_kwargs
            return container

        client.containers.run.side_effect = fake_run
        real_mkdtemp = tempfile.mkdtemp

        def recording_mkdtemp(*args, **kw):
            path = real_mkdtemp(*args, **kw)
            self.run_dirs.append(path)
            return path

        with (
            patch("data.sandbox.get_docker_client", return_value=client),
            patch("data.sandbox.ensure_sandbox_image"),
            patch("data.sandbox.ensure_sandbox_network"),
            patch(
                "data.sandbox._resolve_minio_host_for_sandbox", return_value="10.0.0.2"
            ),
            patch("data.sandbox.tempfile.mkdtemp", side_effect=recording_mkdtemp),
        ):
            result = sandbox.run_script_in_sandbox(
                "print('wrapper')\n",
                self.data_dir,
                "project-uuid",
                run_type=run_type,
                **kwargs,
            )
        return result, container

    def test_explain_run_uses_its_limits_name_and_hooks(self):
        prepared = []
        collected = []

        def prepare(run_dir):
            prepared.append(sorted(os.listdir(run_dir)))
            Path(run_dir, "explainability.py").write_text("x = 1\n")

        def collect(run_dir):
            collected.append(Path(run_dir, "explain", "cases.json").read_text())
            return {"data": "ok"}

        def behaviour(run_dir, _container):
            os.makedirs(os.path.join(run_dir, "explain"))
            Path(run_dir, "explain", "cases.json").write_text("{}")
            return {"StatusCode": 0}

        with patch.dict(os.environ, {}, clear=False):
            for name in EXPLAIN_ENV:
                os.environ.pop(name, None)
            result, container = self._run(
                behaviour,
                logs=b"script output \xff\n",
                prepare_run_dir=prepare,
                collect_outputs=collect,
                container_name="swarmmedhub-explain-abc",
            )
        self.assertTrue(result["success"])
        self.assertEqual(result["collected"], {"data": "ok"})
        self.assertIsNone(result["collect_error"])
        self.assertEqual(result["output"], "script output �\n")
        self.assertEqual(prepared, [["plots", "script.py"]])
        self.assertEqual(collected, ["{}"])
        self.assertEqual(self.run_kwargs["mem_limit"], "2g")
        self.assertEqual(self.run_kwargs["nano_cpus"], 2_000_000_000)
        self.assertEqual(self.run_kwargs["name"], "swarmmedhub-explain-abc")
        self.assertEqual(
            self.run_kwargs["labels"], {"swarmmedhub.run_type": "explainability"}
        )
        self.assertEqual(self.run_kwargs["command"], ["script.py"])
        self.assertEqual(container.wait_timeout, 900)
        self.assertEqual(
            container.logs_kwargs, {"tail": sandbox.EXPLAIN_LOG_TAIL_LINES}
        )
        self.assertTrue(container.removed)
        self.assertFalse(os.path.exists(self.run_dirs[-1]))

    def test_failed_script_is_not_collected(self):
        collect = MagicMock()
        result, _ = self._run(lambda *_: {"StatusCode": 1}, collect_outputs=collect)
        self.assertFalse(result["success"])
        self.assertIn("exited with code 1", result["error"])
        collect.assert_not_called()

    def test_timeout_kills_the_container_and_collects_nothing(self):
        collect = MagicMock()

        def behaviour(_run_dir, _container):
            raise requests.exceptions.ReadTimeout("read timed out")

        calls = []

        def clock():
            calls.append(1)
            return 100.0 if len(calls) == 1 else 100.0 + 901.0

        with patch("data.sandbox.time.monotonic", side_effect=clock):
            result, container = self._run(behaviour, collect_outputs=collect)
        self.assertTrue(result["timed_out"])
        self.assertFalse(result["success"])
        self.assertIn("did not finish within 900 seconds", result["error"])
        self.assertTrue(container.killed)
        self.assertTrue(container.removed)
        collect.assert_not_called()

    def test_early_wait_error_is_a_failure_not_a_timeout(self):
        def behaviour(_run_dir, _container):
            raise requests.exceptions.ConnectionError("daemon went away")

        result, container = self._run(behaviour, collect_outputs=MagicMock())
        self.assertFalse(result["success"])
        self.assertIn("daemon went away", result["error"])
        self.assertNotIn("timed_out", result)
        self.assertTrue(container.removed)

    def test_collect_errors_are_reported(self):
        def collect(_run_dir):
            raise ValueError("the bundle is incomplete")

        result, _ = self._run(lambda *_: {"StatusCode": 0}, collect_outputs=collect)
        self.assertTrue(result["success"])
        self.assertEqual(result["collect_error"], "the bundle is incomplete")
        self.assertIsNone(result["collected"])

    def test_untrusted_outputs_are_copied_safely(self):
        """A symlink to a worker file and a FIFO in the output are not followed."""
        from swarmmed_explain.bundle import safe_copy_bundle

        secret = Path(self.temp_dir, "worker.env")
        secret.write_text("SECRET_KEY=leak\n")
        staging = os.path.join(self.temp_dir, "staging")

        def behaviour(run_dir, _container):
            out = Path(run_dir, "explain")
            out.mkdir()
            os.symlink(secret, out / "cases.json")
            os.mkfifo(out / "summary_1.png")
            return {"StatusCode": 0}

        def collect(run_dir):
            return safe_copy_bundle(os.path.join(run_dir, "explain"), staging)

        started = time.monotonic()
        result, _ = self._run(behaviour, collect_outputs=collect)
        self.assertLess(time.monotonic() - started, 30)
        self.assertIsNone(result["collected"])
        self.assertTrue(result["collect_error"])
        self.assertNotIn("leak", result["collect_error"])
        self.assertEqual(os.listdir(staging), [])

    def test_other_run_types_are_unchanged(self):
        result, container = self._run(
            lambda *_: {"StatusCode": 0}, run_type="results_visualization"
        )
        self.assertTrue(result["success"])
        self.assertEqual(self.run_kwargs["mem_limit"], "1g")
        self.assertEqual(self.run_kwargs["nano_cpus"], 1_000_000_000)
        self.assertNotIn("name", self.run_kwargs)
        self.assertNotIn("labels", self.run_kwargs)
        self.assertEqual(container.wait_timeout, 300)
        self.assertNotIn("collected", result)

    def test_remove_sandbox_container(self):
        import docker

        client = MagicMock()
        client.containers.get.side_effect = docker.errors.NotFound("gone")
        with patch("data.sandbox.get_docker_client", return_value=client):
            self.assertFalse(sandbox.remove_sandbox_container("swarmmedhub-explain-x"))
        client = MagicMock()
        with patch("data.sandbox.get_docker_client", return_value=client):
            self.assertTrue(sandbox.remove_sandbox_container("swarmmedhub-explain-x"))
        client.containers.get.return_value.remove.assert_called_once_with(force=True)
