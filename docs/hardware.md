---
title: Hardware
description: What the platform costs to run, and what the model you train costs on top of it.
---

# Hardware requirements

Two quantities are routinely confused when planning a decentralised study:

* **Platform overhead** — what SwarmMed itself needs to run at a site: the web
  interface, task queue, database, object store and orchestration. Modest, and
  largely independent of the study.
* **Training compute** — what the chosen model and modality need. Dominated by
  the model, not by SwarmMed: a site needs roughly what it would need to train the
  same model on its own data.

A site that trains needs both, the first possibly in reduced form (see below). A
statement such as "runs in resource-limited environments" applies to the first,
never to the second.

## Platform overhead (measured)

The memory figures below come from a single `docker stats` snapshot of the
coordinating host of the worked example, taken on 22 September 2026. No SwarmMed
training job was running, but the scalability benchmark was provisioning a network
through the CLI inside the web-application container at that moment. `docker stats`
reports each container's memory use excluding inactive page cache, which is not
the same as process resident memory, and one snapshot is not a steady state.

| Service | Memory | Note |
|---|---|---|
| Web application | 956 MiB | Django + Gunicorn, 4 workers; includes the running CLI process |
| Task worker | 1.4 GiB | Celery, 8 concurrent slots |
| Object store | 628 MiB | MinIO |
| Cache / broker | 301 MiB | Redis |
| Database | 129 MiB | PostgreSQL 15 |
| Sandbox daemon | 44 MiB | Docker-in-Docker for site scripts |
| Docker API proxy | 20 MiB | |
| Reverse proxy, certificates | 12 MiB | nginx + certbot |
| Connection pooler | 3 MiB | PgBouncer |
| **Total** | **≈ 3.5 GiB** | |

In the same snapshot the web-application container used 92% of one core, for the
CLI's provisioning step, and all other services together used under 5%. CPU use
at idle was not measured separately.

