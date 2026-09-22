---
title: Hardware
description: What the platform costs to run, and what the model you train costs on top of it.
---

# Hardware requirements

Two quantities are routinely confused when planning a decentralised study, and
they differ by an order of magnitude:

* **Platform overhead** — what SwarmMed itself needs to run at a site: the web
  interface, task queue, database, object store and orchestration. Modest, and
  independent of the study.
* **Training compute** — what the chosen model and modality need. Dominated by
  the model, not by SwarmMed, and the same whether the model is trained locally,
  centrally or in a swarm.

A site that only contributes data and compute needs the platform overhead plus
whatever the agreed model requires. A statement such as "runs in resource-limited
environments" applies to the first, never to the second.

## Platform overhead (measured)

Measured on an idle deployment serving the web interface, with no training job
running. Resident memory, steady state:

| Service | Memory | Note |
|---|---|---|
| Web application | 956 MiB | Django + Gunicorn, 4 workers |
| Task worker | 1.4 GiB | Celery, 8 concurrent slots |
| Object store | 628 MiB | MinIO |
| Cache / broker | 301 MiB | Redis |
| Database | 129 MiB | PostgreSQL 15 |
| Sandbox daemon | 44 MiB | Docker-in-Docker for site scripts |
| Docker API proxy | 20 MiB | |
| Reverse proxy, certificates | 12 MiB | nginx + certbot |
| Connection pooler | 3 MiB | PgBouncer |
| **Total** | **≈ 3.8 GiB** | |

CPU at idle is under 5% of one core across all services. Disk: about 20 GB for
the application image, plus the training runtime image (16 GB for the breast-MRI
example), plus workspaces and results.

**Minimum for the platform alone:** 4 cores, 8 GB RAM, 100 GB disk, no GPU.
**Recommended:** 8 cores, 16 GB RAM, 500 GB SSD — this leaves headroom for
concurrent jobs, result artefacts and the sandboxed validation scripts.

A site may run the platform on a small always-on machine and the training on a
separate GPU host; only the client container needs the GPU and the data.

## Training compute by use case

The figures below are per participating site. Only the 3D MRI row is measured in
this deployment; the others are indicative starting points and should be
confirmed for your model.

| Use case | Example model | GPU | VRAM | RAM | Local disk | Source |
|---|---|---|---|---|---|---|
| Tabular / clinical variables | 3-layer MLP, ~10 k parameters | not required | — | 4 GB | < 1 GB | measured (platform example) |
| 2D imaging, moderate cohort | ResNet-50 / ViT-B at 224² | 1 × 12 GB | 8–12 GB | 16 GB | 10–100 GB | indicative |
| Digital pathology, slide level | Attention-MIL over pre-extracted features | 1 × 12 GB | 6–10 GB | 32 GB | 0.5–5 TB for slides | indicative |
| **3D MRI classification** | **MST (DINOv2 ViT-S/14 + slice fusion), unilateral** | **1 × 24 GB** | **2.7–8.1 GB measured** | **32 GB** | **2–7 GB per node** | **measured** |
| 3D segmentation | 3D U-Net at 128³ | 1 × 24–48 GB | 16–40 GB | 64 GB | 0.1–1 TB | indicative |

### The worked example, in detail

The breast-MRI demonstration ran on three hosts, each 16 cores / 125 GB RAM with
one Quadro RTX 6000 (24 GB) or RTX 8000 (48 GB). Measured during the 20-round
run: **2.7–8.1 GB VRAM** and near-100% GPU utilisation at the configured batch
size, 6.8 GB of imaging data at the largest node (520 exams), 1.8 GB at the
smallest (130 exams), and 1.6 GB of scratch space for checkpoints and logs per
node per run. A 20-round run took 3 h 07 m wall clock.

The 24 GB card quoted in the manuscript is a *recommendation* that leaves room
for larger batches and for the 3D segmentation variants of the pipeline; the
classification example as configured fits comfortably in 12 GB.

### Network and storage per round

Each round every site sends its model once and receives the aggregate once. For
the 94 MB MST model over 20 rounds with three sites that is roughly 5.6 GB per
direction at the coordinating host, which relays it (see
[Threat model](threat-model.md#3-the-coordinating-host-sees-model-updates-default-configuration)).
Model size, not dataset size, sets the bandwidth bill: a tabular model moves
kilobytes per round, a 3D segmentation model hundreds of megabytes.

Plan for: model size × rounds × sites × 2, at the coordinating site; model size ×
rounds × 2 at each participating site.

## Sizing a consortium

| Role | Cores | RAM | GPU | Disk |
|---|---|---|---|---|
| Coordinating site (platform + server, no training) | 8 | 16 GB | none | 500 GB |
| Participating site (platform + training) | 16 | 32 GB | 1 × 24 GB | data + 100 GB |
| Participating site (training only, platform elsewhere) | 8 | 32 GB | 1 × 24 GB | data + 50 GB |
| Evaluation / analysis host | 8 | 32 GB | 1 × 12 GB | data + 100 GB |

The platform can be reused across studies: the same installation hosts multiple
projects and networks, so the overhead above is paid once per site, not once per
study.
