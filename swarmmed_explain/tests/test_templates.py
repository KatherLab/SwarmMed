"""The modality templates in examples/: their explanations and their code folders.

Each template's explainability.py runs here in-process, as the sandbox runs it, on a few
synthetic cases in the template's data layout and an untrained model built by the
template's own training.py. The checks are the ones blinded review relies on: every case
starts with an input panel (method "input") that carries no model output, method
comparisons use neutral greys instead of the red and blue of the maps, and the script
prints nothing per case.
"""

import contextlib
import importlib.util
import io
import json
import re
import runpy
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pytest
import torch
from matplotlib.colors import to_rgb
from matplotlib.text import Text

from swarmmed_explain.bundle import MAX_PANELS_PER_CASE
from swarmmed_explain.helper import ExplainHelper, run_explain_script
from swarmmed_explain.plotting import NEGATIVE_COLOR, POSITIVE_COLOR
from swarmmed_explain.run_local import build_manifest

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
TEMPLATES = (
    "tabular_heart_disease",
    "ehr_eicu_mortality",
    "ecg_ptbxl",
    "multimodal_ptbxl",
    "audio_heart_sounds",
    "eeg_sleep_staging",
)
# Words that name or depend on the model's output; none may appear in an input panel.
OUTPUT_WORDS = re.compile(
    r"predict|probab|logit|attribution|outcome|explained|confiden|\bscore\b|"
    r"\b(TP|FP|FN|TN|correct|incorrect)\b",
    re.IGNORECASE,
)
SIGN_COLOURS = {to_rgb(POSITIVE_COLOR), to_rgb(NEGATIVE_COLOR)}


def load_module(path, name, monkeypatch):
    """Import a template file without writing __pycache__ into examples/."""
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_model(template, monkeypatch):
    training = load_module(
        EXAMPLES / template / "training.py", f"{template}_training_test", monkeypatch
    )
    if template == "audio_heart_sounds":
        torch.manual_seed(0)
        return training, training.PCGNet()
    return training, training.build_model()


def tabular_data(rng, training):
    n = 40
    raw = np.zeros((n, 15), np.float32)
    raw[:, 0] = rng.integers(30, 80, n)
    raw[:, 1] = rng.integers(100, 180, n)
    raw[:, 2] = rng.integers(150, 350, n)
    raw[:, 3] = rng.integers(90, 190, n)
    raw[:, 4] = rng.random(n) * 4
    raw[:, 5:8] = rng.integers(0, 2, (n, 3))
    raw[np.arange(n), 8 + rng.integers(0, 4, n)] = 1
    raw[np.arange(n), 12 + rng.integers(0, 3, n)] = 1
    raw[:5, 2] = np.nan  # cholesterol not recorded
    split = {
        "X": rng.normal(size=(n, 15)).astype(np.float32),
        "X_raw": raw,
        "y": np.arange(n) % 2,
        "case_id": np.array([f"site-{k}" for k in range(n)]),
        "feature_names": np.array(training.FEATURE_NAMES),
    }
    return {"train.npz": split, "test.npz": split}


def ehr_data(rng, training):
    names = list(training.FEATURE_NAMES)
    n, width = 40, len(names)
    raw = rng.normal(50, 10, size=(n, width)).astype(np.float32)
    raw[:5, names.index("lactate_max")] = np.nan  # not charted
    raw[:, names.index("male")] = rng.integers(0, 2, n)
    for prefix in ("unit_", "src_"):
        columns = [k for k, name in enumerate(names) if name.startswith(prefix)]
        raw[:, columns] = 0
        raw[np.arange(n), rng.choice(columns, n)] = 1
    split = {
        "X": rng.normal(size=(n, width)).astype(np.float32),
        "X_raw": raw,
        "y": (np.arange(n) % 4 == 0).astype(np.int64),
        "case_id": np.array([str(1000 + k) for k in range(n)]),
        "feature_names": np.array(names),
    }
    return {"train.npz": split, "test.npz": split}


def ecg_arrays(rng, n=4):
    return {
        "X": (rng.normal(size=(n, 12, 1000)) * 200).astype(np.int16),
        "y": (np.arange(n) % 2).astype(np.int8),
        "y_super": np.eye(5, dtype=np.int8)[np.arange(n) % 5],
        "ecg_id": np.arange(100, 100 + n, dtype=np.int32),
    }


def ecg_data(rng, training):
    return {"test.npz": ecg_arrays(rng)}


def multimodal_data(rng, training):
    arrays = ecg_arrays(rng)
    n = len(arrays["y"])
    height = rng.normal(170, 10, n).astype(np.float32)
    height[::2] = np.nan
    arrays.update(
        tab=rng.normal(size=(n, 6)).astype(np.float32),
        age=np.where(np.arange(n) == 0, 90, rng.integers(20, 89, n)).astype(np.float32),
        sex=(np.arange(n) % 2).astype(np.int8),
        height=height,
        weight=rng.normal(80, 10, n).astype(np.float32),
    )
    return {"test.npz": arrays}


