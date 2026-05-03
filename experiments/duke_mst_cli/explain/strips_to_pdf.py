#!/usr/bin/env python3
"""Combine all per-case side-by-side strip PNGs into a single multi-page PDF.

Uses ``selected_cases.csv`` to order pages by (outcome, cohort, prob_malignant)
so the PDF reads TP → FP → FN → TN with both cohorts grouped together.
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from PIL import Image


OUTCOME_ORDER = ("TP", "FP", "FN", "TN")
OUTCOME_TO_LABEL = {
    "TP": "True positive",
    "FP": "False positive",
    "FN": "False negative",
    "TN": "True negative",
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True, type=Path,
                        help="The explainability/ root containing selected_cases.csv and figures/strips/")
    parser.add_argument("--pdf", type=Path, default=None,
                        help="Output PDF path. Defaults to <output-dir>/figures/strips_all.pdf")
    args = parser.parse_args()

    pdf_path = args.pdf or (args.output_dir / "figures" / "strips_all.pdf")
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    strips_dir = args.output_dir / "figures" / "strips"

    selected_csv = args.output_dir / "selected_cases.csv"
    with selected_csv.open() as handle:
        rows = list(csv.DictReader(handle))

    # Order: TP → FP → FN → TN; within outcome, alphabetical cohort then by probability.
    rows.sort(key=lambda r: (
        OUTCOME_ORDER.index(r["outcome"]),
        r["cohort"],
        -float(r["prob_malignant"]) if r["outcome"] in ("TP", "FP") else float(r["prob_malignant"]),
    ))

    with PdfPages(pdf_path) as pdf:
        for row in rows:
            cohort = row["cohort"]
            uid = row["uid"]
            outcome = row["outcome"]
            prob = float(row["prob_malignant"])
            png_path = strips_dir / f"{cohort}_{uid}_strip.png"
            if not png_path.exists():
                print(f"missing strip: {png_path}")
                continue

            img = Image.open(png_path)
            w, h = img.size
            dpi = 150
            fig = plt.figure(figsize=(w / dpi, h / dpi + 0.4), dpi=dpi)
            ax = fig.add_axes([0.0, 0.0, 1.0, h / (h + 60)])
            ax.imshow(img)
            ax.axis("off")
            header_ax = fig.add_axes([0.0, h / (h + 60), 1.0, 60 / (h + 60)])
            header_ax.axis("off")
            header_ax.text(
                0.5, 0.5,
                f"{cohort}  •  {OUTCOME_TO_LABEL[outcome]}  •  {uid}  •  P(Malignant)={prob:.3f}",
                ha="center", va="center", fontsize=11, fontweight="bold",
            )
            pdf.savefig(fig, bbox_inches="tight")
            plt.close(fig)
            img.close()

    print(f"Wrote {pdf_path} with {len(rows)} pages")


if __name__ == "__main__":
    main()
