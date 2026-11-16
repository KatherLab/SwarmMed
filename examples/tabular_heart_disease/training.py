"""SwarmMed template: coronary artery disease from tabular data (UCI Heart Disease).

Swarm learning across hospitals with a small multilayer perceptron, run by NVIDIA FLARE through
SwarmMed's client adapter (``flare_adapter``). Each site reads only its own prepared files
(``prepare_data.py`` writes them; see README.md):

* local-test network on one host: the project data holds one folder per FLARE site
  (``fl-client-1/``, ``fl-client-2/``, ...) and each client reads the folder named after itself;
* real deployment: each site registers only its own files, at the root of its data.

In every round each site

1. receives the global model (round 0 carries no weights: every site starts from the same
   seeded initialisation),
2. scores it on its validation split and prints one ``SWARMMED_METRIC`` line,
3. trains it for ``LOCAL_EPOCHS`` epochs on its training split,
4. sends the weights, the validation metrics and its number of training cases, which
   SwarmMed uses as the aggregation weight.

The test split is not used here; ``evaluate.py`` scores the final global model on it.
The module has no side effects on import apart from limiting torch's threads, so
``evaluate.py`` and local tests can reuse its functions without NVIDIA FLARE.
"""

import io
import math
import os
import time
import zlib
from pathlib import Path

import numpy as np
import torch
from sklearn.metrics import average_precision_score, roc_auc_score
from torch import nn

# Number of swarm rounds. SwarmMed reads this value from this file when it builds the job.
# 10 rounds x 10 local epochs gave the same test AUROC as 20 x 5 in a plain-PyTorch simulation
# of the four sites (10 repeated splits) with half the rounds; on a local-test network the
# rounds, not the computation (milliseconds per epoch), take the time.
SWARM_ROUNDS = 10
LOCAL_EPOCHS = 10  # local epochs per round at each site
BATCH_SIZE = 32
LEARNING_RATE = 1e-3
WEIGHT_DECAY = 1e-4
HIDDEN_UNITS = 32
DROPOUT = 0.2
SEED = 42
THRESHOLD = 0.5  # probability cut-off for accuracy, sensitivity and specificity
NUM_THREADS = 4  # every client shares the host's CPUs in a local-test network

# Model inputs written by prepare_data.py, in this order at every site.
FEATURE_NAMES = (
    "age",
    "trestbps",
    "chol",
    "thalach",
    "oldpeak",
    "sex",
    "fbs",
    "exang",
    "cp_1",
    "cp_2",
    "cp_3",
    "cp_4",
    "restecg_0",
    "restecg_1",
    "restecg_2",
)
TRAINING_SPLITS = ("train", "val")  # the test split is read only by evaluate.py

torch.set_num_threads(NUM_THREADS)


# ----------------------------------------------------------------------------- data


def read_npz(data: bytes) -> dict:
    """Load one prepared split from the bytes of an .npz file (object arrays are refused)."""
    with np.load(io.BytesIO(data), allow_pickle=False) as npz:
        return {key: npz[key] for key in npz.files}


def check_split(arrays: dict, where: str) -> None:
    """Raise a clear error if a split does not match the inputs this model expects."""
    missing = {"X", "y", "feature_names"} - set(arrays)
    if missing:
        raise ValueError(
            f"{where}: arrays {sorted(missing)} are missing; run prepare_data.py"
        )
    names = tuple(str(name) for name in arrays["feature_names"])
    if names != FEATURE_NAMES:
        raise ValueError(
            f"{where}: features {names} differ from the model's {FEATURE_NAMES}. "
            "Prepare every site's data with the same prepare_data.py."
        )
    x, y = arrays["X"], arrays["y"]
    if x.ndim != 2 or x.shape[1] != len(FEATURE_NAMES) or len(y) != len(x):
        raise ValueError(f"{where}: X has shape {x.shape} and y has {len(y)} labels")
    if len(y) == 0:
        raise ValueError(f"{where}: the split is empty")
    if not np.isfinite(x).all():
        raise ValueError(f"{where}: X holds missing or infinite values")
    if not np.isin(y, (0, 1)).all():
        raise ValueError(f"{where}: labels other than 0 and 1")


def site_name() -> str | None:
    """Return this client's FLARE site name (fl-client-1, ...); call after init_flare().

    NVIDIA FLARE knows the name once the client API is initialised. The fallbacks are an
    environment variable set by hand (SwarmMed does not set one) and the job folder, which
    NVIDIA FLARE names app_<site> (the script runs as .../app_<site>/custom/training.py).
    """
    try:
        import nvflare.client as flare

        name = flare.get_site_name()
        if name:
            return str(name)
    except Exception:  # client API missing or not initialised: use the fallbacks
        pass
    for variable in ("SWARMMED_SITE_NAME", "SITE_NAME"):
        value = os.environ.get(variable, "").strip()
        if value:
            return value
    app_folder = Path(__file__).resolve().parent.parent.name
    return app_folder[len("app_") :] if app_folder.startswith("app_") else None


