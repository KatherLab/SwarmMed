"""Swarm learning template: 5-class sleep staging from two EEG channels.

Each site holds 30-s epochs of the EEG channels Fpz-Cz and Pz-Oz from Sleep-EDF
Expanded (sleep cassette), prepared with prepare_data.py:

    train.npz, val.npz, test.npz   X float16 (N, 2, 3000), y int8 (N,) in 0..4
    site_info.json                 source, subjects, counts, preprocessing

The registered data holds either one site's files at its root (a real
deployment) or one folder per FLARE site (fl-client-1/, fl-client-2/, ...), in
which case every client reads only the folder named after it.

Every round, the client evaluates the received global model on its validation
subject, prints one SWARMMED_METRIC line, trains locally for LOCAL_EPOCHS and
sends the weights back. The model, data and metric functions below do not need
NVIDIA FLARE, so evaluate.py and tests can import them.
"""

import io
import os
import time
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
from sklearn.metrics import cohen_kappa_score, f1_score

SWARM_ROUNDS = 10
LOCAL_EPOCHS = 2  # passes over the site's training epochs per swarm round
BATCH_SIZE = 128
LEARNING_RATE = 1e-3
SEED = 0  # same seed at every site, so all sites start from the same weights
NUM_THREADS = 4  # CPU threads per client; local-test clients share one host

CLASS_NAMES = ["W", "N1", "N2", "N3", "REM"]
N_CLASSES = len(CLASS_NAMES)
N_CHANNELS = 2  # EEG Fpz-Cz, EEG Pz-Oz
EPOCH_SAMPLES = 3000  # 30 s at 100 Hz
REQUIRED_FILES = ("train.npz", "val.npz")
LOG_PREFIX = "[eeg_sleep_staging]"


# --- Model ------------------------------------------------------------------------


class SleepStageCNN(nn.Module):
    """Small 1-D CNN for one 30-s, 2-channel EEG epoch (52,997 parameters).

    Input: float32 tensor (batch, 2, 3000), robust-scaled as in prepare_data.py.
    Output: logits (batch, 5) for W, N1, N2, N3, REM.
    """

    def __init__(self, n_channels=N_CHANNELS, n_classes=N_CLASSES):
        """Build the layers."""
        super().__init__()
        self.features = nn.Sequential(
            nn.Conv1d(n_channels, 32, kernel_size=50, stride=6, bias=False),
            nn.BatchNorm1d(32),
            nn.ReLU(),
            nn.MaxPool1d(8),
            nn.Conv1d(32, 64, kernel_size=8, padding=4, bias=False),
            nn.BatchNorm1d(64),
            nn.ReLU(),
            nn.Conv1d(64, 64, kernel_size=8, padding=4, bias=False),
            nn.BatchNorm1d(64),
            nn.ReLU(),
            nn.MaxPool1d(4),
            nn.AdaptiveAvgPool1d(1),
            nn.Flatten(),
        )
        self.head = nn.Linear(64, n_classes)

    def forward(self, x):
        """Return class logits for a batch of epochs."""
        return self.head(self.features(x))


def build_model(seed=SEED):
    """Create the model with a fixed initialisation (identical at every site)."""
    torch.manual_seed(seed)
    return SleepStageCNN()


# --- Data -------------------------------------------------------------------------


def load_split(source):
    """Load one split file (path, bytes or binary file object) as arrays.

    Args:
        source: Path to a .npz file, its bytes, or an open binary file.

    Returns:
        dict[str, np.ndarray]: X (float16), y (int8), subject, night, epoch.

    Raises:
        ValueError: If the arrays do not have the expected shapes or labels.
    """
    if isinstance(source, bytes | bytearray):
        source = io.BytesIO(source)
    with np.load(source, allow_pickle=False) as data:
        arrays = {key: data[key] for key in data.files}
    x, y = arrays.get("X"), arrays.get("y")
    if x is None or y is None:
        raise ValueError(f"split file has arrays {sorted(arrays)}, needs X and y")
    if x.ndim != 3 or x.shape[1:] != (N_CHANNELS, EPOCH_SAMPLES) or len(x) != len(y):
        raise ValueError(
            f"X has shape {x.shape} and y {y.shape}; expected (N, 2, 3000)"
        )
    if len(y) and (y.min() < 0 or y.max() >= len(CLASS_NAMES)):
        raise ValueError(f"labels outside 0..{len(CLASS_NAMES) - 1}")
    return arrays