def audio_data(rng, training):
    n_rec, per_rec = 8, 5
    rec = np.repeat(np.arange(n_rec), per_rec).astype(np.int32)
    test = {
        "x": rng.normal(size=(n_rec * per_rec, 32, 150)).astype(np.float16),
        "y": (rec % 2).astype(np.int8),
        "rec": rec,
        "seg_start_s": np.tile(np.arange(per_rec) * 3.0, n_rec).astype(np.float32),
        "rec_ids": np.array([f"a{k:04d}" for k in range(n_rec)]),
        "rec_y": (np.arange(n_rec) % 2).astype(np.int8),
    }
    info = {"normalisation": {"mean": [0.0] * 32, "std": [1.0] * 32}}
    return {"test.npz": test, "site_info.json": info}


def eeg_data(rng, training):
    n = 40
    return {
        "test.npz": {
            "X": rng.normal(size=(n, 2, 3000)).astype(np.float16),
            "y": (np.arange(n) % 5).astype(np.int8),
            "subject": np.full(n, 7, np.int16),
            "night": np.ones(n, np.int8),
            "epoch": np.arange(100, 100 + n, dtype=np.int32),
        }
    }


DATA = {
    "tabular_heart_disease": tabular_data,
    "ehr_eicu_mortality": ehr_data,
    "ecg_ptbxl": ecg_data,
    "multimodal_ptbxl": multimodal_data,
    "audio_heart_sounds": audio_data,
    "eeg_sleep_staging": eeg_data,
}


def figure_texts(figure):
    return [text.get_text() for text in figure.findobj(Text) if text.get_text().strip()]


def titles(figure):
    found = [text.get_text() for text in figure.texts]
    if figure._suptitle is not None:
        found.append(figure._suptitle.get_text())
    for ax in figure.axes:
        found += [ax.get_title(loc) for loc in ("left", "center", "right")]
    return [text for text in found if text]


def drawn_colours(figure):
    colours = []
    for ax in figure.axes:
        colours += [to_rgb(line.get_color()) for line in ax.get_lines()]
        colours += [patch.get_facecolor()[:3] for patch in ax.patches]
    return colours


def is_grey(colour):
    """True for black, white and greys (also warm or cool ones), never red or blue."""
    return max(colour) - min(colour) < 0.06


def record_figures(monkeypatch):
    """Record every panel's and summary's figure before the helper saves it."""
    figures = []
    add_panel, add_summary = ExplainHelper.add_panel, ExplainHelper.add_summary

    def record(kind, name, title, figure):
        figure = plt.gcf() if figure is None else figure
        figures.append(
            {
                "kind": kind,
                "name": str(name),
                "title": title,
                "texts": figure_texts(figure),
                "titles": titles(figure),
                "colours": drawn_colours(figure),
                "images": [
                    image.get_cmap().name
                    for image in figure.findobj()
                    if type(image).__name__ == "AxesImage"
                ],  # fmt: skip
            }
        )

    def panel(self, case_id, method, title=None, *, figure=None):
        record("panel", method, title, figure)
        figures[-1]["case_id"] = str(case_id)
        return add_panel(self, case_id, method, title, figure=figure)

    def summary(self, title, *, figure=None):
        record("summary", title, title, figure)
        return add_summary(self, title, figure=figure)

    monkeypatch.setattr(ExplainHelper, "add_panel", panel)
    monkeypatch.setattr(ExplainHelper, "add_summary", summary)
    return figures


