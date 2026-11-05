#!/usr/bin/env python3
"""Compare the swarm runs with the matched baselines of the breast MRI worked example.

The matched baselines were trained with train_local_baseline.sh: MediSwarm's
local-training mode, in the image and with the settings of the swarm runs. This
script reads only the per-case predictions that export_global_predictions.py
writes, under results/duke_mst_cli/stat_tests/ (or --stat-dir):

  predictions/swarm/swarm_repetition_{1,2,3}/
      the three swarm runs
  predictions/matched_baseline/<model>/
      matched_local_node_{A,B,C}_rep{k}_{last,best} and
      matched_centralised_rep{k}_{last,best}
  predictions/swarm/<run>/
      single runs, each optional: swarm_fedprox_mu001,
      swarm_ablation_no_nodeC, hub_poc_run1, swarm_scrambled_label_control

It writes, in the same directory:

  matched_auroc_by_model.csv  AUROC of every model on every cohort
  matched_arm_summary.csv     per arm, checkpoint and cohort: AUROC per run, mean,
                              SD and 95% t-interval across runs
  matched_comparisons.csv     swarm minus centralised, minus ensemble and minus
                              node_C: paired DeLong test per run, and the mean
                              difference across runs with its 95% t-interval
  matched_single_runs.csv     each single run: AUROC with DeLong 95% CI, test
                              against 0.5, the 95% prediction interval of the
                              swarm runs, and paired DeLong tests against each
  matched_report.md           the main tables, readable

Run k of a baseline arm is paired with swarm repetition k, and the ensemble of
run k is the case-wise mean of the scores of the three local models of run k.
"last" is the model after the last epoch; "best" is the checkpoint selected on
the validation set. The external cohort pools the test splits of CAM, MHA, RUMC,
UKA and UMCU; RSH is reported on its own.
"""

from __future__ import annotations

import argparse
import csv
import math
from pathlib import Path

import numpy as np
from build_delong_report import auc_from_arrays, fast_delong, paired_delong

EXTERNAL_5SITE = ["CAM", "MHA", "RUMC", "UKA", "UMCU"]
DATASETS = ["DUKE_test", *EXTERNAL_5SITE, "RSH"]
COHORT_GROUPS = {
    "DUKE_internal": ["DUKE_test"],
    "ODELIA_external_5site": EXTERNAL_5SITE,
    **{name: [name] for name in [*EXTERNAL_5SITE, "RSH"]},
}
POOLED = ("DUKE_internal", "ODELIA_external_5site")
RUNS = (1, 2, 3)
CHECKPOINTS = ("last", "best")
BASELINE_ARMS = {
    "node_A": "matched_local_node_A_rep{k}_{c}",
    "node_B": "matched_local_node_B_rep{k}_{c}",
    "node_C": "matched_local_node_C_rep{k}_{c}",
    "centralised": "matched_centralised_rep{k}_{c}",
    "ensemble": "matched_ensemble_rep{k}_{c}",
}
SWARM = "swarm_repetition_{k}"
REFERENCES = ("centralised", "ensemble", "node_C")
SINGLE_RUNS = (
    "swarm_fedprox_mu001",
    "swarm_ablation_no_nodeC",
    "hub_poc_run1",
    "swarm_scrambled_label_control",
)
Z975 = 1.96
# t_{0.975, df} for df = 1..5
T975 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571}

Model = dict[str, dict[str, tuple[int, float]]]  # dataset -> uid -> (label, score)


def load_model(path: Path) -> Model:
    """Read one model's per-cohort prediction CSVs."""
    model: Model = {}
    for dataset in DATASETS:
        csv_path = path / f"{dataset}.csv"
        if not csv_path.exists():
            raise SystemExit(f"Missing {csv_path}")
        with csv_path.open(newline="", encoding="utf-8") as handle:
            model[dataset] = {
                row["uid"]: (int(row["y_true"]), float(row["y_score"]))
                for row in csv.DictReader(handle)
            }
    return model


