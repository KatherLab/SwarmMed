"""End-to-end check of the explainability run path with a real container.

Skipped unless ``SWARMMEDHUB_RUN_SANDBOX_EMULATION=1``. The test runs the Celery
task ``run_results_explanation_task`` and the real ``run_script_in_sandbox``
code (run folder, ``script.py`` from ``wrapper_source()``, the copied
``swarmmed_explain`` package, limits, collection with ``safe_copy_bundle``,
storing). Only the Docker daemon is emulated: instead of ``sandbox-dind`` and
the ``swarmmedhub-sandbox`` image, the local daemon runs the runtime image
(``SWARMMEDHUB_SANDBOX_EMULATION_IMAGE``, default ``swarmcloud-app:latest``) as
``docker run --name swm-dev-sandbox-emul --network none --memory 2g --cpus 2
--user 1000:1000`` with ``python`` as entry point; the container is removed
afterwards. Data and model come from local files instead of MinIO URLs.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import time
import unittest
from unittest.mock import patch

import numpy as np
from django.core.files.base import ContentFile
from PIL import Image

from .models import ExplanationPanel, ExplanationRun
from .tasks import run_results_explanation_task
from .test_explanations import FLARE_UUID, ExplanationFixture

EMULATION = os.getenv("SWARMMEDHUB_RUN_SANDBOX_EMULATION") == "1"
IMAGE = os.getenv("SWARMMEDHUB_SANDBOX_EMULATION_IMAGE", "swarmcloud-app:latest")
CONTAINER = "swm-dev-sandbox-emul"
FEATURES = 5

SCRIPT = """
import numpy as np
from torch import nn

from swarmmed_explain import (
    as_predict_fn,
    feature_ablation,
    integrated_gradients,
    permutation_importance,
    plot_feature_bars,
    roc_auc,
    select_cases,
)

FEATURES = ["f0", "f1", "f2", "f3", "f4"]
model = nn.Sequential(nn.Linear(5, 8), nn.Tanh(), nn.Linear(8, 1))
explain.load_weights(model, strict=True)
model.eval()

with explain.open("fl-client-1/test.npz") as handle:
    archive = np.load(handle)
    X, y, ids = archive["X"], archive["y"], archive["case_ids"]

scores = as_predict_fn(model, output="probability")(X)
reference = X.mean(axis=0)
for case in select_cases(y, scores, n_per_outcome=2):
    case_id = str(ids[case.index])
    explain.add_case(
        case_id,
        label=case.label,
        prediction=case.prediction,
        score=case.score,
        target="class 1",
        outcome=case.outcome,
        details={"site": "fl-client-1"},
    )
    x = X[case.index]
    plot_feature_bars(x, FEATURES, title="Input values", xlabel="value")
    explain.add_panel(case_id, "input", "feature values")
    plot_feature_bars(
        feature_ablation(model, x, baseline=reference),
        FEATURES,
        feature_values=x,
        title="Feature ablation",
    )
    explain.add_panel(case_id, "feature_ablation", "drop when set to the mean")
    ig = integrated_gradients(model, x, baseline=reference, steps=32)
    plot_feature_bars(ig.attributions, FEATURES, title="Integrated gradients")
    explain.add_panel(case_id, "integrated_gradients")

result = permutation_importance(model, X, y, roc_auc, n_repeats=3, seed=0)
plot_feature_bars(
    result.mean, FEATURES, errors=result.std, title="Permutation importance"
)
explain.add_summary("Permutation importance on the test split")
explain.set_info(
    methods=["feature_ablation", "integrated_gradients", "permutation_importance"],
    target="class 1",
    metrics={"auroc_test": roc_auc(y, scores)},
)
print("SWARMMED_EXPLAIN_DONE", len(explain.case_ids))
"""


def _train_model_and_data(folder):
    """Write fl-client-1/test.npz and a trained model's state dict."""
    import torch
    from torch import nn

    rng = np.random.default_rng(7)
    features = rng.normal(size=(120, FEATURES)).astype(np.float32)
    logits = features @ np.array([1.5, -1.0, 0.5, 0.0, 0.0], dtype=np.float32)
    labels = (logits + rng.normal(scale=1.0, size=120) > 0).astype(np.int64)
    case_ids = np.array([f"site-1:{index:04d}" for index in range(120)])
    os.makedirs(os.path.join(folder, "fl-client-1"))
    np.savez(
        os.path.join(folder, "fl-client-1", "test.npz"),
        X=features,
        y=labels,
        case_ids=case_ids,
    )
    torch.manual_seed(0)
    model = nn.Sequential(nn.Linear(FEATURES, 8), nn.Tanh(), nn.Linear(8, 1))
    optimiser = torch.optim.SGD(model.parameters(), lr=0.5)
    inputs = torch.from_numpy(features)
    targets = torch.from_numpy(labels).float().unsqueeze(1)
    for _ in range(200):
        optimiser.zero_grad()
        loss = nn.functional.binary_cross_entropy_with_logits(model(inputs), targets)
        loss.backward()
        optimiser.step()
    os.makedirs(os.path.join(folder, "results", "fl-client-1"))
    torch.save(
        model.state_dict(),
        os.path.join(folder, "results", "fl-client-1", "FL_global_model.pt"),
    )


