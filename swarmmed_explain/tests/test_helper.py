import ast
import io
import json
import os
import sys

import matplotlib.pyplot as plt
import numpy as np
import pytest
import torch
from PIL import Image
from torch import nn

import swarmmed_explain
from swarmmed_explain.bundle import (
    MAX_CASES,
    MAX_PANELS_PER_CASE,
    MAX_SUMMARIES,
    BundleError,
    validate_bundle,
)
from swarmmed_explain.helper import (
    RUNTIME_FILES,
    THREADS_ENV,
    ExplainHelper,
    copy_package,
    run_explain_script,
    thread_limit,
    wrapper_source,
)

PACKAGE_DIR = os.path.dirname(os.path.abspath(swarmmed_explain.__file__))


@pytest.fixture
def site(tmp_path):
    """A site folder and a job's results, listed in a manifest file."""
    data = tmp_path / "data" / "fl-client-1"
    data.mkdir(parents=True)
    np.savez(
        data / "test.npz", X=np.arange(12.0).reshape(4, 3), y=np.array([0, 1, 0, 1])
    )
    (data / "site_info.json").write_text('{"n_test": 4}')
    results = tmp_path / "results" / "fl-client-1"
    results.mkdir(parents=True)
    torch.manual_seed(0)
    model = nn.Linear(3, 1)
    torch.save(
        {"model": model.state_dict(), "train_conf": {"round": 3}},
        results / "FL_global_model.pt",
    )
    manifest = {
        "fl-client-1/test.npz": str(data / "test.npz"),
        "fl-client-1/site_info.json": "data/fl-client-1/site_info.json",
        "results/fl-client-1/FL_global_model.pt": str(results / "FL_global_model.pt"),
    }
    path = tmp_path / "data_manifest.json"
    path.write_text(json.dumps(manifest))
    return path, model


@pytest.fixture
def explain(site, tmp_path):
    return ExplainHelper(site[0], tmp_path / "bundle", verbose=False)


@pytest.fixture
def quiet_threads(monkeypatch):
    for name in (
        THREADS_ENV,
        "OMP_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS",
    ):
        monkeypatch.setenv(name, "4")


def draw(width=2.0, height=1.5):
    figure, axis = plt.subplots(figsize=(width, height))
    axis.plot([0, 1], [1, 0])
    return figure


# --------------------------------------------------------------------- data access


def test_listdir_exists_and_open(explain):
    assert explain.listdir() == [
        "fl-client-1/site_info.json",
        "fl-client-1/test.npz",
        "results/fl-client-1/FL_global_model.pt",
    ]
    assert explain.listdir("fl-client-1/") == ["site_info.json", "test.npz"]
    assert explain.listdir("results") == ["fl-client-1/FL_global_model.pt"]
    assert explain.listdir("missing") == []
    assert explain.exists("fl-client-1")
    assert explain.exists("/fl-client-1/test.npz")
    assert not explain.exists("fl-client")
    assert not explain.exists("fl-client-1/train.npz")
    with explain.open("fl-client-1/test.npz") as handle:
        archive = np.load(handle)
        np.testing.assert_array_equal(archive["y"], [0, 1, 0, 1])
    # A relative path in a manifest file is resolved against the manifest's folder.
    with explain.open("fl-client-1/site_info.json", "r") as handle:
        assert json.load(handle) == {"n_test": 4}
    with pytest.raises(FileNotFoundError, match="not in manifest"):
        explain.open("fl-client-1/train.npz")
    for mode in ("wb", "w", "ab", "r+", "xb"):
        with pytest.raises(ValueError, match="read-only"):
            explain.open("fl-client-1/test.npz", mode)


