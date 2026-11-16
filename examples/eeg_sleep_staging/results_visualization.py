"""Result plots for the EEG sleep-staging template (run by SwarmMedHub in its sandbox).

The sandbox defines the object `visualization` (get_model, save_plot, open,
listdir, exists); the job's result files appear under results/<site>/. The
final global model (FL_global_model.pt) is scored on the test split of every
site in the registered data (one subject per site), and four plots are saved:

1. Confusion matrix, all test subjects pooled (row-normalised).
2. Accuracy, macro-F1 and Cohen's kappa per site and pooled.
3. F1 per sleep stage and site.
4. True and predicted hypnogram of the first site's test subject.

SleepStageCNN below must stay identical to the class in training.py. The
metrics are computed with numpy (same definitions as scikit-learn's accuracy,
macro-F1 and Cohen's kappa) to keep the sandbox's memory use low (1 GB limit;
importing torch alone takes about half of it).
"""

import io

import matplotlib.pyplot as plt
import numpy as np
import torch
import torch.nn as nn

CLASS_NAMES = ["W", "N1", "N2", "N3", "REM"]
STAGE_COLOURS = ["#4C72B0", "#DD8452", "#55A868", "#8172B3", "#C44E52"]
MODEL_FILENAME = "FL_global_model.pt"
SITE_FILES = {"train.npz", "val.npz", "test.npz", "site_info.json"}


class SleepStageCNN(nn.Module):
    """Small 1-D CNN for one 30-s, 2-channel EEG epoch (as in training.py)."""

    def __init__(self, n_channels=2, n_classes=5):
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


def site_locations(keys):
    """Return [(label, prefix)] for the root and each top-level folder with site files."""
    data_keys = [k for k in keys if not k.startswith("results/")]
    locations = [("data root", "")] if any(k in SITE_FILES for k in data_keys) else []
    folders = sorted(
        {
            k.split("/")[0]
            for k in data_keys
            if k.count("/") == 1 and k.split("/")[1] in SITE_FILES
        }
    )
    return locations + [(folder, folder + "/") for folder in folders]


def find_global_model(keys):
    """Return (client name, file name) of the first results/<site>/FL_global_model.pt."""
    candidates = sorted(
        k for k in keys if k.startswith("results/") and k.endswith("/" + MODEL_FILENAME)
    )
    if not candidates:
        raise FileNotFoundError(
            f"No results/<site>/{MODEL_FILENAME} in this job's results"
        )
    _, client, filename = candidates[0].split("/", 2)
    return client, filename


def read_arrays(path):
    """Read one .npz file completely, then return its arrays."""
    with visualization.open(path, "rb") as handle:
        raw = handle.read()
    with np.load(io.BytesIO(raw), allow_pickle=False) as data:
        return {key: data[key] for key in data.files}


def confusion(y_true, y_pred, n_classes=5):
    """Confusion matrix, rows: true stage, columns: predicted stage."""
    cells = np.bincount(y_true * n_classes + y_pred, minlength=n_classes * n_classes)
    return cells.reshape(n_classes, n_classes)


def metrics(y_true, y_pred):
    """Accuracy, macro-F1, Cohen's kappa and F1 per stage, from the confusion matrix.

    Macro-F1 averages over the stages that occur in y_true or y_pred, and F1 is 0
    where it is undefined, as scikit-learn's f1_score(average="macro",
    zero_division=0) does.
    """
    cm = confusion(y_true, y_pred).astype(np.float64)
    tp = np.diag(cm)
    true_counts, pred_counts = cm.sum(axis=1), cm.sum(axis=0)
    denominator = true_counts + pred_counts
    f1 = np.divide(2 * tp, denominator, out=np.zeros(len(tp)), where=denominator > 0)
    total = cm.sum()
    observed = tp.sum() / total
    expected = float(np.dot(true_counts, pred_counts)) / total**2
    kappa = (observed - expected) / (1 - expected) if expected < 1 else 0.0
    return {
        "n": len(y_true),
        "accuracy": float(observed),
        "macro_f1": float(f1[denominator > 0].mean()),
        "kappa": float(kappa),
        "f1": f1,
        "confusion": cm.astype(np.int64),
    }


def save(title):
    """Save the current figure as one Hub plot and free it."""
    plt.tight_layout()
    visualization.save_plot(title)
    plt.close("all")