def ensemble(models: list[Model]) -> Model:
    """Return the case-wise mean of the models' scores."""
    out: Model = {}
    for dataset in DATASETS:
        out[dataset] = {}
        for uid, (label, _score) in models[0][dataset].items():
            if any(m[dataset][uid][0] != label for m in models):
                raise SystemExit(f"Labels differ between models for {dataset} {uid}")
            mean = sum(m[dataset][uid][1] for m in models) / len(models)
            out[dataset][uid] = (label, mean)
    return out


def arrays(model: Model, keys: list[tuple[str, str]]) -> tuple[np.ndarray, np.ndarray]:
    """Return labels and scores of the given cases, in the given order."""
    labels = np.asarray([model[d][u][0] for d, u in keys], dtype=int)
    scores = np.asarray([model[d][u][1] for d, u in keys], dtype=float)
    return labels, scores


def auc_with_se(labels: np.ndarray, scores: np.ndarray) -> tuple[float, float]:
    """Return the AUROC of one model and its DeLong standard error."""
    order = np.argsort(-labels)
    aucs, covariance = fast_delong(scores[order][None, :], int(labels.sum()))
    return float(aucs[0]), math.sqrt(float(covariance[0, 0]))


def mean_sd_ci(values: list[float]) -> tuple[float, float, float, float]:
    """Return mean, SD and the 95% t-interval of the mean."""
    arr = np.asarray(values, dtype=float)
    mean = float(arr.mean())
    if len(arr) < 2:
        return mean, float("nan"), float("nan"), float("nan")
    sd = float(arr.std(ddof=1))
    half = T975[len(arr) - 1] * sd / math.sqrt(len(arr))
    return mean, sd, mean - half, mean + half


def write(path: Path, rows: list[dict]) -> None:
    """Write rows to a CSV file."""
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def f4(value: float) -> str:
    """Format a number with four decimals."""
    return f"{value:.4f}"


