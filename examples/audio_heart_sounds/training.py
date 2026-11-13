"""Swarm learning template: abnormal vs normal heart sound recordings.

Data: PhysioNet/CinC Challenge 2016 phonocardiograms, prepared by prepare_data.py
into 3-second log-mel segments (32 mel bands x 150 frames). Each source database of
the dataset is one site. A site's registered data holds train.npz, val.npz, test.npz
and site_info.json, either in a folder named after the site's FLARE name
(fl-client-1/, fl-client-2/, ... on a local-test network) or at the root of the
registered data (one real site). This script reads train.npz, val.npz and
site_info.json; evaluate.py scores test.npz.

Every swarm round this script
  1. receives the global model (round 0 has none: every site starts from the same
     seeded initialisation),
  2. scores it on the site's validation split and prints one SWARMMED_METRIC line
     (a recording's score is the mean of its segments' probabilities),
  3. trains LOCAL_EPOCHS epochs on the site's training segments,
  4. sends the weights back; aggregation weights them by the number of training
     segments.

The number of rounds is set below; the Hub reads it from this file unless the
environment variable SWARMMEDHUB_SWARM_ROUNDS overrides it.
"""

import io
import json
import os
import time
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn

SWARM_ROUNDS = 12
LOCAL_EPOCHS = 1  # local passes over the site's training segments per round
BATCH_SIZE = 64
LEARNING_RATE = 1e-3
CROP_FRAMES = 128  # training uses random 128-frame (2.56 s) crops of each segment
EVAL_BATCH_SIZE = 256
NUM_THREADS = 4  # CPU threads per site; every local-test client runs on one host
SEED = 0

N_MELS = 32
SEGMENT_FRAMES = 150
REQUIRED_FILES = ("train.npz", "val.npz", "site_info.json")
NPZ_KEYS = ("x", "y", "rec", "rec_ids", "rec_y")


# --- Site and data selection -------------------------------------------------


def site_name():
    """Return this client's FLARE site name; call after flare_adapter.init_flare().

    NVIDIA FLARE's get_site_name() gives the name in every SwarmMed job. Manual runs
    can set SWARMMED_SITE_NAME. The last fallback is the job's app folder name
    (.../app_<site>/custom/training.py).
    """
    try:
        import nvflare.client as flare

        name = flare.get_site_name()
        if name:
            return str(name)
    except Exception as exc:  # fall back to the other sources
        print(f"get_site_name() failed ({exc}); trying the fallbacks")
    name = os.environ.get("SWARMMED_SITE_NAME", "").strip()
    if name:
        return name
    app_dir = Path(__file__).resolve().parent.parent.name
    return app_dir[4:] if app_dir.startswith("app_") else None


def select_site_files(keys, site):
    """Map each file in REQUIRED_FILES to its key in the registered data.

    The site's own folder ("<site>/train.npz", ...) is used when it exists; otherwise
    files at the root of the registered data ("train.npz", ...). Every client sees
    every folder on a local-test network, so only keys under "<site>/" are used.
    """
    keys = set(keys)
    if site and any(k.startswith(f"{site}/") for k in keys):
        chosen = {name: f"{site}/{name}" for name in REQUIRED_FILES}
        missing = [key for key in chosen.values() if key not in keys]
        if missing:
            raise FileNotFoundError(
                f"Site folder '{site}/' is missing {missing}. Run prepare_data.py and "
                "import the whole folder."
            )
        return chosen
    if all(name in keys for name in REQUIRED_FILES):
        return {name: name for name in REQUIRED_FILES}
    folders = sorted({k.split("/", 1)[0] + "/" for k in keys if "/" in k})
    raise FileNotFoundError(
        f"No data for site '{site}': expected '{site}/train.npz' (one folder per site) "
        f"or 'train.npz' at the root (one site's data). Top-level folders in the "
        f"registered data: {folders[:10] or 'none'}. Prepare the data with "
        "prepare_data.py (README.md, 'Data layout')."
    )


def load_npz(raw: bytes) -> dict:
    """Read an .npz file from bytes into a dict of arrays (no pickled objects)."""
    with np.load(io.BytesIO(raw), allow_pickle=False) as z:
        return {key: z[key] for key in z.files}


def normalisation_stats(info):
    """Per mel band mean and std from site_info.json, shaped to broadcast over x."""
    norm = info["normalisation"]
    mean = np.asarray(norm["mean"], dtype=np.float32)
    std = np.asarray(norm["std"], dtype=np.float32)
    if mean.shape != (N_MELS,) or std.shape != (N_MELS,) or np.any(std <= 0):
        raise ValueError("site_info.json: normalisation needs 32 means and 32 stds > 0")
    return mean[None, :, None], std[None, :, None]


