---
title: Scalability
description: What the platform costs as a consortium grows, measured from 3 to 20 sites.
---

# Scalability

Real consortia are larger than the three sites used in the worked example. This
page reports what the platform itself costs as the number of participating sites
grows, measured rather than asserted, and separates that from the cost of the
model being trained.

## How it was measured

Local-test networks of 3, 5, 10 and 20 clients on a single host, three rounds
each, training a small tabular model (~10 kB of weights). The tiny model is
deliberate: it makes per-round time a measurement of *coordination*, since
training itself takes about a second. Running all participants on one host also
removes wide-area latency, so these are lower bounds for the platform's own cost.

Reproduce with:

```bash
swarmed network create --project <ID> --name scale-20 --local-test --clients 20
```

Raw numbers, the figure and the analysis script are in `results/scalability/`
and `experiments/duke_mst_cli/build_scalability_report.py`.

## Results

| Sites | Provisioning (s) | Start all (s) | Job submit (s) | Mean round (s) | Coordinator traffic/round (MB) |
|---|---|---|---|---|---|
| 3 | 4.4 | 7.3 | 12.5 | 43 | 0.7 |
| 5 | 4.4 | 10.1 | 12.5 | 110 | 2.1 |
| 10 | 4.6 | 17.2 | 12.6 | 204 | 6.8 |
| 20 | 4.9 | 31.2 | 12.9 | 338 | 20.2 |

![Platform cost versus number of sites](assets/scalability/scalability.png)

**Constant with size**

* *Provisioning* — 4.4 s at three sites, 4.9 s at twenty. Generating the study
  PKI is not a limiting factor.
* *Job submission* — about 13 s regardless of size.

**Linear in the number of sites**

* *Starting the participants*, about 1.4 s per site: container launch and
  registration, paid once per run.
* *Time per round*, about 17 s per site (fit: 16.6·N + 16 s). With a model this
  small that is almost entirely coordination. The swarm workflow waits for every
  site each round, so the round closes only when the slowest site reports.

**Faster than linear**

* *Coordinator traffic*, roughly N^1.8, because every model update is relayed
  through the coordinating host rather than sent site to site (see
  [Threat model](threat-model.md#3-the-coordinating-host-sees-model-updates-default-configuration)).

## What this means for a real study

Coordination overhead is *additive* to the time spent training and moving real
weights. For the breast-MRI example, each round moves about `2 × 94 MB × N`
through the coordinator:

| Sites | Coordination overhead/round | Model payload/round | Payload over 20 rounds |
|---|---|---|---|
| 3 | 66 s | 0.6 GB | 11 GB |
| 5 | 99 s | 0.9 GB | 18 GB |
| 10 | 182 s | 1.8 GB | 37 GB |
| 20 | 348 s | 3.7 GB | 73 GB |
| 50 | 847 s | 9.2 GB | 184 GB |

Values beyond 20 sites are extrapolated from the fit, not measured.

For a realistic study the practical limits are, in order:

1. **The slowest site.** Wait-for-all means one site with less GPU, more data or
   a slower link sets the pace for the whole consortium every round. This
   dominates everything else well before 20 sites.
2. **Coordinator bandwidth.** At 20 sites and a 94 MB model the coordinating host
   relays roughly 73 GB over a 20-round study; at 50 sites, 184 GB. Plan the
   coordinating site's link accordingly, and prefer a smaller model where the
   science allows it.
3. **Coordination overhead**, which only becomes comparable to training time for
   very large consortia or very small models: about 14 minutes per round at 50
   sites, before any training.

Up to roughly 20 sites the platform is not the bottleneck. Beyond that, the
wait-for-all pattern and the relay topology are the things to revisit — for
example by tolerating late sites rather than waiting for all of them.

## A limitation found while measuring this

The platform's periodic job-monitoring task opens an admin session to every
registered network on a schedule, and those calls block. With the default worker
pool, a handful of registered networks was enough for the monitoring task to
occupy every worker slot, so operations started from the interface or the CLI
queued behind it and did not run. Registered networks accumulate over a study,
so this is a real limit on how many studies one installation can host
concurrently, independent of how many sites each study has. It is being
addressed; until then, stop networks that are no longer in use.