def main():
    """Score the global model on every site's test split and plot the results."""
    torch.set_num_threads(1)
    keys = visualization.listdir("")
    client, filename = find_global_model(keys)
    print(f"Global model: results/{client}/{filename}")
    state = visualization.get_model(client_name=client, model_filename=filename)
    model = SleepStageCNN()
    model.load_state_dict({k: torch.as_tensor(v) for k, v in state.items()})  # strict
    model.eval()

    locations = site_locations(keys)
    if not locations:
        raise FileNotFoundError("No prepared site data found (test.npz).")
    results, pooled_true, pooled_pred, first = {}, [], [], None
    for label, prefix in locations:
        if not visualization.exists(prefix + "test.npz"):
            continue
        arrays = read_arrays(prefix + "test.npz")
        x = torch.from_numpy(arrays["X"])
        with torch.inference_mode():
            y_pred = torch.cat(
                [
                    model(x[i : i + 256].float()).argmax(dim=1)
                    for i in range(0, len(x), 256)
                ]
            ).numpy()
        y_true = arrays["y"].astype(np.int64)
        results[label] = metrics(y_true, y_pred)
        pooled_true.append(y_true)
        pooled_pred.append(y_pred)
        if first is None:
            first = (label, arrays["subject"], arrays["epoch"], y_true, y_pred)
        m = results[label]
        print(
            f"{label}: n={m['n']} accuracy {m['accuracy']:.3f} "
            f"macro-F1 {m['macro_f1']:.3f} kappa {m['kappa']:.3f}"
        )
    y_true_all = np.concatenate(pooled_true)
    y_pred_all = np.concatenate(pooled_pred)
    if len(results) > 1:
        results["pooled"] = metrics(y_true_all, y_pred_all)
        m = results["pooled"]
        print(
            f"pooled: n={m['n']} accuracy {m['accuracy']:.3f} "
            f"macro-F1 {m['macro_f1']:.3f} kappa {m['kappa']:.3f}"
        )

    # 1. Confusion matrix (rows: true stage), pooled over all test subjects.
    cm = confusion(y_true_all, y_pred_all)
    share = cm / np.maximum(cm.sum(axis=1, keepdims=True), 1)
    fig, ax = plt.subplots(figsize=(6.5, 5.5))
    image = ax.imshow(share, cmap="Blues", vmin=0, vmax=1)
    for i in range(5):
        for j in range(5):
            ax.text(
                j,
                i,
                f"{share[i, j]:.2f}\n({cm[i, j]})",
                ha="center",
                va="center",
                fontsize=8,
                color="white" if share[i, j] > 0.6 else "black",
            )
    ax.set_xticks(range(5))
    ax.set_xticklabels(CLASS_NAMES)
    ax.set_yticks(range(5))
    ax.set_yticklabels(CLASS_NAMES)
    ax.set_xlabel("Predicted stage")
    ax.set_ylabel("True stage")
    fig.colorbar(image, ax=ax, fraction=0.046, label="Share of the true stage")
    ax.set_title(f"Confusion matrix, test subjects pooled (n={len(y_true_all)})")
    save("Confusion matrix (test, pooled)")

    # 2. Accuracy, macro-F1 and kappa per site and pooled.
    names = list(results)
    fig, ax = plt.subplots(figsize=(max(7, 1.4 * len(names) + 2), 4.5))
    width = 0.27
    for k, (key, title) in enumerate(
        [("accuracy", "Accuracy"), ("macro_f1", "Macro-F1"), ("kappa", "Cohen's kappa")]
    ):
        values = [results[n][key] for n in names]
        positions = np.arange(len(names)) + (k - 1) * width
        ax.bar(positions, values, width, label=title)
        for x, v in zip(positions, values, strict=True):
            ax.text(x, v + 0.01, f"{v:.2f}", ha="center", fontsize=7)
    ax.set_xticks(np.arange(len(names)))
    ax.set_xticklabels([f"{n}\n(n={results[n]['n']})" for n in names], fontsize=8)
    ax.set_ylim(0, 1.15)
    ax.set_title("Test metrics per site (one subject each) and pooled")
    ax.legend(fontsize=8, ncol=3, loc="upper center")
    save("Test metrics per site and pooled")

    # 3. F1 per stage and site.
    fig, ax = plt.subplots(figsize=(max(7, 1.4 * len(names) + 2), 4.5))
    width = 0.8 / 5
    for k, stage in enumerate(CLASS_NAMES):
        positions = np.arange(len(names)) + (k - 2) * width
        ax.bar(
            positions,
            [results[n]["f1"][k] for n in names],
            width,
            color=STAGE_COLOURS[k],
            label=stage,
        )
    ax.set_xticks(np.arange(len(names)))
    ax.set_xticklabels(names, fontsize=8)
    ax.set_ylim(0, 1.15)
    ax.set_ylabel("F1")
    ax.set_title("F1 per sleep stage on the test subjects")
    ax.legend(fontsize=8, ncol=5, loc="upper center")
    save("F1 per sleep stage and site")

    # 4. True and predicted hypnogram of the first site's test subject.
    label, subjects, epochs, y_true, y_pred = first
    order = [0, 4, 1, 2, 3]  # top to bottom: W, REM, N1, N2, N3
    level = {stage: len(order) - 1 - i for i, stage in enumerate(order)}
    hours = epochs.astype(np.float64) * 30 / 3600
    fig, axes = plt.subplots(2, 1, figsize=(11, 5), sharex=True)
    for ax, values, title in [
        (axes[0], y_true, "Scored"),
        (axes[1], y_pred, "Predicted"),
    ]:
        ax.step(
            hours, [level[int(v)] for v in values], where="post", lw=0.8, color="black"
        )
        ax.set_yticks([level[k] for k in order])
        ax.set_yticklabels([CLASS_NAMES[k] for k in order])
        ax.set_ylabel(title)
        ax.grid(True, axis="x", alpha=0.3)
    axes[-1].set_xlabel("Hours since the start of the recording")
    agreement = float(np.mean(y_true == y_pred))
    fig.suptitle(
        f"Hypnogram, {label}, test subject {int(subjects[0])}: "
        f"agreement {agreement:.2f}"
    )
    save("Hypnogram of a test subject, scored and predicted")


if __name__ == "__main__":
    main()
