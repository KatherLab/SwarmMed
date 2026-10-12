---
title: Background
description: SwarmMed's components, and swarm learning as NVIDIA FLARE implements it and SwarmMed uses it.
---

# Background

SwarmMed was developed to make **decentralized machine learning** on **sensitive medical data** usable through a **simple user interface**. Traditional machine learning requires centralizing data, which is often not feasible or desirable in medicine because of privacy concerns and data-governance rules. In decentralized learning, each institution trains on its own data and only model parameters are exchanged. That reduces data movement; it does not by itself make a collaboration private (see the [Threat model](threat-model.md)).

## 🏗️ System Architecture

SwarmMed consists of several components that each participating site runs on its own host.

### Key Components

*   **Django Web Interface (SwarmMedHub):** Project management, data import, network provisioning, training orchestration and results. nginx serves it over HTTPS on ports 5085 and 443.
*   **`swarmed` CLI:** The same workflow from the command line, run inside the web interface's application container.
*   **Celery Worker:** Handles long-running background tasks such as data synchronization, script execution, network start-up and training job monitoring.
*   **PostgreSQL and Redis:** The database (users, projects, networks, training jobs, audit log), and the task queue and cache.
*   **MinIO:** S3-compatible object storage for imported datasets, project code and scripts, plots and synchronized training results.
*   **Sandbox-dind:** A separate Docker daemon on which user-provided validation and visualization scripts run in short-lived containers with CPU, memory and time limits. Their network is not isolated, so the scripts must be trusted ([Security](security.md#script-sandbox)).
*   **NVIDIA FLARE:** The runtime for decentralized training. It issues each study's certificates, runs the FL server and the FL clients in containers, and carries out the swarm workflow.
*   **Tailscale (optional):** An overlay network between the sites. Federated training does not need it, but network creation in the web interface and the status exchange between sites' web interfaces use the host's Tailscale address ([Networking](networking.md)).

### SwarmMed Hub/CLI Orchestration
``` mermaid
flowchart LR

    subgraph A["User-Facing Entry Points"]
        HUB["SwarmMedHub<br/>Graphical user interface<br/><br/>Project setup, data management,<br/>network provisioning, training control,<br/>and results review"]
        CLI["swarmed CLI<br/>Command-line interface<br/><br/>Scriptable workflow for advanced users,<br/>reproducible automation, and batch-friendly<br/>project, network, and training operations"]
    end

    subgraph B["Shared Orchestration Layer"]
        BOOT["Django bootstrap and context resolution<br/><br/>user resolution<br/>current project/current network<br/>JSON output envelope<br/>wait helpers"]
        PROJ["Project service"]
        DATA["Data service"]
        NET["Network service"]
        TRAIN["Training service"]
        RES["Results service"]
    end

    subgraph C["Shared State and Local Execution"]
        DB["PostgreSQL<br/>projects, jobs, current context"]
        S3["MinIO / S3<br/>training code, datasets, synced artifacts"]
        CEL["Celery<br/>validation, visualization, sync tasks"]
        FLARE["NVFlare runtime and local workspaces<br/>startup packages, training runs, local artifacts"]
        LOCAL["Local node data and sandbox execution<br/>raw data stay on-site throughout training"]
    end

    HUB --> BOOT
    CLI --> BOOT

    BOOT --> PROJ
    BOOT --> DATA
    BOOT --> NET
    BOOT --> TRAIN
    BOOT --> RES

    PROJ --> DB
    DATA --> DB
    NET --> DB
    TRAIN --> DB
    RES --> DB

    PROJ --> S3
    DATA --> S3
    RES --> S3

    DATA --> CEL
    NET --> CEL
    TRAIN --> CEL
    RES --> CEL

    NET --> FLARE
    TRAIN --> FLARE
    RES --> FLARE
    DATA --> LOCAL
    TRAIN --> LOCAL
    RES --> LOCAL

    classDef hub fill:#e8f0ff,stroke:#6d8fcf,stroke-width:2px,color:#1f2d3d;
    classDef cli fill:#e7f6eb,stroke:#6fa27a,stroke-width:2px,color:#1f2d3d;
    classDef service fill:#fff7e8,stroke:#c99a3d,stroke-width:2px,color:#1f2d3d;
    classDef state fill:#f3f4f6,stroke:#8b96a8,stroke-width:2px,color:#1f2d3d;

    class HUB hub;
    class CLI cli;
    class BOOT,PROJ,DATA,NET,TRAIN,RES service;
    class DB,S3,CEL,FLARE,LOCAL state;
```
### SwarmMedHub System Architecture
``` mermaid
graph TD
    User((User)) -->|HTTPS 5085 / 443| Nginx[nginx]
    Nginx -->|HTTP| Web[Django Web Interface]
    Web <-->|ORM| DB[(PostgreSQL)]
    Web <-->|S3 API| Storage[MinIO Object Storage]
    Web -->|Enqueue Task| TaskQueue[Redis]
    TaskQueue <--> Worker[Celery Worker]

    subgraph "Script Sandbox"
        Worker -->|mTLS| SandboxDaemon[Sandbox-dind]
        SandboxDaemon -->|Run| SandboxContainer[Ephemeral Container]
        SandboxContainer -->|Presigned URLs| Storage
    end

    subgraph "NVIDIA FLARE, one study"
        Worker -->|Start container, admin API| FlareServer[FL Server]
        ClientA[FL Client, site A] <-->|mTLS| FlareServer
        ClientB[FL Client, site B] <-->|mTLS| FlareServer
        ClientC[FL Client, site C] <-->|mTLS| FlareServer
    end
```

The diagram shows the coordinating site, whose installation runs the FL server. Each site starts its own FL client from its own installation. The clients connect to the FL server, never to each other: messages between clients, model updates included, are relayed by the server.

---

## 🐝 Swarm Learning

### What is Swarm Learning?

Swarm learning is a decentralized form of **federated learning** in which the responsibilities of **aggregation and model training control are distributed to the participating sites** rather than consolidated in a central server.[^3] The collaborators **share model parameters, not data**, so that each of them can use a model trained on the data of all of them while every dataset stays at its site.

#### HPE Swarm Learning (original paper)

The term comes from Warnat-Herresthal et al., who described Swarm Learning as combining edge computing with blockchain-based peer-to-peer networking and coordination, without a central coordinator, and demonstrated it on clinical data.[^1] In HPE's implementation a blockchain coordinates the peers, and HPE describes the result as a trustless system without a central authority.[^2]

**SwarmMed does not work this way.** It has no blockchain, and its training traffic does not go from peer to peer. It uses NVIDIA FLARE's client-controlled swarm workflow (below), in which an FL server coordinates the run and relays every message between sites, and it assumes a consortium of institutions that have vetted and trust one another. It is not designed for participants that do not ([Threat model](threat-model.md#4-adversaries-considered)).

!!! tip "Original Research Paper"

    You can find the original research paper about Swarm Learning here: [Swarm Learning for decentralized and confidential clinical machine learning](https://doi.org/10.1038/s41586-021-03583-3)

### Why is Swarm Learning important?

Medical data are often not allowed to leave the institution that holds them, and moving large imaging datasets is costly. Decentralized learning lets institutions build a model on their combined data while each dataset stays where it is: only model parameters are exchanged.

### What are the benefits of Swarm Learning?

*   **Data stay at each site.** Raw images, labels and patient identifiers are not part of any message the platform sends between sites, and each site keeps control of its own storage.
*   **Each site contributes to, and receives, a model trained on all participants' data**, without the data being pooled.
*   **No permanently privileged aggregator.** The aggregating site is drawn at random each round, and the FL server computes no aggregate.

These benefits have limits, which the [Threat model](threat-model.md) states in full:

*   Model parameters can carry information about the data they were trained on (model inversion, membership inference). SwarmMed configures no differential privacy and no secure aggregation.
*   The coordinating FL server relays every model update and, in the default configuration, can read it.
*   The training code runs at every site with access to that site's data, so every participant has to trust it.

[^1]: Warnat-Herresthal, S., Schultze, H., Shastry, K.L. et al. Swarm Learning for decentralized and confidential clinical machine learning. Nature 594, 265–270 (2021). [https://doi.org/10.1038/s41586-021-03583-3](https://doi.org/10.1038/s41586-021-03583-3)
[^2]: [https://www.hpe.com/us/en/what-is/swarm-learning.html](https://www.hpe.com/us/en/what-is/swarm-learning.html)

### How does Swarm Learning work?

With Swarm Learning, training is done in multiple rounds. In each round, an aggregator client is randomly chosen from all clients, and then all training clients perform the training task on the current global model params. Once completed, all clients send their training results to the designated client for aggregation. The aggregated results are then applied to the current global model, which will become the base for the next round training. This process repeats until the configured number of rounds are completed.[^3]

SwarmMed configures every site to train and to be eligible as the aggregator, and the aggregator waits for every site's result in every round ([SwarmMed and NVIDIA FLARE](nvflare-comparison.md)).

#### _NVIDIA FLARE_[^4] Swarm Learning architecture

SwarmMed requires every participating site's result and leaves the client-side
`learn_task_timeout` unset. Separate server deadlines can abort the whole job:
`SWARMMEDHUB_SWARM_MAX_STATUS_REPORT_INTERVAL` limits gaps between received
status reports, and `SWARMMEDHUB_SWARM_PROGRESS_TIMEOUT` limits time without a
change in any client's reported workflow state. Set them in the job-submitting
CLI or app process; see [Timeout configuration](nvflare-comparison.md#time-limits-and-failure-behaviour).

In NVIDIA FLARE's client-controlled workflow, the `clients` run the training and the aggregation, and the `server` manages the job's life cycle: it deploys the job, starts the workflow, watches the clients' status reports, and aborts the job when a client reports an error or a time limit is exceeded ([What "fully decentralised" means here](nvflare-comparison.md#what-fully-decentralised-means-here)).

The clients do not connect to each other. Every startup kit sets `allow_adhoc_conns` to `false`, so the clients connect only to the FL server, and **every message between clients, model updates included, is relayed by the server**. NVIDIA FLARE can encrypt such messages end to end with the recipient's certificate (`private_p2p`), but SwarmMed switches this off unless `SWARMMEDHUB_PRIVATE_P2P` is set. In the default configuration the coordinating host can therefore read every model update; [Threat model, section 3](threat-model.md#3-the-coordinating-host-sees-model-updates-default-configuration) explains why setting the variable does not by itself prevent that.

Algorithmically, swarm learning is identical to federated averaging, except that the aggregate is computed by a client drawn at random for each round rather than by the server.

The workflow proceeds as follows:[^3]

- The workflow is started from the `starting_client`. It loads the initial model using the `persistor`, and prepares the initial training params using the `shareable generator`.
- Randomly selects a `client` as the `aggregator` for the next round.
- Broadcast the “learn” task with training params to all `training_clients` and the `aggregation_client`. The task header contains the aggregation client name, the current round number, among other things.
- All `training_clients` do training by invoking the executor configured for the train task.
- Once completed, all `training_clients` send their results to the `aggregation_client`, through the server.
- When the “learn” task is received, the `aggregation_client`:
    - Calls the `shareable generator` to compute the current global model based.
    - sets up a Gatherer object to wait for results from `training_clients`. Note that the `aggregation_client` could also be a `training_client`.
- When a training result is received from another client, the Gatherer object of the `aggregation_client` calls the configured aggregator to accept the result.
- After all participating sites' results are received, the `aggregation_client`:
    - Calls the aggregate method of the aggregator to get the aggregation result.
    - Calls the shareable generator to apply the aggregated result to the current global model.
    - If not all rounds are completed, prepare for next round:
        - Randomly selects the `aggregation_client` for the next round.
        - Calls the `shareable generator` to prepare the training params.
        - Broadcast the “learn” task to other clients for the new round.
    - If all rounds are completed:
          - Broadcast the last result to all `result_clients`
          - Check which client has the best result, and ask that client to distribute the best model to all `result_clients`.

In SwarmMed's jobs, a best model is selected only if the training script reports an `accuracy` value where the model selector looks for it; in every recorded run the final model was the last-round model ([SwarmMed and NVIDIA FLARE](nvflare-comparison.md#division-of-responsibility)).

``` mermaid
sequenceDiagram
    participant SC as Starting Client
    participant Persistor
    participant SG as Shareable Generator
    participant TCs as Training Clients
    participant AC as Aggregator Client
    participant RCs as Result Clients
    participant BestClient as Best Result Client

    note over SC,BestClient: Every message between clients is relayed by the FL server (not shown).

    %% Initialization
    SC->>Persistor: Load initial model
    Persistor-->>SC: Returns initial model
    SC->>SG: learnable_to_shareable (prepare params)
    SG-->>SC: Returns initial training params
    SC->>SC: Randomly select Aggregator Client (AC) for Round 1

    %% Round Starts
    SC->>TCs: Broadcast "learn" task (params, round #, AC info)
    SC->>AC: Broadcast "learn" task (params, round #, AC info)

    %% Aggregator Client Setup
    AC->>SG: shareable_to_learnable (get current global model)
    SG-->>AC: Returns current global model
    AC->>AC: Setup Gatherer to wait for results

    %% Training and Contribution
    par Training
        TCs->>TCs: Execute local training
    and Aggregator Waits
        AC->>AC: (Can also perform training if configured)
    end

    TCs-->>AC: Send training result
    loop For each training result received
        AC->>AC: Fire BEFORE_CONTRIBUTION_ACCEPT event
        AC->>AC: aggregator.accept(result)
        AC->>AC: Fire AFTER_CONTRIBUTION_ACCEPT event
    end
    note right of AC: SwarmMed waits for all sites; server deadlines may abort the job.

    %% Aggregation
    AC->>AC: Fire BEFORE_AGGREGATION event
    AC->>AC: aggregator.aggregate()
    AC-->>AC: Receives aggregation result
    AC->>AC: Fire AFTER_AGGREGATION event

    AC->>SG: shareable_to_learnable (apply aggregated result)
    SG-->>AC: Returns new global model

    %% Check for Completion
    alt Not all rounds completed
        AC->>AC: Randomly select next Aggregator Client
        AC->>SG: learnable_to_shareable (prepare next round params)
        SG-->>AC: Returns next round params
        AC->>TCs: Broadcast "learn" task for new round
        note over AC, TCs: Workflow repeats for the next round.
    else All rounds completed
        AC->>RCs: Broadcast final result
        AC->>AC: Determine which client has the best result
        AC->>BestClient: Request distribution of best model
        BestClient->>RCs: Distribute best model
    end
```

[^3]: NVIDIA FLARE 2.7.1 documentation, client-controlled workflows: [https://nvflare.readthedocs.io/en/2.7.1/programming_guide/controllers/client_controlled_workflows.html#swarm-learning](https://nvflare.readthedocs.io/en/2.7.1/programming_guide/controllers/client_controlled_workflows.html#swarm-learning)
[^4]: [https://nvflare.readthedocs.io](https://nvflare.readthedocs.io)

## 📦 _MINIO_[^5] Object Storage

### What is Minio?

Minio is a self-hosted object storage server that is compatible with the Amazon S3 API. In SwarmMedHub it stores, at each site, the project's training code and scripts, the datasets imported through the web interface or the CLI, plots, and the training results synchronized from the FL workspaces. Training data can instead be read from a local directory that is never copied into MinIO, as in the worked example ([Threat model](threat-model.md#where-training-data-is-read)).

### Why is Minio important for SwarmMedHub?

Each installation runs its own MinIO, so the data imported at a site stay on that site's storage. MinIO no longer publishes community images, so SwarmMed builds release `RELEASE.2025-09-07T16-13-09Z` from its pinned upstream source.

### Key Features

*   **S3 Compatibility:** The platform reads and writes objects through the S3 API.
*   **Encryption:** SwarmMedHub requests server-side encryption (SSE-S3) with MinIO's built-in key for the objects it writes, and connects to MinIO over TLS ([Security](security.md#encryption-at-rest)).
*   **Access Control and Auditing:** MinIO supports per-user policies and audit logging. SwarmMed configures neither: the platform accesses MinIO with the root credentials from `.env`.

[^5]: [https://min.io/](https://min.io/)

## 🎸 _DJANGO_[^6] Web Interface

### What is Django?

Django is a high-level Python web framework that follows the "batteries-included" philosophy. It provides a comprehensive set of tools and libraries for building web applications, including an Object-Relational Mapper (ORM), an authentication system, and a powerful admin interface.

### Why is Django important for SwarmMedHub?

Django runs SwarmMedHub's web interface and the services that the web interface and the CLI share: the data model, authentication, the admin pages, and the background tasks handed to Celery.

### Key Features

*   **Rapid Development:** The "batteries-included" nature of Django lets the platform rely on standard components for common web development tasks instead of reimplementing them.
*   **Security:** Django provides built-in protection against many common web vulnerabilities:
    *   **Cross-Site Scripting (XSS):** Django's template engine automatically escapes variables, which prevents most XSS attacks.
    *   **Cross-Site Request Forgery (CSRF):** Django's CSRF middleware checks every form submission. SwarmMedHub exempts two endpoints used by other sites' web interfaces, which accept the network's shared token instead ([Security](security.md#vulnerability-management)).
    *   **SQL Injection:** Django's ORM uses parameterized queries, which prevents SQL injection vulnerabilities.

[^6]: [https://www.djangoproject.com/](https://www.djangoproject.com/)

## 🌐 _TAILSCALE_[^7] VPN

### What is Tailscale?

Tailscale is a VPN service built on the WireGuard protocol. It creates a private network (a "tailnet") between the devices enrolled in it, which can then reach each other at their Tailscale addresses.

### How does SwarmMedHub use Tailscale?

Federated training does not need Tailscale: each FL client connects out to the FL server, and clients do not connect to each other. The worked example ran over a Tailscale network, and two features of the web interface use the host's Tailscale address: a network created in the web interface takes it as the FL server's address, and a site's web interface recognises itself among a network's participants by it. Training itself needs only a stable IPv4 address for every participant and a path from the clients to the server, so another overlay network, or none, can take Tailscale's place; only Tailscale has been exercised with SwarmMed so far. [Networking](networking.md) compares the options.

### Key Features

*   **WireGuard:** Traffic between devices on the tailnet is encrypted with WireGuard.
*   **Coordination Service:** Tailscale operates the coordination service, which cannot be self-hosted. Its clients can be pointed at a compatible third-party control server such as Headscale; that has not been tried with SwarmMed.

[^7]: [https://tailscale.com/](https://tailscale.com/)