def prepare_split(arrays, info):
    """Normalise one split with the site's own statistics and convert to tensors.

    Returns a dict with x (float32 tensor, segments x 1 x 32 x frames), y (float32
    tensor), rec (segment -> recording index), rec_ids and rec_y (per recording).
    """
    missing = [key for key in NPZ_KEYS if key not in arrays]
    if missing:
        raise ValueError(f"split file is missing arrays {missing}")
    x = arrays["x"]
    if x.ndim != 3 or x.shape[1] != N_MELS or x.shape[2] < CROP_FRAMES:
        raise ValueError(f"x has shape {x.shape}; expected (segments, 32, 150)")
    mean, std = normalisation_stats(info)
    x = (x.astype(np.float32) - mean) / std
    return {
        "x": torch.from_numpy(np.ascontiguousarray(x[:, None])),
        "y": torch.from_numpy(arrays["y"].astype(np.float32)),
        "rec": arrays["rec"].astype(np.int64),
        "rec_ids": arrays["rec_ids"],
        "rec_y": arrays["rec_y"].astype(np.int64),
    }


def load_site_data(fs, site):
    """Load this site's training and validation splits through the data filesystem."""
    chosen = select_site_files(list(fs.manifest), site)
    print(f"site={site} reads {sorted(chosen.values())}")
    info = json.loads(fs.read_bytes(chosen["site_info.json"]).decode("utf-8"))
    train = prepare_split(load_npz(fs.read_bytes(chosen["train.npz"])), info)
    val = prepare_split(load_npz(fs.read_bytes(chosen["val.npz"])), info)
    return train, val, info


# --- Model -------------------------------------------------------------------


class PCGNet(nn.Module):
    """Small CNN on log-mel segments: (batch, 1, 32 mel bands, frames) -> one logit.

    Four 3x3 convolution blocks (16, 32, 64, 64 channels) with GroupNorm (no running
    statistics to average across sites), ReLU and 2x2 max-pooling after the first
    three, then global average pooling, dropout and a linear layer. 60,641 weights.
    Works for any number of frames (128 in training, 150 in evaluation).
    """

    def __init__(self):
        """Build the layers."""
        super().__init__()

        def block(c_in, c_out, pool=True):
            layers = [
                nn.Conv2d(c_in, c_out, 3, padding=1),
                nn.GroupNorm(4, c_out),
                nn.ReLU(),
            ]
            return layers + [nn.MaxPool2d(2)] if pool else layers

        self.features = nn.Sequential(
            *block(1, 16), *block(16, 32), *block(32, 64), *block(64, 64, pool=False)
        )
        self.head = nn.Sequential(
            nn.AdaptiveAvgPool2d(1), nn.Flatten(), nn.Dropout(0.3), nn.Linear(64, 1)
        )

    def forward(self, x):
        """Return one logit per segment (abnormal vs normal), shape (batch,)."""
        return self.head(self.features(x)).squeeze(1)


# --- Metrics -----------------------------------------------------------------


def auroc(labels, scores):
    """Area under the ROC curve (rank formula, ties averaged); nan if one class only."""
    labels = np.asarray(labels).astype(bool)
    scores = np.asarray(scores, dtype=np.float64)
    n_pos = int(labels.sum())
    n_neg = labels.size - n_pos
    if n_pos == 0 or n_neg == 0:
        return float("nan")
    order = np.argsort(scores, kind="mergesort")
    _, first, counts = np.unique(scores[order], return_index=True, return_counts=True)
    ranks = np.empty(scores.size)
    ranks[order] = np.repeat(first + (counts + 1) / 2.0, counts)
    return float((ranks[labels].sum() - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg))


@torch.no_grad()
def predict_segments(model, x):
    """Abnormal-class probability for every segment (evaluation mode, full length)."""
    model.eval()
    out = [
        torch.sigmoid(model(x[i : i + EVAL_BATCH_SIZE]))
        for i in range(0, len(x), EVAL_BATCH_SIZE)
    ]
    return torch.cat(out).numpy().astype(np.float64)


def recording_scores(segment_prob, rec, n_recordings):
    """Mean segment probability per recording."""
    counts = np.bincount(rec, minlength=n_recordings)
    if np.any(counts == 0):
        raise ValueError("a recording has no segments")
    return np.bincount(rec, weights=segment_prob, minlength=n_recordings) / counts