def select_site_files(keys, site: str | None) -> tuple[dict, str]:
    """Return {split: data key} for this site's files and where they were found.

    Prefers the folder named after the site (``fl-client-2/train.npz``), then files at the
    data root (``train.npz``). Keys are matched with ``startswith(site + "/")`` so that
    fl-client-1 never picks up fl-client-10/.
    """
    keys = set(keys)
    if site:
        prefix = f"{site}/"
        if any(key.startswith(prefix) for key in keys):
            files = {split: f"{prefix}{split}.npz" for split in TRAINING_SPLITS}
            missing = sorted(key for key in files.values() if key not in keys)
            if missing:
                raise FileNotFoundError(
                    f"The data folder {prefix} lacks {missing}; prepare it with prepare_data.py."
                )
            return files, prefix
    files = {split: f"{split}.npz" for split in TRAINING_SPLITS}
    if all(key in keys for key in files.values()):
        return files, "data root"
    folders = sorted({key.split("/", 1)[0] + "/" for key in keys if "/" in key})
    raise FileNotFoundError(
        f"No training data for site {site!r}: expected the folder {site}/ (one folder per "
        "FLARE site, as in a local-test network) or train.npz and val.npz at the data root "
        f"(one site's data per Hub). Folders in the project data: {folders[:20] or 'none'}."
    )


def load_site_data(fs, site: str | None) -> tuple[dict, str]:
    """Read this site's train and val splits through the SwarmMed data filesystem."""
    files, source = select_site_files(fs.manifest.keys(), site)
    data = {}
    for split, key in files.items():
        arrays = read_npz(fs.read_bytes(key))
        check_split(arrays, key)
        data[split] = arrays
    return data, source


def load_local_split(path) -> dict:
    """Read one prepared split from a local file (used by evaluate.py and local tests)."""
    arrays = read_npz(Path(path).read_bytes())
    check_split(arrays, str(path))
    return arrays


# ----------------------------------------------------------------------------- model


class HeartDiseaseMLP(nn.Module):
    """15 inputs -> 32 hidden units (ReLU, dropout) -> 1 logit (log-odds of disease).

    No batch normalisation: its running statistics would be averaged across sites with
    different distributions, and small sites give small batches.
    """

    def __init__(self, n_features: int = len(FEATURE_NAMES)):
        super().__init__()
        self.hidden = nn.Linear(n_features, HIDDEN_UNITS)
        self.dropout = nn.Dropout(DROPOUT)
        self.output = nn.Linear(HIDDEN_UNITS, 1)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.output(self.dropout(torch.relu(self.hidden(x)))).squeeze(-1)


def build_model() -> HeartDiseaseMLP:
    """Build the model with the same seeded initial weights at every site."""
    torch.manual_seed(SEED)
    return HeartDiseaseMLP()


def binary_metrics(y, prob, *, threshold: float = THRESHOLD, loss=None) -> dict:
    """AUROC, AUPRC, accuracy, sensitivity, specificity (NaN where undefined), loss, n, n_pos."""
    y = np.asarray(y, dtype=np.int64)
    prob = np.asarray(prob, dtype=np.float64)
    predicted = (prob >= threshold).astype(np.int64)
    n_pos = int(y.sum())
    n_neg = len(y) - n_pos
    both_classes = n_pos > 0 and n_neg > 0
    metrics = {
        "auroc": float(roc_auc_score(y, prob)) if both_classes else math.nan,
        "auprc": float(average_precision_score(y, prob)) if both_classes else math.nan,
        "accuracy": float((predicted == y).mean()),
        "sensitivity": float(predicted[y == 1].mean()) if n_pos else math.nan,
        "specificity": float(1 - predicted[y == 0].mean()) if n_neg else math.nan,
    }
    if loss is not None:
        metrics["loss"] = float(loss)
    metrics.update(n=len(y), n_pos=n_pos)
    return metrics


def predict_proba(model: nn.Module, x) -> np.ndarray:
    """Probability of disease for every row of x (model in evaluation mode)."""
    model.eval()
    with torch.no_grad():
        logits = model(torch.as_tensor(x, dtype=torch.float32))
    return torch.sigmoid(logits).numpy().astype(np.float64)


def evaluate(model: nn.Module, x, y) -> dict:
    """Metrics of the model on (x, y), including the mean binary cross-entropy ``loss``."""
    model.eval()
    with torch.no_grad():
        logits = model(torch.as_tensor(x, dtype=torch.float32))
        target = torch.as_tensor(np.asarray(y), dtype=torch.float32)
        loss = nn.functional.binary_cross_entropy_with_logits(logits, target).item()
    return binary_metrics(y, torch.sigmoid(logits).numpy(), loss=loss)


