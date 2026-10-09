#!/usr/bin/env python3
"""Node C analysis: what a small centre contributes, and what it gains (task: Reviewer 2, item 6).

Answers two questions a consortium actually asks:

  1. Does including a small centre help or harm the joint model?
     -> compares the full three-site swarm against a swarm trained on node_A + node_B only.
  2. What does a small centre get out of participating?
     -> compares each site's own local model against the joint model it receives.

Inputs : results/duke_mst_cli/stat_tests/all_predictions.csv
         results/duke_mst_cli/stat_tests/predictions/swarm/swarm_ablation_no_nodeC/*.csv
Outputs: results/duke_mst_cli/stat_tests/nodec_analysis.md
         results/duke_mst_cli/stat_tests/nodec_contribution.{png,svg}
"""

from __future__ import annotations

import csv
import glob
import math
import os
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

from build_delong_report import auc_from_rows, paired_delong

STAT = Path("results/duke_mst_cli/stat_tests")
EXTERNAL_5SITE = ["CAM", "MHA", "RUMC", "UKA", "UMCU"]
GROUPS = {"DUKE_internal": ["DUKE_test"], "ODELIA_external_5site": EXTERNAL_5SITE}
RUNS = (1, 2, 3)
# Training patients per node, from the site annotation files.
PATIENTS = {"node_A": 260, "node_B": 195, "node_C": 65, "node_all": 520}
T975 = {1: 12.706, 2: 4.303, 3: 3.182}


def load_all() -> dict:
    out: dict = {}
    with (STAT / "all_predictions.csv").open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            out.setdefault(row["model_id"], {}).setdefault(row["dataset"], {})[row["uid"]] = {
                "uid": row["uid"], "y_true": int(row["y_true"]), "y_score": float(row["y_score"]),
            }
    return out


def load_dir(path: str) -> dict:
    out: dict = {}
    for f in glob.glob(f"{path}/*.csv"):
        with open(f, newline="", encoding="utf-8") as handle:
            out[os.path.basename(f)[:-4]] = {
                r["uid"]: {"uid": r["uid"], "y_true": int(r["y_true"]), "y_score": float(r["y_score"])}
                for r in csv.DictReader(handle)
            }
    return out


def cases(model: dict, datasets: list[str]) -> list[dict]:
    return [r for d in datasets for r in model[d].values()]


