#!/usr/bin/env python3
"""Paper-level summary of the DUKE MST swarm experiment.

Reads results/duke_mst_cli/stat_tests/all_predictions.csv (written by build_delong_report.py)
and produces, in the same directory:

  auroc_repetition_summary.csv   per arm x cohort: AUROC per run, mean, SD, 95% t-CI
  noninferiority_tests.csv       swarm vs centralized: paired DeLong CI per run, run-level CI,
                                 non-inferiority verdicts for pre-specified margins
  ensemble_baseline.csv          no-collaboration baseline (mean of the single-node scores)
  gui_vs_cli.csv                 web-interface run against the spread of the CLI runs (if scored)
  scrambled_label_control.csv    swarm trained on permuted labels, against chance and the real
                                 swarm runs (if scored)
  cohort_sizes.csv               exams and patients per cohort, for figure axis labels
  results_summary.md             the tables above, readable

Arms: local node_A / node_B / node_C, centralized (node_all), swarm, and the no-collaboration
ensemble. Run k of the ensemble averages run k of the three local models. The external
five-site cohort is CAM+MHA+RUMC+UKA+UMCU (test splits); RSH is reported per centre only.
"""

from __future__ import annotations

import csv
import math
import re
from collections import defaultdict
from pathlib import Path

import numpy as np

from build_delong_report import auc_from_rows, fast_delong, paired_delong

EXTERNAL_5SITE = ["CAM", "MHA", "RUMC", "UKA", "UMCU"]
COHORT_GROUPS = {
    "DUKE_internal": ["DUKE_test"],
    "ODELIA_external_5site": EXTERNAL_5SITE,
    **{name: [name] for name in [*EXTERNAL_5SITE, "RSH"]},
}
ARMS = {
    "node_A": "local_node_A_run{k}",
    "node_B": "local_node_B_run{k}",
    "node_C": "local_node_C_run{k}",
    "centralized": "local_node_all_run{k}",
    "swarm": "swarm_repetition_{k}",
}
RUNS = (1, 2, 3)
MARGINS = (0.03, 0.05)
# t_{0.975, df} for df = 1..5
T975 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571}


def mean_sd_ci(values: list[float]) -> tuple[float, float, float, float]:
    arr = np.asarray(values, dtype=float)
    mean = float(arr.mean())
    if len(arr) < 2:
        return mean, float("nan"), float("nan"), float("nan")
    sd = float(arr.std(ddof=1))
    half = T975[len(arr) - 1] * sd / math.sqrt(len(arr))
    return mean, sd, mean - half, mean + half


def load_rows(path: Path) -> dict[str, dict[str, dict[str, dict]]]:
    """model_id -> dataset -> uid -> row"""
    out: dict[str, dict[str, dict[str, dict]]] = defaultdict(lambda: defaultdict(dict))
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            out[row["model_id"]][row["dataset"]][row["uid"]] = {
                "uid": row["uid"],
                "y_true": int(row["y_true"]),
                "y_score": float(row["y_score"]),
            }
    return out


def add_ensemble(rows: dict, k: int) -> None:
    """Mean of the three single-node models' scores, case by case, for run k."""
    a, b, c = (rows[f"local_node_{n}_run{k}"] for n in ("A", "B", "C"))
    model_id = f"ensemble_ABC_run{k}"
    for dataset in a:
        for uid, ra in a[dataset].items():
            rb, rc = b[dataset][uid], c[dataset][uid]
            assert ra["y_true"] == rb["y_true"] == rc["y_true"]
            rows[model_id][dataset][uid] = {
                "uid": uid,
                "y_true": ra["y_true"],
                "y_score": (ra["y_score"] + rb["y_score"] + rc["y_score"]) / 3.0,
            }


def group_rows(rows: dict, model_id: str, datasets: list[str]) -> list[dict]:
    return [row for dataset in datasets for row in rows[model_id][dataset].values()]


def load_prediction_dir(path: Path) -> dict[str, dict[str, dict]]:
    """dataset -> uid -> row, from one model's per-cohort prediction CSVs."""
    out: dict[str, dict[str, dict]] = {}
    for csv_path in sorted(path.glob("*.csv")):
        with csv_path.open(newline="", encoding="utf-8") as handle:
            out[csv_path.stem] = {
                row["uid"]: {"uid": row["uid"], "y_true": int(row["y_true"]), "y_score": float(row["y_score"])}
                for row in csv.DictReader(handle)
            }
    return out