def test_manifest_values_and_url_rewriting(tmp_path, capsys):
    url = "http://192.0.2.10:9000/bucket/site/test.npz?X-Amz-Signature=abc"
    manifest = {
        "/fl-client-1/test.npz": url,
        "fl-client-1/b.npz": "file:///srv/data/with%20space.npz",
        "fl-client-1/c.npz": "/srv/data/c.npz",
    }
    explain = ExplainHelper(manifest, tmp_path / "a")
    rewritten = "http://minio:9000/bucket/site/test.npz?X-Amz-Signature=abc"
    assert explain.manifest == {
        "fl-client-1/test.npz": rewritten,
        "fl-client-1/b.npz": "/srv/data/with space.npz",
        "fl-client-1/c.npz": "/srv/data/c.npz",
    }
    kept = ExplainHelper(manifest, tmp_path / "b", rewrite_urls=False, verbose=False)
    assert kept.manifest["fl-client-1/test.npz"] == url
    other = ExplainHelper(
        {"x": url}, tmp_path / "c", internal_host="storage", internal_port=9100
    )
    assert other.manifest["x"].startswith("http://storage:9100/bucket/")
    output = capsys.readouterr().out
    assert "192.0.2.10" not in output and "Signature" not in output
    with pytest.raises(ValueError, match="unsupported URL scheme"):
        ExplainHelper({"x": "s3://bucket/key"}, tmp_path / "d")
    with pytest.raises(ValueError, match="must be strings"):
        ExplainHelper({"x": 3}, tmp_path / "e")
    listing = tmp_path / "list.json"
    listing.write_text("[]")
    with pytest.raises(ValueError, match="must map"):
        ExplainHelper(listing, tmp_path / "f")


def test_get_model_and_load_weights(explain, site, tmp_path):
    _, original = site
    assert explain.find_model() == "results/fl-client-1/FL_global_model.pt"
    weights = explain.get_model()
    assert sorted(weights) == ["bias", "weight"]
    model = nn.Linear(3, 1)
    assert explain.load_weights(model, strict=True)
    for name, value in original.state_dict().items():
        torch.testing.assert_close(model.state_dict()[name], value)
    assert not explain.load_weights(object())
    empty = ExplainHelper({}, tmp_path / "empty", verbose=False)
    with pytest.raises(FileNotFoundError, match="Model weights not found"):
        empty.get_model()


def test_find_model_search_order(tmp_path):
    keys = [
        "results/fl-client-2/model.pt",
        "results/fl-client-1/FL_global_model.pt",
        "results/fl-client-3/best_FL_model.pt",
        "results/fl-client-1/log.txt",
    ]
    explain = ExplainHelper(
        {key: f"/x/{key}" for key in keys}, tmp_path / "a", verbose=False
    )
    assert (
        explain.find_model("fl-client-1", "model.pt") == "results/fl-client-2/model.pt"
    )
    assert explain.find_model("fl-client-1", "other.pt") == keys[1]
    assert explain.find_model("fl-client-3", "other.pt") == keys[2]
    only_npz = ExplainHelper(
        {"results/site/weights.npz": "/x/w.npz", "results/site/log.txt": "/x/l"},
        tmp_path / "b",
        verbose=False,
    )
    assert only_npz.find_model() == "results/site/weights.npz"


# --------------------------------------------------------------------- limits


def test_explain_helper_limits(explain):
    for index in range(MAX_CASES):
        assert explain.add_case(f"case-{index:02d}", outcome="correct") == index + 1
    with pytest.raises(BundleError, match="at most 24 cases"):
        explain.add_case("case-24")
    assert len(explain.case_ids) == MAX_CASES

    for panel in range(1, MAX_PANELS_PER_CASE + 1):
        draw()
        assert explain.add_panel("case-00", "occlusion") == f"case_01_panel_{panel}.png"
        assert plt.get_fignums() == []
    figure = draw()
    with pytest.raises(BundleError, match="4 panels already"):
        explain.add_panel("case-00", "occlusion")
    # The check runs before rendering, so the figure is still open for another use.
    assert plt.get_fignums() == [figure.number]
    assert explain.add_panel("case-23", "occlusion") == "case_24_panel_1.png"

    for position in range(1, MAX_SUMMARIES + 1):
        draw()
        assert explain.add_summary(f"Summary {position}") == f"summary_{position}.png"
    draw()
    with pytest.raises(BundleError, match="at most 4 summary figures"):
        explain.add_summary("Summary 5")

    data = explain.finish()
    assert data["complete"] is True
    assert len(data["cases"]) == MAX_CASES
    assert [len(case["panels"]) for case in data["cases"]][:2] == [4, 0]
    assert len(data["summaries"]) == MAX_SUMMARIES
    assert validate_bundle(explain.output_dir) == data


