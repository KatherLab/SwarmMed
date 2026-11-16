"""SwarmMed template ehr_eicu_mortality: in-hospital death from the first 24 h of an ICU stay.

Each site trains a small multilayer perceptron on its own share of the eICU Collaborative
Research Database Demo (one site per US region; see README.md and prepare_data.py). Per swarm
round, every site

 1. receives the global model (round 0: no weights; every site starts from the same seeded
    initialisation),
 2. scores it on the site's validation split and prints one line
    ``SWARMMED_METRIC site=<name> round=<r> split=val auroc=... auprc=... accuracy=... loss=...
    n=... n_pos=...``,
 3. trains it for LOCAL_EPOCHS epochs on the site's training split,
 4. sends the weights back with the number of training cases as aggregation weight and the
    validation accuracy as the model-selection metric.

Data: the registered data hold either one folder per FLARE site (``fl-client-1/train.npz``, ...)
or one site's files at the root (``train.npz``, ...). Each site reads only its own folder. The
arrays come from prepare_data.py: ``X`` is already imputed and standardised with the site's own
training statistics, so nothing is fitted here and the model's state dict holds only weights.

The final global model is scored with evaluate.py (test split, AUROC with a bootstrap CI).
The number of rounds is SWARM_ROUNDS below unless SWARMMEDHUB_SWARM_ROUNDS is set when the
training is submitted.
"""

import io
import json
import math
import os
import random
import time
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
from sklearn.metrics import average_precision_score, roc_auc_score

SWARM_ROUNDS = 20
LOCAL_EPOCHS = 5
BATCH_SIZE = 32
LEARNING_RATE = 1e-3
WEIGHT_DECAY = 1e-4
HIDDEN_UNITS = 32
DROPOUT = 0.2
SEED = 42
TORCH_THREADS = 4  # every client container could otherwise use all cores of the host

TEMPLATE = "ehr_eicu_mortality"
# The 41 model inputs in the order prepare_data.py writes them (32 continuous, 1 binary,
# 8 one-hot). Every site must have exactly these columns, so the global model fits everywhere.
FEATURE_NAMES = [
    "age", "height_cm", "weight_kg", "pre_icu_days",
    "hr_min", "hr_max", "hr_mean", "rr_min", "rr_max", "rr_mean", "spo2_min", "spo2_mean",
    "sbp_min", "sbp_mean", "map_min", "map_mean", "temp_min", "temp_max", "gcs_min",
    "creat_max", "bun_max", "na_min", "na_max", "k_max", "glucose_max", "hco3_min",
    "wbc_max", "hgb_min", "plt_min", "lactate_max", "bili_max", "albumin_min",
    "male",
    "unit_medsurg", "unit_medical", "unit_cardiac", "unit_surg_neuro",
    "src_ed", "src_or", "src_ward", "src_other",
]  # fmt: skip
METRICS_FILE = (
    "val_metrics.json"  # written next to the job's app folder, see save_round_metrics
)


# ----------------------------------------------------------------------------- model


class MortalityMLP(nn.Module):
    """41 inputs -> 32 ReLU units -> dropout -> 1 logit (1,377 parameters).

    No BatchNorm: its running statistics would be averaged across sites with different
    distributions, and small sites give small batches. The state dict holds only the weights
    of the two linear layers (keys net.0.weight, net.0.bias, net.3.weight, net.3.bias).
    """

    def __init__(self, n_features: int = len(FEATURE_NAMES), hidden: int = HIDDEN_UNITS,
                 dropout: float = DROPOUT):  # fmt: skip
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(n_features, hidden),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(hidden, 1),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.net(x).squeeze(-1)


def build_model() -> MortalityMLP:
    """Seeded initialisation, so that every site starts round 0 from the same weights."""
    torch.manual_seed(SEED)
    return MortalityMLP()


def predict_proba(model: nn.Module, x: np.ndarray) -> np.ndarray:
    """Predicted probability of in-hospital death for each row of x (float32 [n, 41])."""
    model.eval()
    with torch.no_grad():
        logits = model(torch.as_tensor(np.asarray(x, dtype=np.float32)))
    return torch.sigmoid(logits).numpy().astype(float)


# ----------------------------------------------------------------------------- data


def site_name() -> str | None:
    """FLARE site name (call after flare_adapter.init_flare()).

    nvflare.client.get_site_name() works on the Hub and in the simulator. SWARMMED_SITE_NAME or
    SITE_NAME help in manual runs; the last fallback is the job folder app_<site>/custom/.
    """
    try:
        import nvflare.client as flare

        name = flare.get_site_name()
        if name:
            return str(name)
    except Exception as exc:  # noqa: BLE001 - fall back to the other sources
        print(f"{TEMPLATE}: nvflare.client.get_site_name() failed: {exc}")
    for variable in ("SWARMMED_SITE_NAME", "SITE_NAME"):
        value = os.environ.get(variable, "").strip()
        if value:
            return value
    app_dir = Path(__file__).resolve().parent.parent.name
    return app_dir[4:] if app_dir.startswith("app_") else None