def auc_with_se(labels: np.ndarray, scores: np.ndarray) -> tuple[float, float]:
    """AUROC of one model and its DeLong standard error."""
    order = np.argsort(-labels)
    aucs, covariance = fast_delong(scores[order][None, :], int(labels.sum()))
    return float(aucs[0]), math.sqrt(float(covariance[0, 0]))


def main() -> None:
    stat_dir = Path.cwd() / "results" / "duke_mst_cli" / "stat_tests"
    rows = load_rows(stat_dir / "all_predictions.csv")
    for k in RUNS:
        add_ensemble(rows, k)
    arms = {**ARMS, "ensemble_ABC": "ensemble_ABC_run{k}"}

    # 1. repetition summary
    summary_rows = []
    auroc: dict[tuple[str, str], list[float]] = {}
    for group, datasets in COHORT_GROUPS.items():
        for arm, pattern in arms.items():
            values = [auc_from_rows(group_rows(rows, pattern.format(k=k), datasets)) for k in RUNS]
            n_cases = len(group_rows(rows, pattern.format(k=1), datasets))
            mean, sd, lo, hi = mean_sd_ci(values)
            auroc[(group, arm)] = values
            summary_rows.append(
                {
                    "cohort": group,
                    "arm": arm,
                    "n_cases": n_cases,
                    "runs": len(values),
                    "auroc_per_run": ";".join(f"{v:.4f}" for v in values),
                    "mean_auroc": f"{mean:.4f}",
                    "sd_auroc": f"{sd:.4f}",
                    "ci95_low": f"{lo:.4f}",
                    "ci95_high": f"{hi:.4f}",
                    "ci_method": "t interval across runs",
                }
            )
    write(stat_dir / "auroc_repetition_summary.csv", summary_rows)

    # 2. non-inferiority: swarm vs centralized (and vs ensemble), per run paired DeLong + run level
    ni_rows = []
    for group in ("DUKE_internal", "ODELIA_external_5site"):
        datasets = COHORT_GROUPS[group]
        for reference in ("centralized", "ensemble_ABC"):
            deltas = []
            for k in RUNS:
                swarm = group_rows(rows, arms["swarm"].format(k=k), datasets)
                ref = group_rows(rows, arms[reference].format(k=k), datasets)
                ref_by_uid = {r["uid"]: r for r in ref}
                labels = np.asarray([r["y_true"] for r in swarm], dtype=int)
                s_swarm = np.asarray([r["y_score"] for r in swarm], dtype=float)
                s_ref = np.asarray([ref_by_uid[r["uid"]]["y_score"] for r in swarm], dtype=float)
                d = paired_delong(labels, s_swarm, s_ref)
                delta = float(d["auc_a"] - d["auc_b"])
                # 95% CI of the paired difference from the DeLong z statistic
                se = abs(delta / d["z"]) if d["z"] not in (0, 0.0) else float("nan")
                deltas.append(delta)
                ni_rows.append(
                    {
                        "cohort": group,
                        "comparison": f"swarm_minus_{reference}",
                        "level": f"run {k} (paired DeLong, n={len(labels)})",
                        "delta_auroc": f"{delta:.4f}",
                        "ci95_low": f"{delta - 1.96 * se:.4f}",
                        "ci95_high": f"{delta + 1.96 * se:.4f}",
                        "p_value_two_sided": f"{d['p_value']:.4g}",
                        **{f"non_inferior_margin_{m}": str(delta - 1.96 * se > -m) for m in MARGINS},
                    }
                )
            mean, sd, lo, hi = mean_sd_ci(deltas)
            ni_rows.append(
                {
                    "cohort": group,
                    "comparison": f"swarm_minus_{reference}",
                    "level": "across runs (t interval, n=3 runs)",
                    "delta_auroc": f"{mean:.4f}",
                    "ci95_low": f"{lo:.4f}",
                    "ci95_high": f"{hi:.4f}",
                    "p_value_two_sided": "",
                    **{f"non_inferior_margin_{m}": str(lo > -m) for m in MARGINS},
                }
            )
    write(stat_dir / "noninferiority_tests.csv", ni_rows)

    # 3. ensemble baseline table (per cohort group, all arms side by side)
    ens_rows = []
    for group in COHORT_GROUPS:
        ens_rows.append(
            {
                "cohort": group,
                **{
                    f"{arm}_mean_sd": f"{np.mean(auroc[(group, arm)]):.3f} ± {np.std(auroc[(group, arm)], ddof=1):.3f}"
                    for arm in ("node_A", "node_B", "node_C", "ensemble_ABC", "swarm", "centralized")
                },
            }
        )
    write(stat_dir / "ensemble_baseline.csv", ens_rows)

    # 3b. Web interface versus command line, if a Hub run has been scored
    hub_dir = stat_dir / "predictions" / "swarm" / "hub_poc_run1"
    gui_rows = []
    if hub_dir.exists():
        hub = load_prediction_dir(hub_dir)
        for group in ("DUKE_internal", "ODELIA_external_5site"):
            datasets = COHORT_GROUPS[group]
            hub_cases = [r for d in datasets for r in hub[d].values()]
            hub_auc = auc_from_rows(hub_cases)
            cli = auroc[(group, "swarm")]
            mean, sd, _lo, _hi = mean_sd_ci(cli)
            # Interval in which a further run of the same configuration is expected.
            half = T975[len(cli) - 1] * sd * math.sqrt(1 + 1 / len(cli))
            per_run = []
            for k in RUNS:
                cli_by_uid = {
                    r["uid"]: r for r in group_rows(rows, arms["swarm"].format(k=k), datasets)
                }
                labels = np.asarray([r["y_true"] for r in hub_cases], dtype=int)
                s_hub = np.asarray([r["y_score"] for r in hub_cases], dtype=float)
                s_cli = np.asarray([cli_by_uid[r["uid"]]["y_score"] for r in hub_cases], dtype=float)
                d = paired_delong(labels, s_hub, s_cli)
                per_run.append((k, float(d["auc_a"] - d["auc_b"]), float(d["p_value"])))
            gui_rows.append(
                {
                    "cohort": group,
                    "gui_auroc": f"{hub_auc:.4f}",
                    "cli_mean_sd": f"{mean:.4f} ± {sd:.4f}",
                    "cli_95_prediction_interval": f"{mean - half:.4f}-{mean + half:.4f}",
                    "gui_within_cli_spread": str(bool(mean - half <= hub_auc <= mean + half)),
                    "paired_delong_vs_runs": "; ".join(
                        f"run{k}: d={delta:+.4f} p={p:.3g}" for k, delta, p in per_run
                    ),
                    "significant_differences": sum(1 for _, _, p in per_run if p < 0.05),
                }
            )
        write(stat_dir / "gui_vs_cli.csv", gui_rows)

    # 3c. Scrambled-label negative control, if it has been scored: the same three nodes, image,
    # code and configuration as the swarm runs, with each node's training and validation labels
    # permuted within split. It is scored against the true labels of the untouched test sets.
    control_dir = stat_dir / "predictions" / "swarm" / "swarm_scrambled_label_control"
    control_rows = []
    if control_dir.exists():
        control = load_prediction_dir(control_dir)
        for group, datasets in COHORT_GROUPS.items():
            control_cases = [r for d in datasets for r in control[d].values()]
            labels = np.asarray([r["y_true"] for r in control_cases], dtype=int)
            s_control = np.asarray([r["y_score"] for r in control_cases], dtype=float)
            auc, se = auc_with_se(labels, s_control)
            per_run = []
            for k in RUNS:
                swarm_by_uid = {
                    r["uid"]: r for r in group_rows(rows, arms["swarm"].format(k=k), datasets)
                }
                assert set(swarm_by_uid) == {r["uid"] for r in control_cases}, (group, k)
                s_swarm = np.asarray([swarm_by_uid[r["uid"]]["y_score"] for r in control_cases], dtype=float)
                d = paired_delong(labels, s_control, s_swarm)
                per_run.append((k, float(d["auc_a"] - d["auc_b"]), float(d["p_value"])))
            mean, sd, _lo, _hi = mean_sd_ci(auroc[(group, "swarm")])
            control_rows.append(
                {
                    "cohort": group,
                    "n_cases": len(labels),
                    "positives": int(labels.sum()),
                    "control_auroc": f"{auc:.4f}",
                    "control_ci95": f"{max(0.0, auc - 1.96 * se):.4f}-{min(1.0, auc + 1.96 * se):.4f}",
                    "p_vs_chance": f"{math.erfc(abs(auc - 0.5) / se / math.sqrt(2.0)):.3g}",
                    "swarm_mean_sd": f"{mean:.4f} ± {sd:.4f}",
                    "paired_delong_vs_swarm_runs": "; ".join(
                        f"run{k}: d={delta:+.4f} p={p:.3g}" for k, delta, p in per_run
                    ),
                }
            )
        write(stat_dir / "scrambled_label_control.csv", control_rows)

    # 4. cohort sizes for figure labels
    size_rows = []
    for dataset in ["DUKE_test", *EXTERNAL_5SITE, "RSH"]:
        uids = list(rows["swarm_repetition_1"][dataset])
        patients = {re.sub(r"_(left|right)$", "", uid) for uid in uids}
        positives = sum(r["y_true"] for r in rows["swarm_repetition_1"][dataset].values())
        size_rows.append(
            {"cohort": dataset, "exams": len(uids), "patients": len(patients), "positive_exams": positives}
        )
    ext = [r for r in size_rows if r["cohort"] in EXTERNAL_5SITE]
    size_rows.append(
        {
            "cohort": "ODELIA_external_5site",
            "exams": sum(r["exams"] for r in ext),
            "patients": sum(r["patients"] for r in ext),
            "positive_exams": sum(r["positive_exams"] for r in ext),
        }
    )
    write(stat_dir / "cohort_sizes.csv", size_rows)

    # 5. readable summary
    lines = ["# DUKE MST swarm experiment — results summary", ""]
    lines += ["Generated by experiments/duke_mst_cli/build_results_summary.py from all_predictions.csv.", ""]
    lines += ["## AUROC, mean ± SD over three runs (95% t-CI)", ""]
    lines += ["| cohort | arm | n cases | per run | mean ± SD | 95% CI |", "|---|---|---|---|---|---|"]
    for r in summary_rows:
        lines.append(
            f"| {r['cohort']} | {r['arm']} | {r['n_cases']} | {r['auroc_per_run']} | "
            f"{float(r['mean_auroc']):.3f} ± {float(r['sd_auroc']):.3f} | {float(r['ci95_low']):.3f}–{float(r['ci95_high']):.3f} |"
        )
    lines += ["", "## Non-inferiority of swarm vs reference (delta = swarm − reference)", ""]
    lines += ["| cohort | comparison | level | delta | 95% CI | p (two-sided) | NI at 0.03 | NI at 0.05 |", "|---|---|---|---|---|---|---|---|"]
    for r in ni_rows:
        lines.append(
            f"| {r['cohort']} | {r['comparison']} | {r['level']} | {r['delta_auroc']} | {r['ci95_low']} to {r['ci95_high']} | "
            f"{r['p_value_two_sided']} | {r['non_inferior_margin_0.03']} | {r['non_inferior_margin_0.05']} |"
        )
    lines += ["", "Non-inferior at margin m if the lower 95% bound of delta exceeds −m. The margin must be", "pre-specified in the manuscript; both are shown for transparency.", ""]
    if gui_rows:
        lines += ["## Web interface (SwarmMedHub) versus command line", ""]
        lines += [
            "| cohort | GUI run | CLI runs (mean ± SD) | 95% prediction interval for a further run | GUI inside it | paired DeLong vs each CLI run |",
            "|---|---|---|---|---|---|",
        ]
        for r in gui_rows:
            lines.append(
                f"| {r['cohort']} | {r['gui_auroc']} | {r['cli_mean_sd']} | "
                f"{r['cli_95_prediction_interval']} | {r['gui_within_cli_spread']} | {r['paired_delong_vs_runs']} |"
            )
        lines += [
            "",
            "Both interfaces drive the same backend and generate the same NVFlare job. Separate GPU",
            "runs are not bit-reproducible, so the question is whether a GUI-initiated run is",
            "distinguishable from the spread of CLI runs of the same configuration, not whether it is",
            "identical to them.",
            "",
        ]
    if control_rows:
        lines += ["## Scrambled-label negative control", ""]
        lines += [
            "| cohort | n cases (positive) | control AUROC (95% DeLong CI) | p vs 0.5 | swarm runs (mean ± SD) | paired DeLong, control − each swarm run |",
            "|---|---|---|---|---|---|",
        ]
        for r in control_rows:
            lines.append(
                f"| {r['cohort']} | {r['n_cases']} ({r['positives']}) | {r['control_auroc']} ({r['control_ci95']}) | "
                f"{r['p_vs_chance']} | {r['swarm_mean_sd']} | {r['paired_delong_vs_swarm_runs']} |"
            )
        lines += [
            "",
            "Same nodes, image, code and configuration as the swarm runs, with each node's training and",
            "validation labels permuted within split (class balance kept, images unchanged), scored",
            "against the true labels of the untouched test sets. Single cohorts are small, so their",
            "intervals are wide; the pooled rows are the informative ones.",
            "",
        ]
    lines += ["## Cohort sizes", "", "| cohort | exams | patients | positive exams |", "|---|---|---|---|"]
    for r in size_rows:
        lines.append(f"| {r['cohort']} | {r['exams']} | {r['patients']} | {r['positive_exams']} |")
    (stat_dir / "results_summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


def write(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    main()
