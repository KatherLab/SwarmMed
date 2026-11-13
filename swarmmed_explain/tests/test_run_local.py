import os
import subprocess
import sys

import numpy as np
import pytest
import torch
from torch import nn

from swarmmed_explain import run_local
from swarmmed_explain.bundle import BundleError, safe_copy_bundle, validate_bundle
from swarmmed_explain.methods import as_predict_fn
from swarmmed_explain.selection import select_cases

FEATURES = ["age", "bp", "chol", "hr", "st"]

# A complete explainability.py for a tabular toy model, as a template would write it.
TOY_SCRIPT = """
import os

import numpy as np
import torch
from torch import nn

import swarmmed_explain
from swarmmed_explain import (
    as_predict_fn,
    feature_ablation,
    integrated_gradients,
    permutation_importance,
    plot_feature_bars,
    roc_auc,
    select_cases,
)
from swarmmed_explain.plotting import plot_feature_table

# Run from a fresh folder next to a copy of the package, as in the sandbox.
here = os.path.join(os.getcwd(), "swarmmed_explain")
assert os.path.dirname(swarmmed_explain.__file__) == here, swarmmed_explain.__file__
assert torch.get_num_threads() <= 2, torch.get_num_threads()

FEATURES = __FEATURES__
model = nn.Sequential(nn.Linear(5, 8), nn.Tanh(), nn.Linear(8, 1))
assert explain.load_weights(model, strict=True)
model.eval()

with explain.open("fl-client-1/test.npz") as handle:
    archive = np.load(handle)
    X, y, ids = archive["X"], archive["y"], archive["case_ids"]

scores = as_predict_fn(model, output="probability")(X)
baseline = X.mean(axis=0)
for case in select_cases(y, scores, n_per_outcome=2):
    case_id = str(ids[case.index])
    explain.add_case(
        case_id,
        label=case.label,
        prediction=case.prediction,
        score=case.score,
        target="class 1",
        outcome=case.outcome,
        details={"age_group": "60-69"},
    )
    x = X[case.index]
    plot_feature_table(FEATURES, x, reference=baseline, title=f"Values of {case_id}")
    explain.add_panel(case_id, "input", "Recorded values")
    ablation = feature_ablation(model, x, baseline=baseline)
    plot_feature_bars(ablation, FEATURES, feature_values=x, title="Feature ablation")
    explain.add_panel(case_id, "feature_ablation", "drop when set to the mean")
    ig = integrated_gradients(model, x, baseline=baseline, steps=32)
    plot_feature_bars(ig.attributions, FEATURES, title="Integrated gradients")
    explain.add_panel(case_id, "integrated_gradients")

result = permutation_importance(model, X, y, roc_auc, n_repeats=3, seed=0)
plot_feature_bars(result.mean, FEATURES, errors=result.std, title="Permutation")
explain.add_summary("Permutation importance on the test split")
explain.set_info(
    methods=["feature_ablation", "integrated_gradients", "permutation_importance"],
    target="class 1",
    metrics={"auroc_test": roc_auc(y, scores)},
)
print("toy explainability done")
""".replace("__FEATURES__", repr(FEATURES))


@pytest.fixture
def toy_site(tmp_path):
    """A site folder with a test split, a FLARE-style checkpoint and the toy script."""
    torch.manual_seed(0)
    model = nn.Sequential(nn.Linear(5, 8), nn.Tanh(), nn.Linear(8, 1))
    rng = np.random.default_rng(0)
    X = rng.normal(size=(60, 5)).astype(np.float32)
    with torch.no_grad():
        logits = model(torch.from_numpy(X))[:, 0]
        # Centre the logits so that the model predicts both classes.
        model[2].bias -= logits.median()
    scores = as_predict_fn(model, output="probability")(X)
    y = (scores > 0.5).astype(np.int64)
    flipped = rng.random(60) < 0.25
    y[flipped] = 1 - y[flipped]
    ids = np.array([f"site1-{index:04d}" for index in range(60)])
    data = tmp_path / "data" / "fl-client-1"
    data.mkdir(parents=True)
    np.savez(data / "test.npz", X=X, y=y, case_ids=ids)
    (data / ".cache").write_text("hidden files are not listed")
    model_path = tmp_path / "FL_global_model.pt"
    torch.save({"model": model.state_dict()}, model_path)
    script = tmp_path / "my_explainability.py"
    script.write_text(TOY_SCRIPT, encoding="utf-8")
    expected = [str(ids[case.index]) for case in select_cases(y, scores)]
    return {
        "root": tmp_path,
        "data": tmp_path / "data",
        "model": model_path,
        "script": script,
        "expected_ids": expected,
        "outcomes": [case.outcome for case in select_cases(y, scores)],
    }


def run(site, out, *extra, script=None):
    return run_local.main(
        [
            "--script",
            str(script or site["script"]),
            "--data",
            str(site["data"]),
            "--model",
            str(site["model"]),
            "--out",
            str(out),
            *extra,
        ]
    )


