"""SwarmMed template ecg_ptbxl: normal vs abnormal 12-lead ECG (PTB-XL, 100 Hz), swarm training.

Every site trains the same small 1D CNN on its own PTB-XL records and shares only model
weights through NVIDIA FLARE's swarm workflow. Each round, the site
  1. receives the global model (empty in round 0, when it keeps its own initialisation),
  2. scores it on its validation split and prints one SWARMMED_METRIC line,
  3. trains it for LOCAL_EPOCHS passes over its training split, and
  4. sends the weights back, weighted by its number of training records.

Data (written by prepare_data.py): the registered data hold either one site's files at their
root (train.npz, val.npz, test.npz, site_info.json) or one folder per FLARE site name
(fl-client-1/, fl-client-2/, ...), in which case each client reads only the folder named after
it. Only numpy, scikit-learn and torch are needed. See README.md.
"""

import io
import os
import random
import time
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
from sklearn.metrics import roc_auc_score

TEMPLATE = "ecg_ptbxl"

# Swarm rounds; SwarmMed reads this number from the first assignment in this file.
SWARM_ROUNDS = 5
LOCAL_EPOCHS = 1  # passes over the site's training split per round
BATCH_SIZE = 128
LEARNING_RATE = 1e-3
SEED = 0
NUM_THREADS = 4  # CPU threads per site; a local-test network runs every site on one host

N_LEADS = 12
N_SAMPLES = 1000  # 10 s at 100 Hz
ARRAYS = ("X", "y", "y_super", "ecg_id")


# --------------------------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------------------------


class LocalDataFolder:
    """A local folder with the interface training.py uses from flare_adapter's data filesystem.

    Used by evaluate.py and for local tests; inside a FLARE job the data come from
    flare_adapter.get_data_filesystem().
    """

    def __init__(self, root):
        self.root = Path(root)
        self.manifest = {
            path.relative_to(self.root).as_posix(): path
            for path in sorted(self.root.rglob("*"))
            if path.is_file()
        }

    def read_bytes(self, key):
        return self.manifest[key].read_bytes()


def find_site_name():
    """Return this client's FLARE site name, or None outside a FLARE job.

    Call it after flare_adapter.init_flare(). Order: nvflare.client.get_site_name(), the
    SWARMMED_SITE_NAME environment variable (manual runs), the job's app folder name
    (.../app_<site>/custom/training.py).
    """
    try:
        import nvflare.client as flare

        name = flare.get_site_name()
        if name:
            return str(name)
    except Exception:  # noqa: BLE001 - no FLARE client API outside a job
        pass
    name = os.environ.get("SWARMMED_SITE_NAME", "").strip()
    if name:
        return name
    app_folder = Path(__file__).resolve().parent.parent.name
    if app_folder.startswith("app_") and len(app_folder) > 4:
        return app_folder[4:]
    return None


def site_prefix(keys, site):
    """Return the prefix of this site's files in the registered data: '<site>/' or '' (root)."""
    keys = set(keys)
    if site and f"{site}/train.npz" in keys:
        return f"{site}/"
    if "train.npz" in keys:
        return ""
    folders = sorted({key.split("/", 1)[0] for key in keys if "/" in key})
    raise FileNotFoundError(
        f"No data for site {site!r}: expected '{site}/train.npz' (one folder per site) or "
        f"'train.npz' at the root of the registered data (one site). Top-level folders: "
        f"{folders[:20]}. Prepare the data with prepare_data.py."
    )


def read_npz(fs, key, names):
    """Read the named arrays from an .npz file of the registered data."""
    # Reading the whole file first avoids seeking over HTTP.
    with np.load(io.BytesIO(fs.read_bytes(key)), allow_pickle=False) as npz:
        missing = [name for name in names if name not in npz.files]
        if missing:
            raise KeyError(f"{key}: arrays {missing} missing; prepare the data with prepare_data.py")
        return {name: npz[name] for name in names}