def train_local(
    model: nn.Module, x, y, *, epochs: int = LOCAL_EPOCHS, seed: int = SEED
) -> dict:
    """Train the model in place with Adam and binary cross-entropy.

    A new optimiser is created every round, so no optimiser state is carried between rounds.
    Returns the number of optimiser steps and the mean loss and duration of every epoch.
    """
    x = torch.as_tensor(x, dtype=torch.float32)
    y = torch.as_tensor(np.asarray(y), dtype=torch.float32)
    generator = torch.Generator().manual_seed(seed)  # batch order
    torch.manual_seed(seed)  # dropout masks
    optimizer = torch.optim.Adam(
        model.parameters(), lr=LEARNING_RATE, weight_decay=WEIGHT_DECAY
    )
    loss_fn = nn.BCEWithLogitsLoss()
    model.train()
    steps, epoch_loss, epoch_seconds = 0, [], []
    for _ in range(epochs):
        start = time.perf_counter()
        order = torch.randperm(len(x), generator=generator)
        total = 0.0
        for first in range(0, len(x), BATCH_SIZE):
            batch = order[first : first + BATCH_SIZE]
            optimizer.zero_grad()
            loss = loss_fn(model(x[batch]), y[batch])
            loss.backward()
            optimizer.step()
            steps += 1
            total += loss.item() * len(batch)
        epoch_loss.append(total / len(x))
        epoch_seconds.append(time.perf_counter() - start)
    return {"steps": steps, "epoch_loss": epoch_loss, "epoch_seconds": epoch_seconds}


def round_seed(site: str, current_round: int) -> int:
    """Seed for one site's local training in one round (reproducible, different per site)."""
    return (
        SEED * 1_000_003 + 7_919 * int(current_round) + zlib.crc32(site.encode())
    ) % (2**31)


def metric_line(site: str, current_round: int, split: str, metrics: dict) -> str:
    """One SWARMMED_METRIC line, for harvesting results from the client logs."""
    values = " ".join(
        f"{key}={int(value)}" if key in ("n", "n_pos") else f"{key}={value:.4f}"
        for key, value in metrics.items()
    )
    return f"SWARMMED_METRIC site={site} round={current_round} split={split} {values}"


# ----------------------------------------------------------------------------- swarm loop


def main(project_id: str):
    """Train this site's share of the swarm model; SwarmMed inserts the project ID below."""
    import flare_adapter  # SwarmMed copies the adapter next to this file in every job

    flare_adapter.init_flare()
    site = site_name()
    with flare_adapter.get_data_filesystem(project_id) as fs:
        data, source = load_site_data(fs, site)
    label = site or "site"
    train, val = data["train"], data["val"]
    n_train = len(train["y"])
    print(
        f"SWARMMED_DATA site={label} source={source} train_n={n_train} "
        f"train_pos={int(train['y'].sum())} val_n={len(val['y'])} "
        f"val_pos={int(val['y'].sum())} features={len(FEATURE_NAMES)}",
        flush=True,
    )

    model = build_model()
    rounds_trained = 0
    while True:
        global_model = flare_adapter.receive_model()
        if global_model is None:  # the job has ended
            break
        current_round = global_model.current_round
        if current_round is None:
            current_round = rounds_trained
        if global_model.params:  # round 0 has no weights yet
            model.load_state_dict(
                flare_adapter.get_pytorch_state_dict(global_model.params)
            )

        # 1. Score the received global model on this site's validation split.
        val_metrics = evaluate(model, val["X"], val["y"])
        print(metric_line(label, current_round, "val", val_metrics), flush=True)

        # 2. Train it locally.
        fit = train_local(
            model,
            train["X"],
            train["y"],
            epochs=LOCAL_EPOCHS,
            seed=round_seed(label, current_round),
        )
        print(
            f"SWARMMED_TRAIN site={label} round={current_round} epochs={LOCAL_EPOCHS} "
            f"steps={fit['steps']} loss_first_epoch={fit['epoch_loss'][0]:.4f} "
            f"loss_last_epoch={fit['epoch_loss'][-1]:.4f} seconds={sum(fit['epoch_seconds']):.3f}",
            flush=True,
        )

        # 3. Send the weights; n_train is the aggregation weight, and the selector of the best
        #    global model reads "accuracy" (of the received model) from meta.
        flare_adapter.send_model(
            params=model.state_dict(),
            metrics={
                key: float(value)
                for key, value in val_metrics.items()
                if math.isfinite(value)
            },
            meta={
                "NUM_STEPS_CURRENT_ROUND": n_train,
                "accuracy": float(val_metrics["accuracy"]),
            },
        )
        rounds_trained += 1
    print(
        f"Training finished or aborted. site={label} rounds_trained={rounds_trained}",
        flush=True,
    )


if __name__ == "__main__":
    main(project_id="default_project")