def evaluate(model, split):
    """Score a model on one split; recording- and segment-level metrics as floats."""
    seg_prob = predict_segments(model, split["x"])
    y_seg = split["y"].numpy()
    p = np.clip(seg_prob, 1e-7, 1 - 1e-7)
    loss = float(-np.mean(y_seg * np.log(p) + (1 - y_seg) * np.log(1 - p)))
    rec_y = split["rec_y"]
    rec_prob = recording_scores(seg_prob, split["rec"], rec_y.size)
    pred = rec_prob >= 0.5
    pos, neg = rec_y == 1, rec_y == 0
    return {
        "auroc_rec": auroc(rec_y, rec_prob),
        "auroc_seg": auroc(y_seg, seg_prob),
        "acc_rec": float(np.mean(pred == pos)),
        "sens_rec": float(pred[pos].mean()) if pos.any() else float("nan"),
        "spec_rec": float((~pred[neg]).mean()) if neg.any() else float("nan"),
        "loss": loss,
        "n_rec": int(rec_y.size),
        "n_abn": int(pos.sum()),
    }


def format_metrics(metrics):
    """key=value pairs for a SWARMMED_METRIC line."""
    return " ".join(
        f"{k}={v}" if isinstance(v, int) else f"{k}={v:.4f}" for k, v in metrics.items()
    )


# --- Local training ----------------------------------------------------------


def make_loss(train):
    """Binary cross-entropy with the site's class balance as positive weight."""
    n_pos = float(train["y"].sum())
    n_neg = float(len(train["y"]) - n_pos)
    pos_weight = torch.tensor(n_neg / max(n_pos, 1.0))
    return nn.BCEWithLogitsLoss(pos_weight=pos_weight)


def train_epochs(model, train, epochs, rng):
    """Train for a number of epochs; returns (mean batch loss, optimiser steps, seconds).

    Uses a new Adam optimiser each round. Each batch is a random 128-frame crop
    (one offset per batch) of the 150-frame segments.
    """
    model.train()
    loss_fn = make_loss(train)
    optimiser = torch.optim.Adam(model.parameters(), lr=LEARNING_RATE)
    x, y = train["x"], train["y"]
    total, steps, start = 0.0, 0, time.perf_counter()
    for _ in range(epochs):
        order = torch.from_numpy(rng.permutation(len(x)))
        for i in range(0, len(x), BATCH_SIZE):
            idx = order[i : i + BATCH_SIZE]
            offset = int(rng.integers(0, x.shape[-1] - CROP_FRAMES + 1))
            xb = x[idx][..., offset : offset + CROP_FRAMES]
            optimiser.zero_grad()
            loss = loss_fn(model(xb), y[idx])
            loss.backward()
            optimiser.step()
            total += loss.item()
            steps += 1
    return total / max(steps, 1), steps, time.perf_counter() - start


# --- Swarm loop --------------------------------------------------------------


def main(project_id: str):
    """Run the swarm learning loop for this site."""
    # Copied next to this script by SwarmMed. Imported here so that evaluate.py can
    # import this module's model and data functions without NVIDIA FLARE.
    import flare_adapter

    torch.set_num_threads(NUM_THREADS)
    torch.manual_seed(SEED)  # same initial weights at every site
    rng = np.random.default_rng(SEED)

    flare_adapter.init_flare()
    site = site_name()
    with flare_adapter.get_data_filesystem(project_id) as fs:
        train, val, info = load_site_data(fs, site)
    n_train = len(train["x"])
    print(
        f"site={site} source={info.get('source')} train: {n_train} segments of "
        f"{len(train['rec_y'])} recordings, val: {len(val['x'])} segments of "
        f"{len(val['rec_y'])} recordings; {SWARM_ROUNDS} rounds of {LOCAL_EPOCHS} local epoch(s)"
    )

    model = PCGNet()
    while flare_adapter.is_running():
        input_model = flare_adapter.receive_model()
        if input_model is None:
            break
        rnd = input_model.current_round
        if input_model.params:
            model.load_state_dict(
                flare_adapter.get_pytorch_state_dict(input_model.params)
            )

        # The received global model, scored before local training.
        metrics = evaluate(model, val)
        print(
            f"SWARMMED_METRIC site={site} round={rnd} split=val {format_metrics(metrics)}"
        )

        loss, steps, seconds = train_epochs(model, train, LOCAL_EPOCHS, rng)
        print(
            f"site={site} round={rnd} local training: epochs={LOCAL_EPOCHS} "
            f"steps={steps} train_loss={loss:.4f} seconds={seconds:.1f}"
        )

        sent = {f"val_{k}": float(v) for k, v in metrics.items() if np.isfinite(v)}
        sent["train_loss"] = float(loss)
        flare_adapter.send_model(
            params=model.state_dict(),
            metrics=sent,
            meta={"NUM_STEPS_CURRENT_ROUND": n_train, "accuracy": metrics["acc_rec"]},
        )


if __name__ == "__main__":
    main(project_id="default_project")