def run_template(template, tmp_path, monkeypatch):
    """Run the template's explainability.py; return the bundle's cases.json."""
    rng = np.random.default_rng(0)
    training, model = build_model(template, monkeypatch)
    site = tmp_path / "data" / "fl-client-1"
    site.mkdir(parents=True)
    for name, content in DATA[template](rng, training).items():
        if name.endswith(".json"):
            (site / name).write_text(json.dumps(content), encoding="utf-8")
        else:
            np.savez(site / name, **content)
    model_file = tmp_path / "FL_global_model.pt"
    torch.save({"model": model.state_dict()}, model_file)
    manifest = tmp_path / "data_manifest.json"
    manifest.write_text(
        json.dumps(build_manifest(str(tmp_path / "data"), model_file=str(model_file))),
        encoding="utf-8",
    )
    out = tmp_path / "bundle"
    run_explain_script(EXAMPLES / template / "explainability.py", manifest, out)
    return json.loads((out / "cases.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    """Run each template once per module: (cases.json, recorded figures, stdout)."""
    done = {}

    def get(template):
        if template not in done:
            threads = torch.get_num_threads()
            output = io.StringIO()
            with pytest.MonkeyPatch.context() as monkeypatch:
                figures = record_figures(monkeypatch)
                with contextlib.redirect_stdout(output):
                    bundle = run_template(
                        template, tmp_path_factory.mktemp(template), monkeypatch
                    )
            torch.set_num_threads(threads)
            done[template] = (bundle, figures, output.getvalue())
        return done[template]

    return get


@pytest.mark.parametrize("template", TEMPLATES)
def test_template_explanations_start_with_an_input_panel_without_model_output(
    template, runs
):
    bundle, recorded, output = runs(template)
    cases = bundle["cases"]
    assert cases
    for case in cases:
        methods = [panel["method"] for panel in case["panels"]]
        assert methods[0] == "input", (case["case_id"], methods)
        assert methods.count("input") == 1
        assert len(methods) <= MAX_PANELS_PER_CASE
        inputs = [
            fig
            for fig in recorded
            if fig.get("case_id") == case["case_id"] and fig["name"] == "input"
        ]
        assert len(inputs) == 1
        shown = inputs[0]
        words = [shown["title"] or "", *shown["texts"]]
        assert not [text for text in words if OUTPUT_WORDS.search(text)], words
        prediction = str(case["prediction"])
        if len(prediction) > 1 and not prediction.replace(".", "").isdigit():
            pattern = re.compile(rf"\b{re.escape(prediction)}\b")
            assert not [text for text in words if pattern.search(text)], words
        score_texts = {f"{case['score']:.2f}", f"{case['score']:.3f}"}
        assert not [t for t in shown["titles"] if any(s in t for s in score_texts)]
        # No map: tables and signals draw no image, a spectrogram only a grey one.
        assert set(shown["images"]) <= {"gray"}, shown["images"]
        # Nothing per case in the script output (the Hub keeps it hidden until every
        # case is read, but it should not hold model output per case at all).
        for line in output.splitlines():
            assert not (case["case_id"] in line and case["outcome"] in line), line
    assert "score=" not in output


@pytest.mark.parametrize("template", TEMPLATES)
def test_template_method_comparisons_use_neutral_colours(template, runs):
    _, recorded, _ = runs(template)
    compared = [
        fig
        for fig in recorded
        if fig["name"] in ("method_agreement", "time_profile")
        or (fig["kind"] == "summary" and re.search(r"greement|per lead", fig["name"]))
    ]
    if template != "tabular_heart_disease":  # tabular has no comparison figure
        assert compared
    for fig in compared:
        assert fig["colours"], fig["name"]
        assert all(is_grey(colour) for colour in fig["colours"]), (
            fig["name"],
            fig["colours"],
        )
        assert not SIGN_COLOURS & {tuple(c) for c in fig["colours"]}


def test_tabular_ablation_title_states_the_plotted_sign(runs):
    # The bars are f(x) - f(x with the variable at the site mean): the drop.
    _, recorded, _ = runs("tabular_heart_disease")
    ablation = [fig for fig in recorded if fig["name"] == "feature_ablation"]
    assert ablation
    for fig in ablation:
        assert any("drop in log-odds" in text for text in fig["titles"])
        assert not any("change when set" in text for text in fig["titles"])


def test_multimodal_input_panel_names_the_metadata(runs):
    _, recorded, _ = runs("multimodal_ptbxl")
    inputs = [fig for fig in recorded if fig["name"] == "input"]
    assert inputs
    for fig in inputs:
        text = " ".join(fig["titles"])
        assert re.search(r"age (90 or more|\d+ y)", text)
        assert re.search(r"\b(male|female)\b", text)
        assert re.search(r"height (\d+ cm|missing or implausible)", text)
        assert re.search(r"weight (\d+ kg|missing or implausible)", text)


@pytest.mark.parametrize("template", TEMPLATES)
def test_evaluate_writes_no_bytecode_into_the_template(template, tmp_path, monkeypatch):
    # SwarmMed uploads every file of a --code-dir, so evaluate.py, which imports the
    # template's training.py, must not leave __pycache__ next to it.
    folder = tmp_path / template
    folder.mkdir()
    for name in ("evaluate.py", "training.py"):
        (folder / name).write_bytes((EXAMPLES / template / name).read_bytes())
    monkeypatch.setattr(sys, "dont_write_bytecode", False)
    # run_path compiles evaluate.py without caching it, as running it as a script does;
    # audio's evaluate.py imports training.py at this point.
    names = runpy.run_path(str(folder / "evaluate.py"), run_name=f"{template}_eval")
    monkeypatch.setattr(sys, "dont_write_bytecode", False)
    names["load_training_module"]()
    assert not list(folder.rglob("__pycache__")), list(folder.rglob("*"))
