#!/usr/bin/env python3
"""Turn the raw scalability measurements into a figure and a summary table.

Input:  results/scalability/scalability_raw.csv  (written by the benchmark script)
Output: results/scalability/scalability_summary.md
        results/scalability/scalability.png
        results/scalability/scalability.svg

Round times include local CPU training, shared-host contention and shutdown;
they do not isolate coordination. Payload estimates for the breast-MRI model
use the relay pattern, separately from the measured tabular-model traffic.
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
    """Return the least-squares slope and intercept."""
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    denom = sum((x - mx) ** 2 for x in xs)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / denom
    return slope, my - slope * mx


def fit_power(xs: list[float], ys: list[float]) -> float:
    """Exponent b in y ~ N^b."""
    lx = [math.log(x) for x in xs]
    ly = [math.log(y) for y in ys]
    slope, _ = fit_linear(lx, ly)
    return slope


def main() -> None:
    """Write the figure and summary from completed benchmark runs."""
    with (OUT / "scalability_raw.csv").open(newline="", encoding="utf-8") as source:
        all_rows = list(csv.DictReader(source))
    rows = sorted(
        (r for r in all_rows if r["status"] == "ok"), key=lambda r: int(r["n_clients"])
    )
    n = [int(r["n_clients"]) for r in rows]
    provision = [float(r["provision_s"]) for r in rows]
    start = [float(r["start_s"]) for r in rows]
    submit = [float(r["job_submit_s"]) for r in rows]
    per_round = [float(r["mean_round_s"]) for r in rows]
    rounds = int(rows[0]["rounds"])
    server_total = [
        float(r["server_net_rx_mb"]) + float(r["server_net_tx_mb"]) for r in rows
    ]
    server_per_round = [
        v / int(r["rounds"]) for v, r in zip(server_total, rows, strict=True)
    ]

    round_slope, round_intercept = fit_linear([float(v) for v in n], per_round)
    traffic_exponent = fit_power([float(v) for v in n], server_per_round)
    traffic_coefficient = math.exp(
        sum(
            math.log(y) - traffic_exponent * math.log(x)
            for x, y in zip(n, server_per_round, strict=True)
        )
        / len(n)
    )
    start_slope, _ = fit_linear([float(v) for v in n], start)

    # Figure: four panels, one per cost
    fig, axes = plt.subplots(2, 2, figsize=(10, 7))
    panels = [
        (axes[0][0], provision, "Network provisioning", "seconds", "#4878A8"),
        (axes[0][1], start, "Starting all participants", "seconds", "#6B9E78"),
        (
            axes[1][0],
            per_round,
            "Mean time per round\n(includes contended CPU training)",
            "seconds",
            "#C88A3D",
        ),
        (
            axes[1][1],
            server_per_round,
            "Coordinator traffic per round\n(container lifetime RX + TX / rounds)",
            "MB",
            "#8C6BB1",
        ),
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
        n,
        [round_slope * v + round_intercept for v in n],
        "--",
        color="grey",
        linewidth=1,
        label=f"linear fit: {round_slope:.1f}·N {round_intercept:+.0f} s",
    )
    axes[1][0].legend(fontsize=8)
    axes[1][1].plot(
        n,
        [traffic_coefficient * v**traffic_exponent for v in n],
        "--",
        color="grey",
        linewidth=1,
        label=f"power fit: N^{traffic_exponent:.2f}",
    )
    axes[1][1].legend(fontsize=8)
    fig.suptitle(
        "SwarmMed single-host benchmark "
        f"({rounds} rounds, tabular model)\n"
        "One completed run per size; 50 sites used a 600 s status limit",
        fontsize=12,
    )
    fig.tight_layout()
    fig.savefig(OUT / "scalability.png", dpi=200)
    svg_path = OUT / "scalability.svg"
    fig.savefig(svg_path)
    # Matplotlib leaves trailing spaces in SVG paths; keep the tracked copy clean.
    svg_path.write_text(
        "\n".join(line.rstrip() for line in svg_path.read_text().splitlines()) + "\n",
        encoding="utf-8",
    )
    plt.close(fig)

    lines = [
        "# Scalability measurements",
        "",
        "Local-test networks on one host, with a small tabular model "
        f"(~11.5 kB of weights). {rounds} rounds per configuration. "
        "Times include local CPU training and shared-host "
        "contention. Only completed runs enter the table and fits.",
        "",
        "| Sites | Provisioning (s) | Start all (s) | Job submit (s) | "
        "Mean round (s) | Coordinator traffic/round (MB) |",
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
        f"* **Provisioning** ranged from {min(provision):.1f} "
        f"to {max(provision):.1f} s.",
        f"* **Job submission** ranged from {min(submit):.1f} to {max(submit):.1f} s.",
        f"* **Starting the participants** took about {start_slope:.1f} s "
        "per additional site in a linear fit. This times container launches; "
        "registration can finish later.",
        f"* **Mean round time** fits {round_slope:.1f}·N {round_intercept:+.0f} s "
        "across the completed runs. This is a descriptive fit to contended "
        "training and coordination together.",
        f"* **Coordinator traffic** fits roughly N^{traffic_exponent:.2f}. "
        "The counters include registration, job deployment and control messages "
        "over runs of different durations; "
        "this exponent does not describe model-payload scaling.",
        "* The completed 50-site run used "
        "`SWARMMEDHUB_SWARM_MAX_STATUS_REPORT_INTERVAL=600`. "
        "The first attempt aborted with NVFlare 2.7.1's default 90 s status limit; "
        "see `docs/scalability.md` for the log evidence and configuration details.",
        "* Excluded attempts: "
        + ", ".join(
            f"{r['n_clients']} sites ({r['status']})"
            for r in all_rows
            if r["status"] != "ok"
        )
        + ".",
        "",
        "## Projection to a real model",
        "",
        f"For the breast-MRI example ({MST_MODEL_MB:.0f} MB per model), "
        f"the relay-pattern estimate is `2 × {MST_MODEL_MB:.0f} MB × (N − 1)` "
        "per round in each direction. The coordinator receives each copy and "
        "sends it on, so combined RX + TX is twice these values. The 20-round "
        "column includes one final model distribution. These are decimal GB "
        "estimates of model payload, not measured traffic or predictions of "
        "training time.",
        "",
        "| Sites | Each direction, per round (GB) | "
        "Each direction, 20 rounds + final distribution (GB) |",
        "|---|---|---|",
    ]
    for sites in [3, 5, 10, 20, 50]:
        payload_mb = 2 * MST_MODEL_MB * (sites - 1)
        final_mb = MST_MODEL_MB * (sites - 1)
        lines.append(
            f"| {sites} | {payload_mb / 1000:.2f} | "
            f"{(payload_mb * 20 + final_mb) / 1000:.1f} |"
        )
    lines += [
        "",
        "## Caveats",
        "",
        "* One completed run at each of five sizes on one host cannot establish "
        "a capacity limit or predict performance across institutions. "
        "No wide-area links were tested.",
        "* Mean round time is the post-submission wall time until server "
        "completion, polled every 10 s, divided by the round count. "
        "It includes training, coordination and shutdown.",
        "* Memory figures were sampled once per configuration "
        "and are not reliable enough to report.",
        "* SwarmMed requires all sites' responses; slower sites can delay "
        "or prevent completion. "
        "The benchmark did not vary site hardware, dataset size or link quality.",
    ]
    (OUT / "scalability_summary.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    print("\n".join(lines))


if __name__ == "__main__":
    main()