def to_tensors(arrays):
    """Return (X, y) tensors; X stays float16 and is converted per batch."""
    x = torch.from_numpy(np.ascontiguousarray(arrays["X"]))
    y = torch.from_numpy(arrays["y"].astype(np.int64))
    return x, y


def resolve_site_name():
    """Return this client's FLARE site name; call after flare_adapter.init_flare().

    Order: nvflare.client.get_site_name(), the environment variable
    SWARMMED_SITE_NAME (manual runs), then the job folder name app_<site>.
    """
    try:
        import nvflare.client as flare

        name = flare.get_site_name()
        if name:
            return str(name)
    except Exception as exc:  # FLARE not initialised or not installed
        print(f"{LOG_PREFIX} get_site_name() unavailable: {exc}")
    name = os.environ.get("SWARMMED_SITE_NAME", "").strip()
    if name:
        return name
    app_dir = Path(__file__).resolve().parent.parent.name  # .../app_<site>/custom/
    return app_dir[len("app_") :] if app_dir.startswith("app_") else None


def select_site_files(keys, site, required=REQUIRED_FILES):
    """Find this site's files among the registered data's relative paths.

    Prefers the folder named after the site (the local-test layout with one
    folder per site) and falls back to files at the root of the registered data
    (one site's data, as each site registers in a real deployment).

    Args:
        keys: Relative paths of the registered data (the manifest keys).
        site (str | None): This client's site name.
        required (tuple[str, ...]): File names that must be present.

    Returns:
        tuple[dict[str, str], str]: File name to key, and where they were found.

    Raises:
        FileNotFoundError: If neither the site folder nor the root has the files.
    """
    keys = list(keys)
    if site:
        prefix = f"{site}/"
        in_folder = {
            key[len(prefix) :]: key
            for key in keys
            if key.startswith(prefix) and "/" not in key[len(prefix) :]
        }
        if all(name in in_folder for name in required):
            return in_folder, f"folder {prefix}"
    at_root = {key: key for key in keys if "/" not in key}
    if all(name in at_root for name in required):
        return at_root, "root of the registered data"
    folders = sorted({key.split("/", 1)[0] for key in keys if "/" in key})
    raise FileNotFoundError(
        f"Site {site!r}: {', '.join(required)} not found in a folder named "
        f"'{site}/' (one folder per site) or at the root of the registered data "
        f"(one site's data). Top-level folders present: {folders[:20]}. "
        "Run prepare_data.py and import its output as described in README.md."
    )


# --- Training and evaluation -----------------------------------------------------


def class_weights(y, n_classes=N_CLASSES):
    """Cross-entropy weights n / (n_classes * n_c) from local training labels."""
    counts = np.bincount(np.asarray(y), minlength=n_classes).astype(np.float64)
    weights = counts.sum() / (n_classes * np.maximum(counts, 1.0))
    return torch.tensor(weights, dtype=torch.float32)


def train_one_epoch(model, x, y, optimizer, loss_fn, generator):
    """One pass over the training epochs in shuffled mini-batches.

    Returns:
        float: Mean training loss over the pass.
    """
    model.train()
    order = torch.randperm(len(y), generator=generator)
    total, seen = 0.0, 0
    for start in range(0, len(y), BATCH_SIZE):
        batch = order[start : start + BATCH_SIZE]
        if len(batch) < 2:  # BatchNorm needs more than one sample
            continue
        loss = loss_fn(model(x[batch].float()), y[batch])
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
        total += loss.item() * len(batch)
        seen += len(batch)
    return total / max(seen, 1)


@torch.no_grad()
def predict_logits(model, x, batch_size=512):
    """Return float32 logits (N, 5) for float16 or float32 epochs."""
    model.eval()
    outputs = [
        model(x[start : start + batch_size].float())
        for start in range(0, len(x), batch_size)
    ]
    if not outputs:
        return torch.empty(0, len(CLASS_NAMES))
    return torch.cat(outputs)


def sleep_metrics(y_true, y_pred):
    """Accuracy, macro-F1, Cohen's kappa and per-class F1 as Python floats."""
    y_true, y_pred = np.asarray(y_true), np.asarray(y_pred)
    per_class = f1_score(
        y_true,
        y_pred,
        labels=list(range(len(CLASS_NAMES))),
        average=None,
        zero_division=0,
    )
    return {
        "n": len(y_true),
        "accuracy": float(np.mean(y_true == y_pred)) if len(y_true) else float("nan"),
        "macro_f1": float(f1_score(y_true, y_pred, average="macro", zero_division=0)),
        "kappa": float(cohen_kappa_score(y_true, y_pred)),
        "f1_per_class": {
            name: float(v) for name, v in zip(CLASS_NAMES, per_class, strict=True)
        },
    }


