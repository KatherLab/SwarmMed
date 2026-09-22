"""Site data visualisation for a breast-MRI cohort (ODELIA/Duke layout).

Runs in the SwarmMed sandbox and produces at most four figures that let a
clinician or data steward see what this site is about to contribute, before any
training starts. Nothing but the rendered plots leaves the sandbox.

`visualization` is injected by the platform: visualization.open(),
visualization.exists(), visualization.listdir(), visualization.save_plot(title).

Companion to val.py in this directory.
"""

import csv
import io

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

META_DIR = "metadata_unilateral"
POSITIVE_CLASS = 2
CLASS_LABELS = {0: "no lesion", 1: "benign", 2: "malignant"}


def read_csv(path):
    with visualization.open(path, "r") as handle:  # noqa: F821
        return list(csv.DictReader(io.StringIO(handle.read())))


def to_int(value, default=None):
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


annotation = read_csv(f"{META_DIR}/annotation.csv")
split_rows = read_csv(f"{META_DIR}/split.csv")
split_by_uid = {r["UID"]: r["Split"] for r in split_rows}

# 1. Class balance, overall and by split
labels = [to_int(r.get("Lesion")) for r in annotation]
classes = sorted({v for v in labels if v is not None})
splits = ["train", "val", "test"]
fig, ax = plt.subplots(figsize=(7, 4))
bottom = [0] * len(classes)
for split_name in splits:
    counts = [
        sum(
            1
            for r in annotation
            if to_int(r.get("Lesion")) == c and split_by_uid.get(r["UID"]) == split_name
        )
        for c in classes
    ]
    ax.bar([CLASS_LABELS.get(c, str(c)) for c in classes], counts, bottom=bottom, label=split_name)
    bottom = [b + c for b, c in zip(bottom, counts)]
total = len(annotation)
positives = sum(1 for v in labels if v == POSITIVE_CLASS)
ax.set_ylabel("exams")
ax.set_title(
    f"Class balance — {total} exams, {positives} malignant ({positives / total:.0%})"
    if total
    else "Class balance"
)
ax.legend(title="split")
plt.tight_layout()
visualization.save_plot("Class balance by split")  # noqa: F821
plt.close(fig)

# 2. Exams per patient — laterality coverage and any duplicates
per_patient = {}
for r in annotation:
    per_patient[r["PatientID"]] = per_patient.get(r["PatientID"], 0) + 1
counts = sorted(set(per_patient.values()))
fig, ax = plt.subplots(figsize=(7, 4))
ax.bar([str(c) for c in counts], [list(per_patient.values()).count(c) for c in counts])
ax.set_xlabel("exams per patient")
ax.set_ylabel("patients")
ax.set_title(
    f"{len(per_patient)} patients, {total} exams "
    "(2 per patient is expected for a unilateral pipeline)"
)
plt.tight_layout()
visualization.save_plot("Exams per patient")  # noqa: F821
plt.close(fig)

# 3. Age distribution by class, if an Age column is present
ages = [(to_int(r.get("Age")), to_int(r.get("Lesion"))) for r in annotation]
ages = [(a, c) for a, c in ages if a is not None and a > 0]
if ages:
    # Duke stores age in days; convert when the magnitude says so.
    scale = 365.25 if max(a for a, _ in ages) > 200 else 1.0
    fig, ax = plt.subplots(figsize=(7, 4))
    for c in classes:
        values = [a / scale for a, cls in ages if cls == c]
        if values:
            ax.hist(values, bins=20, alpha=0.55, label=f"{CLASS_LABELS.get(c, c)} (n={len(values)})")
    ax.set_xlabel("age at exam (years)")
    ax.set_ylabel("exams")
    ax.set_title("Age distribution by class")
    ax.legend()
    plt.tight_layout()
    visualization.save_plot("Age distribution by class")  # noqa: F821
    plt.close(fig)

# 4. Split composition — what this site contributes to training vs evaluation
fig, ax = plt.subplots(figsize=(7, 4))
split_counts = [sum(1 for r in annotation if split_by_uid.get(r["UID"]) == s) for s in splits]
unassigned = sum(1 for r in annotation if r["UID"] not in split_by_uid)
bars = splits + (["unassigned"] if unassigned else [])
values = split_counts + ([unassigned] if unassigned else [])
ax.bar(bars, values, color=["#4878A8", "#6B9E78", "#C88A3D", "#B4544A"][: len(bars)])
for i, v in enumerate(values):
    ax.text(i, v, str(v), ha="center", va="bottom")
ax.set_ylabel("exams")
ax.set_title("Split composition at this site")
plt.tight_layout()
visualization.save_plot("Split composition")  # noqa: F821
plt.close(fig)