def check_arrays(arrays, key):
    """Raise ValueError when an .npz file does not have the layout prepare_data.py writes."""
    X, y = arrays["X"], arrays["y"]
    if X.dtype != np.int16 or X.ndim != 3 or X.shape[1:] != (N_LEADS, N_SAMPLES):
        raise ValueError(f"{key}: X must be int16 (N, {N_LEADS}, {N_SAMPLES}), got {X.dtype} {X.shape}")
    if y.shape != (len(X),) or not np.isin(y, (0, 1)).all():
        raise ValueError(f"{key}: y must hold one 0/1 label per record")
    if len(X) < 2:
        raise ValueError(f"{key}: fewer than 2 records")


def load_site_data(fs, site, splits=("train", "val"), names=ARRAYS):
    """Load this site's splits; return (prefix, {split: arrays})."""
    prefix = site_prefix(fs.manifest.keys(), site)
    data = {}
    for split in splits:
        key = f"{prefix}{split}.npz"
        if key not in fs.manifest:
            raise FileNotFoundError(f"{key} not found in the registered data")
        data[split] = read_npz(fs, key, names)
        check_arrays(data[split], key)
    return prefix, data


def normalise_ecg(X, chunk=1024):
    """int16 microvolts (N, 12, 1000) -> float32 millivolts minus each lead's median.

    The median is taken over each record and lead separately (torch.median returns the lower of
    the two middle values for 1000 samples). The amplitude is not rescaled and nothing is
    computed across records or sites. evaluate.py imports this function; the sandbox scripts
    (validation, visualization, results visualization, explainability) carry identical copies.
    """
    X = np.asarray(X)
    out = torch.empty(X.shape, dtype=torch.float32)
    for start in range(0, len(X), chunk):
        x = torch.from_numpy(X[start : start + chunk].astype(np.float32) / 1000.0)
        out[start : start + chunk] = x - x.median(dim=-1, keepdim=True).values
    return out


def to_tensors(arrays):
    """Model input and float labels (N, 1) of one split."""
    x = normalise_ecg(arrays["X"])
    y = torch.from_numpy(arrays["y"].astype(np.float32)).unsqueeze(1)
    return x, y


# --------------------------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------------------------


def conv_block(c_in, c_out, kernel):
    return nn.Sequential(
        nn.Conv1d(c_in, c_out, kernel, padding=kernel // 2, bias=False),
        nn.BatchNorm1d(c_out),
        nn.ReLU(),
        nn.MaxPool1d(2),
    )


class ECGNet(nn.Module):
    """1D CNN for (N, 12, 1000) ECGs; returns one logit per record (79,169 parameters).

    Four blocks of Conv1d (kernels 7, 7, 5, 5; 32, 64, 64, 128 channels; no bias), BatchNorm,
    ReLU and max pooling by 2, then global average pooling over time and a linear head.
    """

    def __init__(self, n_leads=N_LEADS, n_outputs=1):
        super().__init__()
        self.features = nn.Sequential(
            conv_block(n_leads, 32, 7),
            conv_block(32, 64, 7),
            conv_block(64, 64, 5),
            conv_block(64, 128, 5),
        )
        self.pool = nn.AdaptiveAvgPool1d(1)
        self.head = nn.Linear(128, n_outputs)

    def forward(self, x):
        return self.head(self.pool(self.features(x)).flatten(1))


def build_model():
    return ECGNet()


# --------------------------------------------------------------------------------------------
# Training and evaluation
# --------------------------------------------------------------------------------------------


def set_seeds(seed):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)


def train_one_epoch(model, optimiser, x, y, generator):
    """One pass over (x, y) in shuffled batches; return (mean loss, number of steps)."""
    loss_fn = nn.BCEWithLogitsLoss()
    model.train()
    order = torch.randperm(len(y), generator=generator)
    total, seen, steps = 0.0, 0, 0
    for start in range(0, len(y), BATCH_SIZE):
        batch = order[start : start + BATCH_SIZE]
        if len(batch) < 2:  # BatchNorm needs more than one record per batch
            continue
        loss = loss_fn(model(x[batch]), y[batch])
        optimiser.zero_grad()
        loss.backward()
        optimiser.step()
        total += loss.item() * len(batch)
        seen += len(batch)
        steps += 1
    return total / max(seen, 1), steps


