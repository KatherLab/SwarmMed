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

The FLARE column describes NVIDIA FLARE 2.7.1, the version SwarmMed's Hub is
pinned to. The FL server and clients run whatever FLARE build their runtime image
carries, and that can differ: the worked example used a MediSwarm fork of FLARE
(see [Version dependency](#version-dependency)). Where the fork behaves
differently, this page says so.

| Concern | NVIDIA FLARE (2.7.1) | SwarmMed |
|---|---|---|
| **Swarm workflow** | Provides it: `SwarmServerController` / `SwarmClientController`, the client-controlled workflow in which training and aggregation run on the clients. Each round the aggregating client is drawn at random from the eligible clients, so the same client can aggregate several rounds in a row | Configures it: every client trains and is eligible to aggregate, every client's result is required in every round, and the number of rounds comes from `SWARMMEDHUB_SWARM_ROUNDS` or the training script's `SWARM_ROUNDS` (10 if neither is set). No new aggregation or communication algorithm. |
| **Aggregation** | `InTimeAccumulateWeightedAggregator`: a weighted average in which each update is weighted by the `NUM_STEPS_CURRENT_ROUND` value its client reports, or by 1 if none is reported | Selects it for full model weights, with no per-site weights, so the weighting follows what the training script reports. Under the Lightning integration used in the worked example that is Lightning's estimate of the optimiser steps (`trainer.estimated_stepping_batches`), which tracks sample count only when batch size and epochs are the same at every site. In the worked example they were not: MediSwarm's training code gives smaller sites more epochs per round (`weighted_epochs`, up to 10) |
| **Model persistence / selection** | `PTFileModelPersistor` or `NPModelPersistor`; `SimpleIntimeModelSelector` | Chooses the persistor from the training script's imports (`PTFileModelPersistor` for PyTorch, Lightning, Transformers or MONAI, and also for TensorFlow or Keras; `NPModelPersistor` otherwise) and wires it and the selector into the generated job. The selector looks for an `accuracy` value at the top level of each update's metadata, where the Client API never puts reported metrics, so no best model is selected unless the training script adds that value itself. In every recorded run, the worked example included, the final model is the last-round model |
| **Transport, identity, PKI** | gRPC with mutual TLS, per-participant X.509 certificates issued at provisioning; optional end-to-end encryption of client-to-client messages (`private_p2p`) | Drives provisioning, exports the startup kits for hand-over out of band, manages ports. Turns `private_p2p` off unless `SWARMMEDHUB_PRIVATE_P2P` is set (see [below](#what-fully-decentralised-means-here)) |
| **Job definition** | Job API | Generates the job from the study's configuration, so no one writes job JSON by hand |
| **Network provisioning** | `nvflare provision` CLI and a project YAML, or the FLARE Dashboard, where sites register and download their own startup kits | Generates the project definition from a form or CLI arguments, runs `nvflare provision`, and exports one startup package (web download or `swarmed network export-package`) that the lead site passes to the other sites out of band; each site imports it. The package holds every site's kit, and each kit also contains an admin identity with the `project_admin` role and a copy of the coordinating server's kit, private key included, so every site ends up with every participant's credentials |
| **Container lifecycle** | Startup scripts in each kit; optional Docker Compose and Helm files generated at provisioning (`DockerBuilder`, `HelmChartBuilder`); Docker and Kubernetes job launchers. Operators start, monitor and stop each site's processes themselves | Starts, monitors and stops the server and client containers on its own host, with GPU and host-network handling, image pinning and per-site environment injection. A participating site has to start in client-only mode (`SWARMMEDHUB_CLIENT_ONLY_MODE`), which stops with an error if the local participant cannot be identified (`SWARMMEDHUB_LOCAL_PARTICIPANT`, hostname or Tailscale address). In the default mode the imported server kit is started too, and every client kit if the local participant cannot be identified |
| **Local data management** | Assumes the training script finds its data | Registers site data and either streams it to the training container or mounts a local directory read-only (`DATA_DIR`, as in the worked example). Runs the project's `validation.py` and `visualization.py` (part of the project's code bundle) against the site's data in a container on a separate Docker-in-Docker daemon (read-only data mount, 1 CPU, 1 GiB RAM, 300 s limit) and records the outcome. The sandbox network is not internal: the scripts have outbound network access and are not isolated from the platform's internal services |
| **User interface** | Admin console (`fl_admin.sh`), the FLARE Python API and the `nvflare` CLI; experiment-tracking integrations (TensorBoard, MLflow, Weights & Biases) for training metrics; and the FLARE Dashboard, a web application for project setup, user and site registration and startup-kit download, which does not manage data, jobs or results | Full-lifecycle web interface (SwarmMedHub) plus a scripted CLI (`swarmed`) over the same backend: projects, data, network, training, results |
| **Results handling** | Artefacts on disk in the workspace | Synchronises artefacts to the project store and lists them for download, shows training progress and logs, and displays the plots made by the project's `results_visualization.py` (run in the sandbox). Training metrics appear only if that script plots them |
| **Study administration** | Partial: role-based authorisation of admin users (`project_admin`, `org_admin`, `lead`, `member`) fixed at provisioning, a per-site `audit.log` of admin commands and job events, and user and site registration in the FLARE Dashboard. No study or membership model, no two-factor authentication | Projects, membership and roles, audit log, two-factor authentication |

A fair one-sentence summary: **SwarmMed is an orchestration, workflow, data
management and usability layer over NVFlare's client-controlled swarm workflow,
aimed at clinical consortia.** That is a real contribution for a protocol; it is
not a new decentralised-learning method.

## What "fully decentralised" means here

Precisely: **model training and parameter aggregation are performed by the
clients**. Each round the aggregating client is drawn at random from the
participating clients, and the same client can be drawn several rounds running.
No central site computes the aggregate, and there is no permanently privileged
aggregator.

The following are *not* decentralised, and should not be implied to be:

* An NVFlare **server process** coordinates the run. It receives job submissions
  and admin commands, deploys the job, sends the configuration and start tasks,
  watches the clients' status reports, aborts the job when a time limit is
  exceeded or a client reports an error (see
  [Time limits and failure behaviour](#time-limits-and-failure-behaviour)), and
  ends the workflow. The clients advance the rounds themselves. The server holds
  no training data and computes no aggregate, but it must be running.

* Client-to-client messages are **relayed through that server**. The startup kits
  do not allow direct connections between clients (`allow_adhoc_conns: false`),
  so every model update passes through the coordinating host. NVFlare's swarm
  workflow can encrypt the tasks and results that carry model weights end to end,
  with the recipient's certificate (`private_p2p`: on by default in NVFlare, and
  effective in secure mode, which SwarmMed's kits use). **SwarmMed switches it
  off** unless `SWARMMEDHUB_PRIVATE_P2P` is set. By default, model updates are
  therefore protected only by per-hop TLS, which the relay terminates, and the
  coordinating host can read them.

    Setting the variable has not been tested in a multi-site SwarmMed deployment,
    and it would not have closed the gap in the worked example. The MediSwarm
    FLARE build it ran on moves model weights out of the message into a separate
    download, which is never end-to-end encrypted, even when the task itself is.
    The encryption would also be to keys the coordinating institution holds: the
    Hub that provisions the study keeps a copy of every participant's private key.
    See [Threat model](threat-model.md#3-the-coordinating-host-sees-model-updates-default-configuration).

* **Study governance is asymmetric** by design: a lead institution creates the
  project, runs provisioning and hands the startup package to the other sites.
  The credentials do not reflect that asymmetry. Every site imports the whole
  package, which contains every participant's private key, the coordinating
  server's private key and, for each site, an admin identity with NVFlare's
  `project_admin` role, which may submit jobs to every site.

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
should not claim one. FLARE-specific code sits in 16 modules, and NVFlare's job
identifiers are stored in the data model;
[Runtime backend and portability](runtime-backend.md) maps it with line
references. In outline:

* `apps/network/provision.py` — turns a study definition into an NVFlare project
  and startup kits.
* `apps/network/services.py`, `apps/network/utils.py` and `apps/network/views.py`
  — startup-kit export and import.
* `apps/network/tasks.py` — container start-up from FLARE startup kits.
* `apps/training/services.py` — builds the NVFlare job (controllers, aggregator,
  persistor, executor) and submits it through the admin API.
* `apps/training/runtime.py`, `apps/training/tasks.py`, `apps/training/utils.py`
  and `apps/training/views.py` — admin sessions to the FL server, job-status
  parsing and training monitoring.
* `apps/network/runtime.py`, `apps/home/views.py` and `apps/logs/views.py` — site
  status, training progress and live training logs, read from FLARE's log
  messages and file layout.
* `apps/training/flare_adapter.py` — the wrapper around `nvflare.client` that is
  copied into every job and imported by the example training scripts; it also
  contains the data-streaming client.
* `apps/results/tasks.py` and `apps/results/artifacts.py` — artefact collection
  from the FLARE workspace layout and FLARE's file names.

Projects, membership, data registration, sandboxed validation and most of the
web interface and CLI do not depend on FLARE directly. Container lifecycle,
training monitoring, results collection and the training scripts do.

Porting to another runtime (Flower, or a future FLARE release with breaking API
changes) would therefore mean reimplementing provisioning, job construction and
submission, status monitoring, container start-up, startup-kit export and import,
results collection and the client adapter, and adapting training scripts written
against the FLARE Client API (the worked example's script imports
`nvflare.client.lightning` directly). That is bounded but substantial work, about
5,200 lines of code. We have not performed such a port, so this is an assessment
of the code structure rather than a demonstrated capability.

### Version dependency

`nvflare==2.7.1` is pinned in `pyproject.toml`; the Hub uses it for provisioning,
job construction and the admin API. By default SwarmMed also builds a runtime
image for each network that installs the same version. An image supplied through
`SWARMMEDHUB_FLARE_IMAGE` replaces that runtime, but only together with
`SWARMMEDHUB_FORCE_CONFIGURED_IMAGE=true` (see
[NVFlare version](runtime-backend.md#nvflare-version)), and must carry a
compatible FLARE build; SwarmMed does not check this.

The worked example used such an image:
`jefftud/odelia@sha256:35b600ea5b8aab58f5c2fa5a59e29e1aed864cc301a2f3b4a2b286aeb4858aa6`
(tagged locally as `odelia:paper-apr2026`). Its FLARE build is MediSwarm's fork,
`nvflare_mediswarm` 2.7.2.dev260427 from branch `MediSwarm-2.7.2`, which reports
its version as `0+unknown`. Compared with 2.7.1, the fork changes the swarm client
and server controllers, the workflow time limits, the model persistors and the
serialisation of model weights (sent as separate downloads). Aggregation
weighting and the model selector are unchanged.

## Why use SwarmMed rather than FLARE directly

If you have engineers who are comfortable writing FLARE job configurations,
provisioning projects, distributing startup kits and operating containers at each
site, FLARE alone is sufficient — SwarmMed would add a layer you do not need.

SwarmMed is worth the layer when: the people running the study are clinicians and
data stewards rather than distributed-systems engineers; a site's contribution
must be auditable and repeatable; each site needs to validate and inspect its own
data before contributing it; and the same infrastructure will host more than one
study.

Two limitations apply today. A participating site has to be started in
client-only mode, which is set through environment variables rather than the
interface; every site in the worked example was started from the command line.
And every network that is provisioned or running adds a blocking status session
to a monitoring task that runs every minute; in our deployment, with three
networks registered, these runs occupied all of the installation's background
workers, which limits how many studies one installation can host at once (see
[Scalability](scalability.md)).