def main() -> None:
    """Build the tables and the readable report."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--stat-dir",
        type=Path,
        default=Path("results/duke_mst_cli/stat_tests"),
        help="directory holding predictions/ (default: %(default)s)",
    )
    stat_dir = parser.parse_args().stat_dir
    predictions = stat_dir / "predictions"

    models: dict[str, Model] = {}
    for k in RUNS:
        models[SWARM.format(k=k)] = load_model(
            predictions / "swarm" / SWARM.format(k=k)
        )
        for c in CHECKPOINTS:
            for arm in ("node_A", "node_B", "node_C", "centralised"):
                name = BASELINE_ARMS[arm].format(k=k, c=c)
                models[name] = load_model(predictions / "matched_baseline" / name)
            local = [
                models[BASELINE_ARMS[a].format(k=k, c=c)]
                for a in ("node_A", "node_B", "node_C")
            ]
            models[BASELINE_ARMS["ensemble"].format(k=k, c=c)] = ensemble(local)
    singles = [n for n in SINGLE_RUNS if (predictions / "swarm" / n).is_dir()]
    for name in singles:
        models[name] = load_model(predictions / "swarm" / name)

    # Every model must have the same cases and labels as swarm repetition 1.
    reference = models[SWARM.format(k=1)]
    for name, model in models.items():
        for dataset in DATASETS:
            same = set(model[dataset]) == set(reference[dataset]) and all(
                model[dataset][u][0] == reference[dataset][u][0]
                for u in reference[dataset]
            )
            if not same:
                first = SWARM.format(k=1)
                raise SystemExit(
                    f"{name}: cases or labels of {dataset} differ from {first}"
                )

    keys = {
        group: [(d, uid) for d in datasets for uid in sorted(reference[d])]
        for group, datasets in COHORT_GROUPS.items()
    }
    auroc = {
        (name, group): auc_from_arrays(*arrays(model, keys[group]))
        for name, model in models.items()
        for group in COHORT_GROUPS
    }

    # 1. AUROC of every model
    model_rows = []
    for name in models:
        for group in COHORT_GROUPS:
            labels, _ = arrays(models[name], keys[group])
            model_rows.append(
                {
                    "model": name,
                    "cohort": group,
                    "n_cases": len(labels),
                    "positives": int(labels.sum()),
                    "auroc": f4(auroc[(name, group)]),
                }
            )
    write(stat_dir / "matched_auroc_by_model.csv", model_rows)

    # 2. Arms: mean, SD and t-interval across the three runs
    arm_rows = []
    arm_patterns = [("swarm", "round 20", SWARM)]
    for c in CHECKPOINTS:
        arm_patterns += [(arm, c, pattern) for arm, pattern in BASELINE_ARMS.items()]
    for group in COHORT_GROUPS:
        for arm, c, pattern in arm_patterns:
            values = [auroc[(pattern.format(k=k, c=c), group)] for k in RUNS]
            mean, sd, low, high = mean_sd_ci(values)
            arm_rows.append(
                {
                    "cohort": group,
                    "arm": arm,
                    "checkpoint": c,
                    "auroc_per_run": ";".join(f4(v) for v in values),
                    "mean": f4(mean),
                    "sd": f4(sd),
                    "ci95_low": f4(low),
                    "ci95_high": f4(high),
                }
            )
    write(stat_dir / "matched_arm_summary.csv", arm_rows)

    # 3. Swarm minus each reference arm: per run (paired DeLong) and across runs
    comparison_rows = []
    for c in CHECKPOINTS:
        for ref in REFERENCES:
            for group in COHORT_GROUPS:
                deltas, swarm_aucs, ref_aucs = [], [], []
                for k in RUNS:
                    labels, s_swarm = arrays(models[SWARM.format(k=k)], keys[group])
                    _, s_ref = arrays(
                        models[BASELINE_ARMS[ref].format(k=k, c=c)], keys[group]
                    )
                    d = paired_delong(labels, s_swarm, s_ref)
                    delta = d["auc_delta"]
                    se = abs(delta / d["z"]) if d["z"] else float("nan")
                    deltas.append(delta)
                    swarm_aucs.append(d["auc_a"])
                    ref_aucs.append(d["auc_b"])
                    comparison_rows.append(
                        {
                            "checkpoint": c,
                            "comparison": f"swarm_minus_{ref}",
                            "cohort": group,
                            "level": f"run {k}",
                            "swarm_auroc": f4(d["auc_a"]),
                            "reference_auroc": f4(d["auc_b"]),
                            "delta": f4(delta),
                            "ci95_low": f4(delta - Z975 * se),
                            "ci95_high": f4(delta + Z975 * se),
                            "ci_method": "paired DeLong",
                            "z": f"{d['z']:.3f}",
                            "p_value": f"{d['p_value']:.4g}",
                            "retention_percent": "",
                        }
                    )
                mean, sd, low, high = mean_sd_ci(deltas)
                retention = 100 * np.mean(swarm_aucs) / np.mean(ref_aucs)
                comparison_rows.append(
                    {
                        "checkpoint": c,
                        "comparison": f"swarm_minus_{ref}",
                        "cohort": group,
                        "level": "across runs",
                        "swarm_auroc": f4(float(np.mean(swarm_aucs))),
                        "reference_auroc": f4(float(np.mean(ref_aucs))),
                        "delta": f4(mean),
                        "ci95_low": f4(low),
                        "ci95_high": f4(high),
                        "ci_method": "t interval across runs",
                        "z": "",
                        "p_value": "",
                        "retention_percent": f"{retention:.1f}",
                    }
                )
    write(stat_dir / "matched_comparisons.csv", comparison_rows)

    # 4. Single runs against the three swarm runs
    single_rows = []
    for name in singles:
        for group in COHORT_GROUPS:
            labels, scores = arrays(models[name], keys[group])
            auc, se = auc_with_se(labels, scores)
            swarm_aucs = [auroc[(SWARM.format(k=k), group)] for k in RUNS]
            mean, sd, _low, _high = mean_sd_ci(swarm_aucs)
            half = T975[len(RUNS) - 1] * sd * math.sqrt(1 + 1 / len(RUNS))
            row = {
                "run": name,
                "cohort": group,
                "n_cases": len(labels),
                "positives": int(labels.sum()),
                "auroc": f4(auc),
                "ci95_low": f4(max(0.0, auc - Z975 * se)),
                "ci95_high": f4(min(1.0, auc + Z975 * se)),
                "p_vs_0.5": f"{math.erfc(abs(auc - 0.5) / se / math.sqrt(2.0)):.4g}",
                "swarm_mean": f4(mean),
                "swarm_sd": f4(sd),
                "swarm_pi95_low": f4(mean - half),
                "swarm_pi95_high": f4(mean + half),
                "inside_pi95": str(mean - half <= auc <= mean + half),
            }
            for k in RUNS:
                _, s_swarm = arrays(models[SWARM.format(k=k)], keys[group])
                d = paired_delong(labels, scores, s_swarm)
                row[f"delta_vs_run{k}"] = f4(d["auc_delta"])
                row[f"p_vs_run{k}"] = f"{d['p_value']:.4g}"
            single_rows.append(row)
    if single_rows:
        write(stat_dir / "matched_single_runs.csv", single_rows)

    # 5. Readable report
    lines = [
        "# Breast MRI worked example: swarm and matched baselines",
        "",
        "Generated by experiments/duke_mst_cli/build_matched_baseline_report.py.",
        "",
        "## AUROC, mean ± SD of three runs",
        "",
        "| arm | checkpoint | " + " | ".join(COHORT_GROUPS) + " |",
        "|---|---|" + "---|" * len(COHORT_GROUPS),
    ]
    by_arm = {(r["cohort"], r["arm"], r["checkpoint"]): r for r in arm_rows}
    for arm, c, _pattern in arm_patterns:
        cells = [
            f"{by_arm[(g, arm, c)]['mean']} ± {by_arm[(g, arm, c)]['sd']}"
            for g in COHORT_GROUPS
        ]
        lines.append(f"| {arm} | {c} | " + " | ".join(cells) + " |")
    lines += [
        "",
        "## Swarm minus reference (last checkpoint)",
        "",
        "Per run: paired DeLong test with its 95% CI. Across runs: mean of the three",
        "differences with a 95% t-interval, and the swarm's mean AUROC as a percentage",
        "of the reference's.",
        "",
        "| comparison | cohort | run 1 | run 2 | run 3 | across runs | retention |",
        "|---|---|---|---|---|---|---|",
    ]
    for ref in REFERENCES:
        for group in COHORT_GROUPS:
            rows = [
                r
                for r in comparison_rows
                if r["checkpoint"] == "last"
                and r["comparison"] == f"swarm_minus_{ref}"
                and r["cohort"] == group
            ]
            per_run = [f"{r['delta']} (P = {r['p_value']})" for r in rows[:-1]]
            last = rows[-1]
            lines.append(
                f"| swarm − {ref} | {group} | "
                + " | ".join(per_run)
                + f" | {last['delta']} ({last['ci95_low']} to {last['ci95_high']})"
                + f" | {last['retention_percent']}% |"
            )
    if single_rows:
        lines += [
            "",
            "## Single runs",
            "",
            "AUROC with DeLong 95% CI; the 95% prediction interval of the three swarm",
            "runs for a further run; and paired DeLong tests against each swarm run.",
            "",
            "| run | cohort | AUROC (95% CI) | P vs 0.5 | swarm 95% PI "
            "| vs run 1 | vs run 2 | vs run 3 |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for r in single_rows:
            if r["cohort"] not in POOLED:
                continue
            versus = [
                f"{r[f'delta_vs_run{k}']} (P = {r[f'p_vs_run{k}']})" for k in RUNS
            ]
            lines.append(
                f"| {r['run']} | {r['cohort']} | "
                f"{r['auroc']} ({r['ci95_low']}–{r['ci95_high']}) | "
                f"{r['p_vs_0.5']} | {r['swarm_pi95_low']}–{r['swarm_pi95_high']} | "
                + " | ".join(versus)
                + " |"
            )
    lines.append("")
    (stat_dir / "matched_report.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
