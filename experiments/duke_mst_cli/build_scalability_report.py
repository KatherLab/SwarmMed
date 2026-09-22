#!/usr/bin/env python3
"""Turn the raw scalability measurements into a figure and a summary table.

Input:  results/scalability/scalability_raw.csv  (written by the benchmark script)
Output: results/scalability/scalability_summary.md
        results/scalability/scalability.png
        results/scalability/scalability.svg

The benchmark uses a deliberately tiny model so that per-round time reflects
coordination cost rather than training cost. Projections for a real model are
computed from the measured per-round payload of the breast-MRI example.
"""

from __future__ import annotations

import csv
import math
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

OUT = Path("results/scalability")
MST_MODEL_MB = 94.0  # size of one FL_global_model.pt for the worked example


def fit_linear(xs: list[float], ys: list[float]) -> tuple[float, float]:
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    denom = sum((x - mx) ** 2 for x in xs)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom
    return slope, my - slope * mx


def fit_power(xs: list[float], ys: list[float]) -> float:
    """Exponent b in y ~ N^b."""
    lx = [math.log(x) for x in xs]
    ly = [math.log(y) for y in ys]
    slope, _ = fit_linear(lx, ly)
    return slope


def main() -> None:
    rows = [r for r in csv.DictReader(open(OUT / "scalability_raw.csv")) if r["status"] == "ok"]
    n = [int(r["n_clients"]) for r in rows]
    provision = [float(r["provision_s"]) for r in rows]
    start = [float(r["start_s"]) for r in rows]
    submit = [float(r["job_submit_s"]) for r in rows]
    per_round = [float(r["mean_round_s"]) for r in rows]
    rounds = int(rows[0]["rounds"])
    server_total = [float(r["server_net_rx_mb"]) + float(r["server_net_tx_mb"]) for r in rows]
    server_per_round = [v / rounds for v in server_total]

    round_slope, round_intercept = fit_linear([float(v) for v in n], per_round)
    traffic_exponent = fit_power([float(v) for v in n], server_per_round)
    start_slope, start_intercept = fit_linear([float(v) for v in n], start)

    # Figure: four panels, one per cost
    fig, axes = plt.subplots(2, 2, figsize=(10, 7))
    panels = [
        (axes[0][0], provision, "Network provisioning", "seconds", "#4878A8"),
        (axes[0][1], start, "Starting all participants", "seconds", "#6B9E78"),
        (axes[1][0], per_round, "Mean time per round\n(tiny model: coordination only)", "seconds", "#C88A3D"),
        (axes[1][1], server_per_round, "Coordinator traffic per round", "MB", "#8C6BB1"),
    ]
    for ax, values, title, unit, colour in panels:
        ax.plot(n, values, "o-", color=colour, linewidth=2, markersize=7)
        ax.set_title(title, fontsize=11)
        ax.set_xlabel("participating sites")
        ax.set_ylabel(unit)
        ax.set_xticks(n)
        ax.grid(alpha=0.3, linestyle=":")
        ax.set_ylim(bottom=0)
    axes[1][0].plot(
        n, [round_slope * v + round_intercept for v in n], "--", color="grey", linewidth=1,
        label=f"linear fit: {round_slope:.1f}·N {round_intercept:+.0f} s",
    )
    axes[1][0].legend(fontsize=8)
    axes[1][1].plot(
        n, [server_per_round[0] * (v / n[0]) ** traffic_exponent for v in n], "--", color="grey",
        linewidth=1, label=f"power fit: N^{traffic_exponent:.2f}",
    )
    axes[1][1].legend(fontsize=8)
    fig.suptitle(
        "SwarmMed platform cost as a consortium grows "
        f"({rounds} rounds, tabular model, single host)", fontsize=12,
    )
    fig.tight_layout()
    fig.savefig(OUT / "scalability.png", dpi=200)
    fig.savefig(OUT / "scalability.svg")
    plt.close(fig)

    lines = [
        "# Scalability measurements",
        "",
        "Local-test networks of increasing size on a single host, with a small tabular model "
        f"(~10 kB of weights) so that per-round time reflects coordination rather than training. "
        f"{rounds} rounds per configuration.",
        "",
        "| Sites | Provisioning (s) | Start all (s) | Job submit (s) | Mean round (s) | Coordinator traffic/round (MB) |",
        "|---|---|---|---|---|---|",
    ]
    for i, sites in enumerate(n):
        lines.append(
            f"| {sites} | {provision[i]:.1f} | {start[i]:.1f} | {submit[i]:.1f} | "
            f"{per_round[i]:.0f} | {server_per_round[i]:.1f} |"
        )
    lines += [
        "",
        "## What scales, and how",
        "",
        f"* **Provisioning is effectively constant**: {provision[0]:.1f} s at {n[0]} sites versus "
        f"{provision[-1]:.1f} s at {n[-1]}. Certificate generation is not a limiting factor.",
        f"* **Job submission is constant** at ~{sum(submit)/len(submit):.0f} s regardless of size.",
        f"* **Starting the participants grows linearly**, about {start_slope:.1f} s per additional "
        "site; this is container launch and registration, and is paid once per run.",
        f"* **Time per round grows linearly**, about {round_slope:.0f} s per site "
        f"(fit: {round_slope:.1f}·N {round_intercept:+.0f} s). With a model this small, essentially "
        "all of that is coordination: the swarm workflow waits for every site each round.",
        f"* **Coordinator traffic grows as roughly N^{traffic_exponent:.1f}** — faster than linear, "
        "because every model update is relayed through the coordinating host.",
        "",
        "## Projection to a real model",
        "",
        f"The coordination cost above is additive to the cost of moving real weights. For the "
        f"breast-MRI example ({MST_MODEL_MB:.0f} MB per model), each round moves about "
        f"`2 × {MST_MODEL_MB:.0f} MB × N` through the coordinator:",
        "",
        "| Sites | Coordination overhead/round | Model payload/round | Payload over 20 rounds |",
        "|---|---|---|---|",
    ]
    for sites in [3, 5, 10, 20, 50]:
        overhead = round_slope * sites + round_intercept
        payload_mb = 2 * MST_MODEL_MB * sites
        lines.append(
            f"| {sites} | {overhead:.0f} s | {payload_mb / 1024:.1f} GB | {payload_mb * 20 / 1024:.0f} GB |"
        )
    lines += [
        "",
        f"At 50 sites the coordination overhead alone would be roughly "
        f"{round_slope * 50 + round_intercept:.0f} s per round "
        f"({(round_slope * 50 + round_intercept) * 20 / 3600:.1f} h over 20 rounds) before any "
        "training or model transfer, and the coordinator would relay about "
        f"{2 * MST_MODEL_MB * 50 * 20 / 1024:.0f} GB. Values beyond 20 sites are extrapolated from "
        "the fit, not measured.",
        "",
        "## Caveats",
        "",
        "* All participants ran on one host, so these numbers isolate platform coordination and "
        "exclude wide-area latency and bandwidth, which would add to them.",
        "* The per-round figure excludes real training time, which dominates for any realistic model "
        "and is unaffected by the platform.",
        "* Memory figures were sampled once per configuration and are not reliable enough to report.",
        "* The coordination cost is a property of the wait-for-all swarm workflow: every site must "
        "report before a round closes, so one slow site sets the pace for all of them.",
    ]
    (OUT / "scalability_summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