def select_site_files(keys, site: str | None) -> tuple[dict, str]:
    """Return ({file name: manifest key}, description) of this site's files.

    One folder per site: only the files directly under ``<site>/`` (a plain prefix test,
    so fl-client-1 never matches fl-client-10). One site's data at the root: the root files.
    """
    keys = sorted(keys)
    if site:
        prefix = site + "/"
        own = {k[len(prefix) :]: k for k in keys if k.startswith(prefix)}
        own = {name: key for name, key in own.items() if "/" not in name}
        if own:
            return own, f"folder {prefix}"
    root = {k: k for k in keys if "/" not in k}
    if root:
        return root, "root of the registered data"
    folders = sorted({k.split("/", 1)[0] for k in keys if "/" in k})
    raise RuntimeError(
        f"No data for site {site!r}: no files under '{site}/' and no files at the root of the "
        f"registered data (top-level folders: {folders[:20]}). Import the site folders "
        "written by prepare_data.py so that the keys start with 'fl-client-k/', or one site's "
        "files at the root."
    )


def load_npz(content: bytes, name: str) -> dict:
    """Read one split written by prepare_data.py and check it; return its arrays."""
    with np.load(io.BytesIO(content), allow_pickle=False) as npz:
        arrays = {key: npz[key] for key in npz.files}
    missing = [key for key in ("X", "y", "feature_names") if key not in arrays]
    if missing:
        raise ValueError(
            f"{name} lacks the arrays {missing}; run prepare_data.py again."
        )
    x, y = arrays["X"], arrays["y"]
    names = [str(n) for n in arrays["feature_names"]]
    if names != FEATURE_NAMES:
        raise ValueError(
            f"{name}: the feature columns differ from the ones this template expects "
            f"({len(names)} vs {len(FEATURE_NAMES)}); prepare the data with this template's "
            "prepare_data.py."
        )
    if x.ndim != 2 or x.shape != (len(y), len(FEATURE_NAMES)):
        raise ValueError(f"{name}: X has shape {x.shape} for {len(y)} labels.")
    if not np.isfinite(x).all():
        raise ValueError(
            f"{name}: X holds NaN or infinite values; it should be imputed."
        )
    if not set(np.unique(y).tolist()) <= {0, 1}:
        raise ValueError(f"{name}: labels must be 0 or 1, found {np.unique(y)[:10]}.")
    arrays["X"] = x.astype(np.float32)
    arrays["y"] = y.astype(np.int64)
    return arrays


# ----------------------------------------------------------------------------- metrics


def binary_metrics(y: np.ndarray, p: np.ndarray) -> dict:
    """AUROC, AUPRC (NaN with one class), accuracy at 0.5, log loss, n, n_pos."""
    y = np.asarray(y).astype(int)
    p = np.asarray(p, dtype=float)
    both = len(np.unique(y)) == 2
    eps = 1e-7
    clipped = np.clip(p, eps, 1 - eps)
    loss = (
        -np.mean(y * np.log(clipped) + (1 - y) * np.log(1 - clipped))
        if len(y)
        else np.nan
    )
    return {
        "auroc": float(roc_auc_score(y, p)) if both else float("nan"),
        "auprc": float(average_precision_score(y, p)) if both else float("nan"),
        "accuracy": float(((p >= 0.5).astype(int) == y).mean())
        if len(y)
        else float("nan"),
        "loss": float(loss),
        "n": int(len(y)),
        "n_pos": int(y.sum()),
    }


def metric_line(site: str, current_round: int, metrics: dict) -> str:
    values = " ".join(
        f"{key}={metrics[key]}"
        if key in ("n", "n_pos")
        else f"{key}={metrics[key]:.4f}"
        for key in ("auroc", "auprc", "accuracy", "loss", "n", "n_pos")
    )
    return f"SWARMMED_METRIC site={site} round={current_round} split=val {values}"


# ----------------------------------------------------------------------------- training


def train_local(model: nn.Module, x: torch.Tensor, y: torch.Tensor, epochs: int,
                generator: torch.Generator) -> tuple[float, int]:  # fmt: skip
    """Train for `epochs` passes over (x, y); return the mean batch loss and the step count.

    A new Adam optimiser per round (no optimiser state is carried across rounds), shuffled
    batches drawn with the site's own generator.
    """
    optimiser = torch.optim.Adam(
        model.parameters(), lr=LEARNING_RATE, weight_decay=WEIGHT_DECAY
    )
    loss_fn = nn.BCEWithLogitsLoss()
    model.train()
    total, steps = 0.0, 0
    for _ in range(epochs):
        order = torch.randperm(len(x), generator=generator)
        for start in range(0, len(x), BATCH_SIZE):
            batch = order[start : start + BATCH_SIZE]
            optimiser.zero_grad()
            loss = loss_fn(model(x[batch]), y[batch])
            loss.backward()
            optimiser.step()
            total += float(loss.item())
            steps += 1
    return total / max(steps, 1), steps