def test_explain_helper_rejects_bad_calls(explain):
    plt.close("all")
    explain.add_case("c1")
    with pytest.raises(ValueError, match="no open matplotlib figure"):
        explain.add_panel("c1", "occlusion")
    draw()
    with pytest.raises(BundleError, match="unknown case_id"):
        explain.add_panel("c2", "occlusion")
    with pytest.raises(BundleError, match="added already"):
        explain.add_case("c1")
    with pytest.raises(BundleError, match="looks like a direct identifier"):
        explain.add_case("c2", details={"patient_name": "x"})
    with pytest.raises(BundleError, match="outcome must be"):
        explain.add_case("c2", outcome="true positive")
    with pytest.raises(BundleError, match="must match"):
        explain.add_case("Jane Doe")
    with pytest.raises(BundleError, match="title is required"):
        explain.add_summary(None)
    with pytest.raises(BundleError, match="one string"):
        explain.set_info(methods="occlusion")
    with pytest.raises(BundleError, match="finite"):
        explain.set_info(metrics={"auroc": float("nan")})
    # BundleError is a ValueError, so scripts can catch either.
    assert issubclass(BundleError, ValueError)
    assert explain.case_ids == ["c1"]


def test_add_panel_saves_the_given_figure_and_closes_it(explain):
    explain.add_case("c1", label=1, prediction=0, score=np.float32(0.3), outcome="FN")
    first = draw(3.0, 2.0)
    second = draw()
    name = explain.add_panel("c1", "integrated_gradients", "IG", figure=first)
    assert not plt.fignum_exists(first.number)
    assert plt.fignum_exists(second.number)
    with Image.open(os.path.join(explain.output_dir, name)) as image:
        image.load()
        assert image.format == "PNG"
        assert 150 <= image.width <= 330 and 100 <= image.height <= 230
    assert explain.add_panel("c1", "occlusion") == "case_01_panel_2.png"
    assert not plt.fignum_exists(second.number)
    explain.set_info(methods=["integrated_gradients", "occlusion"], target="abnormal")
    explain.set_info(notes="baseline: zeros", metrics={"auroc_test": 0.81})
    data = explain.finalize()
    assert data["info"] == {
        "methods": ["integrated_gradients", "occlusion"],
        "target": "abnormal",
        "notes": "baseline: zeros",
        "metrics": {"auroc_test": 0.81},
    }
    case = data["cases"][0]
    assert (case["label"], case["prediction"], case["outcome"]) == ("1", "0", "FN")
    assert case["score"] == pytest.approx(0.3)
    assert [panel["method"] for panel in case["panels"]] == [
        "integrated_gradients",
        "occlusion",
    ]
    assert explain.finish() == data
    with pytest.raises(RuntimeError, match="finished"):
        explain.add_case("c2")


# --------------------------------------------------------------------- runner


def write_script(path, body):
    path.write_text(body, encoding="utf-8")
    return path


def test_run_explain_script_injects_explain_and_finishes(site, tmp_path, quiet_threads):
    script = write_script(
        tmp_path / "explainability.py",
        "import sys\n"
        "import matplotlib.pyplot as plt\n"
        "assert __name__ == '__main__'\n"
        "assert sys.argv[0].endswith('explainability.py')\n"
        "explain.add_case('c1', label=1, prediction=1, score=0.9, outcome='TP')\n"
        "plt.plot([0, 1], [0, 1])\n"
        "explain.add_panel('c1', 'occlusion', 'occlusion over time')\n"
        "explain.set_info(methods=['occlusion'])\n",
    )
    argv = sys.argv[:]
    data = run_explain_script(script, site[0], tmp_path / "out")
    assert sys.argv == argv
    assert data["complete"] is True
    assert [case["case_id"] for case in data["cases"]] == ["c1"]
    assert validate_bundle(tmp_path / "out") == data