class _EmulatedContainers:
    """Runs what the sandbox code asks for on the local daemon, as the emulation."""

    def __init__(self, client, requested):
        self.client = client
        self.requested = requested

    def run(self, **kwargs):
        self.requested.update(kwargs)
        options = dict(kwargs)
        options["image"] = IMAGE
        options["entrypoint"] = ["python"]
        options["name"] = CONTAINER
        options["user"] = "1000:1000"
        options["network_mode"] = "none"
        for key in ("network", "extra_hosts", "shm_size"):
            options.pop(key, None)
        return self.client.containers.run(**options)

    def get(self, name):
        return self.client.containers.get(CONTAINER)


@unittest.skipUnless(
    EMULATION, "set SWARMMEDHUB_RUN_SANDBOX_EMULATION=1 to run a local container"
)
class SandboxEmulationTests(ExplanationFixture):
    def setUp(self):
        import docker

        super().setUp()
        self.docker = docker.from_env()
        self._remove_container()
        self.addCleanup(self._remove_container)
        self.local_data = os.path.join(self.work_dir, "data")
        _train_model_and_data(self.local_data)
        self.project.explainability_script.save(
            "explainability.py", ContentFile(SCRIPT.encode()), save=True
        )
        self.run_obj = ExplanationRun.objects.create(
            project=self.project,
            job=self.job,
            flare_job_id=FLARE_UUID,
            user=self.member,
        )

    def _remove_container(self):
        import docker

        with contextlib.suppress(docker.errors.NotFound):
            self.docker.containers.get(CONTAINER).remove(force=True)

    def _manifest(self, project, flare_id, filesystem, log):
        manifest = {}
        for relative in (
            "fl-client-1/test.npz",
            "results/fl-client-1/FL_global_model.pt",
        ):
            target = os.path.join(filesystem.temp_dir, relative)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copyfile(os.path.join(self.local_data, relative), target)
            manifest[relative] = relative  # resolved against the manifest's folder
        with open(os.path.join(filesystem.temp_dir, "data_manifest.json"), "w") as f:
            json.dump(manifest, f)
        return manifest

    def test_real_script_runs_through_the_worker_path(self):
        requested = {}
        client = type("Client", (), {})()
        client.containers = _EmulatedContainers(self.docker, requested)
        started = time.monotonic()
        with (
            patch.dict(
                os.environ,
                {
                    "SWARMMEDHUB_EXPLAIN_MEM_LIMIT": "2g",
                    "SWARMMEDHUB_EXPLAIN_CPUS": "2",
                    "SWARMMEDHUB_EXPLAIN_TIMEOUT": "900",
                    "SWARMMEDHUB_ENABLE_GPU": "false",
                },
            ),
            patch("results.tasks._build_explanation_manifest", self._manifest),
            patch("data.sandbox.get_docker_client", return_value=client),
            patch("data.sandbox.ensure_sandbox_image"),
            patch("data.sandbox.ensure_sandbox_network"),
            patch(
                "data.sandbox._resolve_minio_host_for_sandbox",
                side_effect=OSError("no MinIO in the emulation"),
            ),
            patch("data.sandbox.get_host_path", side_effect=lambda path: path),
        ):
            outcome = run_results_explanation_task(self.run_obj.id, FLARE_UUID)
        elapsed = time.monotonic() - started

        self.run_obj.refresh_from_db()
        print(
            f"\nsandbox emulation: {elapsed:.1f} s, status {self.run_obj.status}, "
            f"success {self.run_obj.success}, cases {self.run_obj.cases.count()}"
        )
        self.assertEqual(outcome, {"success": True}, self.run_obj.error_message)
        self.assertEqual(self.run_obj.status, "completed")
        self.assertIn("SWARMMED_EXPLAIN_DONE", self.run_obj.output)

        self.assertEqual(requested["image"], "swarmmedhub-sandbox")
        self.assertEqual(requested["command"], ["script.py"])
        self.assertEqual(requested["mem_limit"], "2g")
        self.assertEqual(requested["nano_cpus"], 2_000_000_000)
        self.assertEqual(
            requested["name"], f"swarmmedhub-explain-{self.run_obj.identifier}"
        )

        cases = list(self.run_obj.cases.all())
        self.assertGreaterEqual(len(cases), 4)
        self.assertLessEqual(len(cases), 8)
        outcomes = {case.outcome for case in cases}
        self.assertTrue(outcomes <= {"TP", "FP", "FN", "TN"})
        for case in cases:
            self.assertTrue(case.case_id.startswith("site-1:"))
            methods = [panel.method for panel in case.panels.all()]
            self.assertEqual(
                methods, ["input", "feature_ablation", "integrated_gradients"]
            )
        self.assertEqual(self.run_obj.summaries.count(), 1)
        self.assertIn("auroc_test", self.run_obj.info["metrics"])
        panel = ExplanationPanel.objects.filter(case__run=self.run_obj).first()
        with panel.image.open("rb") as handle:
            image = Image.open(io.BytesIO(handle.read()))
            image.load()
        self.assertEqual(image.size, (panel.width, panel.height))
        import docker

        with self.assertRaises(docker.errors.NotFound):
            self.docker.containers.get(CONTAINER)
        self.assertEqual(os.listdir(self.temp_dir), [])