def evaluate(model, x, y):
    """Metrics of the model on (x, y), including the unweighted cross-entropy."""
    logits = predict_logits(model, x)
    metrics = sleep_metrics(y.numpy(), logits.argmax(dim=1).numpy())
    metrics["loss"] = float(nn.functional.cross_entropy(logits, y).item())
    return metrics


def train_local(model, x, y, epochs, loss_fn, generator):
    """Train for a number of epochs with a fresh Adam optimiser.

    Returns:
        list[float]: Mean training loss of each epoch.
    """
    optimizer = torch.optim.Adam(model.parameters(), lr=LEARNING_RATE)
    return [
        train_one_epoch(model, x, y, optimizer, loss_fn, generator)
        for _ in range(epochs)
    ]


# --- Swarm learning loop ----------------------------------------------------------


def main(project_id: str):
    """Run the client side of swarm training through SwarmMed's flare_adapter."""
    import flare_adapter  # copied into the job's custom/ folder by SwarmMed

    torch.set_num_threads(NUM_THREADS)
    np.random.seed(SEED)
    flare_adapter.init_flare()
    site = resolve_site_name()

    with flare_adapter.get_data_filesystem(project_id) as fs:
        files, location = select_site_files(sorted(fs.manifest), site)
        print(f"{LOG_PREFIX} site={site}: reading data from the {location}")
        train = load_split(fs.read_bytes(files["train.npz"]))
        val = load_split(fs.read_bytes(files["val.npz"]))
    x_train, y_train = to_tensors(train)
    x_val, y_val = to_tensors(val)
    n_train = len(y_train)
    counts = np.bincount(train["y"], minlength=len(CLASS_NAMES)).tolist()
    print(
        f"{LOG_PREFIX} site={site}: train {n_train} epochs from "
        f"{len(np.unique(train['subject']))} subjects, class counts "
        f"{dict(zip(CLASS_NAMES, counts, strict=True))}; val {len(y_val)} epochs"
    )

    model = build_model()
    loss_fn = nn.CrossEntropyLoss(weight=class_weights(train["y"]))
    generator = torch.Generator().manual_seed(SEED)

    while flare_adapter.is_running():
        input_model = flare_adapter.receive_model()
        if input_model is None:
            break
        current_round = input_model.current_round
        if input_model.params:  # round 0 delivers empty params: keep the local start
            model.load_state_dict(
                flare_adapter.get_pytorch_state_dict(input_model.params)
            )

        # 1. The received global model on this site's validation subject.
        val_metrics = evaluate(model, x_val, y_val)
        print(
            f"SWARMMED_METRIC site={site} round={current_round} split=val "
            f"val_acc={val_metrics['accuracy']:.4f} "
            f"val_macro_f1={val_metrics['macro_f1']:.4f} "
            f"val_kappa={val_metrics['kappa']:.4f} "
            f"val_loss={val_metrics['loss']:.4f} n_val={val_metrics['n']}"
        )

        # 2. Local training.
        started = time.time()
        losses = train_local(model, x_train, y_train, LOCAL_EPOCHS, loss_fn, generator)
        print(
            f"{LOG_PREFIX} site={site} round={current_round}: trained {LOCAL_EPOCHS} "
            f"epoch(s) on {n_train} samples in {time.time() - started:.1f} s, "
            f"loss {' '.join(f'{v:.4f}' for v in losses)}"
        )

        # 3. Send the weights. The aggregator weights by NUM_STEPS_CURRENT_ROUND;
        #    the best-model selector reads "accuracy" from meta.
        flare_adapter.send_model(
            params=model.state_dict(),
            metrics={
                "val_acc": val_metrics["accuracy"],
                "val_macro_f1": val_metrics["macro_f1"],
                "val_kappa": val_metrics["kappa"],
                "val_loss": val_metrics["loss"],
                "train_loss": float(losses[-1]),
            },
            meta={
                "NUM_STEPS_CURRENT_ROUND": n_train,
                "accuracy": val_metrics["accuracy"],
            },
        )
    print(f"{LOG_PREFIX} site={site}: training finished")


if __name__ == "__main__":
    main(project_id="default_project")