def test_run_explain_script_failures_leave_the_bundle_incomplete(
    site, tmp_path, quiet_threads
):
    failing = write_script(
        tmp_path / "failing.py",
        "explain.add_case('c1')\nraise RuntimeError('model file is broken')\n",
    )
    with pytest.raises(RuntimeError, match="model file is broken"):
        run_explain_script(failing, site[0], tmp_path / "out1")
    with pytest.raises(BundleError, match="incomplete"):
        validate_bundle(tmp_path / "out1")
    assert validate_bundle(tmp_path / "out1", require_complete=False)["cases"]

    exiting = write_script(
        tmp_path / "exiting.py", "import sys\nexplain.add_case('c1')\nsys.exit(0)\n"
    )
    assert run_explain_script(exiting, site[0], tmp_path / "out2")["complete"] is True
    error_exit = write_script(tmp_path / "error_exit.py", "import sys\nsys.exit(3)\n")
    with pytest.raises(SystemExit):
        run_explain_script(error_exit, site[0], tmp_path / "out3")
    with pytest.raises(BundleError, match="incomplete"):
        validate_bundle(tmp_path / "out3")


def test_thread_limit(monkeypatch):
    monkeypatch.setenv(THREADS_ENV, "3")
    assert thread_limit() == 3
    monkeypatch.setenv(THREADS_ENV, "0")
    assert thread_limit() == 1
    monkeypatch.setenv(THREADS_ENV, "many")
    fallback = thread_limit()
    assert fallback is None or fallback >= 1


def test_wrapper_source_is_valid_python_with_the_given_paths():
    source = wrapper_source()
    assert "run_explain_script('explainability.py', " in source
    assert "'/home/sandboxuser/data/data_manifest.json', 'explain')" in source
    odd = wrapper_source("a 'quoted' \"name\".py", "/tmp/m\\n.json", "out\ndir")
    tree = ast.parse(odd)
    call = tree.body[-1].value
    assert [ast.literal_eval(arg) for arg in call.args] == [
        "a 'quoted' \"name\".py",
        "/tmp/m\\n.json",
        "out\ndir",
    ]


def test_copy_package_copies_the_runtime_modules_only(tmp_path):
    modules = {name for name in os.listdir(PACKAGE_DIR) if name.endswith(".py")}
    assert set(RUNTIME_FILES) == modules
    target = copy_package(tmp_path)
    assert target == os.path.join(str(tmp_path), "swarmmed_explain")
    assert sorted(os.listdir(target)) == sorted(RUNTIME_FILES)
    for name in RUNTIME_FILES:
        with (
            open(os.path.join(PACKAGE_DIR, name), "rb") as original,
            open(os.path.join(target, name), "rb") as copy,
        ):
            assert copy.read() == original.read()
    with pytest.raises(FileExistsError):
        copy_package(tmp_path)


def test_bundle_module_imports_without_torch_or_matplotlib(tmp_path):
    copy_package(tmp_path)
    code = (
        "import sys\n"
        "import swarmmed_explain.bundle\n"
        "import swarmmed_explain.helper\n"
        "heavy = [m for m in ('torch', 'matplotlib', 'numpy', 'django', 'PIL')"
        " if m in sys.modules]\n"
        "print(','.join(heavy))\n"
    )
    import subprocess

    completed = subprocess.run(
        [sys.executable, "-E", "-c", code],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    assert completed.stdout.strip() == ""


def test_png_written_by_a_panel_has_no_text_metadata(explain):
    explain.add_case("c1")
    draw()
    name = explain.add_panel("c1", "occlusion")
    with open(os.path.join(explain.output_dir, name), "rb") as handle:
        raw = handle.read()
    assert b"matplotlib" not in raw.lower()
    with Image.open(io.BytesIO(raw)) as image:
        assert "Software" not in image.info