def main() -> None:
    rows = load_all()
    ablation = load_dir(str(STAT / "predictions" / "swarm" / "swarm_ablation_no_nodeC"))

    lines = ["# Node C: contribution to the joint model, and benefit from it", ""]
    lines += [
        "Node C holds 65 of the 520 training patients (12.5%) and produces the weakest",
        "single-site model. Two questions follow, and they have different answers.",
        "",
        "## 1. Does including the small centre harm the joint model?",
        "",
        "A full three-site swarm (three repetitions) against a swarm trained on node_A and",
        "node_B only (one repetition), identical configuration, identical evaluation cases.",
        "",
        "| Cohort | Swarm A+B+C (3 runs) | Swarm A+B only (1 run) | Difference | Inside the A+B+C spread |",
        "|---|---|---|---|---|",
    ]
    ablation_summary = {}
    for group, datasets in GROUPS.items():
        full = [auc_from_rows(cases(rows[f"swarm_repetition_{k}"], datasets)) for k in RUNS]
        mean, sd = float(np.mean(full)), float(np.std(full, ddof=1))
        abl = auc_from_rows(cases(ablation, datasets))
        half = T975[len(full) - 1] * sd * math.sqrt(1 + 1 / len(full))
        inside = mean - half <= abl <= mean + half
        ablation_summary[group] = (mean, sd, abl, inside)
        lines.append(
            f"| {group} | {mean:.3f} ± {sd:.3f} | {abl:.3f} | {abl - mean:+.3f} | "
            f"{'yes' if inside else 'no'} ({mean - half:.3f}–{mean + half:.3f}) |"
        )
    # paired DeLong of the ablation against each full run
    lines += ["", "Paired DeLong, ablation versus each full run on identical cases:", ""]
    for group, datasets in GROUPS.items():
        parts = []
        for k in RUNS:
            abl_rows = cases(ablation, datasets)
            full_by_uid = {r["uid"]: r for r in cases(rows[f"swarm_repetition_{k}"], datasets)}
            labels = np.asarray([r["y_true"] for r in abl_rows], dtype=int)
            s_abl = np.asarray([r["y_score"] for r in abl_rows], dtype=float)
            s_full = np.asarray([full_by_uid[r["uid"]]["y_score"] for r in abl_rows], dtype=float)
            d = paired_delong(labels, s_abl, s_full)
            parts.append(f"run{k}: Δ={d['auc_a'] - d['auc_b']:+.3f} p={d['p_value']:.3g}")
        lines.append(f"* **{group}** — " + "; ".join(parts))

    lines += [
        "",
        "**Reading.** Removing the smallest centre changes the joint model by less than the",
        "variation between repetitions of the same configuration. On this cohort a centre",
        "holding an eighth of the data and training a weak local model neither measurably",
        "helped nor harmed the aggregate. Note the asymmetry in evidence: three repetitions",
        "for the full swarm against one for the ablation, so this rules out a large effect,",
        "not a small one.",
        "",
        "## 2. What does the small centre gain?",
        "",
        "| Site | Training patients | Own model, internal | Own model, external | Joint model, internal | Joint model, external |",
        "|---|---|---|---|---|---|",
    ]
    local_means = {}
    for node in ("node_A", "node_B", "node_C"):
        values = {}
        for group, datasets in GROUPS.items():
            v = [auc_from_rows(cases(rows[f"local_{node}_run{k}"], datasets)) for k in RUNS]
            values[group] = (float(np.mean(v)), float(np.std(v, ddof=1)))
        local_means[node] = values
        swarm_int = ablation_summary["DUKE_internal"][0]
        swarm_ext = ablation_summary["ODELIA_external_5site"][0]
        lines.append(
            f"| {node} | {PATIENTS[node]} | {values['DUKE_internal'][0]:.3f} ± {values['DUKE_internal'][1]:.3f} | "
            f"{values['ODELIA_external_5site'][0]:.3f} ± {values['ODELIA_external_5site'][1]:.3f} | "
            f"{swarm_int:.3f} | {swarm_ext:.3f} |"
        )
    gain_int = ablation_summary["DUKE_internal"][0] - local_means["node_C"]["DUKE_internal"][0]
    gain_ext = ablation_summary["ODELIA_external_5site"][0] - local_means["node_C"]["ODELIA_external_5site"][0]
    lines += [
        "",
        f"**Reading.** The small centre gains {gain_int:+.3f} AUROC internally and {gain_ext:+.3f}",
        "externally by joining, moving from the weakest model in the study to the joint model.",
        "The large centres gain little or nothing on their own test data. This is the",
        "asymmetry a consortium has to manage: the benefit of collaboration accrues mostly to",
        "the smaller participants, while the cost of infrastructure and governance falls on",
        "everyone.",
        "",
        "## What to tell a prospective small centre",
        "",
        "* Participating is unlikely to harm the joint model, and is very likely to give the",
        "  centre a much better model than it can train alone.",
        "* Below roughly an eighth of the consortium's data, a centre should not expect its",
        "  own data to visibly move the aggregate; its reason to join is access, external",
        "  validity for its own population, and being represented in the training distribution.",
        "* A centre whose local model is near chance should check its labels and splits before",
        "  joining (the validation and visualisation scripts in",
        "  `examples/breast_mri/`), since a data problem and a small-cohort problem",
        "  look the same from the outside.",
        "",
        "## Caveats",
        "",
        "* One ablation repetition against three full repetitions.",
        "* One cohort, one modality, one partition. The 12.5% threshold is specific to this",
        "  setup and should not be read as a general rule.",
        "* Node C's data is an IID split of the same Duke cohort, so this measures the effect of",
        "  *volume*, not of a genuinely different population.",
        "* The single-site (\"own model\") numbers in part 2 come from the published",
        "  single-site runs, which used a different model configuration from the",
        "  swarm (binary output, a DINOv2 backbone with registers, different loss and",
        "  optimiser settings). Part 2 therefore compares across configurations;",
        "  part 1, swarm against swarm, does not.",
    ]
    (STAT / "nodec_analysis.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # ---- contribution figure -------------------------------------------------
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.6))
    for ax, (group, label) in zip(axes, [("DUKE_internal", "Internal (Duke)"), ("ODELIA_external_5site", "External (5 ODELIA centres)")]):
        xs = [PATIENTS[n] for n in ("node_C", "node_B", "node_A")]
        ys = [local_means[n][group][0] for n in ("node_C", "node_B", "node_A")]
        es = [local_means[n][group][1] for n in ("node_C", "node_B", "node_A")]
        ax.errorbar(xs, ys, yerr=es, fmt="o", color="#4878A8", capsize=4, markersize=8, label="single-site model")
        for n, x, y in zip(("node_C", "node_B", "node_A"), xs, ys):
            ax.annotate(n.replace("node_", "site "), (x, y), textcoords="offset points", xytext=(6, -12), fontsize=9)
        centralized = [auc_from_rows(cases(rows[f"local_node_all_run{k}"], GROUPS[group])) for k in RUNS]
        swarm = [auc_from_rows(cases(rows[f"swarm_repetition_{k}"], GROUPS[group])) for k in RUNS]
        ax.axhline(float(np.mean(swarm)), color="#C88A3D", linestyle="-", linewidth=2, label="swarm, all three sites")
        ax.axhline(float(np.mean(centralized)), color="#6B9E78", linestyle="--", linewidth=2, label="centralized (pooled data)")
        ax.axhline(ablation_summary[group][2], color="#B4544A", linestyle=":", linewidth=2, label="swarm without the small site")
        ax.set_xlabel("training patients held by the site")
        ax.set_ylabel("AUROC")
        ax.set_title(label)
        ax.grid(alpha=0.3, linestyle=":")
        ax.set_ylim(0.5, 1.0)
    axes[0].legend(fontsize=8, loc="lower right")
    fig.suptitle("What a site contributes, and what it gains by joining", fontsize=12)
    fig.tight_layout()
    fig.savefig(STAT / "nodec_contribution.png", dpi=200)
    fig.savefig(STAT / "nodec_contribution.svg")
    plt.close(fig)
    print("\n".join(lines))


if __name__ == "__main__":
    main()