def finite_floats(values: dict) -> dict:
    """Python floats only, without NaN (FLARE metrics and JSON stay clean)."""
    return {
        k: float(v) for k, v in values.items() if v is not None and math.isfinite(v)
    }


def save_round_metrics(site: str, history: list[dict]) -> None:
    """Write the per-round validation metrics next to the job's app folder.

    Only inside a FLARE job (this file at app_<site>/custom/training.py): the Hub uploads the
    app folder with the results, so results_visualization.py can plot the rounds. Never
    written in other runs; a failure here never stops the training.
    """
    here = Path(__file__).resolve()
    if here.parent.name != "custom" or not here.parent.parent.name.startswith("app_"):
        return
    path = here.parent.parent / METRICS_FILE
    try:
        payload = {
            "template": TEMPLATE,
            "site": site,
            "split": "val",
            "rounds": history,
        }
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        print(f"{TEMPLATE}: could not write {path}: {exc}")


def main(project_id: str):
    # flare_adapter exists only inside a FLARE job (SwarmMed copies it next to this file), so
    # it is imported here: evaluate.py can import this module without NVFlare.
    import flare_adapter

    torch.set_num_threads(TORCH_THREADS)
    random.seed(SEED)
    np.random.seed(SEED)
    torch.manual_seed(SEED)

    flare_adapter.init_flare()
    site = site_name()

    with flare_adapter.get_data_filesystem(project_id) as fs:
        files, where = select_site_files(fs.manifest.keys(), site)
        for needed in ("train.npz", "val.npz"):
            if needed not in files:
                raise RuntimeError(
                    f"{needed} is missing in the {where} (found {sorted(files)}); "
                    "run prepare_data.py and import its output."
                )
        train = load_npz(fs.read_bytes(files["train.npz"]), "train.npz")
        val = load_npz(fs.read_bytes(files["val.npz"]), "val.npz")
    site = site or "site"
    n_train = len(train["y"])
    print(
        f"{TEMPLATE}: site={site} reads the {where}: train n={n_train} "
        f"deaths={int(train['y'].sum())}, val n={len(val['y'])} deaths={int(val['y'].sum())}; "
        f"{LOCAL_EPOCHS} local epochs per round, batch {BATCH_SIZE}, Adam lr {LEARNING_RATE}"
    )
    if len(np.unique(train["y"])) < 2:
        print(
            f"{TEMPLATE}: warning: the training split of {site} holds one class only."
        )

    x_train = torch.from_numpy(train["X"])
    y_train = torch.from_numpy(train["y"].astype(np.float32))
    model = build_model()
    generator = torch.Generator().manual_seed(SEED)
    history = []
    local_round = 0

    while True:
        input_model = flare_adapter.receive_model()
        if input_model is None:
            print(
                f"{TEMPLATE}: site={site} no more tasks; training finished or aborted."
            )
            break
        current_round = input_model.current_round
        if current_round is None:
            current_round = local_round
        local_round = current_round + 1
        if input_model.params:
            state = flare_adapter.get_pytorch_state_dict(input_model.params)
            model.load_state_dict({k: v.float() for k, v in state.items()}, strict=True)

        # Score the received global model (round 0: the shared initialisation).
        val_metrics = binary_metrics(val["y"], predict_proba(model, val["X"]))
        print(metric_line(site, current_round, val_metrics))

        started = time.perf_counter()
        train_loss, steps = train_local(
            model, x_train, y_train, LOCAL_EPOCHS, generator
        )
        seconds = time.perf_counter() - started
        print(
            f"{TEMPLATE}: site={site} round={current_round} local training: {LOCAL_EPOCHS} "
            f"epochs, {steps} steps, mean loss {train_loss:.4f}, {seconds:.2f} s"
        )

        record = {"round": current_round, **{f"val_{k}": v for k, v in val_metrics.items()},
                  "train_loss": train_loss, "local_steps": steps,
                  "local_seconds": round(seconds, 4)}  # fmt: skip
        # JSON has no NaN: an undefined metric is written as null.
        history.append(
            {k: None if isinstance(v, float) and not math.isfinite(v) else v
             for k, v in record.items()}
        )  # fmt: skip
        save_round_metrics(site, history)

        metrics = finite_floats(
            {
                "val_auroc": val_metrics["auroc"],
                "val_auprc": val_metrics["auprc"],
                "val_accuracy": val_metrics["accuracy"],
                "val_loss": val_metrics["loss"],
                "val_n": val_metrics["n"],
                "val_n_pos": val_metrics["n_pos"],
                "train_loss": train_loss,
            }
        )
        flare_adapter.send_model(
            params=model.state_dict(),
            metrics=metrics,
            meta={
                "NUM_STEPS_CURRENT_ROUND": n_train,
                "accuracy": float(val_metrics["accuracy"]),
            },
        )


if __name__ == "__main__":
    main(project_id="default_project")
