"""How the breast-MRI example's explanation script checks its cohorts.

``experiments/duke_mst_cli/explain/run_explainability.py`` explains cases of the
test splits of its fixed cohorts or, with ``--site-institution``, of a training
site's own share. It is loaded by path here, as it is run. The model, the dataset
and the gradient maps (grad-cam is not installed here) are replaced by stand-ins, so
neither a GPU nor the training image is needed.
"""

import importlib.util
import json
import logging
import sys
import tempfile
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

EXPLAIN_DIR = (
    Path(__file__).resolve().parents[2] / "experiments" / "duke_mst_cli" / "explain"
)
# Imported by plain name: the script's siblings, predictor.py's import from the
# folder above, and vit_cam, which is replaced by a stand-in.
PLAIN_MODULES = ("occlusion", "predictor", "viz", "vit_cam", "evaluate_global_model")


def _restore_module(name, module):
    if module is None:
        sys.modules.pop(name, None)
    else:
        sys.modules[name] = module


def _record(uid, target, pred, prob_malignant):
    """Return one case as predictor.predict_per_case returns it."""
    return {
        "uid": uid,
        "target": target,
        "pred": pred,
        "prob_no": 0.0,
        "prob_benign": 1.0 - prob_malignant,
        "prob_malignant": prob_malignant,
    }


class RunExplainabilityTests(SimpleTestCase):
    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.temp = Path(temp_dir.name)
        self.duke_root = self.temp / "DUKE_iid"
        self.output_dir = self.temp / "explanations"
        for name in PLAIN_MODULES:
            self.addCleanup(_restore_module, name, sys.modules.pop(name, None))
        vit_cam = ModuleType("vit_cam")
        vit_cam.compute_cam_volume = MagicMock(name="compute_cam_volume")
        sys.modules["vit_cam"] = vit_cam
        search_path = patch.object(sys, "path", [str(EXPLAIN_DIR), *sys.path])
        search_path.start()
        self.addCleanup(search_path.stop)
        # Import without writing __pycache__ into the example.
        with patch.object(sys, "dont_write_bytecode", True):
            spec = importlib.util.spec_from_file_location(
                "duke_mst_run_explainability", EXPLAIN_DIR / "run_explainability.py"
            )
            self.script = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.script)

    def run_main(self, *arguments):
        argv = [
            "run_explainability.py",
            "--checkpoint",
            str(self.temp / "FL_global_model.pt"),
            "--duke-root",
            str(self.duke_root),
            "--output-dir",
            str(self.output_dir),
            *arguments,
        ]
        with patch.object(sys, "argv", argv), patch.object(logging, "basicConfig"):
            self.script.main()

    def test_site_without_site_institution_stops_before_the_model_loads(self):
        known = list(self.script.COHORT_ALIASES)
        self.assertNotIn("site", known)
        with (
            patch.object(self.script, "load_model") as load_model,
            self.assertRaises(SystemExit) as stopped,
        ):
            self.run_main("--split", "val", "--cohorts", "site")
        self.assertEqual(
            str(stopped.exception),
            "Unknown cohort(s): site (the cohort 'site' needs --site-institution). "
            f"Known: {known}",
        )
        load_model.assert_not_called()
        self.assertFalse(self.output_dir.exists())

    def test_an_unknown_cohort_stops_before_the_model_loads(self):
        known = list(self.script.COHORT_ALIASES)
        with (
            patch.object(self.script, "load_model") as load_model,
            self.assertRaises(SystemExit) as stopped,
        ):
            self.run_main("--cohorts", "duke", "dukee")
        self.assertEqual(
            str(stopped.exception), f"Unknown cohort(s): dukee. Known: {known}"
        )
        load_model.assert_not_called()

    def test_a_split_without_cases_stops_the_run_and_names_the_split(self):
        for arguments, message in (
            (
                ("--site-institution", "node_A", "--cohorts", "site"),
                f"No 'test' cases for cohort site under {self.duke_root / 'node_A'}; "
                "a site's own share has the splits train and val (--split val)",
            ),
            (
                ("--split", "val", "--cohorts", "duke"),
                f"No 'val' cases for cohort duke under {self.duke_root / 'test'}",
            ),
        ):
            with self.subTest(arguments=arguments):
                with (
                    patch.object(self.script, "load_model"),
                    patch.object(
                        self.script, "build_dataloader", return_value=([], None)
                    ),
                    patch.object(self.script, "predict_per_case") as predict,
                    self.assertRaises(SystemExit) as stopped,
                ):
                    self.run_main(*arguments)
                self.assertEqual(str(stopped.exception), message)
                predict.assert_not_called()

    def test_site_cases_skip_the_auroc_check_with_a_warning(self):
        records = [
            _record("case-1", target=2, pred=0, prob_malignant=0.4),
            _record("case-2", target=1, pred=1, prob_malignant=0.1),
        ]
        arguments = "--site-institution node_A --split val --cohorts site --outcomes TP"
        with (
            patch.object(self.script, "load_model"),
            patch.object(
                self.script, "build_dataloader", return_value=(records, None)
            ) as build_dataloader,
            patch.object(self.script, "predict_per_case", return_value=records),
            patch.object(self.script, "render_composite"),
            patch.object(self.script, "render_extended"),
            self.assertLogs("duke_mst_explain.run", "WARNING") as logs,
        ):
            self.run_main(*arguments.split())
        build_dataloader.assert_called_once_with(
            self.duke_root, institution="node_A", split="val", num_workers=4
        )
        self.assertIn(
            "No published AUROC for site; skipping the AUROC check", logs.output[0]
        )
        self.assertNotIn("reference JSON", "\n".join(logs.output))
        self.assertEqual(
            json.loads((self.output_dir / "metrics_check.json").read_text()),
            {"site": {"measured": 1.0, "published": None, "delta": None}},
        )
        predictions = self.output_dir / "per_case_predictions" / "site_node_A_val.csv"
        self.assertTrue(predictions.is_file())
        report = (self.output_dir / "report.md").read_text(encoding="utf-8")
        self.assertIn("| site | 1.0000 | — | — |", report)

    def test_published_cohorts_are_still_checked(self):
        ref_dir = self.temp / "eval"
        ref_dir.mkdir()
        (ref_dir / "duke_test_eval.json").write_text(
            json.dumps({"results": [{"metrics": {"auroc": 0.9007}}]})
        )
        args = SimpleNamespace(ref_json_dir=ref_dir, auroc_tolerance=0.01)
        metrics_check = {}
        self.script._check_against_published("duke", 0.9007, args, metrics_check)
        self.assertEqual(
            metrics_check["duke"],
            {"measured": 0.9007, "published": 0.9007, "delta": 0.0},
        )
        with self.assertRaisesMessage(RuntimeError, "AUROC mismatch for duke"):
            self.script._check_against_published("duke", 0.8, args, metrics_check)
        with self.assertLogs("duke_mst_explain.run", "WARNING") as logs:
            self.script._check_against_published("MHA", 0.7, args, metrics_check)
        missing = ref_dir / self.script.COHORT_ALIASES["MHA"]["ref_json"]
        self.assertIn(f"No reference JSON at {missing}", logs.output[0])
        self.assertEqual(
            metrics_check["MHA"], {"measured": 0.7, "published": None, "delta": None}
        )