@torch.no_grad()
def predict_logits(model, x, batch_size=512):
    model.eval()
    return torch.cat([model(x[i : i + batch_size]) for i in range(0, len(x), batch_size)])


def evaluate(model, x, y):
    """AUROC, accuracy at probability 0.5 and mean BCE loss of the model on (x, y)."""
    logits = predict_logits(model, x)
    loss = float(nn.functional.binary_cross_entropy_with_logits(logits, y))
    prob = torch.sigmoid(logits).squeeze(1).numpy()
    truth = y.squeeze(1).numpy()
    metrics = {"val_loss": loss, "val_acc": float(((prob >= 0.5) == (truth == 1)).mean())}
    if len(np.unique(truth)) == 2:
        metrics["val_auroc"] = float(roc_auc_score(truth, prob))
    return metrics


def metric_line(site, current_round, metrics, n):
    values = " ".join(f"{key}={metrics[key]:.4f}" for key in ("val_auroc", "val_acc", "val_loss") if key in metrics)
    return f"SWARMMED_METRIC site={site} round={current_round} split=val {values} n={n}"


# --------------------------------------------------------------------------------------------
# Swarm training (NVIDIA FLARE client API through SwarmMed's flare_adapter)
# --------------------------------------------------------------------------------------------


def main(project_id: str):
    torch.set_num_threads(NUM_THREADS)
    set_seeds(SEED)
    # SwarmMed copies flare_adapter.py next to this script when it builds the job. Importing it
    # here keeps the functions above usable without NVIDIA FLARE (evaluate.py, local tests).
    import flare_adapter

    flare_adapter.init_flare()
    site = find_site_name()
    label = site or "unknown-site"
    with flare_adapter.get_data_filesystem(project_id) as fs:
        prefix, data = load_site_data(fs, site, ("train", "val"))
    x_train, y_train = to_tensors(data["train"])
    x_val, y_val = to_tensors(data["val"])
    n_train, n_val = len(y_train), len(y_val)
    print(
        f"{TEMPLATE}: site={label} data={prefix or '<root>'} n_train={n_train} "
        f"(abnormal {int(y_train.sum())}) n_val={n_val} (abnormal {int(y_val.sum())}) "
        f"local_epochs={LOCAL_EPOCHS} threads={torch.get_num_threads()}",
        flush=True,
    )
    del data

    model = build_model()
    generator = torch.Generator().manual_seed(SEED)
    while True:
        input_model = flare_adapter.receive_model()
        if input_model is None:
            print(f"{TEMPLATE}: site={label} no further task; training finished or aborted.", flush=True)
            break
        current_round = input_model.current_round
        if input_model.params:
            model.load_state_dict(flare_adapter.get_pytorch_state_dict(input_model.params))
        model_source = "global" if input_model.params else "initial"

        # 1. Score the received model on this site's validation split.
        val_metrics = evaluate(model, x_val, y_val)
        print(metric_line(label, current_round, val_metrics, n_val) + f" model={model_source}", flush=True)

        # 2. Local training; a new optimiser every round, so no state from earlier rounds.
        optimiser = torch.optim.Adam(model.parameters(), lr=LEARNING_RATE)
        started = time.time()
        for epoch in range(1, LOCAL_EPOCHS + 1):
            train_loss, _ = train_one_epoch(model, optimiser, x_train, y_train, generator)
            print(
                f"{TEMPLATE}: site={label} round={current_round} local_epoch={epoch}/{LOCAL_EPOCHS} "
                f"train_loss={train_loss:.4f} seconds={time.time() - started:.1f}",
                flush=True,
            )

        # 3. Send the weights; SwarmMed weights each site by NUM_STEPS_CURRENT_ROUND, and the
        #    swarm's best-model selection reads "accuracy" from the metadata.
        flare_adapter.send_model(
            params=model.state_dict(),
            metrics={**val_metrics, "train_loss": float(train_loss)},
            meta={"NUM_STEPS_CURRENT_ROUND": n_train, "accuracy": val_metrics["val_acc"]},
        )


if __name__ == "__main__":
    main(project_id="default_project")