def test_run_local_end_to_end_on_a_toy_model(toy_site, capfd):
    out = toy_site["root"] / "bundle"
    code = run(toy_site, out, "--threads", "2", "--timeout", "300")
    captured = capfd.readouterr()
    assert code == 0, captured.err
    assert "toy explainability done" in captured.out
    assert "run_local: 1 data files, 1 result files" in captured.out
    assert "explain: loading model weights from results/fl-client-1/" in captured.out
    assert "run_local: bundle OK: 8 cases, 24 panels, 1 summary" in captured.out

    data = validate_bundle(out)
    assert data["complete"] is True
    assert [case["case_id"] for case in data["cases"]] == toy_site["expected_ids"]
    assert [case["outcome"] for case in data["cases"]] == toy_site["outcomes"]
    assert toy_site["outcomes"] == ["TP", "TP", "FP", "FP", "FN", "FN", "TN", "TN"]
    for case in data["cases"]:
        assert [panel["method"] for panel in case["panels"]] == [
            "input",
            "feature_ablation",
            "integrated_gradients",
        ]
        assert case["details"] == {"age_group": "60-69"}
        assert case["target"] == "class 1"
        assert 0.0 <= case["score"] <= 1.0
    assert data["info"]["methods"] == [
        "feature_ablation",
        "integrated_gradients",
        "permutation_importance",
    ]
    assert 0.5 < data["info"]["metrics"]["auroc_test"] <= 1.0
    assert [summary["file"] for summary in data["summaries"]] == ["summary_1.png"]

    # The Hub's worker copy accepts the bundle and re-encodes every image.
    copied = safe_copy_bundle(out, toy_site["root"] / "copied")
    assert copied["data"] == data
    assert len(copied["files"]) == 25


def test_run_local_reports_a_failing_script(toy_site, tmp_path, capfd):
    failing = tmp_path / "failing.py"
    failing.write_text("explain.add_case('c1')\nraise RuntimeError('broken model')\n")
    out = tmp_path / "failed"
    assert run(toy_site, out, script=failing) == 1
    captured = capfd.readouterr()
    assert "RuntimeError: broken model" in captured.err
    assert "failed with exit code 1" in captured.err
    with pytest.raises(BundleError, match="incomplete"):
        validate_bundle(out)


def test_run_local_stops_a_script_at_the_timeout(toy_site, tmp_path, capfd):
    slow = tmp_path / "slow.py"
    slow.write_text("import time\ntime.sleep(60)\n")
    assert run(toy_site, tmp_path / "slow_out", "--timeout", "2", script=slow) == 1
    assert "stopped the script after 2 s" in capfd.readouterr().err


def test_run_local_output_folder_rules(toy_site, tmp_path, capfd):
    out = tmp_path / "out"
    out.mkdir()
    (out / "keep.txt").write_text("not a bundle file")
    quick = tmp_path / "quick.py"
    quick.write_text("explain.add_case('c1', outcome='correct')\n")
    assert run(toy_site, out, script=quick) == 2
    assert "is not empty" in capfd.readouterr().err
    assert run(toy_site, out, "--force", script=quick) == 2
    assert "not part of a bundle" in capfd.readouterr().err

    (out / "keep.txt").unlink()
    assert run(toy_site, out, script=quick) == 0
    assert validate_bundle(out)["cases"][0]["case_id"] == "c1"
    assert run(toy_site, out, script=quick) == 2
    assert run(toy_site, out, "--force", script=quick) == 0
    assert validate_bundle(out)["cases"][0]["outcome"] == "correct"


def test_run_local_argument_errors(toy_site, tmp_path, capfd):
    assert run(toy_site, tmp_path / "a", script=tmp_path / "missing.py") == 2
    assert "does not exist" in capfd.readouterr().err
    assert run(toy_site, tmp_path / "b", "--threads", "0") == 2
    assert "--threads must be at least 1" in capfd.readouterr().err
    assert run(toy_site, tmp_path / "c", "--client", "../escape") == 2
    assert "not a plain folder name" in capfd.readouterr().err
    with pytest.raises(SystemExit):
        run_local.main(["--script", "x.py", "--data", "d", "--out", "o"])


def test_build_manifest(tmp_path):
    data = tmp_path / "data"
    (data / "fl-client-1").mkdir(parents=True)
    (data / "fl-client-1" / "test.npz").write_bytes(b"x")
    (data / "fl-client-1" / ".hidden").write_bytes(b"x")
    (data / ".git").mkdir()
    (data / ".git" / "config").write_bytes(b"x")
    (data / "site_info.json").write_text("{}")
    results = tmp_path / "results"
    (results / "fl-client-1").mkdir(parents=True)
    (results / "fl-client-1" / "FL_global_model.pt").write_bytes(b"x")
    (results / "fl-client-1" / "training.py").write_text("x = 1")
    (results / "fl-client-2").mkdir()
    (results / "fl-client-2" / "metrics.json").write_text("{}")

    manifest = run_local.build_manifest(str(data), results_dir=str(results))
    assert sorted(manifest) == [
        "fl-client-1/test.npz",
        "results/fl-client-1/FL_global_model.pt",
        "results/fl-client-2/metrics.json",
        "site_info.json",
    ]
    assert all(os.path.isabs(path) for path in manifest.values())

    model = tmp_path / "weights.pt"
    model.write_bytes(b"x")
    single = run_local.build_manifest(
        str(data), model_file=str(model), client_name="site-a"
    )
    assert single["results/site-a/weights.pt"] == str(model)
    with pytest.raises(ValueError, match="plain folder name"):
        run_local.build_manifest(str(data), model_file=str(model), client_name="a/b")
    (data / "results" / "site-a").mkdir(parents=True)
    (data / "results" / "site-a" / "weights.pt").write_bytes(b"x")
    with pytest.raises(ValueError, match="both a data file and a result file"):
        run_local.build_manifest(str(data), model_file=str(model), client_name="site-a")
    with pytest.raises(FileNotFoundError):
        run_local.build_manifest(str(tmp_path / "missing"))
    with pytest.raises(FileNotFoundError):
        run_local.build_manifest(str(data), model_file=str(tmp_path / "missing.pt"))


def test_run_local_module_help():
    completed = subprocess.run(
        [sys.executable, "-m", "swarmmed_explain.run_local", "--help"],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert completed.returncode == 0
    assert "--script" in completed.stdout and "--force" in completed.stdout
