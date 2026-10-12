---
title: SwarmMed and NVIDIA FLARE
description: What SwarmMed adds on top of NVFlare, what it does not change, and how portable it is.
---

# SwarmMed and NVIDIA FLARE

SwarmMed does not replace NVIDIA FLARE and does not implement a learning
algorithm. FLARE provides the decentralised runtime; SwarmMed provides the
orchestration, provisioning, data management and interfaces that a clinical
consortium needs around it. This page states the division precisely, because
getting it wrong in either direction is misleading.

## Division of responsibility

| Concern | NVIDIA FLARE (2.7.1) | SwarmMed |
|---|---|---|
| **Swarm workflow** | Provides it: `SwarmServerController` / `SwarmClientController`, the client-controlled workflow in which training and aggregation run on the clients and the aggregating client rotates each round | Configures it. No new aggregation or communication algorithm. |
| **Aggregation** | `InTimeAccumulateWeightedAggregator` (sample-count weighted averaging) | Selects and parameterises it |
| **Model persistence / selection** | `PTFileModelPersistor`, `SimpleIntimeModelSelector` | Wires them into the generated job |
| **Transport, identity, PKI** | gRPC with mutual TLS, per-participant X.509 issued at provisioning | Drives provisioning, distributes startup kits, manages ports |
| **Job definition** | Job API | Generates the job from the study's configuration, so no one writes job JSON by hand |
| **Network provisioning** | `nvflare provision` CLI and a project YAML | Generates the project definition from a form or CLI arguments, runs provisioning, packages and distributes per-site startup kits, re-imports them at each site |
| **Container lifecycle** | Documented startup scripts, run by hand | Starts, monitors and stops server and client containers, with GPU and host-network handling, image pinning and per-site environment injection |
| **Local data management** | Assumes the training script finds its data | Registers site data, streams it to the training container, runs site-supplied validation and visualisation scripts in an isolated sandbox, records the outcome |
| **User interface** | Admin CLI and a monitoring dashboard for engineers | Full-lifecycle web interface (SwarmMedHub) plus a scripted CLI (`swarmed`) over the same backend: projects, data, network, training, results |
| **Results handling** | Artefacts on disk in the workspace | Synchronises artefacts to the project store, presents metrics and site-supplied result visualisations, makes them downloadable |
| **Study administration** | Out of scope | Projects, membership and roles, audit log, two-factor authentication |

A fair one-sentence summary: **SwarmMed is an orchestration, workflow, data
management and usability layer over NVFlare's client-controlled swarm workflow,
aimed at clinical consortia.** That is a real contribution for a protocol; it is
not a new decentralised-learning method.

## What "fully decentralised" means here

Precisely: **model training and parameter aggregation are performed by the
clients**, with the aggregating client rotating each round. No central site
computes the aggregate, and there is no permanently privileged aggregator.

The following are *not* decentralised, and should not be implied to be:

* An NVFlare **server process** coordinates the run — job submission, task
  assignment, round control, admin channel. It holds no training data and
  computes no aggregate, but it must be running.
* Client-to-client messages are **relayed through that server**, and by default
  are not end-to-end encrypted, so the coordinating host can observe model
  updates. See [Threat model](threat-model.md#3-the-coordinating-host-sees-model-updates-default-configuration).
* **Study governance is asymmetric** by design: a lead institution initiates the
  project, provisions the PKI and distributes the startup kits.

The defensible claim is decentralised *computation and aggregation* within a
coordinated consortium, not a leaderless network.

## Time limits and failure behaviour

SwarmMed exposes two server-controller deadlines when constructing a job:

| Setting | What it limits | NVIDIA FLARE 2.7.1 default | MediSwarm build default (worked example) |
|---|---|---|---|
| `SWARMMEDHUB_SWARM_MAX_STATUS_REPORT_INTERVAL` | Time since the server last received a status report from any individual client | 90 s | 360 000 s (100 h) |
| `SWARMMEDHUB_SWARM_PROGRESS_TIMEOUT` | Time since the server last received a changed workflow status from any client | 3 600 s | 360 000 s (100 h) |

Set the values in seconds in the **job-submitting process**, before submission:
the CLI process for a CLI submission, or the application process for a web
submission. Setting them only on the Celery worker does not configure a job
submitted by either interface. The generated server configuration records the
overrides; existing jobs are unaffected. When a setting is absent, the runtime
build's default applies. NVFlare 2.7.1 requires a status-report interval of at
least 10 s and a progress timeout of at least 5 s.

For example, the completed 50-site benchmark raised only the status deadline:

```bash
docker exec -w /app \
    -e SWARMMEDHUB_SWARM_MAX_STATUS_REPORT_INTERVAL=600 \
    -e SWARMMEDHUB_SWARM_ROUNDS=3 \
    swarmmedhub python3 -m swarmed_cli.main \
    training start --user <hub-user> --network <NET>
```

The 600 s value was sufficient for that benchmark; it is not a general setting
for every study. The first attempt aborted at the default 90 s deadline (see
[Scalability](scalability.md#the-50-site-status-timeout)).

Clients attach status reports to task-poll requests. Receiving an unchanged
report refreshes the status-report deadline but does not count as progress.
Progress means a change in the reported workflow state, such as starting or
finishing a learn task; training epochs alone do not change that state. With the
unmodified 3,600 s progress timeout, an hour without such a change at any site
aborts the job even if reports continue to arrive. Exceeding either server
deadline aborts the whole workflow; it does not complete a round with fewer sites.

Other controller deadlines are not exposed by SwarmMed. NVFlare 2.7.1 defaults
to 300 s for accepting the workflow configuration, 10 s for starting the
workflow and 10 s for acknowledging a learn task or training result; the
MediSwarm build uses 360,000 s for each. The client-side `learn_task_timeout`
defaults to `None` in both builds, and SwarmMed leaves it unset. There is no
separate local-training or gathering deadline configured, but the server
deadlines still apply. SwarmMed sets `min_responses_required` to the number of
participating clients, so every site's result is required. Raising a server
deadline permits a longer wait; it does not add dropout tolerance.

## Backend portability

There is no abstract runtime interface in the code today, and the documentation
should not claim one. What is true, and more useful, is that the FLARE-specific
logic is confined to two modules:

* `apps/network/provision.py` — turns a study definition into an NVFlare project
  and startup kits.
* `apps/training/services.py` — builds the NVFlare job (controllers, aggregator,
  persistor, executor) and submits it through the admin API.

Everything else — projects, membership, data registration and streaming,
sandboxed validation, container lifecycle, results, the interface and the CLI —
is independent of the learning backend and talks to it through those two modules.

Porting to another runtime (Flower, or a future FLARE release with breaking API
changes) therefore means reimplementing provisioning and job construction for
that runtime, while the surrounding workflow is unaffected. That is a bounded
piece of work, on the order of the two modules above, not a rewrite. We have not
performed such a port, so this is an assessment of the code structure rather than
a demonstrated capability.

The version dependency is explicit: `nvflare==2.7.1` is pinned in
`pyproject.toml`, and the training container image must carry a compatible FLARE
build.

## Why use SwarmMed rather than FLARE directly

If you have engineers who are comfortable writing FLARE job configurations,
provisioning projects, distributing startup kits and operating containers at each
site, FLARE alone is sufficient — SwarmMed would add a layer you do not need.

SwarmMed is worth the layer when: the people running the study are clinicians and
data stewards rather than distributed-systems engineers; a site's contribution
must be auditable and repeatable; each site needs to validate and inspect its own
data before contributing it; and the same infrastructure will host more than one
study.
