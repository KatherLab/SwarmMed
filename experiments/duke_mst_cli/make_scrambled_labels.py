#!/usr/bin/env python3
"""Build a scrambled-label copy of one site's training data, for the negative control.

The images are hard-linked (identical bytes, no extra disk). In annotation.csv the
Lesion labels are randomly permuted within each split, train and val separately, so
every split keeps its patients and its class balance while any association between
image and label is destroyed. split.csv is copied unchanged. The held-out test set is
not part of a site folder and is never touched: the trained model is scored against
the true test labels.

The control in the paper used seeds 20261007, 20261008 and 20261009 for node_A,
node_B and node_C; with the same input files they reproduce its label files exactly.

usage: make_scrambled_labels.py <site_dir> <output_dir> <seed>

<site_dir> holds data_unilateral/ and metadata_unilateral/{annotation.csv,split.csv}.
A SCRAMBLE_MANIFEST.json with the label counts before and after is written next to the
copy.
"""

import argparse
import csv
import json
import os
import random
import subprocess  # nosec B404
import sys
from collections import Counter

SPLITS = ("train", "val")


def read_csv(path):
    """Return the field names and rows of a CSV file."""
    with open(path, newline="") as handle:
        reader = csv.DictReader(handle)
        return reader.fieldnames, list(reader)


def main():
    """Copy one site folder with its labels permuted within each split."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("site_dir")
    parser.add_argument("output_dir")
    parser.add_argument("seed", type=int)
    args = parser.parse_args()

    src_meta = os.path.join(args.site_dir, "metadata_unilateral")
    dst_meta = os.path.join(args.output_dir, "metadata_unilateral")
    if os.path.exists(args.output_dir):
        sys.exit(f"refusing to overwrite existing {args.output_dir}")

    _fields, split_rows = read_csv(os.path.join(src_meta, "split.csv"))
    splits = {row["UID"]: row["Split"] for row in split_rows}
    if any(split not in SPLITS for split in splits.values()):
        sys.exit("site data contains a split other than train/val; refusing")

    os.makedirs(dst_meta)
    subprocess.run(  # nosec B603 B607
        [
            "cp",
            "-al",
            os.path.join(args.site_dir, "data_unilateral"),
            os.path.join(args.output_dir, "data_unilateral"),
        ],
        check=True,
    )
    subprocess.run(  # nosec B603 B607
        [
            "cp",
            os.path.join(src_meta, "split.csv"),
            os.path.join(dst_meta, "split.csv"),
        ],
        check=True,
    )

    fields, rows = read_csv(os.path.join(src_meta, "annotation.csv"))
    original = {row["UID"]: row["Lesion"] for row in rows}

    # One generator, splits in a fixed order, rows in file order: the same seed and
    # input give the same permutation.
    rng = random.Random(args.seed)  # nosec B311 - a shuffle, not a secret
    for split in SPLITS:
        positions = [i for i, row in enumerate(rows) if splits.get(row["UID"]) == split]
        labels = [rows[i]["Lesion"] for i in positions]
        rng.shuffle(labels)
        for i, label in zip(positions, labels, strict=True):
            rows[i]["Lesion"] = label

    with open(os.path.join(dst_meta, "annotation.csv"), "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    report = {
        "source": args.site_dir,
        "destination": args.output_dir,
        "seed": args.seed,
        "exams": len(rows),
    }
    for split in SPLITS:
        uids = {row["UID"] for row in rows if splits.get(row["UID"]) == split}
        before = Counter(original[uid] for uid in uids)
        after = Counter(row["Lesion"] for row in rows if row["UID"] in uids)
        unchanged = sum(
            1
            for row in rows
            if row["UID"] in uids and row["Lesion"] == original[row["UID"]]
        )
        report[split] = {
            "exams": len(uids),
            "label_counts_before": dict(before),
            "label_counts_after": dict(after),
            "class_balance_preserved": before == after,
            "fraction_labels_unchanged": round(unchanged / max(1, len(uids)), 3),
        }
    with open(os.path.join(args.output_dir, "SCRAMBLE_MANIFEST.json"), "w") as handle:
        json.dump(report, handle, indent=2)
    print(json.dumps(report))


if __name__ == "__main__":
    main()