Disk, measured on the same host: the application image, as built there in May
2026, is 19.9 GB (one image, shared by the web application and the task worker;
images built since 9 October 2026 leave out `workspaces/` and `results/`, see
[Security](security.md#secrets-and-start-up-checks)), and the Python environment
that `make install` creates on the host is 9.6 GiB. The breast-MRI runtime image
is 15.6 GB (5.2 GB to download). Every host that runs a FLARE container needs it,
the coordinating host included, because the FL server runs from the same image.
Workspaces and results come on top.

**Suggested minimum for the platform alone:** 4 cores, 8 GB RAM, 100 GB disk, no GPU.
**Recommended:** 8 cores, 16 GB RAM, 500 GB SSD — this leaves headroom for
concurrent jobs, result artefacts and the sandboxed validation scripts. Both are
estimates from the figures above, not tested configurations; the host they were
measured on has 24 logical CPUs and 62 GiB of RAM.

The platform starts FLARE containers through the Docker daemon of the host it runs
on; starting them on another machine has not been tested. In the worked example
only the coordinating host ran the full platform. Each training host ran a reduced
copy of SwarmMed — its Python environment with PostgreSQL and Redis containers,
without the web application, task worker or object store — into which the startup
kit was imported and from which the client container was started in client-only
mode. That is how the example was run, not a packaged deployment mode. Only the
client container needs the GPU and the data.

## Training compute by use case

The figures below are per participating site. Only the tabular and 3D MRI rows
were run in this deployment, and both only partly measured: the tabular example
ran CPU-only, so "not required" for its GPU is measured; for the 3D MRI example
the GPU memory in use and the data size per site are measured. In those two rows
the RAM figures and the GPU size are recommendations. The other rows are
indicative starting points and should be confirmed for your model.

| Use case | Example model | GPU | VRAM | RAM | Local data | Source |
|---|---|---|---|---|---|---|
| Tabular / clinical variables | 3-layer MLP with batch norm, ~2.7 k parameters (~11 kB) | not required | — | 4 GB | 56 kB in total (example data) | ran CPU-only in the scalability benchmark; RAM indicative |
| 2D imaging, moderate cohort | ResNet-50 / ViT-B at 224² | 1 × 12 GB | 8–12 GB | 16 GB | 10–100 GB | indicative |
| Digital pathology, slide level | Attention-MIL over pre-extracted features | 1 × 12 GB | 6–10 GB | 32 GB | 0.5–5 TB for slides | indicative |
| **3D MRI classification** | **MST (DINOv2 ViT-S/14 + slice fusion), unilateral** | **1 × 24 GB** | **2.5–2.6 GiB measured** | **32 GB** | **1.8–6.8 GiB per site** | **VRAM and data size measured; GPU size and RAM recommended** |
| 3D segmentation | 3D U-Net at 128³ | 1 × 24–48 GB | 16–40 GB | 64 GB | 0.1–1 TB | indicative |

**Long rounds.** NVFlare 2.7.1 defaults to aborting a job after 3,600 s without
a change in any client's reported workflow state (`progress_timeout`). Starting
or finishing a training task changes that state; training epochs alone do not.
Local training can therefore trigger this deadline even while status reports
continue to arrive. SwarmMed exposes `SWARMMEDHUB_SWARM_PROGRESS_TIMEOUT` to
adjust it, and `SWARMMEDHUB_SWARM_MAX_STATUS_REPORT_INTERVAL` for the separate
deadline on missing reports (90 s by default in NVFlare 2.7.1). Set these in
seconds in the job-submitting CLI or app process before creating the job (see
[Timeout configuration](nvflare-comparison.md#time-limits-and-failure-behaviour)).
The worked example's MediSwarm runtime defaults to 360,000 s (100 h) for both
server deadlines. The client-side `learn_task_timeout` remains unset and is not
exposed by SwarmMed; server deadlines still apply during local training.

### The worked example, in detail

The breast-MRI demonstration trained on three hosts of one institution, each with
16 logical CPUs, 125 GiB of RAM and one Quadro RTX 6000 (24 GB) or Quadro RTX 8000
(48 GB). A fourth host (24 logical CPUs, 62 GiB of RAM; its GPU was not used) ran
the platform and the FL server. The Duke breast-MRI dataset was split across the
three training hosts, and all four hosts were connected over a Tailscale overlay.
The runtime image was
`jefftud/odelia@sha256:35b600ea5b8aab58f5c2fa5a59e29e1aed864cc301a2f3b4a2b286aeb4858aa6`
(PyTorch 2.2.2, CUDA 12.1, a MediSwarm fork of NVFlare).

No GPU samples were taken during the 20-round run itself. `nvidia-smi` samples
from the 2-round smoke test, the two-site ablation run and a later run of the
same configuration show 2,572–2,710 MiB (2.5–2.6 GiB) of GPU memory in use on
each training host while its client was training, and GPU utilisation of 79–94%
(one sample read 0%). A smoke-test reading of 8,103 MiB on node_A's GPU is left
out, because an unrelated test deployment was using that GPU at the time.

Imaging data on disk: 6.8 GiB at the largest node (520 unilateral breast exams
from 260 patients) and 1.8 GiB at the smallest (130 exams from 65 patients). After
the smoke test and the 20-round run, node_A's scratch directory held 1.6 GiB.

The 20-round run took 3 h 01 m wall clock (09:12:48 to 12:14:04 UTC in the FL
server log); the Hub's job record, which runs from submission until the Hub
registered completion, shows 3 h 07 m. When the run started, the unrelated test
deployment still held memory on node_A's GPU but showed 0% utilisation; its
activity during the run was not recorded.

The 24 GB GPU in the tables is a recommendation. The measured 2.5–2.6 GiB suggests
that the classification example as configured would fit on a much smaller card,
but it has only been run on 24 GB and 48 GB cards.

### Network and storage per round

Each round the aggregating site, chosen at random among all sites every round,
receives the model of every other site and sends the new aggregate back to each of
them. Client-to-client messages are relayed by the coordinating host (see
[Threat model](threat-model.md#3-the-coordinating-host-sees-model-updates-default-configuration)),
so it carries 2 × (sites − 1) model transfers per round in each direction. For
the 94 MB MST model over 20 rounds with three sites that is about 7.7 GB per
direction, including the distribution of the final model. The FL server container
of the worked example measured 9.8 GB in and 9.4 GB out; that total also includes
the 2-round smoke test (about 0.9 GB per direction expected) and the container's
other traffic over the ten days it ran.

For a small model, protocol and control traffic dominate instead. In the
scalability benchmark (tabular model of about 11 kB, all sites on one host, stock
NVFlare 2.7.1), the FL server container exchanged 2.1 MB (in plus out) over a
three-round job at three sites — about 0.7 MB per round, job deployment and
registration included — and about 20 MB per round at twenty sites (see
[Scalability](scalability.md)). Model size dominates only once the model is large,
like the 94 MB MST model above or a 3D segmentation model of hundreds of
megabytes.

Plan for about 2 × (sites − 1) × model size × rounds in each direction at the
coordinating site. A participating site sends and receives about one model per
round, rising to (sites − 1) models in each direction in a round in which it
aggregates.

## Sizing a consortium

Recommendations for a study like the worked example, not measured configurations:

| Role | Cores | RAM | GPU | Disk |
|---|---|---|---|---|
| Coordinating site (platform + server, no training) | 8 | 16 GB | none | 500 GB |
| Participating site (platform + training) | 16 | 32 GB | 1 × 24 GB | data + 100 GB |
| Participating site (training only, reduced installation as above) | 8 | 32 GB | 1 × 24 GB | data + 50 GB |
| Evaluation / analysis host | 8 | 32 GB | 1 × 12 GB | data + 100 GB |

A coordinating site trains too unless the network is created without a local
client: the web interface adds the coordinating host as a training site, and the
CLI does so unless `--no-local-client` is given, as it was in the worked example.

The platform can be reused across studies: the same installation hosts multiple
projects and networks, so the memory and disk overhead above is paid once per
site, not once per study. Concurrency is limited, however. A periodic
job-monitoring task runs every minute and blocks on an admin session to each
active network and each running training job. In our deployment, with three
networks registered and, at the time, every completed job polled as well, its
runs piled up until they occupied all eight worker slots, and a network start
requested by a user then waited indefinitely. Completed jobs are no longer polled,
but the per-network sessions remain; stop networks that are not in use (see
[Scalability](scalability.md)).
